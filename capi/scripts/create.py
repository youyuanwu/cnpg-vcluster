from __future__ import annotations

import json
from contextlib import nullcontext
import hashlib
import re
from pathlib import Path
from collections.abc import Callable

from scripts.cnpg import (
    _cnpg_ready,
    _render_cluster,
    _render_operator,
    _verify_marker,
    _write_marker,
    install_cnpg,
)
from scripts.lib.addons import (
    apply_addons,
    render_resource_set,
    verify_network,
    wait_network_ready,
    verify_addon_source_ownership,
)
from scripts.lib.files import IntegrityError
from scripts.lib.kube import ManagementClient
from scripts.lib.process import run
from scripts.lib.tenant_runtime import OperationJournal, TenantRuntime
from scripts.lib.tenant_spec import TenantSpec
from scripts.lib.tenants import (
    NOT_FOUND,
    _tenant_kubectl,
    apply_bootstrap_rbac,
    apply_control_plane,
    apply_workers,
    ensure_tenant_kubeconfig,
    export_tenant_kubeconfig,
    inspect_storage_volume,
    lifecycle_markers,
    management_resource_identities,
    require_recorded_management_identities,
    render_tenant_manifests,
    resource_lifecycle_markers,
    tenant_kubeconfig_path,
    storage_volume_name,
    verify_tenant_control_plane_contract,
    verify_tenant_management_ownership,
)
from scripts.machines import worker_snapshot
from scripts.storage import _storage_status, ensure_storage_ready
from scripts.network import _repair_addons
from scripts.lib.images import (
    TENANT_HOST_IMAGE_KEYS,
    preload_worker_images,
    restore_host_images,
)


CNPG_COMPATIBILITY_REVISION = "docker-volume-hostpath-v1"


def _incomplete_snapshot_error(exc: RuntimeError) -> bool:
    message = str(exc)
    return bool(
        NOT_FOUND.search(message)
        or 'server doesn\'t have a resource type "cluster"' in message
        or "no such object" in message.lower()
        or message.startswith("Machine is not Ready:")
        or message.startswith("DevMachine is not Ready:")
        or message.endswith("-worker topology is not exact")
        or message
        == "Machine, DevMachine, container, and Node sets do not match"
    )


def _require_compatibility(config: dict[str, str]) -> None:
    expected = {"CNPG_COMPATIBILITY_REVISION": CNPG_COMPATIBILITY_REVISION}
    for key, value in expected.items():
        if config.get(key) != value:
            raise IntegrityError(f"unsupported {key}: {config.get(key)}")


def validate_selected_tenant_inputs(
    root: Path,
    config: dict[str, str],
    tenant,
) -> None:
    _require_compatibility(config)
    render_tenant_manifests(root, config, tenant)
    render_resource_set(root, config, tenant)
    _render_operator(root, config, tenant)
    _render_cluster(root, config, tenant)


