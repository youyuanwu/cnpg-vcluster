from __future__ import annotations

from .common import *
from .contracts import _expected_tenant_markers, _management_resource_specs
from .foundation import _get_management_resource, load_inventory
from .rendering import _azure_tags, _azure_tags_match, _require_markers





def _management_resource_name(kind: str, name: str) -> str:
    return f"{kind.lower()}/{name}"


def _management_object_summary(payload: Mapping[str, object]) -> dict[str, object]:
    metadata = payload.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    owner_references = metadata.get("ownerReferences")
    owner_references = owner_references if isinstance(owner_references, list) else []
    return {
        "apiVersion": str(payload.get("apiVersion", "")),
        "kind": str(payload.get("kind", "")),
        "name": str(metadata.get("name", "")),
        "uid": str(metadata.get("uid", "")),
        "resourceVersion": str(metadata.get("resourceVersion", "")),
        "ownerUids": sorted(
            str(reference["uid"])
            for reference in owner_references
            if isinstance(reference, dict)
            and isinstance(reference.get("uid"), str)
            and reference["uid"]
        ),
        "finalizers": sorted(
            str(value)
            for value in metadata.get("finalizers", [])
            if isinstance(value, str)
        ),
        "deletionTimestamp": metadata.get("deletionTimestamp"),
    }


def _list_namespaced_management_objects(
    root: Path,
    namespace: str,
) -> list[dict[str, object]]:
    response = _kubectl(
        root,
        "api-resources",
        "--namespaced=true",
        "--verbs=list",
        "-o",
        "name",
        check=False,
    )
    if response.returncode != 0:
        raise RuntimeError(
            "Azure management API discovery failed: " + response.stderr
        )
    resource_types = sorted(set(response.stdout.split()))
    if not resource_types:
        raise RuntimeError("Azure management API discovery returned no resources")
    objects: list[dict[str, object]] = []
    for resource_type in resource_types:
        listed = _kubectl(
            root,
            "-n",
            namespace,
            "get",
            resource_type,
            "-o",
            "json",
            check=False,
        )
        if listed.returncode != 0:
            raise RuntimeError(
                f"Azure management inventory failed for {resource_type}: "
                f"{listed.stderr}"
            )
        try:
            payload = json.loads(listed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Azure management inventory is invalid for {resource_type}"
            ) from exc
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise RuntimeError(
                f"Azure management inventory is invalid for {resource_type}"
            )
        for item in items:
            if not isinstance(item, dict):
                raise RuntimeError(
                    f"Azure management inventory contains an invalid {resource_type}"
                )
            objects.append(item)
    return objects


