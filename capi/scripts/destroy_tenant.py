from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

from scripts.cnpg import _verify_marker, cnpg_artifacts_present, delete_cnpg
from scripts.create import validate_create_inputs, verified_tenant_snapshot
from scripts.lib.addons import delete_addons, verify_network
from scripts.lib.files import write_private_file
from scripts.lib.kube import ManagementClient
from scripts.lib.management import (
    require_management_ownership,
    validate_management_kubeconfig,
)
from scripts.lib.process import run
from scripts.lib.tenants import (
    _tenant_kubectl,
    delete_tenant,
    inspect_management_resource,
    inspect_storage_volume,
    ensure_tenant_kubeconfig,
    storage_record_path,
    storage_volume_name,
    tenant_kubeconfig_path,
    verify_tenant_management_ownership,
)
from scripts.machines import worker_snapshot
from scripts.storage import _delete_storage
from scripts.tools import verify_all_inputs


NOT_FOUND = re.compile(r"Error from server \(NotFound\):", re.IGNORECASE)


def _selected_tenants(root: Path, config: dict[str, str], name: str):
    tenants = validate_create_inputs(root, config)
    matches = [tenant for tenant in tenants if tenant.name == name]
    if len(matches) != 1:
        raise RuntimeError(f"unknown tenant: {name}")
    return matches[0], next(tenant for tenant in tenants if tenant.name != name)


def _tenant_resource(root: Path, config: dict[str, str], tenant, *arguments: str):
    response = _tenant_kubectl(
        root,
        config,
        tenant,
        *arguments,
        "-o",
        "json",
        check=False,
    )
    if response.returncode == 0:
        return json.loads(response.stdout)
    if NOT_FOUND.search(response.stderr):
        return None
    raise RuntimeError(
        f"tenant API inspection failed for {' '.join(arguments)}: {response.stderr}"
    )


def _journal_path(root: Path, tenant) -> Path:
    return root / ".runtime" / "deletions" / f"{tenant.name}.json"


def _write_journal(root: Path, tenant, cluster_uid: str) -> None:
    write_private_file(
        _journal_path(root, tenant),
        json.dumps(
            {
                "schema": 1,
                "tenant": tenant.name,
                "clusterUID": cluster_uid,
                "phase": "api-cleanup-complete",
            },
            sort_keys=True,
        )
        + "\n",
    )


def validate_deletion_journal(
    root: Path,
    tenant,
    cluster_uid: str | None = None,
) -> dict[str, object]:
    path = _journal_path(root, tenant)
    details = path.lstat()
    if (
        path.is_symlink()
        or not path.is_file()
        or details.st_uid != os.getuid()
        or details.st_mode & 0o077
    ):
        raise RuntimeError("tenant deletion journal is not an owner-only regular file")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (
        payload.get("schema") != 1
        or payload.get("tenant") != tenant.name
        or payload.get("phase") != "api-cleanup-complete"
        or not payload.get("clusterUID")
    ):
        raise RuntimeError("tenant deletion journal is invalid")
    if cluster_uid is not None and payload["clusterUID"] != cluster_uid:
        raise RuntimeError("tenant deletion journal Cluster UID mismatch")
    return payload


def _storage_smoke_present(root: Path, config: dict[str, str], tenant) -> bool:
    return any(
        _tenant_resource(root, config, tenant, *scope) is not None
        for scope in (
            ("get", "pvc/storage-smoke"),
            ("get", f"pv/{tenant.name}-storage-smoke"),
            ("get", f"storageclass/{config['SPIKE_STORAGE_CLASS']}"),
        )
    )


def _verify_live_api_cleanup(root: Path, config: dict[str, str], tenant) -> None:
    checks = (
        ("-n", config["DATABASE_NAMESPACE"], "get", f"cluster/{tenant.cnpg_cluster}"),
        ("get", f"pv/{tenant.cnpg_cluster}-pv-1"),
        ("get", f"pv/{tenant.cnpg_cluster}-pv-2"),
        ("get", f"pv/{tenant.cnpg_cluster}-pv-3"),
        ("-n", "kube-system", "get", "daemonset/capi-kube-proxy"),
        ("-n", "kube-system", "get", "daemonset/calico-node"),
        ("get", "deployment/storage-smoke"),
        ("get", "pvc/storage-smoke"),
        ("get", f"pv/{tenant.name}-storage-smoke"),
        ("get", f"storageclass/{config['SPIKE_STORAGE_CLASS']}"),
    )
    for arguments in checks:
        if _tenant_resource(root, config, tenant, *arguments) is not None:
            raise RuntimeError(
                f"tenant live API resource remains: {' '.join(arguments)}"
            )


def prepare_tenant_deletion(
    root: Path,
    config: dict[str, str],
    client,
    tenant,
    cluster: dict[str, object],
) -> None:
    journal_path = _journal_path(root, tenant)
    if journal_path.exists():
        validate_deletion_journal(
            root, tenant, str(cluster["metadata"]["uid"])
        )
    if cnpg_artifacts_present(root, config, tenant):
        delete_cnpg(root, config, tenant)
    if _storage_smoke_present(root, config, tenant):
        _delete_storage(root, config, tenant)
    delete_addons(root, config, client, tenant)
    _verify_live_api_cleanup(root, config, tenant)
    if not journal_path.exists():
        _write_journal(root, tenant, cluster["metadata"]["uid"])


