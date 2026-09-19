from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Mapping, Sequence

from scripts.create import (
    reconcile_tenant,
    stable_tenant_snapshot,
)
from scripts.destroy_tenant import delete_selected_tenant
from scripts.lib.config import load_configuration
from scripts.lib.files import private_file_exists, read_private_file
from scripts.lib.kube import ManagementClient
from scripts.lib.management import (
    allocate_tenant_endpoint,
    management_status,
    release_tenant_endpoint,
    require_management_ownership,
    tenant_endpoint_allocation,
    validate_management_kubeconfig,
    validate_management_network,
)
from scripts.lib.process import run
from scripts.lib.tenant_runtime import (
    OperationJournal,
    TenantIdentity,
    TenantRuntime,
    foundation_sha256,
)
from scripts.lib.tenant_spec import TenantSpec
from scripts.lib.tenant_status import TenantStatus
from scripts.lib.tenants import (
    Tenant,
    NOT_FOUND,
    inspect_management_resource,
    inspect_storage_volume,
    lifecycle_markers,
    recorded_local_specs,
    recorded_local_tenants,
    require_recorded_management_identities,
    resolve_tenant_storage,
    storage_record_path,
    storage_volume_name,
    tenant_from_spec,
    tenant_kubeconfig_path,
    validate_local_tenant_spec,
    verify_tenant_management_ownership,
)
from scripts.status import (
    collect_local_lifecycle_status,
    collect_management_status,
    management_status_healthy,
)
from scripts.tools import verify_all_inputs
from scripts.verify import verify_tenant_functional


