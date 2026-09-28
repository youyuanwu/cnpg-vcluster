from __future__ import annotations

from .common import *

from .common import (
    PREFIX_RE,
    FOUNDATION_INVENTORY_SCHEMA,
    READY_EVIDENCE_MAX_AGE_SECONDS,
    FOUNDATION_DEFAULT_KEYS,
    REQUIRED_PROVIDERS,
    CONTROLLER_DEPLOYMENTS,
    CAPZ_EXTERNAL_CONTROL_PLANE_LABEL,
    CAPZ_AZURECLUSTER_WEBHOOK,
    MANAGEMENT_RESOURCE_PLURALS,
    KNOWN_AZURE_TENANT_TYPES,
    KNOWN_ASO_TENANT_KINDS,
    KNOWN_ASO_FOUNDATION_REFERENCE_OUTPUTS,
    KNOWN_CONTROLLER_MANAGEMENT_KINDS,
    KNOWN_ORCHESTRATION_MANAGEMENT_KINDS,
    KNOWN_NAMESPACE_CHILD_KINDS,
    REMOVED_TENANT_CONFIG_KEYS,
    AzureDeletionError,
    load_azure_configuration,
    names,
    tenant_names,
    _az,
    _json,
    _azure_id_equal,
    _foundation_networks,
    _validate_foundation_networks,
    _recorded_azure_specs,
    _validate_networks,
    _active_subscription,
    _sku_available,
    _reference_image_available,
    _foundation_defaults_checksum,
    _azure_runtime_path,
    _runtime_dir,
    azure_tenant_runtime_path,
    _tenant_runtime_dir,
    _management_kubeconfig,
    _tenant_kubeconfig,
    _kubectl,
    _tenant_kubectl,
    _helm,
)
from .foundation import (
    preflight,
    _deployment_parameters,
    _write_inventory,
    create_foundation,
    _patch_capz_identity,
    _install_capi_capz,
    _capz_external_control_plane_webhook_ready,
    _configure_capz_external_control_plane_webhook,
    _install_kamaji,
    _install_kamaji_provider,
    _controller_identities,
    create_management,
    load_inventory,
    _foundation_identity,
    _get_management_resource,
    _deployment_ready,
    _inspect_foundation,
    foundation_status,
    destroy,
)
from .contracts import (
    MANAGEMENT_RESOURCE_DESCRIPTORS,
    RESOURCE_IDENTITY_KEYS,
    _expected_tenant_markers,
    _management_resource_specs,
)
from .rendering import (
    _marker_annotations,
    _metadata,
    _external_azure_cluster_metadata,
    _azure_tags,
    _azure_tags_match,
    _write_manifest,
    _render_tenant_control_plane,
    _render_worker_pool,
    _render_addon_job,
    _resource_ref,
    _identity_key,
    _require_markers,
    _reconcile_manifest,
)
from .readiness import (
    _wait_tenant_endpoint,
    _retain_external_control_plane_lb,
    _capture_tenant_kubeconfig,
    _wait_worker_registered,
    _capture_vmss_identities,
    _wait_addon_job,
    _condition_true,
    _workload_ready,
    _collect_ready_observations,
    _wait_ready_observations,
    _tenant_spec_blockers,
)
from .ownership import (
    _management_resource_name,
    _management_object_summary,
    _list_namespaced_management_objects,
    _classify_management_owned_resources,
    discover_management_owned_resources,
    classify_azure_owned_resources,
    discover_azure_owned_resources,
    _merge_owned_discoveries,
    _merge_management_discoveries,
    _recorded_owned_resources,
    _journal_discovery,
    _require_recorded_resources_present,
    _discover_owned_repeatedly,
)

