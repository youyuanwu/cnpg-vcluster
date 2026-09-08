from __future__ import annotations

from pathlib import Path

from scripts.cnpg import _verify_marker
from scripts.create import (
    reconcile_tenant,
    require_no_pending_deletions,
    stable_tenant_snapshot,
    validate_create_inputs,
)
from scripts.lib.kube import ManagementClient
from scripts.lib.addons import verify_addon_source_ownership
from scripts.lib.management import (
    require_management_ownership,
    validate_management_kubeconfig,
)
from scripts.lib.tenants import (
    _tenant_kubectl,
    tenant_kubeconfig_path,
    verify_tenant_management_ownership,
)
from scripts.network import _repair_addons
from scripts.tools import verify_all_inputs


def select_tenant(root: Path, config: dict[str, str], name: str):
    tenants = validate_create_inputs(root, config)
    matches = [tenant for tenant in tenants if tenant.name == name]
    if len(matches) != 1:
        raise RuntimeError(f"unknown tenant: {name}")
    return matches[0], next(tenant for tenant in tenants if tenant.name != name)


def repair(root: Path, config: dict[str, str], name: str) -> None:
    verify_all_inputs(root, config)
    tenant, survivor = select_tenant(root, config, name)
    require_management_ownership(root, config)
    validate_management_kubeconfig(root, config)
    require_no_pending_deletions(root, [tenant])
    client = ManagementClient(root, config)
    survivor_before = stable_tenant_snapshot(root, config, client, survivor)
    _verify_marker(root, config, survivor)
    owned = verify_tenant_management_ownership(config, client, tenant)
    rebuild_workers = "kamajicontrolplane" not in owned
    verify_addon_source_ownership(root, config, client, tenant)
    before = None
    if tenant_kubeconfig_path(root, tenant).is_file():
        ready = _tenant_kubectl(
            root,
            config,
            tenant,
            "get",
            "--raw=/readyz",
            check=False,
        )
        if ready.returncode == 0:
            before = stable_tenant_snapshot(
                root,
                config,
                client,
                tenant,
                allow_incomplete=True,
            )
    from scripts.lib.tenants import apply_control_plane, export_tenant_kubeconfig

    apply_control_plane(root, config, client, tenant)
    export_tenant_kubeconfig(root, config, client, tenant)
    _repair_addons(root, config, client, tenant)
    if rebuild_workers:
        for machine in owned.get("machines", []):
            client.kubectl(
                "-n",
                tenant.namespace,
                "delete",
                f"machine/{machine['metadata']['name']}",
                "--wait=false",
            )
    reconcile_tenant(
        root,
        config,
        client,
        tenant,
        before=before,
        repair_mode=True,
    )
    _verify_marker(root, config, tenant)
    _verify_marker(root, config, survivor)
    survivor_after = stable_tenant_snapshot(root, config, client, survivor)
    if survivor_after != survivor_before:
        raise RuntimeError(f"repair changed the survivor tenant: {survivor.name}")
    print(f"tenant repaired without changing {survivor.name}: {tenant.name}")