def _verify_deleted(root: Path, config: dict[str, str], client, tenant) -> None:
    for resource in (
        f"namespace/{tenant.namespace}",
        f"cluster/{tenant.name}",
    ):
        arguments = (
            ("get", resource)
            if resource.startswith("namespace/")
            else ("-n", tenant.namespace, "get", resource)
        )
        response = client.kubectl(*arguments, check=False)
        if response.returncode == 0:
            raise RuntimeError(f"selected tenant resource remains: {resource}")
        if not NOT_FOUND.search(response.stderr):
            raise RuntimeError(
                f"selected tenant absence inspection failed for {resource}: "
                f"{response.stderr}"
            )
    containers = run(
        [
            "docker",
            "ps",
            "-aq",
            "--filter",
            f"label=io.x-k8s.kind.cluster={tenant.name}",
        ],
        timeout=30,
    ).stdout.split()
    if containers:
        raise RuntimeError(f"selected tenant Docker resources remain: {containers}")
    if inspect_storage_volume(storage_volume_name(config, tenant)) is not None:
        raise RuntimeError("selected tenant Docker volume remains")
    if tenant_kubeconfig_path(root, tenant).exists():
        raise RuntimeError("selected tenant kubeconfig remains")
    if tenant_kubeconfig_path(root, tenant).parent.exists():
        raise RuntimeError("selected tenant credential runtime directory remains")
    if storage_record_path(root, tenant).exists():
        raise RuntimeError("selected tenant storage identity record remains")
    if storage_record_path(root, tenant).parent.exists():
        raise RuntimeError("selected tenant storage runtime directory remains")
    for relative in (
        Path("rendered/tenants") / tenant.name,
        Path("rendered/addons") / tenant.name,
        Path("rendered/storage") / tenant.name,
        Path("rendered/cnpg") / tenant.name,
    ):
        if (root / ".runtime" / relative).exists():
            raise RuntimeError(f"selected tenant runtime subtree remains: {relative}")


def finish_prepared_tenant_deletion(
    root: Path,
    config: dict[str, str],
    client,
    tenant,
) -> None:
    delete_tenant(root, config, client, tenant)
    for relative in (
        Path("rendered/addons") / tenant.name,
        Path("rendered/storage") / tenant.name,
        Path("rendered/cnpg") / tenant.name,
    ):
        shutil.rmtree(root / ".runtime" / relative, ignore_errors=True)
    _journal_path(root, tenant).unlink(missing_ok=True)
    for evidence in (
        root / ".runtime" / "evidence" / "create-success.json",
        root / ".runtime" / "evidence" / "verify-success.json",
    ):
        evidence.unlink(missing_ok=True)
    _verify_deleted(root, config, client, tenant)


def finish_journaled_tenant_deletion(
    root: Path,
    config: dict[str, str],
    client,
    tenant,
) -> None:
    verify_tenant_management_ownership(config, client, tenant)
    if inspect_management_resource(
        client, tenant, f"cluster/{tenant.name}"
    ) is not None:
        raise RuntimeError("journal-only cleanup requires an absent Cluster")
    validate_deletion_journal(root, tenant)
    tenant_kubeconfig_path(root, tenant).unlink(missing_ok=True)
    finish_prepared_tenant_deletion(root, config, client, tenant)


def destroy_tenant_stack(root: Path, config: dict[str, str], name: str) -> None:
    verify_all_inputs(root, config)
    tenant, survivor = _selected_tenants(root, config, name)
    require_management_ownership(root, config)
    validate_management_kubeconfig(root, config)
    client = ManagementClient(root, config)
    survivor_before = verified_tenant_snapshot(
        root, config, client, survivor
    )
    owned = verify_tenant_management_ownership(config, client, tenant)
    cluster = owned.get("cluster")
    journal = _journal_path(root, tenant)
    if cluster is not None:
        ensure_tenant_kubeconfig(root, config, client, tenant)
        prepare_tenant_deletion(root, config, client, tenant, cluster)
    elif (
        not journal.exists()
        and not tenant_kubeconfig_path(root, tenant).exists()
        and inspect_storage_volume(storage_volume_name(config, tenant)) is None
    ):
        for relative in (
            Path("rendered/tenants") / tenant.name,
            Path("rendered/addons") / tenant.name,
            Path("rendered/storage") / tenant.name,
            Path("rendered/cnpg") / tenant.name,
        ):
            shutil.rmtree(root / ".runtime" / relative, ignore_errors=True)
    else:
        record = validate_deletion_journal(root, tenant)
        if record["tenant"] != tenant.name:
            raise RuntimeError("tenant deletion journal does not match target")
        tenant_kubeconfig_path(root, tenant).unlink(missing_ok=True)
    finish_prepared_tenant_deletion(root, config, client, tenant)
    survivor_after = verified_tenant_snapshot(
        root, config, client, survivor
    )
    if survivor_after != survivor_before:
        raise RuntimeError(f"targeted deletion changed survivor: {survivor.name}")
    print(f"tenant deleted; survivor remains healthy: {tenant.name} -> {survivor.name}")