def _exact_delete_management_resource(
    root: Path,
    spec: TenantSpec,
    identity: TenantIdentity | OperationJournal,
    *,
    namespace: str | None,
    resource: str,
    uid_key: str,
    cascade: str,
) -> bool:
    payload = _get_management_resource(root, namespace, resource)
    if payload is None:
        return False
    _require_markers(payload, _expected_tenant_markers(spec, identity), resource)
    metadata = payload.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    uid = metadata.get("uid")
    resource_version = metadata.get("resourceVersion")
    if uid != identity.observed.get(uid_key):
        raise RuntimeError(f"Azure tenant management UID changed: {resource}")
    if not isinstance(resource_version, str) or not resource_version:
        raise RuntimeError(
            f"Azure tenant management resourceVersion is absent: {resource}"
        )
    api_version = payload.get("apiVersion")
    kind = payload.get("kind")
    name = metadata.get("name")
    plural = MANAGEMENT_RESOURCE_PLURALS.get(str(kind))
    if (
        not isinstance(api_version, str)
        or not api_version
        or not isinstance(name, str)
        or not name
        or plural is None
    ):
        raise RuntimeError(
            f"Azure tenant management API identity is invalid: {resource}"
        )
    if "/" in api_version:
        group, version = api_version.split("/", 1)
        base = (
            "/apis/"
            + urllib.parse.quote(group, safe=".")
            + "/"
            + urllib.parse.quote(version, safe="")
        )
    else:
        base = "/api/" + urllib.parse.quote(api_version, safe="")
    if namespace is None:
        path = (
            f"{base}/{plural}/"
            + urllib.parse.quote(name, safe="")
        )
    else:
        path = (
            f"{base}/namespaces/"
            + urllib.parse.quote(namespace, safe="")
            + f"/{plural}/"
            + urllib.parse.quote(name, safe="")
        )
    propagation = {
        "background": "Background",
        "foreground": "Foreground",
        "orphan": "Orphan",
    }.get(cascade)
    if propagation is None:
        raise RuntimeError(f"unsupported Kubernetes deletion propagation: {cascade}")
    _kubectl(
        root,
        "delete",
        f"--raw={path}",
        "-f",
        "-",
        input_text=json.dumps(
            {
                "apiVersion": "v1",
                "kind": "DeleteOptions",
                "propagationPolicy": propagation,
                "preconditions": {
                    "uid": uid,
                    "resourceVersion": resource_version,
                },
            },
            separators=(",", ":"),
        )
        + "\n",
    )
    return True


def _enable_capz_external_control_plane_delete(
    root: Path,
    spec: TenantSpec,
    identity: TenantIdentity | OperationJournal,
) -> None:
    resource = f"azurecluster/{tenant_names(spec)['azureCluster']}"
    deadline = time.monotonic() + 120
    while True:
        payload = _get_management_resource(root, spec.namespace, resource)
        if payload is None:
            return
        _require_markers(payload, _expected_tenant_markers(spec, identity), resource)
        metadata = payload.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        if metadata.get("uid") != identity.observed.get("azureClusterUid"):
            raise RuntimeError(f"Azure tenant management UID changed: {resource}")
        if metadata.get("deletionTimestamp"):
            break
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "AzureCluster deletion did not start before the CAPZ workaround"
            )
        time.sleep(2)

    azure_cluster_spec = payload.get("spec")
    azure_cluster_spec = (
        azure_cluster_spec if isinstance(azure_cluster_spec, dict) else {}
    )
    network_spec = azure_cluster_spec.get("networkSpec")
    network_spec = network_spec if isinstance(network_spec, dict) else {}
    api_server_lb = network_spec.get("apiServerLB")
    labels = metadata.get("labels")
    labels = labels if isinstance(labels, dict) else {}
    if azure_cluster_spec.get("controlPlaneEnabled") is not False:
        raise RuntimeError(
            "CAPZ external control-plane ownership changed during deletion"
        )
    if labels.get(CAPZ_EXTERNAL_CONTROL_PLANE_LABEL) != "true":
        raise RuntimeError(
            "CAPZ external control-plane label changed during deletion"
        )
    if isinstance(api_server_lb, dict) and api_server_lb.get("type") == "Public":
        return
    _kubectl(
        root,
        "-n",
        spec.namespace,
        "patch",
        resource,
        "--type=merge",
        "--field-manager=cnpg-vcluster-azure",
        "-p",
        json.dumps(
            {
                "spec": {
                    "networkSpec": {
                        "apiServerLB": {"type": "Public"},
                    }
                }
            },
            separators=(",", ":"),
        ),
    )
    updated = _get_management_resource(root, spec.namespace, resource)
    if updated is None:
        return
    updated_metadata = updated.get("metadata")
    updated_metadata = (
        updated_metadata if isinstance(updated_metadata, dict) else {}
    )
    updated_spec = updated.get("spec")
    updated_spec = updated_spec if isinstance(updated_spec, dict) else {}
    network_spec = updated_spec.get("networkSpec")
    network_spec = network_spec if isinstance(network_spec, dict) else {}
    if (
        updated_metadata.get("uid") != identity.observed.get("azureClusterUid")
        or not updated_metadata.get("deletionTimestamp")
        or updated_spec.get("controlPlaneEnabled") is not False
        or updated_metadata.get("labels", {}).get(
            CAPZ_EXTERNAL_CONTROL_PLANE_LABEL
        )
        != "true"
        or not isinstance(network_spec.get("apiServerLB"), dict)
        or network_spec["apiServerLB"].get("type") != "Public"
    ):
        raise RuntimeError(
            "CAPZ external control-plane deletion workaround was not retained"
        )


