from __future__ import annotations

from pathlib import Path

from scripts.cnpg import _verify_marker
from scripts.create import (
    reconcile_tenant,
    require_no_pending_deletions,
    validate_create_inputs,
    verified_tenant_snapshot,
)
from scripts.lib.kube import ManagementClient
from scripts.lib.addons import verify_addon_source_ownership
from scripts.lib.management import (
    require_management_ownership,
    validate_management_kubeconfig,
)
from scripts.lib.tenants import (
    _tenant_kubectl,
    ensure_tenant_kubeconfig,
    tenant_kubeconfig_path,
    validate_tenant_kubeconfig_file,
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
    survivor_before = verified_tenant_snapshot(
        root, config, client, survivor
    )
    owned = verify_tenant_management_ownership(config, client, tenant)
    rebuild_workers = "kamajicontrolplane" not in owned
    verify_addon_source_ownership(root, config, client, tenant)
    before = None
    if tenant_kubeconfig_path(root, tenant).is_file():
        try:
            validate_tenant_kubeconfig_file(
                root,
                config,
                client,
                tenant,
                check_access=False,
            )
        except RuntimeError:
            pass
        else:
            ready = _tenant_kubectl(
                root,
                config,
                tenant,
                "get",
                "--raw=/readyz",
                check=False,
            )
            if ready.returncode == 0:
                from scripts.create import stable_tenant_snapshot

                before = stable_tenant_snapshot(
                    root,
                    config,
                    client,
                    tenant,
                    allow_incomplete=True,
                )
    from scripts.lib.tenants import apply_control_plane

    apply_control_plane(root, config, client, tenant)
    ensure_tenant_kubeconfig(root, config, client, tenant)
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
    survivor_after = verified_tenant_snapshot(
        root, config, client, survivor
    )
    if survivor_after != survivor_before:
        raise RuntimeError(f"repair changed the survivor tenant: {survivor.name}")
    print(f"tenant repaired without changing {survivor.name}: {tenant.name}")