def _classify_management_owned_resources(
    spec: TenantSpec,
    identity: TenantIdentity | OperationJournal,
    namespace: Mapping[str, object] | None,
    objects: Sequence[Mapping[str, object]],
    *,
    require_complete: bool,
    verified_uids: Sequence[str] = (),
) -> dict[str, object]:
    markers = _expected_tenant_markers(spec, identity)
    expected = {
        (kind, name): (key, namespace_name)
        for key, namespace_name, kind, name in _management_resource_specs(spec)
    }
    records: list[dict[str, object]] = []
    if namespace is not None:
        records.append(_management_object_summary(namespace))
    records.extend(_management_object_summary(item) for item in objects)
    by_identity = {
        (str(item["kind"]), str(item["name"])): item
        for item in records
        if item["kind"] and item["name"]
    }
    unknown = []
    classified: dict[tuple[str, str], str] = {}
    verified_uid_set = set(verified_uids)
    owned_uids = {
        uid
        for key, uid in identity.observed.items()
        if key.endswith("Uid") and isinstance(uid, str) and uid
    }
    for resource_identity, (key, _) in expected.items():
        item = by_identity.get(resource_identity)
        recorded_uid = identity.observed.get(key)
        if item is None:
            if require_complete:
                unknown.append(
                    {
                        "kind": resource_identity[0],
                        "name": resource_identity[1],
                        "reason": "recorded management resource is absent",
                    }
                )
            continue
        if not recorded_uid:
            unknown.append(
                {
                    "kind": item["kind"],
                    "name": item["name"],
                    "reason": "recorded management UID is absent",
                }
            )
            continue
        if item["uid"] != recorded_uid:
            unknown.append(
                {
                    "kind": item["kind"],
                    "name": item["name"],
                    "reason": "management UID changed",
                }
            )
            continue
        payload = namespace if resource_identity[0] == "Namespace" else next(
            (
                value
                for value in objects
                if value.get("kind") == resource_identity[0]
                and value.get("metadata", {}).get("name") == resource_identity[1]
            ),
            None,
        )
        if payload is None:
            raise RuntimeError("Azure management inventory changed during classification")
        _require_markers(
            payload,
            markers,
            _management_resource_name(resource_identity[0], resource_identity[1]),
        )
        classification = (
            "orchestration"
            if resource_identity[0] in KNOWN_ORCHESTRATION_MANAGEMENT_KINDS
            else "controller"
        )
        classified[resource_identity] = classification
        owned_uids.add(str(item["uid"]))
    changed = True
    while changed:
        changed = False
        for item in records:
            identity_key = (str(item["kind"]), str(item["name"]))
            if identity_key in classified:
                continue
            owner_uids = set(item["ownerUids"])
            if owner_uids & owned_uids:
                kind = str(item["kind"])
                if kind in (
                    KNOWN_CONTROLLER_MANAGEMENT_KINDS
                    | KNOWN_ORCHESTRATION_MANAGEMENT_KINDS
                    | KNOWN_NAMESPACE_CHILD_KINDS
                ):
                    classified[identity_key] = (
                        "namespace-child"
                        if kind in KNOWN_NAMESPACE_CHILD_KINDS
                        else "controller"
                    )
                    if item["uid"]:
                        owned_uids.add(str(item["uid"]))
                    changed = True
    for item in records:
        identity_key = (str(item["kind"]), str(item["name"]))
        if identity_key in classified:
            continue
        kind = str(item["kind"])
        name = str(item["name"])
        payload = namespace if kind == "Namespace" else next(
            (
                value
                for value in objects
                if value.get("kind") == kind
                and value.get("metadata", {}).get("name") == name
            ),
            None,
        )
        observed_markers = (
            resource_lifecycle_markers(payload)
            if isinstance(payload, Mapping)
            else {}
        )
        resource_spec = (
            payload.get("spec")
            if isinstance(payload, Mapping)
            else None
        )
        resource_tags = (
            resource_spec.get("tags")
            if isinstance(resource_spec, Mapping)
            else None
        )
        expected_tags = _azure_tags(markers)
        has_azure_tags = isinstance(resource_tags, dict) and any(
            key in resource_tags for key in expected_tags
        )
        exact_azure_tags = _azure_tags_match(resource_tags, expected_tags)
        if any(observed_markers.values()) or has_azure_tags:
            if observed_markers != markers and not exact_azure_tags:
                reason = "foreign lifecycle markers"
            elif kind not in (
                KNOWN_CONTROLLER_MANAGEMENT_KINDS
                | KNOWN_ORCHESTRATION_MANAGEMENT_KINDS
            ):
                reason = "unknown marked management kind"
            else:
                classified[identity_key] = (
                    "orchestration"
                    if kind in KNOWN_ORCHESTRATION_MANAGEMENT_KINDS
                    else "controller"
                )
                continue
        elif item["uid"] in verified_uid_set and kind in (
            KNOWN_CONTROLLER_MANAGEMENT_KINDS
            | KNOWN_ORCHESTRATION_MANAGEMENT_KINDS
            | KNOWN_NAMESPACE_CHILD_KINDS
        ):
            classified[identity_key] = (
                "namespace-child"
                if kind in KNOWN_NAMESPACE_CHILD_KINDS
                else (
                    "orchestration"
                    if kind in KNOWN_ORCHESTRATION_MANAGEMENT_KINDS
                    else "controller"
                )
            )
            continue
        elif kind in KNOWN_NAMESPACE_CHILD_KINDS or (
            kind == "ConfigMap" and name == "kube-root-ca.crt"
        ) or (
            kind == "Secret" and name.startswith("default-token-")
        ):
            classified[identity_key] = "namespace-child"
            continue
        else:
            reason = "unclassifiable namespace resource"
        unknown.append({"kind": kind, "name": name, "reason": reason})
    result = {
        "controller": sorted(
            (
                item
                for item in records
                if classified.get((str(item["kind"]), str(item["name"])))
                == "controller"
            ),
            key=lambda item: (str(item["kind"]), str(item["name"])),
        ),
        "orchestration": sorted(
            (
                item
                for item in records
                if classified.get((str(item["kind"]), str(item["name"])))
                == "orchestration"
            ),
            key=lambda item: (str(item["kind"]), str(item["name"])),
        ),
        "namespaceChildren": sorted(
            (
                item
                for item in records
                if classified.get((str(item["kind"]), str(item["name"])))
                == "namespace-child"
            ),
            key=lambda item: (str(item["kind"]), str(item["name"])),
        ),
        "unknown": sorted(
            unknown,
            key=lambda item: json.dumps(item, sort_keys=True),
        ),
    }
    if result["unknown"]:
        raise RuntimeError(
            "Azure tenant management ownership is unknown: "
            + json.dumps(result["unknown"], sort_keys=True)
        )
    return result