class LocalTenantAdapter:
    def __init__(self, *, clock=time.time) -> None:
        self.clock = clock
        self._survivor_snapshots: dict[str, dict[str, dict[str, object]]] = {}

    @staticmethod
    def _config(root: Path) -> dict[str, str]:
        return load_configuration(root)

    @staticmethod
    def _foundation(
        root: Path,
        config: dict[str, str],
        *,
        require_healthy: bool,
    ) -> tuple[dict[str, str], bool]:
        observed = management_status(root, config)
        any_management = any(
            observed.get(key)
            for key in (
                "clusterReported",
                "containerPresent",
                "ownershipRecord",
                "kubeconfig",
            )
        )
        if not any_management:
            if require_healthy:
                raise RuntimeError("local management foundation is absent")
            return {}, False
        identity = require_management_ownership(root, config)
        validate_management_kubeconfig(root, config)
        network = validate_management_network(root, config)
        health = collect_management_status(root, config, strict=True)
        healthy = management_status_healthy(health)
        if require_healthy and not healthy:
            raise RuntimeError("local management foundation is unhealthy")
        kubeconfig = root / ".runtime" / "management" / "kubeconfig"
        foundation = {
            "managementContainer": identity.identifier,
            "managementNetwork": str(network["network_id"]),
            "managementKubeconfigSHA256": hashlib.sha256(
                read_private_file(kubeconfig)
            ).hexdigest(),
        }
        return foundation, healthy

    def foundation_identity(
        self,
        root: Path,
        spec: TenantSpec,
    ) -> Mapping[str, str]:
        config = self._config(root)
        verify_all_inputs(root, config)
        existing = tuple(recorded_local_specs(root).values())
        validate_local_tenant_spec(
            root,
            spec,
            config,
            existing_specs=existing,
        )
        foundation, _ = self._foundation(
            root,
            config,
            require_healthy=True,
        )
        return foundation

    @staticmethod
    def intended_resources(spec: TenantSpec) -> Sequence[str]:
        return (
            f"Namespace/{spec.namespace}",
            f"Cluster/{spec.namespace}/{spec.name}",
            f"DevCluster/{spec.namespace}/{spec.name}",
            f"KamajiControlPlane/{spec.namespace}/{spec.name}",
            f"MachineDeployment/{spec.namespace}/{spec.name}-worker",
            f"KubeadmConfigTemplate/{spec.namespace}/{spec.name}-worker",
            f"DevMachineTemplate/{spec.namespace}/{spec.name}-worker",
            f"DockerVolume/{spec.name}",
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
        current = runtime.load_operation()
        endpoint = allocate_tenant_endpoint(root, config, spec.name)
        marker_operation_id = current.observed.get(
            "markerOperationId",
            current.operation_id,
        )
        current = runtime.update_operation(
            current,
            phase="endpoint-allocated",
            observed={
                "endpoint": endpoint,
                "markerOperationId": marker_operation_id,
            },
        )
        markers = lifecycle_markers(spec, current)
        tenant = tenant_from_spec(root, spec, endpoint, markers=markers)
        resolve_tenant_storage(root, config, tenant)
        observed = reconcile_tenant(
            root,
            config,
            ManagementClient(root, config),
            tenant,
            timings=timings,
            runtime=runtime,
            journal=current,
        )
        functional = verify_tenant_functional(
            root,
            config,
            ManagementClient(root, config),
            tenant,
        )
        if not all(
            (
                functional["network"],
                functional["database"],
                bool(functional["workers"]),
                bool(functional["storage"]),
            )
        ):
            raise RuntimeError("tenant functional Ready probes are incomplete")
        runtime.write_ready_evidence(
            {
                "schema": 1,
                "profile": "local",
                "tenant": spec.name,
                "specificationSha256": spec.sha256(),
                "foundationIdentity": dict(current.foundation_identity),
                "observed": dict(observed),
                "verifiedAt": self.clock(),
                "functional": {
                    "controlPlane": True,
                    "workers": True,
                    "network": True,
                    "storage": True,
                    "database": True,
                },
            }
        )
        return observed

    @staticmethod
    def _operation_failed(
        runtime: TenantRuntime,
        journal: OperationJournal,
    ) -> bool:
        path = (
            runtime.paths.evidence
            / f"{journal.operation}-{journal.operation_id}.json"
        )
        if not private_file_exists(path):
            return False
        payload = json.loads(read_private_file(path).decode())
        records = payload.get("records")
        if not isinstance(records, list):
            raise RuntimeError("tenant lifecycle timing evidence is invalid")
        return any(
            isinstance(record, dict) and record.get("status") == "failed"
            for record in records
        )

    @staticmethod
    def _unrecorded_tenant(root: Path, config: dict[str, str], name: str) -> Tenant:
        return Tenant(
            name=name,
            namespace=name,
            vip="0.0.0.0",
            pod_cidr="0.0.0.0/32",
            service_cidr="0.0.0.0/32",
            dns_ip="0.0.0.0",
            domain=f"{name}.capi.local",
            storage_host_path=root / ".runtime" / "storage" / name,
            cnpg_cluster=f"{name}-postgres",
            workers=1,
        )

    def _inspect_absence(
        self,
        root: Path,
        config: dict[str, str],
        tenant_name: str,
    ) -> TenantStatus:
        foundation, healthy = self._foundation(
            root,
            config,
            require_healthy=False,
        )
        del foundation
        tenant = self._unrecorded_tenant(root, config, tenant_name)
        present: dict[str, object] = {}
        observed_management = management_status(root, config)
        management_exists = any(
            observed_management.get(key)
            for key in (
                "clusterReported",
                "containerPresent",
                "ownershipRecord",
                "kubeconfig",
            )
        )
        if management_exists:
            client = ManagementClient(root, config)
            namespace = client.kubectl(
                "get",
                f"namespace/{tenant_name}",
                "-o",
                "json",
                check=False,
            )
            if namespace.returncode == 0:
                present["namespace"] = tenant_name
            elif not NOT_FOUND.search(namespace.stderr):
                raise RuntimeError(
                    "tenant namespace absence inspection failed: "
                    + namespace.stderr
                )
            for kind, name in (
                ("cluster", tenant_name),
                ("devcluster", tenant_name),
                ("kamajicontrolplane", tenant_name),
                ("machinedeployment", f"{tenant_name}-worker"),
                ("kubeadmconfigtemplate", f"{tenant_name}-worker"),
                ("devmachinetemplate", f"{tenant_name}-worker"),
            ):
                payload = inspect_management_resource(
                    client,
                    tenant,
                    f"{kind}/{name}",
                )
                if payload is not None:
                    present[kind] = payload["metadata"]["uid"]
        containers = run(
            [
                "docker",
                "ps",
                "-aq",
                "--filter",
                f"label=io.x-k8s.kind.cluster={tenant_name}",
            ],
            timeout=30,
        ).stdout.split()
        if containers:
            present["docker"] = sorted(containers)
        volume = inspect_storage_volume(storage_volume_name(config, tenant))
        if volume is not None:
            present["volume"] = volume.get("Name")
        network_path = root / ".runtime" / "management" / "network.json"
        if private_file_exists(network_path):
            endpoint = tenant_endpoint_allocation(root, config, tenant_name)
            if endpoint is not None:
                present["endpoint"] = endpoint
        paths = (
            TenantRuntime(root, "local", tenant_name).paths.ready,
            tenant_kubeconfig_path(root, tenant),
            storage_record_path(root, tenant),
            root / ".runtime" / "rendered" / "tenants" / tenant_name,
            root / ".runtime" / "rendered" / "addons" / tenant_name,
            root / ".runtime" / "rendered" / "storage" / tenant_name,
            root / ".runtime" / "rendered" / "cnpg" / tenant_name,
        )
        residue = [
            str(path.relative_to(root))
            for path in paths
            if os.path.lexists(path)
        ]
        if residue:
            present["runtime"] = residue
        if present:
            return TenantStatus(
                profile="local",
                tenant=tenant_name,
                classification="ownership-invalid",
                foundation_healthy=healthy,
                components={"residue": present},
                blockers=(
                    "tenant resources exist without an authoritative lifecycle identity",
                ),
            )
        return TenantStatus(
            profile="local",
            tenant=tenant_name,
            classification="absent",
            foundation_healthy=healthy,
            components={"inspected": True},
        )

    def status(self, root: Path, tenant: str) -> TenantStatus:
        config = self._config(root)
        runtime = TenantRuntime(root, "local", tenant)
        identity = runtime.load_identity() if runtime.identity_exists() else None
        operation = runtime.load_operation() if runtime.operation_exists() else None
        if identity is None and operation is None:
            return self._inspect_absence(root, config, tenant)
        foundation, healthy = self._foundation(
            root,
            config,
            require_healthy=False,
        )
        binding = (
            identity.foundation_identity
            if identity is not None
            else operation.foundation_identity
        )
        if dict(binding) != foundation:
            raise RuntimeError("tenant foundation binding changed")
        if operation is not None:
            if operation.operation == "delete":
                classification = (
                    "failed"
                    if self._operation_failed(runtime, operation)
                    else "deleting"
                )
            else:
                classification = (
                    "failed"
                    if self._operation_failed(runtime, operation)
                    else "progressing"
                )
            endpoint = operation.observed.get("endpoint")
            if endpoint is not None:
                allocated = tenant_endpoint_allocation(
                    root,
                    config,
                    tenant,
                )
                if allocated != endpoint:
                    raise RuntimeError(
                        "tenant endpoint allocation identity changed"
                    )
                spec = TenantSpec.from_mapping(operation.specification)
                selected = tenant_from_spec(
                    root,
                    spec,
                    endpoint,
                    markers=lifecycle_markers(spec, operation),
                )
                resolve_tenant_storage(root, config, selected)
                verify_tenant_management_ownership(
                    config,
                    ManagementClient(root, config),
                    selected,
                    expected_markers=selected.lifecycle_markers,
                )
            return TenantStatus(
                profile="local",
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
                    else ()
                ),
            )
        assert identity is not None
        endpoint = tenant_endpoint_allocation(root, config, tenant)
        if endpoint is None or endpoint != identity.observed.get("endpoint"):
            raise RuntimeError("tenant endpoint allocation identity changed")
        selected = tenant_from_spec(root, identity.specification, endpoint)
        resolve_tenant_storage(root, config, selected)
        evidence = (
            runtime.load_ready_evidence()
            if private_file_exists(runtime.paths.ready)
            else None
        )
        return collect_local_lifecycle_status(
            root,
            config,
            selected,
            identity,
            evidence,
            foundation_healthy=healthy,
            now=self.clock(),
        )

    def authoritative_absence(
        self,
        root: Path,
        tenant: str,
    ) -> TenantStatus:
        return self._inspect_absence(root, self._config(root), tenant)

    def validate_delete(
        self,
        root: Path,
        spec: TenantSpec,
        identity: TenantIdentity,
    ) -> None:
        config = self._config(root)
        snapshots = {}
        blockers = []
        client = ManagementClient(root, config)
        endpoint = identity.observed.get("endpoint")
        marker_operation_id = identity.observed.get("markerOperationId")
        if not endpoint or not marker_operation_id:
            raise RuntimeError("tenant deletion identity is incomplete")
        target = tenant_from_spec(root, spec, endpoint)
        target.lifecycle_markers = {
            "tenant": spec.name,
            "profile": "local",
            "specificationSha256": spec.sha256(),
            "foundationSha256": foundation_sha256(identity.foundation_identity),
            "operationId": marker_operation_id,
        }
        target_resources = verify_tenant_management_ownership(
            config,
            client,
            target,
            expected_markers=target.lifecycle_markers,
        )
        require_recorded_management_identities(
            target_resources,
            identity.observed,
            require_present=False,
        )
        survivor_specs = recorded_local_specs(root)
        for survivor_name, survivor_spec in survivor_specs.items():
            if survivor_name == spec.name:
                continue
            status = self.status(root, survivor_name)
            if status.classification != "ready":
                blockers.append(
                    f"{survivor_name}: {status.classification}: "
                    + "; ".join(status.blockers)
                )
                continue
            endpoint = tenant_endpoint_allocation(
                root,
                config,
                survivor_name,
            )
            if endpoint is None:
                blockers.append(
                    f"{survivor_name}: endpoint allocation is absent"
                )
                continue
            survivor = tenant_from_spec(
                root,
                survivor_spec,
                endpoint,
            )
            resolve_tenant_storage(root, config, survivor)
            snapshot = stable_tenant_snapshot(
                root,
                config,
                client,
                survivor,
            )
            if snapshot is None:
                blockers.append(
                    f"{survivor.name}: structural identity snapshot is incomplete"
                )
            else:
                snapshots[survivor_name] = snapshot
        if blockers:
            raise RuntimeError(
                "local tenant deletion refused because survivors are not Ready: "
                + " | ".join(blockers)
            )
        self._survivor_snapshots[spec.name] = snapshots

    def delete(
        self,
        root: Path,
        spec: TenantSpec,
        identity: TenantIdentity,
        runtime: TenantRuntime,
        journal: OperationJournal,
        timings,
    ) -> None:
        del runtime
        config = self._config(root)
        endpoint = identity.observed.get("endpoint")
        marker_operation_id = identity.observed.get("markerOperationId")
        if not endpoint or not marker_operation_id:
            raise RuntimeError("tenant deletion identity is incomplete")
        tenant = tenant_from_spec(root, spec, endpoint)
        resolve_tenant_storage(root, config, tenant)
        expected_markers = {
            "tenant": spec.name,
            "profile": "local",
            "specificationSha256": spec.sha256(),
            "foundationSha256": foundation_sha256(identity.foundation_identity),
            "operationId": marker_operation_id,
        }
        snapshots = self._survivor_snapshots.pop(spec.name, {})
        with timings.phase("deletion"):
            delete_selected_tenant(
                root,
                config,
                ManagementClient(root, config),
                tenant,
                expected_markers=expected_markers,
                expected_identities=identity.observed,
            )
        with timings.phase("absence"):
            release_tenant_endpoint(
                root,
                config,
                spec.name,
                canonical_absent=True,
            )
            if tenant_endpoint_allocation(root, config, spec.name) is not None:
                raise RuntimeError("tenant endpoint allocation remains after deletion")
        with timings.phase("foundation-verification"):
            observed_foundation, healthy = self._foundation(
                root,
                config,
                require_healthy=True,
            )
            if (
                observed_foundation != dict(identity.foundation_identity)
                or not healthy
            ):
                raise RuntimeError("local foundation changed during tenant deletion")
            client = ManagementClient(root, config)
            survivor_specs = recorded_local_specs(root)
            for survivor_name in snapshots:
                survivor_spec = survivor_specs.get(survivor_name)
                if survivor_spec is None:
                    raise RuntimeError(
                        f"survivor lifecycle identity disappeared: {survivor_name}"
                    )
                endpoint = tenant_endpoint_allocation(
                    root,
                    config,
                    survivor_name,
                )
                if endpoint is None:
                    raise RuntimeError(
                        f"survivor endpoint disappeared: {survivor_name}"
                    )
                survivor = tenant_from_spec(root, survivor_spec, endpoint)
                resolve_tenant_storage(root, config, survivor)
                if survivor.name == spec.name:
                    continue
                status = self.status(root, survivor.name)
                if status.classification != "ready":
                    raise RuntimeError(
                        f"survivor is no longer Ready: {survivor.name}: "
                        f"{status.classification}"
                    )
                after = stable_tenant_snapshot(
                    root,
                    config,
                    client,
                    survivor,
                )
                if after != snapshots.get(survivor.name):
                    raise RuntimeError(
                        f"targeted deletion changed survivor: {survivor.name}"
                    )
