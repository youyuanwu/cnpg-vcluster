from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from scripts.cnpg import (
    _cnpg_ready,
    _storage_identity,
    _verify_filesystem,
    _verify_marker,
)
from scripts.create import reconcile_tenant, validate_create_inputs
from scripts.create_management import create_management
from scripts.destroy import destroy
from scripts.destroy_tenant import (
    _journal_path,
    finish_prepared_tenant_deletion,
    finish_journaled_tenant_deletion,
    prepare_tenant_deletion,
)
from scripts.endpoint import _verify_bootstrap_secret
from scripts.lib.addons import verify_network, wait_network_ready
from scripts.lib.host import prepare_inotify
from scripts.lib.kube import ManagementClient
from scripts.lib.management import (
    require_management_ownership,
    validate_management_kubeconfig,
)
from scripts.machines import worker_snapshot
from scripts.lib.process import run
from scripts.lib.tenants import (
    _tenant_kubectl,
    configured_tenants,
    ensure_tenant_kubeconfig,
    inspect_management_resource,
    inspect_storage_volume,
    storage_record_path,
    storage_volume_name,
    tenant_kubeconfig_path,
    verify_authoritative_endpoint,
    verify_tenant_control_plane_contract,
    verify_tenant_management_ownership,
    verify_worker_runtime,
)
from scripts.lib.files import write_private_file
from scripts.tools import _verify_private_input, verify_all_inputs


RETAINED_SCHEMA = 1
DEV_TESTS = ("endpoint", "network", "machines", "storage", "database")


def retained_path(root: Path) -> Path:
    return root / ".runtime" / "retained-management.json"


def _hash(value: str | bytes) -> str:
    data = value.encode() if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()


def _retained_payload(root: Path, config: dict[str, str]) -> dict[str, object]:
    identity = root / ".runtime" / "management" / "identity.json"
    if not identity.is_file():
        raise RuntimeError("management identity is absent")
    machine_id = Path("/etc/machine-id").read_bytes()
    docker_id = run(
        ["docker", "info", "--format", "{{.ID}}"],
        timeout=30,
    ).stdout.strip()
    branch = run(
        ["git", "branch", "--show-current"], timeout=30, cwd=root.parent
    ).stdout.strip()
    revision = run(
        ["git", "rev-parse", "HEAD"], timeout=30, cwd=root.parent
    ).stdout.strip()
    return {
        "schema": RETAINED_SCHEMA,
        "uid": os.getuid(),
        "root": str(root.resolve()),
        "branch": branch,
        "revision": revision,
        "configuration": _hash(
            json.dumps(config, sort_keys=True, separators=(",", ":"))
        ),
        "managementIdentity": _hash(_verify_private_input(identity)),
        "host": _hash(machine_id),
        "docker": _hash(docker_id),
    }