def discover_management_owned_resources(
    root: Path,
    spec: TenantSpec,
    identity: TenantIdentity | OperationJournal,
    *,
    require_complete: bool,
    verified_uids: Sequence[str] = (),
) -> dict[str, object]:
    namespace = _get_management_resource(root, None, f"namespace/{spec.namespace}")
    if namespace is None:
        if require_complete:
            raise RuntimeError("Azure tenant Namespace is absent")
        return {
            "controller": [],
            "orchestration": [],
            "namespaceChildren": [],
            "unknown": [],
        }
    objects = _list_namespaced_management_objects(root, spec.namespace)
    return _classify_management_owned_resources(
        spec,
        identity,
        namespace,
        objects,
        require_complete=require_complete,
        verified_uids=verified_uids,
    )


def classify_azure_owned_resources(
    resources: Sequence[Mapping[str, object]],
    expected_markers: Mapping[str, str],
    *,
    parent_ids: Sequence[str] = (),
    aso_objects: Sequence[Mapping[str, object]] = (),
    parent_uids: Sequence[str] = (),
    verified_ids: Sequence[str] = (),
) -> dict[str, object]:
    expected_tags = _azure_tags(expected_markers)
    normalized_parents = tuple(parent.rstrip("/").lower() for parent in parent_ids)
    normalized_verified = {identifier.lower() for identifier in verified_ids}
    owned = []
    unknown = []
    for resource in resources:
        identifier = resource.get("id")
        resource_type = str(resource.get("type", "")).lower()
        tags = resource.get("tags")
        tags = tags if isinstance(tags, dict) else {}
        marked = any(key in tags for key in expected_tags)
        exact = _azure_tags_match(tags, expected_tags)
        child = isinstance(identifier, str) and any(
            identifier.lower().startswith(parent + "/")
            for parent in normalized_parents
        )
        verified = isinstance(identifier, str) and identifier.lower() in normalized_verified
        if not (marked or exact or child or verified):
            continue
        if not isinstance(identifier, str) or not identifier:
            unknown.append({"kind": "AzureResource", "reason": "missing id"})
        elif resource_type not in KNOWN_AZURE_TENANT_TYPES:
            unknown.append(
                {"kind": "AzureResource", "id": identifier, "type": resource_type}
            )
        elif marked and not exact:
            unknown.append(
                {"kind": "AzureResource", "id": identifier, "reason": "foreign markers"}
            )
        else:
            owned_resource = {"id": identifier, "type": resource_type}
            if resource_type == "microsoft.network/networkinterfaces":
                virtual_machine_id = resource.get("virtualMachineId")
                if not isinstance(virtual_machine_id, str) or not virtual_machine_id:
                    unknown.append(
                        {
                            "kind": "AzureResource",
                            "id": identifier,
                            "reason": "missing VMSS instance association",
                        }
                    )
                    continue
                owned_resource["virtualMachineId"] = virtual_machine_id
            owned.append(owned_resource)
    parent_uid_set = set(parent_uids)
    aso_owned = []
    for payload in aso_objects:
        kind = payload.get("kind")
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            unknown.append({"kind": str(kind), "reason": "missing metadata"})
            continue
        owner_uids = {
            reference.get("uid")
            for reference in metadata.get("ownerReferences", [])
            if isinstance(reference, dict)
        }
        marker_values = resource_lifecycle_markers(payload)
        resource_spec = payload.get("spec")
        resource_spec = resource_spec if isinstance(resource_spec, dict) else {}
        exact = (
            marker_values == dict(expected_markers)
            or _azure_tags_match(resource_spec.get("tags"), expected_tags)
        )
        parent_owned = bool(owner_uids & parent_uid_set)
        if not (exact or parent_owned):
            unknown.append(
                {
                    "kind": str(kind),
                    "name": str(metadata.get("name", "")),
                    "reason": "foreign ASO ownership",
                }
            )
            continue
        if kind not in KNOWN_ASO_TENANT_KINDS:
            unknown.append(
                {
                    "kind": str(kind),
                    "name": str(metadata.get("name", "")),
                    "reason": "unknown ASO ownership",
                }
            )
        else:
            status = payload.get("status")
            resource_id = status.get("id") if isinstance(status, dict) else None
            if not isinstance(resource_id, str) or not resource_id:
                unknown.append(
                    {
                        "kind": str(kind),
                        "name": str(metadata.get("name", "")),
                        "reason": "missing Azure resource id",
                    }
                )
                continue
            aso_owned.append(
                {
                    "kind": str(kind),
                    "name": str(metadata.get("name", "")),
                    "uid": str(metadata.get("uid", "")),
                    "azureResourceId": resource_id,
                }
            )
    unique_owned = {str(item["id"]).lower(): item for item in owned}
    result = {
        "azure": sorted(unique_owned.values(), key=lambda item: str(item["id"])),
        "aso": sorted(aso_owned, key=lambda item: (item["kind"], item["name"])),
        "unknown": sorted(unknown, key=lambda item: json.dumps(item, sort_keys=True)),
    }
    if result["unknown"]:
        raise RuntimeError(
            "Azure tenant resource ownership is unknown: "
            + json.dumps(result["unknown"], sort_keys=True)
        )
    return result


