from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from scripts.lib.controller_catalog import (
    ManagementResource,
    load_management_resources,
)
from scripts.lib.database_controller import (
    inspect_catalog_inventory, require_absent_legacy_database_crd,
)
from scripts.lib.kube import ManagementClient
from scripts.lib.process import run


LEGACY_RESOURCES = (
    (None, "validatingwebhookconfiguration/tenant-controller-validating-webhook"),
    ("tenant-system", "service/tenant-controller-webhook"),
    ("tenant-system", "certificate.cert-manager.io/tenant-controller-serving-cert"),
    ("tenant-system", "issuer.cert-manager.io/tenant-controller-selfsigned"),
    ("tenant-system", "secret/tenant-controller-serving-cert"),
    ("tenant-system", "configmap/tenant-endpoint-allocations"),
)

def _missing(response, *, undiscovered: bool = False) -> bool:
    pattern = r"not\s*found|notfound"
    if undiscovered:
        pattern += r"|doesn't have a resource type|could not find the requested resource"
    return response.returncode != 0 and bool(
        re.search(pattern, f"{response.stdout}{response.stderr}", re.IGNORECASE)
    )


def verify_absent(client: ManagementClient, namespace: str | None, resource: str) -> None:
    scope = ("-n", namespace) if namespace else ()
    response = client.kubectl(*scope, "get", resource, "-o", "name", check=False)
    if not _missing(
        response,
        undiscovered=resource.startswith(
            ("certificate.cert-manager.io/", "issuer.cert-manager.io/")
        ),
    ):
        raise RuntimeError(
            f"unsupported legacy resource blocks activation: {resource}"
        )


def delete_named(
    config: dict[str, str],
    client: ManagementClient,
    namespace: str | None,
    resource: str,
) -> None:
    scope = ("-n", namespace) if namespace else ()
    response = client.kubectl(
        *scope,
        "delete",
        resource,
        "--ignore-not-found=true",
        "--wait=true",
        "--cascade=foreground",
        f"--timeout={config['DELETE_TIMEOUT']}",
        check=False,
    )
    if response.returncode != 0 and not _missing(response, undiscovered=True):
        raise RuntimeError(f"failed to remove {resource}: {response.stderr}")


def require_clean_controller_state(
    root: Path,
    client: ManagementClient,
) -> None:
    tenants = client.kubectl(
        "get",
        "tenants.tenancy.cnpg-vcluster.io",
        "-o",
        "name",
        check=False,
    )
    if tenants.returncode == 0 and tenants.stdout.strip():
        raise RuntimeError(
            f"Tenant resources block activation: {tenants.stdout.strip()}"
        )
    if tenants.returncode != 0:
        raise RuntimeError(f"failed to inspect Tenant resources: {tenants.stderr}")
    require_absent_legacy_database_crd(client)
    inspect_catalog_inventory(client)
    for resource in load_management_resources(root):
        _verify_discovery(client, resource)
        for item in _inventory(client, resource):
            if _inventory_blocks(resource, item["metadata"]):
                raise RuntimeError(
                    f"{resource.kubectl_resource} residue blocks activation: "
                    f"{item['metadata']['name']}"
                )
    volumes = set(run(
        ["docker", "volume", "ls", "-q"],
        timeout=30,
    ).stdout.split())
    tenant_volumes = {name for name in volumes if name.endswith("-storage")}
    for label in (
        "cnpg-vcluster.capi/role",
        "cnpg-vcluster.capi/tenant",
        "tenancy.cnpg-vcluster.io/tenant-uid",
    ):
        tenant_volumes.update(
            run(
                ["docker", "volume", "ls", "-q", "--filter", f"label={label}"],
                timeout=30,
            ).stdout.split()
        )
    if tenant_volumes:
        raise RuntimeError(
            f"Tenant storage volumes block activation: {sorted(tenant_volumes)}"
        )
    capd_containers: set[str] = set()
    for role in ("worker", "external-load-balancer"):
        capd_containers.update(
            run(
                [
                    "docker",
                    "ps",
                    "-aq",
                    "--filter",
                    f"label=io.x-k8s.kind.role={role}",
                ],
                timeout=30,
            ).stdout.split()
        )
    project_role_containers = set(
        run(
            [
                "docker",
                "ps",
                "-aq",
                "--filter",
                "label=cnpg-vcluster.capi/role",
            ],
            timeout=30,
        ).stdout.split()
    )
    tenant_marked_containers: set[str] = set()
    for label in (
        "cnpg-vcluster.capi/tenant",
        "tenancy.cnpg-vcluster.io/tenant-uid",
    ):
        tenant_marked_containers.update(
            run(
                ["docker", "ps", "-aq", "--filter", f"label={label}"],
                timeout=30,
            ).stdout.split()
        )
    offline_registries = set(
        run(
            [
                "docker",
                "ps",
                "-aq",
                "--filter",
                "label=cnpg-vcluster.capi/role=offline-registry",
            ],
            timeout=30,
        ).stdout.split()
    )
    containers = (
        capd_containers
        | tenant_marked_containers
        | (project_role_containers - offline_registries)
    )
    if containers:
        raise RuntimeError(
            f"CAPD Tenant containers block activation: {sorted(containers)}"
        )
    for namespace, resource in LEGACY_RESOURCES:
        verify_absent(client, namespace, resource)