def write_retained_state(root: Path, config: dict[str, str]) -> None:
    write_private_file(
        retained_path(root),
        json.dumps(
            _retained_payload(root, config),
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
    )


def validate_retained_state(root: Path, config: dict[str, str]) -> None:
    path = retained_path(root)
    if not path.exists() and not path.is_symlink():
        raise RuntimeError(
            "retained management is not initialized; run `just dev-bootstrap`"
        )
    try:
        observed = json.loads(_verify_private_input(path).decode())
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError("retained management state is invalid") from exc
    require_management_ownership(root, config)
    validate_management_kubeconfig(root, config)
    expected = _retained_payload(root, config)
    if observed != expected:
        raise RuntimeError(
            "retained management is stale or belongs to another context; "
            "run `just dev-clean` then `just dev-bootstrap`"
        )


def dev_bootstrap(root: Path, config: dict[str, str]) -> None:
    state = retained_path(root)
    management_identity = root / ".runtime" / "management" / "identity.json"
    if state.exists() or state.is_symlink():
        validate_retained_state(root, config)
    elif management_identity.exists() or management_identity.is_symlink():
        raise RuntimeError(
            "management exists without retained binding; run `just dev-clean` first"
        )
    prepare_inotify(root, config)
    create_management(root, config)
    write_retained_state(root, config)
    print("retained management is ready")


def _delete_representative_tenant(
    root: Path,
    config: dict[str, str],
    client: ManagementClient,
    tenant,
) -> None:
    owned = verify_tenant_management_ownership(config, client, tenant)
    cluster = owned.get("cluster")
    journal = _journal_path(root, tenant)
    if cluster is None and (journal.exists() or journal.is_symlink()):
        finish_journaled_tenant_deletion(root, config, client, tenant)
        return
    if cluster is None:
        partial = any(
            value is not None
            for key, value in owned.items()
            if key != "cluster"
        )
        if (
            partial
            or tenant_kubeconfig_path(root, tenant).exists()
            or tenant_kubeconfig_path(root, tenant).is_symlink()
            or storage_record_path(root, tenant).exists()
            or storage_record_path(root, tenant).is_symlink()
            or inspect_storage_volume(storage_volume_name(config, tenant)) is not None
        ):
            raise RuntimeError(
                "partial retained tenant state blocks recreation; run `just dev-clean`"
            )
        return
    ensure_tenant_kubeconfig(root, config, client, tenant)
    prepare_tenant_deletion(root, config, client, tenant, cluster)
    finish_prepared_tenant_deletion(root, config, client, tenant)


def dev_tenant(root: Path, config: dict[str, str]) -> None:
    validate_retained_state(root, config)
    verify_all_inputs(root, config)
    client = ManagementClient(root, config)
    tenant = validate_create_inputs(root, config)[0]
    _delete_representative_tenant(root, config, client, tenant)
    reconcile_tenant(root, config, client, tenant)
    write_retained_state(root, config)
    print(f"retained management tenant recreated and verified: {tenant.name}")


def dev_up(root: Path, config: dict[str, str]) -> None:
    dev_bootstrap(root, config)
    client = ManagementClient(root, config)
    tenant = validate_create_inputs(root, config)[0]
    reconcile_tenant(root, config, client, tenant)
    write_retained_state(root, config)
    print(f"retained development infrastructure is ready: {tenant.name}")


def _registered_worker(
    root: Path,
    config: dict[str, str],
    client: ManagementClient,
    tenant,
    machine: dict[str, object],
) -> dict[str, object]:
    name = machine["metadata"]["name"]
    devmachine = inspect_management_resource(
        client, tenant, f"devmachine/{name}"
    )
    kubeadm = inspect_management_resource(
        client, tenant, f"kubeadmconfig/{name}"
    )
    node_name = machine.get("status", {}).get("nodeRef", {}).get("name")
    secret_name = (
        kubeadm.get("status", {}).get("dataSecretName")
        if kubeadm is not None
        else None
    )
    if devmachine is None or kubeadm is None or not node_name or not secret_name:
        raise RuntimeError(f"worker registration is incomplete: {name}")
    node = json.loads(
        _tenant_kubectl(
            root,
            config,
            tenant,
            "get",
            f"node/{node_name}",
            "-o",
            "json",
        ).stdout
    )
    return {
        "machine": machine,
        "devmachine": devmachine,
        "kubeadm": kubeadm,
        "node": node,
        "secret": secret_name,
    }


def _dev_test_endpoint(
    root: Path,
    config: dict[str, str],
    client: ManagementClient,
    tenant,
) -> None:
    resources = verify_tenant_management_ownership(config, client, tenant)
    verify_tenant_control_plane_contract(config, tenant, resources)
    machines = resources.get("machines") or []
    if len(machines) != tenant.workers:
        raise RuntimeError(f"worker count is not exact: {tenant.name}")
    for machine in machines:
        registered = _registered_worker(
            root, config, client, tenant, machine
        )
        verify_authoritative_endpoint(
            root, config, client, tenant, registered
        )
        verify_worker_runtime(root, config, tenant, registered)
        _verify_bootstrap_secret(config, client, tenant, registered)


def _dev_test_network(
    root: Path,
    config: dict[str, str],
    client: ManagementClient,
    tenant,
) -> None:
    wait_network_ready(root, config, tenant)
    verify_network(root, config, tenant)


def _dev_test_machines(
    root: Path,
    config: dict[str, str],
    client: ManagementClient,
    tenant,
) -> None:
    worker_snapshot(root, config, client, tenant)


def _dev_test_storage(
    root: Path,
    config: dict[str, str],
    client: ManagementClient,
    tenant,
) -> None:
    del client
    _storage_identity(root, config, tenant)
    _verify_filesystem(config, tenant)


def _dev_test_database(
    root: Path,
    config: dict[str, str],
    client: ManagementClient,
    tenant,
) -> None:
    del client
    if not _cnpg_ready(root, config, tenant):
        raise RuntimeError(f"tenant CNPG is not healthy: {tenant.name}")
    _verify_marker(root, config, tenant)


def dev_test(root: Path, config: dict[str, str], suite: str) -> None:
    validate_retained_state(root, config)
    client = ManagementClient(root, config)
    tenant = configured_tenants(root, config)[0]
    runners = {
        "endpoint": _dev_test_endpoint,
        "network": _dev_test_network,
        "machines": _dev_test_machines,
        "storage": _dev_test_storage,
        "database": _dev_test_database,
    }
    selected = DEV_TESTS if suite == "all" else (suite,)
    unknown = [name for name in selected if name not in runners]
    if unknown:
        raise RuntimeError(
            f"unknown retained test {unknown[0]!r}; expected all or one of: "
            + " ".join(DEV_TESTS)
        )
    for name in selected:
        started = time.monotonic()
        runners[name](root, config, client, tenant)
        print(
            "CAPI_DEV_TEST "
            + json.dumps(
                {
                    "schema": 1,
                    "suite": name,
                    "seconds": round(time.monotonic() - started, 3),
                    "status": "passed",
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )


def dev_clean(root: Path, config: dict[str, str]) -> None:
    destroy(root, config)
    print("retained development state removed")