def discover_azure_owned_resources(
    root: Path,
    config: Mapping[str, str],
    spec: TenantSpec,
    identity: TenantIdentity | OperationJournal,
    *,
    require_parents: bool = True,
    require_azure_resources: bool = True,
    verified_resource_ids: Sequence[str] = (),
) -> dict[str, object]:
    markers = _expected_tenant_markers(spec, identity)
    selected = tenant_names(spec)
    for key, resource in (
        ("azureClusterUid", f"azurecluster/{selected['azureCluster']}"),
        ("azureMachinePoolUid", f"azuremachinepool/{selected['pool']}"),
    ):
        parent = _get_management_resource(root, spec.namespace, resource)
        if parent is None:
            if require_parents:
                raise RuntimeError(f"Azure tenant discovery parent is absent: {resource}")
            continue
        _require_markers(parent, markers, resource)
        if parent.get("metadata", {}).get("uid") != identity.observed.get(key):
            raise RuntimeError(f"Azure tenant discovery parent identity changed: {resource}")
    inventory = load_inventory(root, config)
    outputs = inventory["outputs"]
    assert isinstance(outputs, dict)
    resources = _json(
        [
            "az",
            "resource",
            "list",
            "--resource-group",
            str(outputs["resourceGroupName"]),
            "--output",
            "json",
        ]
    )
    if not isinstance(resources, list):
        raise RuntimeError("Azure tenant resource discovery returned invalid resources")
    aso_objects: list[Mapping[str, object]] = []
    parent_ids = [
        value
        for key, value in identity.observed.items()
        if key == "vmssId"
    ]
    verified_ids = list(verified_resource_ids)
    serialized_recorded = identity.observed.get("azureResources")
    if serialized_recorded:
        try:
            recorded_payload = json.loads(serialized_recorded)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "recorded Azure tenant resource inventory is invalid"
            ) from exc
        recorded_azure = (
            recorded_payload.get("azure")
            if isinstance(recorded_payload, dict)
            else None
        )
        if not isinstance(recorded_azure, list):
            raise RuntimeError(
                "recorded Azure tenant resource inventory is invalid"
            )
        for item in recorded_azure:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                raise RuntimeError(
                    "recorded Azure tenant resource inventory is invalid"
                )
            verified_ids.append(item["id"])
    current_resource_ids = {
        str(resource.get("id", "")).lower()
        for resource in resources
        if isinstance(resource, dict)
    }
    if parent_ids and any(
        parent.lower() in current_resource_ids for parent in parent_ids
    ):
        instance_response = _az(
            "vmss",
            "list-instances",
            "--resource-group",
            str(outputs["resourceGroupName"]),
            "--name",
            tenant_names(spec)["pool"],
            "--query",
            "[].{id:id,type:type,tags:tags}",
            "--output",
            "json",
            check=False,
        )
        if instance_response.returncode != 0:
            raise RuntimeError("Azure tenant VMSS instance discovery failed")
        instances = json.loads(instance_response.stdout)
        if not isinstance(instances, list):
            raise RuntimeError(
                "Azure tenant VMSS instance discovery returned invalid resources"
            )
        for instance in instances:
            if not isinstance(instance, dict) or not isinstance(
                instance.get("id"), str
            ):
                raise RuntimeError(
                    "Azure tenant VMSS instance discovery returned an invalid instance"
                )
            if not instance.get("type"):
                instance["type"] = (
                    "Microsoft.Compute/virtualMachineScaleSets/virtualMachines"
                )
            resources.append(instance)
            verified_ids.append(instance["id"])
        nic_response = _az(
            "vmss",
            "nic",
            "list",
            "--resource-group",
            str(outputs["resourceGroupName"]),
            "--vmss-name",
            selected["pool"],
            "--query",
            "[].{id:id,type:type,tags:tags,virtualMachineId:virtualMachine.id}",
            "--output",
            "json",
            check=False,
        )
        if nic_response.returncode != 0:
            raise RuntimeError("Azure tenant VMSS NIC discovery failed")
        nics = json.loads(nic_response.stdout)
        if not isinstance(nics, list):
            raise RuntimeError("Azure tenant VMSS NIC discovery returned invalid resources")
        for nic in nics:
            if not isinstance(nic, dict) or not isinstance(nic.get("id"), str):
                raise RuntimeError("Azure tenant VMSS NIC discovery returned an invalid NIC")
            if not nic.get("type"):
                nic["type"] = "Microsoft.Network/networkInterfaces"
            if not isinstance(nic.get("virtualMachineId"), str):
                raise RuntimeError(
                    "Azure tenant VMSS NIC discovery returned an unbound NIC"
                )
            resources.append(nic)
            verified_ids.append(nic["id"])
    parent_uids = [
        value
        for key, value in identity.observed.items()
        if key in {"azureClusterUid", "azureMachinePoolUid"}
    ]
    namespace = _get_management_resource(root, None, f"namespace/{spec.namespace}")
    if namespace is None:
        if require_parents:
            raise RuntimeError("Azure tenant Namespace is absent during discovery")
        return classify_azure_owned_resources(
            resources,
            markers,
            parent_ids=parent_ids,
            parent_uids=parent_uids,
            verified_ids=verified_ids,
        )
    aso_resource_types = set()
    for group in (
        "network.azure.com",
        "compute.azure.com",
        "resources.azure.com",
    ):
        response = _kubectl(
            root,
            "api-resources",
            "--api-group",
            group,
            "--namespaced=true",
            "-o",
            "name",
            check=False,
        )
        if response.returncode != 0:
            raise RuntimeError(
                f"Azure Service Operator API discovery failed for {group}"
            )
        aso_resource_types.update(response.stdout.split())
    for resource_name in sorted(aso_resource_types):
        aso_response = _kubectl(
            root,
            "-n",
            spec.namespace,
            "get",
            resource_name,
            "-o",
            "json",
            check=False,
        )
        if aso_response.returncode != 0:
            raise RuntimeError(
                f"Azure Service Operator discovery failed for {resource_name}"
            )
        payload = json.loads(aso_response.stdout)
        items = payload.get("items", [])
        if not isinstance(items, list):
            raise RuntimeError("Azure Service Operator discovery returned invalid objects")
        for item in items:
            if isinstance(item, dict):
                metadata = item.get("metadata")
                metadata = metadata if isinstance(metadata, dict) else {}
                status = item.get("status")
                resource_id = status.get("id") if isinstance(status, dict) else None
                kind = item.get("kind")
                if kind in KNOWN_ASO_FOUNDATION_REFERENCE_OUTPUTS:
                    expected_id = outputs[
                        KNOWN_ASO_FOUNDATION_REFERENCE_OUTPUTS[str(kind)]
                    ]
                    annotations = metadata.get("annotations")
                    annotations = (
                        annotations if isinstance(annotations, dict) else {}
                    )
                    if (
                        not isinstance(resource_id, str)
                        or not _azure_id_equal(resource_id, expected_id)
                        or annotations.get(
                            "serviceoperator.azure.com/reconcile-policy"
                        )
                        != "skip"
                    ):
                        raise RuntimeError(
                            f"Azure Service Operator foundation reference changed: {kind}"
                        )
                    continue
                aso_objects.append(item)
                owner_uids = {
                    reference.get("uid")
                    for reference in metadata.get("ownerReferences", [])
                    if isinstance(reference, dict)
                }
                resource_spec = item.get("spec")
                resource_spec = (
                    resource_spec if isinstance(resource_spec, dict) else {}
                )
                owned = (
                    resource_lifecycle_markers(item) == markers
                    or _azure_tags_match(
                        resource_spec.get("tags"),
                        _azure_tags(markers),
                    )
                    or bool(owner_uids & set(parent_uids))
                )
                if owned and isinstance(resource_id, str) and resource_id:
                    verified_ids.append(resource_id)
                    resource_response = _az(
                        "resource",
                        "show",
                        "--ids",
                        resource_id,
                        "--output",
                        "json",
                        check=False,
                    )
                    if resource_response.returncode != 0:
                        if require_azure_resources:
                            raise RuntimeError(
                                "recorded Azure Service Operator resource is absent"
                            )
                        continue
                    resource_payload = json.loads(resource_response.stdout)
                    if not isinstance(resource_payload, dict):
                        raise RuntimeError(
                            "Azure Service Operator resource discovery is invalid"
                        )
                    resources.append(resource_payload)
    return classify_azure_owned_resources(
        resources,
        markers,
        parent_ids=parent_ids,
        aso_objects=aso_objects,
        parent_uids=parent_uids,
        verified_ids=verified_ids,
    )


