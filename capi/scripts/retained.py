from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from scripts.cnpg import (
    SQLProbeCleanupError,
    _cnpg_ready,
    _storage_identity,
    _verify_filesystem,
    _verify_marker,
)
from scripts.create import (
    reconcile_tenant,
    stable_tenant_snapshot,
    validate_create_inputs,
)
from scripts.create_management import create_management
from scripts.destroy import destroy
from scripts.destroy_tenant import (
    _journal_path,
    finish_prepared_tenant_deletion,
    finish_journaled_tenant_deletion,
    prepare_tenant_deletion,
)
from scripts.endpoint import _verify_bootstrap_secret
from scripts.lib.addons import (
    NetworkProbeCleanupError,
    verify_addon_source_ownership,
    verify_network,
    wait_network_ready,
)
from scripts.lib.host import prepare_inotify, validate_inotify_state
from scripts.lib.kube import ManagementClient
from scripts.lib.management import (
    require_management_ownership,
    validate_management_network,
    validate_management_kubeconfig,
    validate_management_server_version,
)
from scripts.lib.registry import validate_retained_offline_registry
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
    validate_tenant_kubeconfig_file,
    verify_authoritative_endpoint,
    verify_tenant_control_plane_contract,
    verify_tenant_management_ownership,
    verify_worker_runtime,
)
from scripts.lib.files import write_private_file
from scripts.preflight import run_retained_preflight
from scripts.status import (
    collect_management_status,
    collect_tenant_status,
    management_status_healthy,
)
from scripts.tools import _verify_private_input, verify_all_inputs


RETAINED_SCHEMA = 1
DEV_UP_EVIDENCE_SCHEMA = 1
DEV_TESTS = ("endpoint", "network", "machines", "storage", "database")


def retained_path(root: Path) -> Path:
    return root / ".runtime" / "retained-management.json"


def dev_up_evidence_path(root: Path) -> Path:
    return root / ".runtime" / "evidence" / "dev-up-success.json"


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


def _dev_up_evidence_payload(
    config: dict[str, str],
    tenant,
    identity: dict[str, object],
) -> dict[str, object]:
    return {
        "schema": DEV_UP_EVIDENCE_SCHEMA,
        "tenantCompatibilityRevision": config["TENANT_COMPATIBILITY_REVISION"],
        "cnpgCompatibilityRevision": config["CNPG_COMPATIBILITY_REVISION"],
        "tenant": {
            "name": tenant.name,
            "namespace": tenant.namespace,
            "endpoint": f"{tenant.vip}:{config['SPIKE_API_PORT']}",
            "podCIDR": tenant.pod_cidr,
            "serviceCIDR": tenant.service_cidr,
            "domain": tenant.domain,
            "database": tenant.cnpg_cluster,
            "identity": identity,
        },
    }


