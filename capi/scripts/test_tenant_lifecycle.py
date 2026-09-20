from __future__ import annotations

import json
from pathlib import Path

from scripts.create import stable_tenant_snapshot
from scripts.create_management import create_management
from scripts.destroy import destroy
from scripts.destroy_tenant import prepare_tenant_deletion
from scripts.lib.kube import ManagementClient
from scripts.lib.locking import tools_lock
from scripts.lib.management import tenant_endpoint_allocation
from scripts.lib.redaction import redact
from scripts.lib.tenant_runtime import TenantRuntime
from scripts.lib.tenants import resolve_tenant_storage, tenant_from_spec
from scripts.tenant import execute


def _spec_paths(root: Path) -> dict[str, Path]:
    directory = root / "config" / "tenants" / "tests"
    return {
        name: directory / f"{name}.json"
        for name in ("tenant-a", "tenant-b", "tenant-c")
    }


def _tenant(root: Path, config: dict[str, str], name: str):
    identity = TenantRuntime(root, "local", name).load_identity()
    endpoint = tenant_endpoint_allocation(root, config, name)
    if endpoint is None:
        raise RuntimeError(f"tenant endpoint allocation is absent: {name}")
    tenant = tenant_from_spec(root, identity.specification, endpoint)
    return resolve_tenant_storage(root, config, tenant)


def _snapshot(root: Path, config: dict[str, str], name: str):
    snapshot = stable_tenant_snapshot(
        root,
        config,
        ManagementClient(root, config),
        _tenant(root, config, name),
    )
    if snapshot is None:
        raise RuntimeError(f"tenant snapshot is incomplete: {name}")
    return snapshot


def _create(root: Path, path: Path) -> None:
    execute(root, ["create", "local", str(path)])


def _delete(root: Path, name: str) -> None:
    execute(root, ["delete", "local", name, f"local/{name}"])


def _require_status(root: Path, name: str, classification: str) -> None:
    from scripts.local_tenant import LocalTenantAdapter

    status = LocalTenantAdapter().status(root, name)
    if status.classification != classification:
        raise RuntimeError(
            f"unexpected tenant status for {name}: "
            f"{status.classification}: {status.blockers}"
        )


def _drift_kube_proxy(root: Path, config: dict[str, str], name: str) -> None:
    from scripts.lib.tenants import _tenant_kubectl

    tenant = _tenant(root, config, name)
    _tenant_kubectl(
        root,
        config,
        tenant,
        "-n",
        "kube-system",
        "patch",
        "configmap/capi-kube-proxy",
        "--type=merge",
        "-p",
        '{"data":{"config.conf":"apiVersion: kubeproxy.config.k8s.io/v1alpha1\\n'
        'kind: KubeProxyConfiguration\\nconntrack:\\n  maxPerCore: 1\\n"}}',
    )


def run_tenant_lifecycle(root: Path, config: dict[str, str]) -> None:
    failure = None
    specs = _spec_paths(root)
    try:
        with tools_lock(root, exclusive=True):
            create_management(root, config)
        for name in ("tenant-a", "tenant-b", "tenant-c"):
            _create(root, specs[name])
            _require_status(root, name, "ready")

        survivors = {
            name: _snapshot(root, config, name)
            for name in ("tenant-a", "tenant-b")
        }
        _delete(root, "tenant-c")
        _require_status(root, "tenant-c", "absent")
        for name, before in survivors.items():
            if _snapshot(root, config, name) != before:
                raise RuntimeError(f"targeted deletion changed survivor: {name}")

        _create(root, specs["tenant-c"])
        target_before = _snapshot(root, config, "tenant-c")
        try:
            execute(
                root,
                ["delete", "local", "tenant-c", "wrong-confirmation"],
            )
        except RuntimeError:
            pass
        else:
            raise RuntimeError("invalid deletion confirmation was accepted")
        if _snapshot(root, config, "tenant-c") != target_before:
            raise RuntimeError("invalid confirmation changed the target")
        _drift_kube_proxy(root, config, "tenant-a")
        try:
            _delete(root, "tenant-c")
        except RuntimeError:
            pass
        else:
            raise RuntimeError("unhealthy survivor did not block deletion")
        if _snapshot(root, config, "tenant-c") != target_before:
            raise RuntimeError("refused deletion changed the target")
        _create(root, specs["tenant-a"])

        tenant_c = _tenant(root, config, "tenant-c")
        client = ManagementClient(root, config)
        cluster = json.loads(
            client.kubectl(
                "-n",
                tenant_c.namespace,
                "get",
                f"cluster/{tenant_c.name}",
                "-o",
                "json",
            ).stdout
        )
        prepare_tenant_deletion(root, config, client, tenant_c, cluster)
        _delete(root, "tenant-c")
        _require_status(root, "tenant-c", "absent")
        _create(root, specs["tenant-c"])

        _delete(root, "tenant-b")
        _delete(root, "tenant-c")
        _delete(root, "tenant-a")
        for name in ("tenant-a", "tenant-b", "tenant-c"):
            _require_status(root, name, "absent")
        print(
            json.dumps(
                {
                    "arbitraryTenant": "tenant-c",
                    "multipleSurvivors": ["tenant-a", "tenant-b"],
                    "recreated": "tenant-c",
                    "soleTenantDeleted": "tenant-a",
                },
                sort_keys=True,
            )
        )
    except BaseException as exc:
        failure = exc
    try:
        with tools_lock(root, exclusive=True):
            destroy(root, config)
    except BaseException as cleanup:
        if failure is None:
            raise
        failure.add_note(f"cleanup also failed: {redact(str(cleanup))}")
    if failure is not None:
        raise failure
