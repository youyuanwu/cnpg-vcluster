from __future__ import annotations

import json
import hashlib
import re
from pathlib import Path

from scripts.cnpg import (
    _cnpg_ready,
    _render_cluster,
    _render_operator,
    _verify_marker,
    _write_marker,
    install_cnpg,
)
from scripts.create_management import create_management
from scripts.lib.addons import (
    apply_addons,
    render_resource_set,
    verify_network,
    wait_network_ready,
    verify_addon_source_ownership,
)
from scripts.lib.files import IntegrityError, write_private_file
from scripts.lib.kube import ManagementClient
from scripts.lib.process import run
from scripts.lib.tenants import (
    _tenant_kubectl,
    apply_bootstrap_rbac,
    apply_control_plane,
    apply_workers,
    configured_tenants,
    ensure_tenant_kubeconfig,
    export_tenant_kubeconfig,
    inspect_storage_volume,
    render_tenant_manifests,
    tenant_kubeconfig_path,
    storage_volume_name,
    verify_tenant_control_plane_contract,
    verify_tenant_management_ownership,
)
from scripts.machines import worker_snapshot
from scripts.tools import verify_all_inputs
from scripts.lib.images import (
    TENANT_HOST_IMAGE_KEYS,
    preload_worker_images,
    restore_host_images,
)


TENANT_COMPATIBILITY_REVISION = "capi-kamaji-two-tenant-v1"
CNPG_COMPATIBILITY_REVISION = "docker-volume-hostpath-v1"


def _require_compatibility(config: dict[str, str]) -> None:
    expected = {
        "TENANT_COMPATIBILITY_REVISION": TENANT_COMPATIBILITY_REVISION,
        "CNPG_COMPATIBILITY_REVISION": CNPG_COMPATIBILITY_REVISION,
    }
    for key, value in expected.items():
        if config.get(key) != value:
            raise IntegrityError(f"unsupported {key}: {config.get(key)}")


def validate_create_inputs(root: Path, config: dict[str, str]):
    _require_compatibility(config)
    tenants = configured_tenants(root, config)
    for tenant in tenants:
        render_tenant_manifests(root, config, tenant)
        render_resource_set(root, config, tenant)
        _render_operator(root, config, tenant)
        _render_cluster(root, config, tenant)
    return tenants


def require_no_pending_deletions(root: Path, tenants) -> None:
    pending = [
        tenant.name
        for tenant in tenants
        if (
            root / ".runtime" / "deletions" / f"{tenant.name}.json"
        ).exists()
        or (
            root / ".runtime" / "deletions" / f"{tenant.name}.json"
        ).is_symlink()
    ]
    if pending:
        raise RuntimeError(
            f"pending tenant deletion blocks reconciliation: {', '.join(pending)}"
        )


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
        resources = {}
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
            "kubeconfigSHA256": hashlib.sha256(
                tenant_kubeconfig_path(root, tenant).read_bytes()
            ).hexdigest(),
        }
    except RuntimeError:
        if allow_incomplete:
            return None
        raise


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
    client,
    tenant,
    *,
    before: dict[str, object] | None = None,
    repair_mode: bool = False,
):
    existing_database = None
    if not repair_mode and tenant_kubeconfig_path(root, tenant).is_file():
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
        ):
            existing_database = False
        else:
            raise RuntimeError(
                f"tenant database inspection failed: {tenant.name}: {response.stderr}"
            )
    if before is None and not repair_mode:
        before = stable_tenant_snapshot(
            root,
            config,
            client,
            tenant,
            allow_incomplete=existing_database is not True,
        )
    apply_control_plane(root, config, client, tenant)
    if repair_mode:
        ensure_tenant_kubeconfig(root, config, client, tenant)
    else:
        export_tenant_kubeconfig(root, config, client, tenant)
    apply_bootstrap_rbac(root, config, tenant)
    restore_host_images(root, config, TENANT_HOST_IMAGE_KEYS)
    apply_workers(root, config, client, tenant)
    preload_worker_images(root, config, client, tenant)
    apply_addons(root, config, client, tenant)
    wait_network_ready(root, config, tenant)
    verify_network(root, config, tenant)
    worker_snapshot(root, config, client, tenant)
    install_cnpg(root, config, tenant)
    _write_marker(root, config, tenant)
    _verify_marker(root, config, tenant)
    after = stable_tenant_snapshot(root, config, client, tenant)
    if after is None:
        raise RuntimeError(f"tenant identity snapshot is incomplete: {tenant.name}")
    if before is not None and before != after:
        raise RuntimeError(f"repeated create changed healthy identities: {tenant.name}")
    return after


def create(root: Path, config: dict[str, str]) -> dict[str, object]:
    verify_all_inputs(root, config)
    create_management(root, config)
    tenants = validate_create_inputs(root, config)
    require_no_pending_deletions(root, tenants)
    client = ManagementClient(root, config)
    snapshots = {
        tenant.name: reconcile_tenant(root, config, client, tenant)
        for tenant in tenants
    }
    evidence = {
        "tenantCompatibilityRevision": config["TENANT_COMPATIBILITY_REVISION"],
        "cnpgCompatibilityRevision": config["CNPG_COMPATIBILITY_REVISION"],
        "tenants": {
            tenant.name: {
                "namespace": tenant.namespace,
                "endpoint": f"{tenant.vip}:{config['SPIKE_API_PORT']}",
                "podCIDR": tenant.pod_cidr,
                "serviceCIDR": tenant.service_cidr,
                "domain": tenant.domain,
                "database": tenant.cnpg_cluster,
                "identity": snapshots[tenant.name],
            }
            for tenant in tenants
        },
    }
    write_private_file(
        root / ".runtime" / "evidence" / "create-success.json",
        json.dumps(evidence, sort_keys=True) + "\n",
    )
    print("two tenant clusters reconciled")
    return evidence