def _merge_owned_discoveries(
    discoveries: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    azure: dict[str, dict[str, object]] = {}
    aso: dict[tuple[str, str], dict[str, object]] = {}
    for discovery in discoveries:
        unknown = discovery.get("unknown")
        if unknown:
            raise RuntimeError(
                "Azure tenant resource discovery is incomplete: "
                + json.dumps(unknown, sort_keys=True)
            )
        azure_items = discovery.get("azure")
        aso_items = discovery.get("aso")
        if not isinstance(azure_items, list) or not isinstance(aso_items, list):
            raise RuntimeError("Azure tenant resource discovery is invalid")
        for item in azure_items:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                raise RuntimeError("Azure tenant resource discovery is invalid")
            azure[item["id"].lower()] = dict(item)
        for item in aso_items:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("kind"), str)
                or not isinstance(item.get("uid"), str)
            ):
                raise RuntimeError("Azure tenant ASO discovery is invalid")
            aso[(item["kind"], item["uid"])] = dict(item)
    return {
        "azure": sorted(azure.values(), key=lambda item: str(item["id"])),
        "aso": sorted(
            aso.values(),
            key=lambda item: (str(item["kind"]), str(item.get("name", ""))),
        ),
        "unknown": [],
    }


def _merge_management_discoveries(
    discoveries: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {
        "controller": [],
        "orchestration": [],
        "namespaceChildren": [],
        "unknown": [],
    }
    for category in ("controller", "orchestration", "namespaceChildren"):
        merged = {}
        for discovery in discoveries:
            unknown = discovery.get("unknown")
            if unknown:
                raise RuntimeError(
                    "Azure tenant management discovery is incomplete: "
                    + json.dumps(unknown, sort_keys=True)
                )
            items = discovery.get(category)
            if not isinstance(items, list):
                raise RuntimeError("Azure tenant management discovery is invalid")
            for item in items:
                if (
                    not isinstance(item, dict)
                    or not isinstance(item.get("kind"), str)
                    or not isinstance(item.get("name"), str)
                ):
                    raise RuntimeError("Azure tenant management discovery is invalid")
                key = (
                    item["kind"],
                    item["name"],
                    str(item.get("uid", "")),
                )
                merged[key] = dict(item)
        result[category] = sorted(
            merged.values(),
            key=lambda item: (
                str(item["kind"]),
                str(item["name"]),
                str(item.get("uid", "")),
            ),
        )
    return result


def _recorded_owned_resources(identity: TenantIdentity) -> dict[str, object]:
    serialized = identity.observed.get("azureResources")
    if not serialized:
        raise RuntimeError("recorded Azure tenant resource inventory is absent")
    try:
        payload = json.loads(serialized)
    except json.JSONDecodeError as exc:
        raise RuntimeError("recorded Azure tenant resource inventory is invalid") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("recorded Azure tenant resource inventory is invalid")
    return _merge_owned_discoveries((payload,))


def _journal_discovery(
    journal: OperationJournal,
    key: str,
) -> dict[str, object] | None:
    serialized = journal.observed.get(key)
    if serialized is None:
        return None
    try:
        payload = json.loads(serialized)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"recorded Azure deletion discovery is invalid: {key}"
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeError(
            f"recorded Azure deletion discovery is invalid: {key}"
        )
    return payload


def _require_recorded_resources_present(
    recorded: Mapping[str, object],
    discovered: Mapping[str, object],
) -> None:
    recorded_azure = {
        str(item["id"]).lower()
        for item in recorded["azure"]
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    discovered_azure = {
        str(item["id"]).lower()
        for item in discovered["azure"]
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    recorded_aso = {
        (str(item.get("kind")), str(item.get("uid")))
        for item in recorded["aso"]
        if isinstance(item, dict)
    }
    discovered_aso = {
        (str(item.get("kind")), str(item.get("uid")))
        for item in discovered["aso"]
        if isinstance(item, dict)
    }
    missing = sorted(recorded_azure - discovered_azure)
    missing_aso = sorted(recorded_aso - discovered_aso)
    if missing or missing_aso:
        raise RuntimeError(
            "recorded Azure tenant resource inventory changed before deletion: "
            + json.dumps(
                {"azure": missing, "aso": missing_aso},
                sort_keys=True,
            )
        )


def _discover_owned_repeatedly(
    root: Path,
    config: Mapping[str, str],
    spec: TenantSpec,
    identity: TenantIdentity | OperationJournal,
    *,
    passes: int,
    require_parents: bool,
    require_azure_resources: bool,
    verified_resource_ids: Sequence[str] = (),
) -> dict[str, object]:
    if passes < 2:
        raise RuntimeError("Azure tenant discovery must be repeated")
    discoveries = []
    verified_ids = list(verified_resource_ids)
    for _ in range(passes):
        discovery = discover_azure_owned_resources(
            root,
            config,
            spec,
            identity,
            require_parents=require_parents,
            require_azure_resources=require_azure_resources,
            verified_resource_ids=verified_ids,
        )
        discoveries.append(discovery)
        verified_ids.extend(
            str(item["id"])
            for item in discovery["azure"]
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        )
    return _merge_owned_discoveries(discoveries)
