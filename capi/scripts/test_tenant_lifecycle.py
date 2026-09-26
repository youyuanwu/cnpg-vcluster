from __future__ import annotations

import json
import ipaddress
from pathlib import Path

from scripts.create_management import create_management
from scripts.destroy import destroy
from scripts.lib.controller_scenarios import (
    delete_controller_tenant,
    manifest_tenant_name,
    tenant_document,
    tenant_manifest,
    tenant_snapshot,
    tenant_from_document,
    wait_tenant_absent,
    wait_tenant_ready,
)
from scripts.lib.controller_client import apply_tenant_document, tenant_manifest_document
from scripts.cnpg import _sql
from scripts.test_e2e import capture_tenant_deletion_identity, verify_tenant_deletion
from scripts.lib.kube import ManagementClient
from scripts.lib.locking import tools_lock
from scripts.lib.redaction import redact
from scripts.lib.tenants import _tenant_kubectl, export_tenant_kubeconfig


def _apply(
    root: Path,
    config: dict[str, str],
    name: str,
) -> dict[str, object]:
    client = ManagementClient(root, config)
    apply_tenant_document(client, tenant_manifest_document(config, name))
    document = wait_tenant_ready(root, config, name)
    export_tenant_kubeconfig(root, config, client, tenant_from_document(root, config, document))
    return document


def _snapshot(
    config: dict[str, str],
    client: ManagementClient,
    name: str,
) -> dict[str, object]:
    document = tenant_document(client, name)
    if document is None:
        raise RuntimeError(f"Tenant is absent: {name}")
    return tenant_snapshot(config, client, document)


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


def _verify_isolation(root: Path, config: dict[str, str], client, names) -> None:
    documents = [wait_tenant_ready(root, config, name) for name in names]
    identities = [tenant_snapshot(config, client, document) for document in documents]
    for field in ("slotId", "endpoint", "podCIDR", "serviceCIDR"):
        if len({identity["allocation"][field] for identity in identities}) != len(names):
            raise RuntimeError(f"Tenants share an allocation {field}")
    networks = [
        ipaddress.ip_network(identity["allocation"][field])
        for identity in identities for field in ("podCIDR", "serviceCIDR")
    ]
    if any(left.overlaps(right) for i, left in enumerate(networks) for right in networks[i + 1:]):
        raise RuntimeError("Tenant allocated networks overlap")
    tenants = [tenant_from_document(root, config, document) for document in documents]
    for tenant in tenants:
        result = _sql(
            root, config, tenant,
            "CREATE TABLE isolation_marker(value text PRIMARY KEY);"
            f"INSERT INTO isolation_marker VALUES ('{tenant.name}');"
            "SELECT value FROM isolation_marker;",
        )
        if result != tenant.name:
            raise RuntimeError(f"Tenant SQL isolation write failed: {tenant.name}")
    for tenant, peer in zip(tenants, tenants[1:] + tenants[:1]):
        if _sql(root, config, tenant, "SELECT value FROM isolation_marker;") != tenant.name:
            raise RuntimeError(f"Tenant SQL data crossed an isolation boundary: {tenant.name}")
        response = _tenant_kubectl(
            root, config, tenant, f"--server=https://{peer.vip}:{config['SPIKE_API_PORT']}",
            "get", "--raw=/api", check=False,
        )
        if response.returncode == 0 or not any(
            token in response.stderr.lower() for token in ("x509", "unauthorized", "forbidden")
        ):
            raise RuntimeError("cross-Tenant API credential refusal was not proven")


def _delete_exact(root, config, client, name) -> None:
    document = wait_tenant_ready(root, config, name)
    identity = capture_tenant_deletion_identity(config, client, document)
    delete_controller_tenant(root, config, name)
    verify_tenant_deletion(client, identity)


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
        _verify_isolation(root, config, client, names)

        survivors = {
            name: _snapshot(config, client, name)
            for name in ("tenant-a", "tenant-b")
        }
        first_tenant_c_uid = _snapshot(config, client, "tenant-c")["uid"]
        _delete_exact(root, config, client, "tenant-c")
        for name, before in survivors.items():
            if _snapshot(config, client, name) != before:
                raise RuntimeError(f"targeted deletion changed survivor: {name}")
            tenant = tenant_from_document(root, config, wait_tenant_ready(root, config, name))
            if _sql(root, config, tenant, "SELECT value FROM isolation_marker;") != name:
                raise RuntimeError(f"targeted deletion changed survivor SQL data: {name}")

        recreated = _apply(root, config, "tenant-c")
        if recreated["metadata"]["uid"] == first_tenant_c_uid:
            raise RuntimeError("recreated Tenant retained its previous UID")

        _drift_kube_proxy(root, config, "tenant-a")
        _delete_exact(root, config, client, "tenant-c")
        if tenant_document(client, "tenant-a") is None:
            raise RuntimeError("peer-independent deletion removed tenant-a")

        manifest = tenant_manifest(root, "tenant-c")
        if manifest_tenant_name(manifest) != "tenant-c":
            raise RuntimeError("Tenant manifest identity changed")
        apply_tenant_document(client, tenant_manifest_document(config, "tenant-c"))
        delete_controller_tenant(root, config, "tenant-c")

        _apply(root, config, "tenant-b")
        _apply(root, config, "tenant-c")
        identities = {
            name: capture_tenant_deletion_identity(config, client, wait_tenant_ready(root, config, name))
            for name in ("tenant-b", "tenant-c")
        }
        delete_controller_tenant(root, config, "tenant-b", wait=False)
        delete_controller_tenant(root, config, "tenant-c", wait=False)
        wait_tenant_absent(root, config, "tenant-b")
        wait_tenant_absent(root, config, "tenant-c")
        for identity in identities.values():
            verify_tenant_deletion(client, identity)
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
