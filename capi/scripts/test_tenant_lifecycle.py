from __future__ import annotations

import json
from pathlib import Path

from scripts.create_management import create_management
from scripts.destroy import destroy
from scripts.lib.controller_scenarios import (
    apply_controller_tenant,
    delete_controller_tenant,
    manifest_tenant_name,
    tenant_document,
    tenant_manifest,
    tenant_snapshot,
    wait_tenant_absent,
    wait_tenant_ready,
)
from scripts.lib.kube import ManagementClient
from scripts.lib.locking import tools_lock
from scripts.lib.redaction import redact
from scripts.lib.tenants import _tenant_kubectl


def _apply(
    root: Path,
    config: dict[str, str],
    name: str,
) -> dict[str, object]:
    _, _, document = apply_controller_tenant(
        root, config, tenant_manifest(root, name)
    )
    return document


def _snapshot(client: ManagementClient, name: str) -> dict[str, object]:
    document = tenant_document(client, name)
    if document is None:
        raise RuntimeError(f"Tenant is absent: {name}")
    return tenant_snapshot(document)


def _drift_kube_proxy(root: Path, config: dict[str, str], name: str) -> None:
    document = wait_tenant_ready(root, config, name)
    from scripts.lib.controller_scenarios import tenant_from_document

    tenant = tenant_from_document(root, config, document)
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
    names = ("tenant-a", "tenant-b", "tenant-c")
    client = ManagementClient(root, config)
    first_tenant_c_uid = None
    try:
        with tools_lock(root, exclusive=True):
            create_management(root, config)
        for name in names:
            _apply(root, config, name)

        survivors = {
            name: _snapshot(client, name)
            for name in ("tenant-a", "tenant-b")
        }
        first_tenant_c_uid = _snapshot(client, "tenant-c")["uid"]
        delete_controller_tenant(root, config, "tenant-c")
        for name, before in survivors.items():
            if _snapshot(client, name) != before:
                raise RuntimeError(f"targeted deletion changed survivor: {name}")

        recreated = _apply(root, config, "tenant-c")
        if recreated["metadata"]["uid"] == first_tenant_c_uid:
            raise RuntimeError("recreated Tenant retained its previous UID")

        _drift_kube_proxy(root, config, "tenant-a")
        delete_controller_tenant(root, config, "tenant-c")
        if tenant_document(client, "tenant-a") is None:
            raise RuntimeError("peer-independent deletion removed tenant-a")

        manifest = tenant_manifest(root, "tenant-c")
        if manifest_tenant_name(manifest) != "tenant-c":
            raise RuntimeError("Tenant manifest identity changed")
        from scripts.lib.controller_client import apply_tenant

        apply_tenant(root, config, manifest)
        delete_controller_tenant(root, config, "tenant-c")

        _apply(root, config, "tenant-b")
        _apply(root, config, "tenant-c")
        delete_controller_tenant(root, config, "tenant-b", wait=False)
        delete_controller_tenant(root, config, "tenant-c", wait=False)
        wait_tenant_absent(root, config, "tenant-b")
        wait_tenant_absent(root, config, "tenant-c")
        delete_controller_tenant(root, config, "tenant-b")
        delete_controller_tenant(root, config, "tenant-c")
        delete_controller_tenant(root, config, "tenant-a")

        for name in names:
            if tenant_document(client, name) is not None:
                raise RuntimeError(f"Tenant remained after deletion: {name}")
        print(
            json.dumps(
                {
                    "concurrentDeletion": ["tenant-b", "tenant-c"],
                    "peerIndependentDeletion": "tenant-c",
                    "recreated": "tenant-c",
                    "survivors": ["tenant-a", "tenant-b"],
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