def stable_tenant_snapshot(
    root: Path,
    config: dict[str, str],
    client,
    tenant,
    *,
    allow_incomplete: bool = False,
):
    if not tenant_kubeconfig_path(root, tenant).is_file():
        return None
    try:
        namespace = json.loads(
            client.kubectl(
                "get",
                f"namespace/{tenant.namespace}",
                "-o",
                "json",
            ).stdout
        )
        resources = {"namespace": namespace["metadata"]["uid"]}
        for kind, name in (
            ("cluster", tenant.name),
            ("devcluster", tenant.name),
            ("kamajicontrolplane", tenant.name),
            ("machinedeployment", f"{tenant.name}-worker"),
            ("kubeadmconfigtemplate", f"{tenant.name}-worker"),
            ("devmachinetemplate", f"{tenant.name}-worker"),
        ):
            payload = json.loads(
                client.kubectl(
                    "-n",
                    tenant.namespace,
                    "get",
                    f"{kind}/{name}",
                    "-o",
                    "json",
                ).stdout
            )
            resources[kind] = payload["metadata"]["uid"]
        load_balancer = json.loads(
            run(["docker", "inspect", f"{tenant.name}-lb"], timeout=30).stdout
        )[0]
        volume = inspect_storage_volume(storage_volume_name(config, tenant))
        if volume is None:
            return None
        database = json.loads(
            _tenant_kubectl(
                root,
                config,
                tenant,
                "-n",
                config["DATABASE_NAMESPACE"],
                "get",
                f"cluster/{tenant.cnpg_cluster}",
                "-o",
                "json",
            ).stdout
        )
        operator = json.loads(
            _tenant_kubectl(
                root,
                config,
                tenant,
                "-n",
                config["CNPG_NAMESPACE"],
                "get",
                "deployment/cnpg-controller-manager",
                "-o",
                "json",
            ).stdout
        )
        pvcs = json.loads(
            _tenant_kubectl(
                root,
                config,
                tenant,
                "-n",
                config["DATABASE_NAMESPACE"],
                "get",
                "pvc",
                "-l",
                f"cnpg.io/cluster={tenant.cnpg_cluster}",
                "-o",
                "json",
            ).stdout
        )["items"]
        storage = {}
        for pvc in pvcs:
            pv = json.loads(
                _tenant_kubectl(
                    root,
                    config,
                    tenant,
                    "get",
                    f"pv/{pvc['spec']['volumeName']}",
                    "-o",
                    "json",
                ).stdout
            )
            storage[pvc["metadata"]["name"]] = {
                "pvcUID": pvc["metadata"]["uid"],
                "pv": pv["metadata"]["name"],
                "pvUID": pv["metadata"]["uid"],
            }
        return {
            "resources": resources,
            "loadBalancerID": load_balancer["Id"],
            "workers": worker_snapshot(root, config, client, tenant),
            "volume": {
                "name": volume["Name"],
                "createdAt": volume["CreatedAt"],
                "mountpoint": volume["Mountpoint"],
            },
            "databaseUID": database["metadata"]["uid"],
            "operatorUID": operator["metadata"]["uid"],
            "storage": storage,
            "storageSmoke": _storage_status(root, config, tenant),
            "kubeconfigSHA256": hashlib.sha256(
                tenant_kubeconfig_path(root, tenant).read_bytes()
            ).hexdigest(),
        }
    except RuntimeError as exc:
        if allow_incomplete and _incomplete_snapshot_error(exc):
            return None
        raise