def write_dev_up_evidence(
    root: Path,
    config: dict[str, str],
    tenant,
    identity: dict[str, object],
) -> None:
    write_private_file(
        dev_up_evidence_path(root),
        json.dumps(
            _dev_up_evidence_payload(config, tenant, identity),
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
    )


def load_dev_up_evidence(
    root: Path,
    config: dict[str, str],
    tenant,
) -> dict[str, object]:
    path = dev_up_evidence_path(root)
    try:
        payload = json.loads(_verify_private_input(path).decode())
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError("retained dev-up evidence is invalid") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("retained dev-up evidence is invalid")
    tenant_payload = payload.get("tenant")
    identity = (
        tenant_payload.get("identity")
        if isinstance(tenant_payload, dict)
        else None
    )
    if not isinstance(identity, dict):
        raise RuntimeError("retained dev-up evidence identity is invalid")
    expected = _dev_up_evidence_payload(config, tenant, identity)
    if payload != expected:
        raise RuntimeError(
            "retained dev-up evidence is stale or incompatible; "
            "run `just dev-clean` then `just dev-up`"
        )
    return identity


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


def _management_is_healthy(root: Path, config: dict[str, str]) -> bool:
    observed = collect_management_status(root, config)
    if not management_status_healthy(observed):
        return False
    client = ManagementClient(root, config)
    validate_management_server_version(config, client)
    network = validate_management_network(root, config)
    validate_retained_offline_registry(
        root,
        config,
        f"{config['KIND_CLUSTER_NAME']}-control-plane",
        network,
    )
    return True


def _tenant_is_healthy(
    root: Path,
    config: dict[str, str],
    client: ManagementClient,
    tenant,
    cluster: dict[str, object],
    expected_identity: dict[str, object],
) -> bool:
    kubeconfig = tenant_kubeconfig_path(root, tenant)
    if not kubeconfig.exists() and not kubeconfig.is_symlink():
        return False
    validate_tenant_kubeconfig_file(
        root, config, client, tenant, check_access=True
    )
    verify_addon_source_ownership(
        root, config, client, tenant, require_present=False
    )
    before = stable_tenant_snapshot(
        root, config, client, tenant, allow_incomplete=True
    )
    if before is None:
        return False
    if before != expected_identity:
        raise RuntimeError(
            f"retained tenant identity is incompatible: {tenant.name}"
        )
    if not collect_tenant_status(
        root, config, client, tenant, cluster, strict=True
    ).get("ready"):
        return False
    verify_addon_source_ownership(
        root, config, client, tenant, require_present=True
    )
    _dev_test_endpoint(root, config, client, tenant)
    _dev_test_machines(root, config, client, tenant)
    _dev_test_storage(root, config, client, tenant)
    try:
        _dev_test_network(root, config, client, tenant)
        _dev_test_database(root, config, client, tenant)
    except (NetworkProbeCleanupError, SQLProbeCleanupError):
        raise
    except RuntimeError:
        return False
    after = stable_tenant_snapshot(root, config, client, tenant)
    if after != before or after != expected_identity:
        raise RuntimeError(
            f"retained tenant identity changed during health validation: {tenant.name}"
        )
    return True


def _reconcile_dev_up(
    root: Path,
    config: dict[str, str],
    client: ManagementClient,
    tenant,
    *,
    include_management: bool,
) -> None:
    if include_management:
        prepare_inotify(root, config)
        create_management(root, config)
        client = ManagementClient(root, config)
    identity = reconcile_tenant(root, config, client, tenant)
    write_dev_up_evidence(root, config, tenant, identity)
    write_retained_state(root, config)


def _emit_dev_up_result(path: str, started: float) -> None:
    print(
        "CAPI_DEV_UP "
        + json.dumps(
            {
                "path": path,
                "schema": 1,
                "seconds": round(time.monotonic() - started, 3),
                "status": "passed",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def dev_up(root: Path, config: dict[str, str]) -> None:
    started = time.monotonic()
    state = retained_path(root)
    if not state.exists() and not state.is_symlink():
        dev_bootstrap(root, config)
        client = ManagementClient(root, config)
        tenant = validate_create_inputs(root, config)[0]
        identity = reconcile_tenant(root, config, client, tenant)
        write_dev_up_evidence(root, config, tenant, identity)
        write_retained_state(root, config)
        path = "bootstrap"
    else:
        validate_retained_state(root, config)
        run_retained_preflight(root, config, require_inotify=False)
        validate_inotify_state(root, config)
        tenant = validate_create_inputs(root, config)[0]
        client = ManagementClient(root, config)
        evidence_path = dev_up_evidence_path(root)
        if not evidence_path.exists() and not evidence_path.is_symlink():
            _reconcile_dev_up(
                root,
                config,
                client,
                tenant,
                include_management=True,
            )
            path = "full-reconcile"
        else:
            expected_identity = load_dev_up_evidence(
                root, config, tenant
            )
            if not _management_is_healthy(root, config):
                _reconcile_dev_up(
                    root,
                    config,
                    client,
                    tenant,
                    include_management=True,
                )
                path = "full-reconcile"
            else:
                owned = verify_tenant_management_ownership(
                    config, client, tenant
                )
                cluster = owned.get("cluster")
                journal = _journal_path(root, tenant)
                if cluster is not None and (
                    journal.exists() or journal.is_symlink()
                ):
                    raise RuntimeError(
                        f"pending tenant deletion blocks reconciliation: {tenant.name}"
                    )
                if cluster is not None and _tenant_is_healthy(
                    root,
                    config,
                    client,
                    tenant,
                    cluster,
                    expected_identity,
                ):
                    path = "healthy"
                else:
                    if cluster is None:
                        _delete_representative_tenant(
                            root, config, client, tenant
                        )
                    _reconcile_dev_up(
                        root,
                        config,
                        client,
                        tenant,
                        include_management=False,
                    )
                    path = "tenant-reconcile"
    _emit_dev_up_result(path, started)
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