def _verify_discovery(
    client: ManagementClient,
    resource: ManagementResource,
) -> None:
    response = client.kubectl(
        "get",
        f"--raw={resource.discovery_path}",
        check=False,
    )
    if response.returncode != 0:
        raise RuntimeError(
            f"failed to discover {resource.api_version} {resource.kind}: "
            f"{response.stderr}"
        )
    try:
        document = json.loads(response.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"invalid discovery response for {resource.api_version} {resource.kind}"
        ) from exc
    entries = document.get("resources") if isinstance(document, dict) else None
    if not isinstance(entries, list) or not any(
        isinstance(entry, dict)
        and entry.get("name") == resource.plural
        and entry.get("kind") == resource.kind
        and entry.get("namespaced") is resource.namespaced
        for entry in entries
    ):
        raise RuntimeError(
            f"catalog resource is not served: {resource.api_version} {resource.kind}"
        )


def _inventory(
    client: ManagementClient,
    resource: ManagementResource,
) -> list[dict[str, object]]:
    response = client.kubectl(
        "get",
        f"--raw={resource.inventory_path}",
        check=False,
    )
    if response.returncode != 0:
        raise RuntimeError(
            f"failed to inspect {resource.api_version} {resource.kind}: "
            f"{response.stderr}"
        )
    try:
        document = json.loads(response.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"invalid inventory response for {resource.api_version} {resource.kind}"
        ) from exc
    items = document.get("items") if isinstance(document, dict) else None
    if (
        not isinstance(document, dict)
        or document.get("apiVersion") != resource.api_version
        or document.get("kind") != f"{resource.kind}List"
        or not isinstance(items, list)
    ):
        raise RuntimeError(
            f"invalid inventory response for {resource.api_version} {resource.kind}"
        )
    for item in items:
        metadata = item.get("metadata") if isinstance(item, dict) else None
        namespace = metadata.get("namespace") if isinstance(metadata, dict) else None
        if isinstance(metadata, dict):
            for field, expected in (
                ("annotations", dict),
                ("labels", dict),
                ("ownerReferences", list),
            ):
                if field in metadata and not isinstance(metadata[field], expected):
                    raise RuntimeError(
                        f"invalid inventory identity for "
                        f"{resource.api_version} {resource.kind}"
                    )
            for field in ("annotations", "labels"):
                values = metadata.get(field, {})
                if not all(
                    isinstance(key, str) and isinstance(value, str)
                    for key, value in values.items()
                ):
                    raise RuntimeError(
                        f"invalid inventory identity for "
                        f"{resource.api_version} {resource.kind}"
                    )
            for owner in metadata.get("ownerReferences", []):
                if (
                    not isinstance(owner, dict)
                    or any(
                        not isinstance(owner.get(field), str)
                        or not owner[field]
                        for field in ("apiVersion", "kind", "name", "uid")
                    )
                    or any(
                        field in owner and not isinstance(owner[field], bool)
                        for field in ("controller", "blockOwnerDeletion")
                    )
                ):
                    raise RuntimeError(
                        f"invalid inventory identity for "
                        f"{resource.api_version} {resource.kind}"
                    )
        valid_namespace = (
            isinstance(namespace, str)
            and bool(namespace)
            and (
                resource.inventory_namespace is None
                or namespace == resource.inventory_namespace
            )
            if resource.namespaced
            else namespace in (None, "")
        )
        if (
            not isinstance(item, dict)
            or (
                "apiVersion" in item
                and item["apiVersion"] != resource.api_version
            )
            or ("kind" in item and item["kind"] != resource.kind)
            or not isinstance(metadata, dict)
            or not isinstance(metadata.get("name"), str)
            or not metadata["name"]
            or not isinstance(metadata.get("uid"), str)
            or not metadata["uid"]
            or not valid_namespace
        ):
            raise RuntimeError(
                f"invalid inventory identity for {resource.api_version} {resource.kind}"
            )
    return items


def _inventory_blocks(
    resource: ManagementResource,
    metadata: dict[str, object],
) -> bool:
    annotations = metadata.get("annotations")
    labels = metadata.get("labels")
    owners = metadata.get("ownerReferences")
    annotations = annotations if isinstance(annotations, dict) else {}
    labels = labels if isinstance(labels, dict) else {}
    owners = owners if isinstance(owners, list) else []
    tenant_marked = (
        "tenancy.cnpg-vcluster.io/tenant" in annotations
        or "tenancy.cnpg-vcluster.io/tenant-uid" in annotations
    )
    allocation_marked = (
        "tenancy.cnpg-vcluster.io/slot-id" in labels
        or "tenancy.cnpg-vcluster.io/tenant" in labels
        or annotations.get("tenancy.cnpg-vcluster.io/resource")
        == "allocation-lease"
        or "tenancy.cnpg-vcluster.io/slot-id" in annotations
    )
    if tenant_marked or allocation_marked:
        return True
    if resource.inventory_policy == "block-any-instance":
        return True
    if resource.inventory_policy == "tenant-markers":
        marked = False
    elif resource.inventory_policy == "tenant-markers-or-kamaji-owner":
        marked = any(
            owner.get("kind") == "KamajiControlPlane"
            for owner in owners
        )
    elif resource.inventory_policy == "allocation-markers":
        marked = False
    else:
        raise RuntimeError("management resource catalog inventory policy is invalid")
    return marked or not resource.exemptions


def activation_ticket(
    configuration_hash: str,
    token: str,
    previous_hash: str | None,
) -> dict[str, object]:
    if not configuration_hash or not token:
        raise ValueError("activation ticket identity is incomplete")
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": "tenant-controller-activation",
            "namespace": "tenant-system",
        },
        "data": {
            "configurationHash": configuration_hash,
            "previousConfigurationHash": previous_hash or "",
            "token": token,
            "hostClean": "true",
            "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        },
    }