def _owned_tenant_machines(
    root: Path,
    spec: TenantSpec,
    identity: TenantIdentity | OperationJournal,
) -> list[Mapping[str, object]]:
    response = _kubectl(
        root,
        "-n",
        spec.namespace,
        "get",
        "machines",
        "-l",
        f"cluster.x-k8s.io/cluster-name={spec.name}",
        "-o",
        "json",
    )
    payload = json.loads(response.stdout)
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise RuntimeError("Azure tenant Machine discovery returned invalid objects")
    expected_markers = _expected_tenant_markers(spec, identity)
    expected_pool_uid = identity.observed.get("machinePoolUid")
    result = []
    for machine in items:
        if not isinstance(machine, dict):
            raise RuntimeError("Azure tenant Machine discovery returned an invalid object")
        metadata = machine.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        name = metadata.get("name")
        uid = metadata.get("uid")
        annotations = metadata.get("annotations")
        owner_uids = {
            reference.get("uid")
            for reference in metadata.get("ownerReferences", [])
            if isinstance(reference, dict)
        }
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(uid, str)
            or not uid
            or not isinstance(annotations, dict)
            or expected_pool_uid not in owner_uids
        ):
            raise RuntimeError("Azure tenant Machine ownership is invalid")
        _require_markers(machine, expected_markers, f"machine/{name}")
        result.append(machine)
    return result


def _exclude_tenant_machines_from_drain(
    root: Path,
    spec: TenantSpec,
    identity: TenantIdentity | OperationJournal,
) -> None:
    for machine in _owned_tenant_machines(root, spec, identity):
        metadata = machine["metadata"]
        assert isinstance(metadata, dict)
        name = metadata["name"]
        uid = metadata["uid"]
        _kubectl(
            root,
            "-n",
            spec.namespace,
            "patch",
            f"machine/{name}",
            "--type=json",
            "-p",
            json.dumps(
                [
                    {
                        "op": "test",
                        "path": "/metadata/uid",
                        "value": uid,
                    },
                    {
                        "op": "add",
                        "path": (
                            "/metadata/annotations/"
                            "machine.cluster.x-k8s.io~1exclude-node-draining"
                        ),
                        "value": "true",
                    },
                ],
                separators=(",", ":"),
            ),
        )