def observed_tenant_identities(
    snapshot: dict[str, object],
    *,
    endpoint: str,
    marker_operation_id: str,
) -> dict[str, str]:
    resources = snapshot["resources"]
    volume = snapshot["volume"]
    return {
        "endpoint": endpoint,
        "markerOperationId": marker_operation_id,
        "namespaceUID": str(resources["namespace"]),
        "clusterUID": str(resources["cluster"]),
        "devClusterUID": str(resources["devcluster"]),
        "controlPlaneUID": str(resources["kamajicontrolplane"]),
        "machineDeploymentUID": str(resources["machinedeployment"]),
        "kubeadmTemplateUID": str(resources["kubeadmconfigtemplate"]),
        "devMachineTemplateUID": str(resources["devmachinetemplate"]),
        "loadBalancerID": str(snapshot["loadBalancerID"]),
        "kubeconfigSHA256": str(snapshot["kubeconfigSHA256"]),
        "storageVolumeName": str(volume["name"]),
        "storageVolumeCreatedAt": str(volume["createdAt"]),
        "storageVolumeMountpoint": str(volume["mountpoint"]),
        "workersSHA256": hashlib.sha256(
            json.dumps(
                snapshot["workers"],
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
        "databaseUID": str(snapshot["databaseUID"]),
        "operatorUID": str(snapshot["operatorUID"]),
        "databaseStorageSHA256": hashlib.sha256(
            json.dumps(
                snapshot["storage"],
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
        "storageProbeSHA256": hashlib.sha256(
            json.dumps(
                snapshot["storageSmoke"],
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
    }


def _management_identities(resources: dict[str, object]) -> dict[str, str]:
    return management_resource_identities(resources)


def _record_management_identities(
    runtime: TenantRuntime,
    journal: OperationJournal,
    resources: dict[str, object],
    *,
    phase: str,
) -> OperationJournal:
    expected_markers = lifecycle_markers(
        TenantSpec.from_mapping(journal.specification),
        journal,
    )
    current = journal
    for resource, identifier in _management_identities(resources).items():
        if current.observed.get(resource) == identifier:
            continue
        payload_key = next(
            key
            for key, observed_key in {
                "namespace": "namespaceUID",
                "cluster": "clusterUID",
                "devcluster": "devClusterUID",
                "kamajicontrolplane": "controlPlaneUID",
                "machinedeployment": "machineDeploymentUID",
                "kubeadmconfigtemplate": "kubeadmTemplateUID",
                "devmachinetemplate": "devMachineTemplateUID",
            }.items()
            if observed_key == resource
        )
        payload = resources[payload_key]
        current = runtime.recover_observed_identity(
            current,
            resource=resource,
            identifier=identifier,
            markers=resource_lifecycle_markers(payload),
            phase=phase,
        )
    if current.observed.get("markerOperationId") is None:
        current = runtime.update_operation(
            current,
            phase=phase,
            observed={"markerOperationId": expected_markers["operationId"]},
        )
    return current


def verified_tenant_snapshot(
    root: Path,
    config: dict[str, str],
    client,
    tenant,
):
    resources = verify_tenant_management_ownership(config, client, tenant)
    verify_tenant_control_plane_contract(config, tenant, resources)
    verify_addon_source_ownership(
        root, config, client, tenant, require_present=True
    )
    ensure_tenant_kubeconfig(root, config, client, tenant)
    verify_network(root, config, tenant)
    worker_snapshot(root, config, client, tenant)
    if not _cnpg_ready(root, config, tenant):
        raise RuntimeError(f"tenant CNPG is not healthy: {tenant.name}")
    _verify_marker(root, config, tenant)
    snapshot = stable_tenant_snapshot(root, config, client, tenant)
    if snapshot is None:
        raise RuntimeError(f"tenant identity snapshot is incomplete: {tenant.name}")
    return snapshot


def reconcile_tenant(
    root: Path,
    config: dict[str, str],
    client=None,
    tenant=None,
    *,
    before: dict[str, object] | None = None,
    timings=None,
    runtime: TenantRuntime | None = None,
    journal: OperationJournal | None = None,
    checkpoint: Callable[[str, OperationJournal], None] | None = None,
):
    phase = timings.phase if timings is not None else lambda _: nullcontext()
    if (runtime is None) != (journal is None):
        raise RuntimeError("tenant runtime and journal must be supplied together")
    validate_selected_tenant_inputs(root, config, tenant)
    current_journal = journal
    with phase("control-plane"):
        if client is None:
            client = ManagementClient(root, config)
        if current_journal is not None:
            existing_resources = verify_tenant_management_ownership(
                config,
                client,
                tenant,
                expected_markers=tenant.lifecycle_markers or None,
            )
            require_recorded_management_identities(
                existing_resources,
                current_journal.observed,
                require_present=True,
            )
        existing_database = None
        if tenant_kubeconfig_path(root, tenant).is_file():
            response = _tenant_kubectl(
                root,
                config,
                tenant,
                "-n",
                config["DATABASE_NAMESPACE"],
                "get",
                f"cluster/{tenant.cnpg_cluster}",
                check=False,
            )
            if response.returncode == 0:
                existing_database = True
            elif re.search(
                r"Error from server \(NotFound\):",
                response.stderr,
                re.IGNORECASE,
            ) or 'server doesn\'t have a resource type "cluster"' in response.stderr:
                existing_database = False
            else:
                raise RuntimeError(
                    f"tenant database inspection failed: {tenant.name}: {response.stderr}"
                )
        if before is None:
            before = stable_tenant_snapshot(
                root,
                config,
                client,
                tenant,
                allow_incomplete=True,
            )
        apply_control_plane(root, config, client, tenant)
        export_tenant_kubeconfig(root, config, client, tenant)
        apply_bootstrap_rbac(root, config, tenant)
        ensure_tenant_kubeconfig(root, config, client, tenant)
        resources = verify_tenant_management_ownership(
            config,
            client,
            tenant,
            expected_markers=tenant.lifecycle_markers or None,
        )
        if runtime is not None and current_journal is not None:
            current_journal = _record_management_identities(
                runtime,
                current_journal,
                resources,
                phase="control-plane-ready",
            )
            current_journal = runtime.update_operation(
                current_journal,
                phase="credentials-ready",
                observed={
                    "endpoint": tenant.vip,
                    "kubeconfigSHA256": hashlib.sha256(
                        tenant_kubeconfig_path(root, tenant).read_bytes()
                    ).hexdigest(),
                },
            )
            if checkpoint is not None:
                checkpoint("control-plane", current_journal)
    with phase("workers"):
        restore_host_images(root, config, TENANT_HOST_IMAGE_KEYS)
        apply_workers(root, config, client, tenant)
        preload_worker_images(root, config, client, tenant)
        resources = verify_tenant_management_ownership(
            config,
            client,
            tenant,
            expected_markers=tenant.lifecycle_markers or None,
        )
        if runtime is not None and current_journal is not None:
            current_journal = _record_management_identities(
                runtime,
                current_journal,
                resources,
                phase="workers-applied",
            )
            if checkpoint is not None:
                checkpoint("workers", current_journal)
    with phase("add-ons"):
        apply_addons(root, config, client, tenant)
        _repair_addons(root, config, client, tenant)
        wait_network_ready(root, config, tenant)
        verify_network(root, config, tenant)
        workers = worker_snapshot(root, config, client, tenant)
        volume = inspect_storage_volume(storage_volume_name(config, tenant))
        if volume is None:
            raise RuntimeError("tenant storage volume is absent")
        if runtime is not None and current_journal is not None:
            current_journal = runtime.update_operation(
                current_journal,
                phase="network-ready",
                observed={
                    "loadBalancerID": str(
                        json.loads(
                            run(
                                ["docker", "inspect", f"{tenant.name}-lb"],
                                timeout=30,
                            ).stdout
                        )[0]["Id"]
                    ),
                    "storageVolumeName": str(volume["Name"]),
                    "storageVolumeCreatedAt": str(volume["CreatedAt"]),
                    "storageVolumeMountpoint": str(volume["Mountpoint"]),
                    "workersSHA256": hashlib.sha256(
                        json.dumps(
                            workers,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode()
                    ).hexdigest(),
                },
            )
            if checkpoint is not None:
                checkpoint("network", current_journal)
    with phase("data-services"):
        ensure_storage_ready(root, config, tenant)
        install_cnpg(root, config, tenant)
        _write_marker(root, config, tenant)
        _verify_marker(root, config, tenant)
        from scripts.cnpg import _verify_filesystem

        _verify_filesystem(config, tenant)
        after = stable_tenant_snapshot(root, config, client, tenant)
        if after is None:
            raise RuntimeError(f"tenant identity snapshot is incomplete: {tenant.name}")
        if before is not None and before != after:
            raise RuntimeError(f"repeated create changed healthy identities: {tenant.name}")
        marker_operation_id = (
            current_journal.observed.get("markerOperationId")
            if current_journal is not None
            else tenant.lifecycle_markers.get("operationId", "internal")
        )
        observed = observed_tenant_identities(
            after,
            endpoint=tenant.vip,
            marker_operation_id=str(marker_operation_id),
        )
        if runtime is not None and current_journal is not None:
            current_journal = runtime.update_operation(
                current_journal,
                phase="ready",
                observed=observed,
            )
            if checkpoint is not None:
                checkpoint("data-services", current_journal)
        return observed
