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
from .deletion import (
    _exact_delete_management_resource,
    _enable_capz_external_control_plane_delete,
    _owned_tenant_machines,
    _exclude_tenant_machines_from_drain,
    _deletion_diagnostics,
    _write_deletion_diagnostics,
    _remove_private_tree,
    _tenant_tagged_azure_resources,
    _operation_failed,
)

class AzureTenantAdapter:
    def __init__(
        self,
        *,
        clock=time.time,
        monotonic=time.monotonic,
        sleep=time.sleep,
    ) -> None:
        self.clock = clock
        self.monotonic = monotonic
        self.sleep = sleep
        self._delete_snapshots: dict[str, dict[str, object]] = {}

    @staticmethod
    def _config(root: Path) -> dict[str, str]:
        return load_azure_configuration(root)

    def foundation_identity(
        self,
        root: Path,
        spec: TenantSpec,
    ) -> Mapping[str, str]:
        config = self._config(root)
        if spec.kubernetes_version != config[
            "AZURE_SUPPORTED_TENANT_KUBERNETES_VERSION"
        ].removeprefix("v"):
            raise RuntimeError("unsupported Azure tenant Kubernetes version")
        _validate_networks(
            config,
            spec,
            recorded_specs=_recorded_azure_specs(root, excluding=spec.name),
        )
        identity, _, _ = _inspect_foundation(root, config, require_healthy=True)
        _sku_available(config, config["AZURE_TENANT_NODE_SKU"])
        _reference_image_available(config, spec.kubernetes_version)
        return identity

    @staticmethod
    def intended_resources(spec: TenantSpec) -> Sequence[str]:
        selected = tenant_names(spec)
        management = tuple(
            (
                f"{kind}/{namespace}/{name}"
                if namespace is not None
                else f"{kind}/{name}"
            )
            for _, namespace, kind, name in _management_resource_specs(spec)
        )
        return (
            *management[:8],
            f"VirtualMachineScaleSet/{selected['pool']}",
            *management[8:],
            f"Credential/{spec.name}",
        )

    def create(
        self,
        root: Path,
        spec: TenantSpec,
        runtime: TenantRuntime,
        journal: OperationJournal,
        timings,
    ) -> Mapping[str, str]:
        config = self._config(root)
        inventory = load_inventory(root, config)
        current = runtime.load_operation()
        marker_operation_id = current.observed.get(
            "markerOperationId",
            current.operation_id,
        )
        current = runtime.update_operation(
            current,
            phase="markers-recorded",
            observed={"markerOperationId": marker_operation_id},
        )
        with timings.phase("control-plane"):
            manifest = _render_tenant_control_plane(
                root,
                config,
                inventory,
                spec,
                current,
            )
            current = _reconcile_manifest(
                root,
                spec,
                runtime,
                current,
                manifest,
                phase="control-plane-resources",
            )
            _retain_external_control_plane_lb(root, spec, current)
            control_plane = _wait_tenant_endpoint(root, config, spec)
            endpoint = control_plane["spec"]["controlPlaneEndpoint"]
            endpoint_text = f"{endpoint['host']}:{endpoint['port']}"
            write_private_file(
                _tenant_runtime_dir(root, spec.name) / "endpoint.json",
                json.dumps(endpoint, sort_keys=True) + "\n",
            )
            current = runtime.update_operation(
                current,
                phase="control-plane-endpoint",
                observed={"endpoint": endpoint_text},
            )
            current = _capture_tenant_kubeconfig(
                root,
                spec,
                runtime,
                current,
            )
        with timings.phase("workers"):
            manifest = _render_worker_pool(
                root,
                config,
                inventory,
                spec,
                current,
            )
            current = _reconcile_manifest(
                root,
                spec,
                runtime,
                current,
                manifest,
                phase="worker-resources",
            )
            _wait_worker_registered(root, config, spec)
            current = _capture_vmss_identities(
                root,
                config,
                spec,
                runtime,
                current,
            )
        with timings.phase("add-ons"):
            manifest = _render_addon_job(root, config, spec, current)
            current = _reconcile_manifest(
                root,
                spec,
                runtime,
                current,
                manifest,
                phase="addon-resources",
            )
            _wait_addon_job(root, config, spec)
            observations = _wait_ready_observations(root, config, spec)
            component_identities = observations["componentIdentities"]
            if not isinstance(component_identities, dict) or not all(
                isinstance(value, str) and value
                for value in component_identities.values()
            ):
                raise RuntimeError("Azure tenant add-on identities are incomplete")
            current = runtime.update_operation(
                current,
                phase="addons-ready",
                observed={
                    "nodeIdentities": json.dumps(
                        observations["nodes"],
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    **{
                        f"{key}Uid": value
                        for key, value in component_identities.items()
                    },
                },
            )
            discovery = discover_azure_owned_resources(
                root,
                config,
                spec,
                current,
            )
            current = runtime.update_operation(
                current,
                phase="ready",
                observed={
                    "azureResources": json.dumps(
                        discovery,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                },
            )
        runtime.write_ready_evidence(
            {
                "schema": 1,
                "profile": "azure",
                "tenant": spec.name,
                "specificationSha256": spec.sha256(),
                "foundationIdentity": dict(current.foundation_identity),
                "observed": dict(current.observed),
                "verifiedAt": self.clock(),
                "ready": observations,
            }
        )
        print(f"Azure tenant is Ready: {spec.name}")
        return dict(current.observed)

    def _inspect_absence(
        self,
        root: Path,
        config: Mapping[str, str],
        tenant: str,
        *,
        foundation_healthy: bool,
    ) -> TenantStatus:
        namespace = _get_management_resource(root, None, f"namespace/{tenant}")
        management_residue = (
            [
                _management_object_summary(item)
                for item in _list_namespaced_management_objects(root, tenant)
            ]
            if namespace is not None
            else []
        )
        resources = _tenant_tagged_azure_resources(root, config, tenant)
        tenant_runtime = azure_tenant_runtime_path(root, tenant)
        runtime_residue = (
            sorted(
                str(path.relative_to(tenant_runtime))
                for path in tenant_runtime.rglob("*")
            )
            if tenant_runtime.exists()
            else []
        )
        if (
            namespace is not None
            or management_residue
            or resources
            or runtime_residue
        ):
            return TenantStatus(
                profile="azure",
                tenant=tenant,
                classification="ownership-invalid",
                foundation_healthy=foundation_healthy,
                components={
                    "namespacePresent": namespace is not None,
                    "managementResidue": management_residue,
                    "azureResources": resources,
                    "runtimeResidue": runtime_residue,
                },
                blockers=(
                    "Azure tenant resources exist without an authoritative identity",
                ),
            )
        return TenantStatus(
            profile="azure",
            tenant=tenant,
            classification="absent" if foundation_healthy else "degraded",
            foundation_healthy=foundation_healthy,
            components={"inspected": True},
            blockers=() if foundation_healthy else ("Azure foundation is unhealthy",),
        )

    def status(self, root: Path, tenant: str) -> TenantStatus:
        config = self._config(root)
        runtime = TenantRuntime(root, tenant)
        identity = runtime.load_identity() if runtime.identity_exists() else None
        operation = runtime.load_operation() if runtime.operation_exists() else None
        try:
            foundation, healthy, foundation_blockers = _inspect_foundation(
                root,
                config,
                require_healthy=False,
            )
        except BaseException as exc:
            if identity is None and operation is None:
                return TenantStatus(
                    profile="azure",
                    tenant=tenant,
                    classification="degraded",
                    foundation_healthy=False,
                    blockers=(str(exc),),
                )
            raise
        if identity is None and operation is None:
            try:
                return self._inspect_absence(
                    root,
                    config,
                    tenant,
                    foundation_healthy=healthy,
                )
            except BaseException as exc:
                return TenantStatus(
                    profile="azure",
                    tenant=tenant,
                    classification="ownership-invalid",
                    foundation_healthy=healthy,
                    blockers=(str(exc),),
                )
        binding = (
            identity.foundation_identity
            if identity is not None
            else operation.foundation_identity
        )
        if dict(binding) != foundation:
            raise RuntimeError("Azure tenant foundation binding changed")
        if operation is not None:
            classification = (
                "failed"
                if _operation_failed(runtime, operation)
                else ("deleting" if operation.operation == "delete" else "progressing")
            )
            return TenantStatus(
                profile="azure",
                tenant=tenant,
                classification=classification,
                foundation_healthy=healthy,
                components={
                    "operation": operation.operation,
                    "phase": operation.phase,
                },
                blockers=(
                    ("tenant lifecycle operation failed",)
                    if classification == "failed"
                    else foundation_blockers
                ),
            )
        assert identity is not None
        spec = identity.specification
        selected = tenant_names(spec)
        expected_markers = _expected_tenant_markers(spec, identity)
        management_specs = _management_resource_specs(spec)
        status_specs = (*management_specs[:-2], management_specs[-1], management_specs[-2])
        resources = tuple(
            (key, namespace, f"{kind.lower()}/{name}")
            for key, namespace, kind, name in status_specs
        )
        blockers = list(foundation_blockers)
        observed_payloads: dict[str, dict[str, object]] = {}
        for key, namespace, resource in resources:
            payload = _get_management_resource(root, namespace, resource)
            if payload is None:
                blockers.append(f"tenant resource is absent: {resource}")
                continue
            observed_payloads[key] = payload
            _require_markers(payload, expected_markers, resource)
            if payload.get("metadata", {}).get("uid") != identity.observed.get(key):
                blockers.append(f"tenant resource identity changed: {resource}")
        blockers.extend(
            _tenant_spec_blockers(spec, selected, observed_payloads, config)
        )
        secret = _get_management_resource(
            root,
            spec.namespace,
            f"secret/{spec.name}-kubeconfig",
        )
        if (
            secret is None
            or secret.get("metadata", {}).get("uid")
            != identity.observed.get("tenantKubeconfigSecretUid")
        ):
            blockers.append("tenant kubeconfig Secret identity changed")
        elif isinstance(secret.get("data", {}).get("value"), str):
            try:
                secret_content = base64.b64decode(
                    secret["data"]["value"],
                    validate=True,
                )
            except ValueError:
                blockers.append("tenant kubeconfig Secret content is invalid")
            else:
                if hashlib.sha256(secret_content).hexdigest() != identity.observed.get(
                    "tenantKubeconfigSha256"
                ):
                    blockers.append("tenant kubeconfig Secret content changed")
        control_plane_payload = observed_payloads.get("kamajiControlPlaneUid", {})
        endpoint = control_plane_payload.get("spec", {}).get("controlPlaneEndpoint", {})
        if (
            not isinstance(endpoint, dict)
            or f"{endpoint.get('host')}:{endpoint.get('port')}"
            != identity.observed.get("endpoint")
        ):
            blockers.append("tenant control-plane endpoint identity changed")
        kubeconfig = _tenant_kubeconfig(root, tenant)
        if hashlib.sha256(read_private_file(kubeconfig)).hexdigest() != identity.observed.get(
            "tenantKubeconfigSha256"
        ):
            blockers.append("tenant kubeconfig identity changed")
        observations, ready_blockers = _collect_ready_observations(root, config, spec)
        blockers.extend(ready_blockers)
        if json.dumps(
            observations["nodes"],
            sort_keys=True,
            separators=(",", ":"),
        ) != identity.observed.get("nodeIdentities"):
            blockers.append("tenant Node identities changed")
        component_identities = observations["componentIdentities"]
        if isinstance(component_identities, dict):
            for key, value in component_identities.items():
                if value != identity.observed.get(f"{key}Uid"):
                    blockers.append(f"tenant add-on identity changed: {key}")
        discovery = discover_azure_owned_resources(root, config, spec, identity)
        if json.dumps(discovery, sort_keys=True, separators=(",", ":")) != identity.observed.get(
            "azureResources"
        ):
            blockers.append("Azure tenant owned-resource inventory changed")
        evidence = runtime.load_ready_evidence()
        verified_at = evidence.get("verifiedAt")
        now = self.clock()
        verified_at_is_valid = (
            isinstance(verified_at, (int, float))
            and not isinstance(verified_at, bool)
            and math.isfinite(verified_at)
        )
        if (
            evidence.get("profile") != "azure"
            or evidence.get("tenant") != tenant
            or evidence.get("specificationSha256") != spec.sha256()
            or evidence.get("foundationIdentity") != dict(identity.foundation_identity)
            or evidence.get("observed") != dict(identity.observed)
            or evidence.get("ready") != observations
            or not verified_at_is_valid
            or now < verified_at
            or now - verified_at > READY_EVIDENCE_MAX_AGE_SECONDS
        ):
            blockers.append("Azure Ready evidence does not match current identities")
        return TenantStatus(
            profile="azure",
            tenant=tenant,
            classification="ready" if healthy and not blockers else "degraded",
            foundation_healthy=healthy,
            components={
                "requestedWorkers": spec.workers,
                "readyReplicas": observations["readyReplicas"],
                "nodes": observations["nodes"],
                "controlPlaneAvailable": observations["controlPlaneAvailable"],
                "cloudControllerReady": observations["cloudController"],
                "cloudNodeReady": observations["cloudNode"],
                "tenantNetworkReady": (
                    observations["calicoNode"]
                    and observations["calicoControllers"]
                ),
            },
            blockers=tuple(blockers),
        )

    def authoritative_absence(self, root: Path, tenant: str) -> TenantStatus:
        config = self._config(root)
        _, healthy, _ = _inspect_foundation(root, config, require_healthy=False)
        return self._inspect_absence(
            root,
            config,
            tenant,
            foundation_healthy=healthy,
        )

    def validate_delete(
        self,
        root: Path,
        spec: TenantSpec,
        identity: TenantIdentity,
    ) -> None:
        if (
            identity.profile != "azure"
            or identity.tenant != spec.name
            or identity.specification_sha256 != spec.sha256()
            or identity.specification.to_mapping() != spec.to_mapping()
        ):
            raise RuntimeError("Azure tenant deletion specification binding changed")
        config = self._config(root)
        _active_subscription(config)
        foundation, healthy, blockers = _inspect_foundation(
            root,
            config,
            require_healthy=False,
        )
        if not healthy:
            raise RuntimeError(
                "Azure management foundation is unhealthy: " + "; ".join(blockers)
            )
        if foundation != dict(identity.foundation_identity):
            raise RuntimeError("Azure tenant foundation binding changed")
        runtime = TenantRuntime(root, spec.name)
        pending = runtime.load_operation() if runtime.operation_exists() else None
        if pending is not None and (
            pending.operation != "delete"
            or pending.specification_sha256 != spec.sha256()
            or dict(pending.foundation_identity) != foundation
        ):
            raise RuntimeError("conflicting Azure tenant lifecycle operation exists")
        require_complete = pending is None
        recorded_management = []
        recorded_azure = []
        if pending is not None:
            for key in (
                "deleteManagementBefore",
                "deleteManagementDiscovered",
            ):
                discovery = _journal_discovery(pending, key)
                if discovery is not None:
                    recorded_management.append(discovery)
            for key in (
                "deleteAzureBefore",
                "deleteAzureDiscovered",
                "deleteAzureFinalDiscovery",
            ):
                discovery = _journal_discovery(pending, key)
                if discovery is not None:
                    recorded_azure.append(discovery)
        management_history = _merge_management_discoveries(recorded_management)
        management_current = discover_management_owned_resources(
            root,
            spec,
            identity,
            require_complete=require_complete,
            verified_uids=tuple(
                str(item["uid"])
                for category in (
                    "controller",
                    "orchestration",
                    "namespaceChildren",
                )
                for item in management_history[category]
                if isinstance(item, dict)
                and isinstance(item.get("uid"), str)
                and item["uid"]
            ),
        )
        management = _merge_management_discoveries(
            (management_history, management_current)
        )
        if require_complete:
            kubeconfig_secret = next(
                (
                    item
                    for item in management["controller"]
                    if item["kind"] == "Secret"
                    and item["name"] == f"{spec.name}-kubeconfig"
                ),
                None,
            )
            if (
                kubeconfig_secret is None
                or kubeconfig_secret["uid"]
                != identity.observed.get("tenantKubeconfigSecretUid")
            ):
                raise RuntimeError("Azure tenant kubeconfig Secret identity changed")
        azure_history = _merge_owned_discoveries(recorded_azure)
        azure_current = _discover_owned_repeatedly(
            root,
            config,
            spec,
            identity,
            passes=2,
            require_parents=require_complete,
            require_azure_resources=require_complete,
            verified_resource_ids=tuple(
                str(item["id"])
                for item in azure_history["azure"]
                if isinstance(item, dict)
                and isinstance(item.get("id"), str)
            ),
        )
        azure = _merge_owned_discoveries((azure_history, azure_current))
        if require_complete:
            _require_recorded_resources_present(
                _recorded_owned_resources(identity),
                azure,
            )
        self._delete_snapshots[spec.name] = {
            "foundation": foundation,
            "foundationHealth": {
                "healthy": healthy,
                "blockers": list(blockers),
            },
            "management": management,
            "azure": azure,
        }

    def _wait_for_controller_cleanup(
        self,
        root: Path,
        config: Mapping[str, str],
        spec: TenantSpec,
        identity: TenantIdentity,
        initial_management: Mapping[str, object],
        initial_azure: Mapping[str, object],
    ) -> tuple[dict[str, object], dict[str, object]]:
        deadline = self.monotonic() + parse_duration(
            config["AZURE_TENANT_TIMEOUT"]
        )
        accumulated = _merge_owned_discoveries((initial_azure,))
        management_history = _merge_management_discoveries(
            (initial_management,)
        )
        last_management: dict[str, object] = {}
        while True:
            try:
                last_management = discover_management_owned_resources(
                    root,
                    spec,
                    identity,
                    require_complete=False,
                    verified_uids=tuple(
                        str(item["uid"])
                        for category in (
                            "controller",
                            "orchestration",
                            "namespaceChildren",
                        )
                        for item in management_history[category]
                        if isinstance(item, dict)
                        and isinstance(item.get("uid"), str)
                        and item["uid"]
                    ),
                )
                management_history = _merge_management_discoveries(
                    (management_history, last_management)
                )
                current = _discover_owned_repeatedly(
                    root,
                    config,
                    spec,
                    identity,
                    passes=2,
                    require_parents=False,
                    require_azure_resources=False,
                    verified_resource_ids=tuple(
                        str(item["id"])
                        for item in accumulated["azure"]
                        if isinstance(item, dict)
                        and isinstance(item.get("id"), str)
                    ),
                )
            except BaseException as exc:
                raise AzureDeletionError(
                    "Azure tenant controller cleanup discovery failed: "
                    + str(exc),
                    management=management_history,
                    azure=accumulated,
                ) from exc
            accumulated = _merge_owned_discoveries((accumulated, current))
            controller = last_management["controller"]
            azure_remaining = current["azure"]
            aso_remaining = current["aso"]
            if not controller and not azure_remaining and not aso_remaining:
                return management_history, accumulated
            if self.monotonic() >= deadline:
                blockers = {
                    "management": [
                        f"{item['kind']}/{item['name']}"
                        for item in controller
                    ],
                    "azureResourceIds": [
                        item["id"] for item in azure_remaining
                    ],
                    "aso": [
                        f"{item['kind']}/{item['name']}"
                        for item in aso_remaining
                    ],
                }
                raise AzureDeletionError(
                    "Azure tenant controller cleanup timed out: "
                    + json.dumps(blockers, sort_keys=True),
                    management=management_history,
                    azure=accumulated,
                )
            self.sleep(min(10, max(0, deadline - self.monotonic())))

    def _wait_for_worker_cleanup(
        self,
        root: Path,
        config: Mapping[str, str],
        spec: TenantSpec,
        identity: TenantIdentity,
    ) -> None:
        deadline = self.monotonic() + parse_duration(
            config["AZURE_TENANT_TIMEOUT"]
        )
        selected = tenant_names(spec)
        inventory = load_inventory(root, config)
        outputs = inventory.get("outputs")
        if not isinstance(outputs, dict):
            raise RuntimeError("Azure foundation inventory outputs are invalid")
        resource_group = outputs.get("resourceGroupName")
        vmss_id = identity.observed.get("vmssId")
        if (
            not isinstance(resource_group, str)
            or not resource_group
            or not isinstance(vmss_id, str)
            or not vmss_id
        ):
            raise RuntimeError("Azure tenant VMSS identity is absent")
        resources = (
            (
                f"machinepool/{selected['pool']}",
                "machinePoolUid",
            ),
            (
                f"azuremachinepool/{selected['pool']}",
                "azureMachinePoolUid",
            ),
        )
        while True:
            remaining = []
            for resource, uid_key in resources:
                payload = _get_management_resource(
                    root,
                    spec.namespace,
                    resource,
                )
                if payload is None:
                    continue
                _require_markers(
                    payload,
                    _expected_tenant_markers(spec, identity),
                    resource,
                )
                metadata = payload.get("metadata")
                metadata = metadata if isinstance(metadata, dict) else {}
                if metadata.get("uid") != identity.observed.get(uid_key):
                    raise RuntimeError(
                        f"Azure tenant management UID changed: {resource}"
                    )
                remaining.append(resource)
            remaining.extend(
                f"machine/{machine['metadata']['name']}"
                for machine in _owned_tenant_machines(root, spec, identity)
            )
            vmss_response = _az(
                "vmss",
                "list",
                "--resource-group",
                resource_group,
                "--query",
                "[].id",
                "--output",
                "json",
                check=False,
            )
            if vmss_response.returncode != 0:
                raise RuntimeError("Azure tenant VMSS absence check failed")
            try:
                vmss_ids = json.loads(vmss_response.stdout)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    "Azure tenant VMSS absence check returned invalid data"
                ) from exc
            if not isinstance(vmss_ids, list) or not all(
                isinstance(item, str) for item in vmss_ids
            ):
                raise RuntimeError(
                    "Azure tenant VMSS absence check returned invalid data"
                )
            if any(_azure_id_equal(item, vmss_id) for item in vmss_ids):
                remaining.append(f"vmss/{selected['pool']}")
            if not remaining:
                return
            if self.monotonic() >= deadline:
                raise RuntimeError(
                    "Azure tenant worker cleanup timed out: "
                    + json.dumps(remaining, sort_keys=True)
                )
            self.sleep(min(10, max(0, deadline - self.monotonic())))

    def _wait_for_tenant_absence(
        self,
        root: Path,
        config: Mapping[str, str],
        spec: TenantSpec,
        identity: TenantIdentity,
        initial_azure: Mapping[str, object],
    ) -> dict[str, object]:
        deadline = self.monotonic() + parse_duration(
            config["AZURE_TENANT_TIMEOUT"]
        )
        accumulated = _merge_owned_discoveries((initial_azure,))
        while True:
            namespace = _get_management_resource(
                root,
                None,
                f"namespace/{spec.namespace}",
            )
            current = _discover_owned_repeatedly(
                root,
                config,
                spec,
                identity,
                passes=2,
                require_parents=False,
                require_azure_resources=False,
                verified_resource_ids=tuple(
                    str(item["id"])
                    for item in accumulated["azure"]
                    if isinstance(item, dict)
                    and isinstance(item.get("id"), str)
                ),
            )
            accumulated = _merge_owned_discoveries((accumulated, current))
            if (
                namespace is None
                and not current["azure"]
                and not current["aso"]
            ):
                return accumulated
            if self.monotonic() >= deadline:
                raise RuntimeError(
                    "Azure tenant final absence timed out: "
                    + json.dumps(
                        {
                            "namespacePresent": namespace is not None,
                            "azureResourceIds": [
                                item["id"] for item in current["azure"]
                            ],
                            "aso": [
                                f"{item['kind']}/{item['name']}"
                                for item in current["aso"]
                            ],
                        },
                        sort_keys=True,
                    )
                )
            self.sleep(min(10, max(0, deadline - self.monotonic())))

    def delete(
        self,
        root: Path,
        spec: TenantSpec,
        identity: TenantIdentity,
        runtime: TenantRuntime,
        journal: OperationJournal,
        timings,
    ) -> None:
        config = self._config(root)
        snapshot = self._delete_snapshots.pop(spec.name, None)
        if snapshot is None:
            self.validate_delete(root, spec, identity)
            snapshot = self._delete_snapshots.pop(spec.name)
        current = runtime.load_operation()
        management = snapshot["management"]
        azure = snapshot["azure"]
        foundation_before = snapshot["foundation"]
        foundation_health_before = snapshot.get(
            "foundationHealth",
            {"healthy": True, "blockers": []},
        )
        try:
            initial_records = {
                "deleteFoundationBefore": json.dumps(
                    foundation_before,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "deleteFoundationHealthBefore": json.dumps(
                    foundation_health_before,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "deleteManagementBefore": json.dumps(
                    management,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "deleteAzureBefore": json.dumps(
                    azure,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            }
            current = runtime.update_operation(
                current,
                phase="delete-inventory-recorded",
                observed={
                    key: value
                    for key, value in initial_records.items()
                    if key not in current.observed
                },
            )
            with timings.phase("worker-deletion"):
                _exclude_tenant_machines_from_drain(
                    root,
                    spec,
                    identity,
                )
                _exact_delete_management_resource(
                    root,
                    spec,
                    identity,
                    namespace=spec.namespace,
                    resource=f"machinepool/{tenant_names(spec)['pool']}",
                    uid_key="machinePoolUid",
                    cascade="foreground",
                )
                self._wait_for_worker_cleanup(
                    root,
                    config,
                    spec,
                    identity,
                )
                current = runtime.update_operation(
                    current,
                    phase="worker-resources-absent",
                )
            with timings.phase("deletion"):
                _exact_delete_management_resource(
                    root,
                    spec,
                    identity,
                    namespace=spec.namespace,
                    resource=f"cluster/{spec.name}",
                    uid_key="clusterUid",
                    cascade="foreground",
                )
                _enable_capz_external_control_plane_delete(
                    root,
                    spec,
                    identity,
                )
                current = runtime.update_operation(
                    current,
                    phase="cluster-deletion-requested",
                )
            with timings.phase("controller-cleanup"):
                management, discovered = self._wait_for_controller_cleanup(
                    root,
                    config,
                    spec,
                    identity,
                    management,
                    azure,
                )
                azure = discovered
                current = runtime.update_operation(
                    current,
                    phase="controller-resources-absent",
                    observed={
                        "deleteManagementDiscovered": json.dumps(
                            management,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        if "deleteManagementDiscovered" not in current.observed
                        else current.observed["deleteManagementDiscovered"],
                        "deleteAzureDiscovered": json.dumps(
                            discovered,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        if "deleteAzureDiscovered" not in current.observed
                        else current.observed["deleteAzureDiscovered"]
                    },
                )
            selected = tenant_names(spec)
            with timings.phase("orchestration-cleanup"):
                for uid_key, resource in (
                    (
                        "cloudValuesConfigMapUid",
                        f"configmap/{selected['cloudValues']}",
                    ),
                    (
                        "networkValuesConfigMapUid",
                        f"configmap/{selected['networkValues']}",
                    ),
                    ("addonJobUid", f"job/{selected['addonJob']}"),
                    (
                        "azureClusterIdentityUid",
                        f"azureclusteridentity/{selected['azureClusterIdentity']}",
                    ),
                ):
                    _exact_delete_management_resource(
                        root,
                        spec,
                        identity,
                        namespace=spec.namespace,
                        resource=resource,
                        uid_key=uid_key,
                        cascade="foreground",
                    )
                _exact_delete_management_resource(
                    root,
                    spec,
                    identity,
                    namespace=None,
                    resource=f"namespace/{spec.namespace}",
                    uid_key="namespaceUid",
                    cascade="foreground",
                )
                current = runtime.update_operation(
                    current,
                    phase="orchestration-deletion-requested",
                )
            with timings.phase("azure-absence"):
                azure = self._wait_for_tenant_absence(
                    root,
                    config,
                    spec,
                    identity,
                    azure,
                )
                current = runtime.update_operation(
                    current,
                    phase="tenant-resources-absent",
                    observed={
                        "deleteAzureFinalDiscovery": json.dumps(
                            azure,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        if "deleteAzureFinalDiscovery" not in current.observed
                        else current.observed["deleteAzureFinalDiscovery"]
                    },
                )
            with timings.phase("foundation-verification"):
                foundation_after, healthy, blockers = _inspect_foundation(
                    root,
                    config,
                    require_healthy=False,
                )
                if not healthy:
                    raise RuntimeError(
                        "Azure management foundation changed during tenant deletion: "
                        + "; ".join(blockers)
                    )
                if foundation_after != foundation_before:
                    raise RuntimeError(
                        "Azure management foundation identity changed during "
                        "tenant deletion"
                    )
                current = runtime.update_operation(
                    current,
                    phase="foundation-verified",
                    observed={
                        "deleteFoundationHealthAfter": json.dumps(
                            {"healthy": healthy, "blockers": list(blockers)},
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        if "deleteFoundationHealthAfter" not in current.observed
                        else current.observed["deleteFoundationHealthAfter"]
                    },
                )
            with timings.phase("runtime-cleanup"):
                _remove_private_tree(azure_tenant_runtime_path(root, spec.name))
                status = self._inspect_absence(
                    root,
                    config,
                    spec.name,
                    foundation_healthy=True,
                )
                if status.classification != "absent":
                    raise RuntimeError(
                        "Azure tenant did not reach canonical absence: "
                        + "; ".join(status.blockers)
                    )
                runtime.update_operation(current, phase="canonical-absence")
        except BaseException as exc:
            if isinstance(exc, AzureDeletionError):
                management = exc.management
                azure = exc.azure
            try:
                _write_deletion_diagnostics(
                    runtime,
                    runtime.load_operation(),
                    _deletion_diagnostics(
                        root,
                        spec,
                        identity,
                        management=management
                        if isinstance(management, Mapping)
                        else None,
                        azure=azure if isinstance(azure, Mapping) else None,
                        error=exc,
                    ),
                )
            except BaseException as diagnostics_error:
                exc.add_note(
                    "Azure deletion diagnostics failed: "
                    + redact(str(diagnostics_error))
                )
            raise