def _deletion_diagnostics(
    root: Path,
    spec: TenantSpec,
    identity: TenantIdentity | OperationJournal,
    *,
    management: Mapping[str, object] | None,
    azure: Mapping[str, object] | None,
    error: BaseException | str,
) -> dict[str, object]:
    selected = tenant_names(spec)
    conditions = []
    for resource in (
        f"cluster/{selected['cluster']}",
        f"kamajicontrolplane/{selected['controlPlane']}",
        f"azurecluster/{selected['azureCluster']}",
        f"kubeadmconfig/{selected['pool']}",
        f"machinepool/{selected['pool']}",
        f"azuremachinepool/{selected['pool']}",
    ):
        try:
            payload = _get_management_resource(root, spec.namespace, resource)
        except BaseException as exc:
            conditions.append(
                {"resource": resource, "inspectionError": redact(str(exc))}
            )
            continue
        if payload is None:
            continue
        metadata = payload.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        status = payload.get("status")
        status = status if isinstance(status, dict) else {}
        conditions.append(
            {
                "resource": resource,
                "uid": metadata.get("uid"),
                "deletionTimestamp": metadata.get("deletionTimestamp"),
                "finalizers": metadata.get("finalizers", []),
                "conditions": status.get("conditions", []),
                "failureReason": status.get("failureReason"),
                "failureMessage": status.get("failureMessage"),
            }
        )
    events = []
    try:
        response = _kubectl(
            root,
            "-n",
            spec.namespace,
            "get",
            "events",
            "-o",
            "json",
            check=False,
        )
        if response.returncode == 0:
            payload = json.loads(response.stdout)
            for item in payload.get("items", []) if isinstance(payload, dict) else []:
                if not isinstance(item, dict):
                    continue
                involved = item.get("involvedObject")
                involved = involved if isinstance(involved, dict) else {}
                events.append(
                    {
                        "type": item.get("type"),
                        "reason": item.get("reason"),
                        "message": item.get("message"),
                        "kind": involved.get("kind"),
                        "name": involved.get("name"),
                    }
                )
    except BaseException as exc:
        events.append({"inspectionError": redact(str(exc))})
    return {
        "schema": 1,
        "tenant": spec.name,
        "specificationSha256": spec.sha256(),
        "foundationSha256": foundation_sha256(identity.foundation_identity),
        "error": str(error),
        "management": management or {},
        "azure": azure or {},
        "conditions": conditions,
        "events": events[-100:],
    }


def _write_deletion_diagnostics(
    runtime: TenantRuntime,
    journal: OperationJournal,
    payload: Mapping[str, object],
) -> Path:
    path = runtime.paths.evidence / f"delete-diagnostics-{journal.operation_id}.json"
    write_private_file(
        path,
        json.dumps(redact_value(dict(payload)), sort_keys=True) + "\n",
    )
    return path


def _remove_private_tree(path: Path) -> None:
    try:
        details = path.lstat()
    except FileNotFoundError:
        return
    if (
        path.is_symlink()
        or not stat.S_ISDIR(details.st_mode)
        or details.st_uid != os.getuid()
        or details.st_mode & 0o077
    ):
        raise RuntimeError(f"private runtime directory is unsafe: {path}")
    for child in path.iterdir():
        details = child.lstat()
        if stat.S_ISDIR(details.st_mode):
            _remove_private_tree(child)
        elif (
            stat.S_ISREG(details.st_mode)
            and details.st_uid == os.getuid()
            and not details.st_mode & 0o077
        ):
            child.unlink()
        else:
            raise RuntimeError(f"private runtime artifact is unsafe: {child}")
    path.rmdir()


def _tenant_tagged_azure_resources(
    root: Path,
    config: Mapping[str, str],
    tenant: str,
) -> list[dict[str, object]]:
    del root
    resources = _json(
        [
            "az",
            "resource",
            "list",
            "--resource-group",
            names(config)["resourceGroup"],
            "--output",
            "json",
        ]
    )
    if not isinstance(resources, list):
        raise RuntimeError("Azure tenant absence discovery returned invalid resources")
    residues = []
    for item in resources:
        if not isinstance(item, dict):
            raise RuntimeError("Azure tenant absence discovery returned invalid resources")
        tags = item.get("tags")
        tags = tags if isinstance(tags, dict) else {}
        if tags.get("cnpg-vcluster-tenant") != tenant:
            continue
        identifier = item.get("id")
        if not isinstance(identifier, str) or not identifier:
            raise RuntimeError("Azure tenant residue has no resource ID")
        residues.append(
            {
                "id": identifier,
                "type": str(item.get("type", "")).lower(),
            }
        )
    return sorted(residues, key=lambda item: str(item["id"]))


def _operation_failed(runtime: TenantRuntime, journal: OperationJournal) -> bool:
    path = runtime.paths.evidence / f"{journal.operation}-{journal.operation_id}.json"
    if not private_file_exists(path):
        return False
    payload = json.loads(read_private_file(path).decode())
    records = payload.get("records", [])
    if not isinstance(records, list):
        raise RuntimeError("Azure tenant timing evidence is invalid")
    return any(
        isinstance(record, dict) and record.get("status") == "failed"
        for record in records
    )
