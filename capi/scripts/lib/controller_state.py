from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

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
    catalog = json.loads(
        (root / "controller/config/management-resources.json").read_text(
            encoding="utf-8"
        )
    )
    if not isinstance(catalog, list) or not catalog:
        raise RuntimeError("management resource catalog is invalid")
    for entry in catalog:
        if not isinstance(entry, dict) or set(entry) != {
            "apiVersion",
            "kind",
            "plural",
            "namespaced",
            "role",
            "class",
            "parentKind",
            "alternateParentKind",
            "workerSuffix",
            "watched",
            "inventoryPolicy",
            "exemptions",
        }:
            raise RuntimeError("management resource catalog is invalid")
        if (
            not isinstance(entry["namespaced"], bool)
            or not isinstance(entry["role"], str)
            or not entry["role"]
            or entry["class"] not in {"root", "descendant", "typed"}
            or (
                entry["parentKind"] is not None
                and not isinstance(entry["parentKind"], str)
            )
            or (
                entry["alternateParentKind"] is not None
                and not isinstance(entry["alternateParentKind"], str)
            )
            or not isinstance(entry["workerSuffix"], bool)
            or not isinstance(entry["watched"], bool)
            or not isinstance(entry["exemptions"], list)
            or not all(isinstance(value, str) for value in entry["exemptions"])
        ):
            raise RuntimeError("management resource catalog metadata is invalid")
        policy = entry["inventoryPolicy"]
        group, separator, _version = entry["apiVersion"].partition("/")
        resource = (
            f"{entry['plural']}.{group}" if separator else entry["plural"]
        )
        if policy == "block-any-instance":
            response = client.kubectl(
                "get", resource, "-A", "-o", "name", check=False
            )
            if response.returncode != 0:
                raise RuntimeError(
                    f"failed to inspect provider resource {resource}: "
                    f"{response.stderr}"
                )
            if response.stdout.strip():
                raise RuntimeError(
                    f"provider residue blocks activation: {response.stdout.strip()}"
                )
            continue
        if policy == "allocation-markers":
            document = client.json(
                "-n", "tenant-system", "get", resource
            )
            for item in document.get("items", []):
                metadata = item.get("metadata", {})
                if (
                    "tenancy.cnpg-vcluster.io/slot-id"
                    in metadata.get("labels", {})
                    or "tenancy.cnpg-vcluster.io/tenant"
                    in metadata.get("labels", {})
                    or metadata.get("annotations", {}).get(
                        "tenancy.cnpg-vcluster.io/resource"
                    )
                    == "allocation-lease"
                    or "tenancy.cnpg-vcluster.io/slot-id"
                    in metadata.get("annotations", {})
                    or "tenancy.cnpg-vcluster.io/tenant"
                    in metadata.get("annotations", {})
                    or "tenancy.cnpg-vcluster.io/tenant-uid"
                    in metadata.get("annotations", {})
                ):
                    raise RuntimeError(
                        "allocation Lease residue blocks activation"
                    )
            continue
        arguments = ["get", resource]
        if entry["namespaced"]:
            arguments.append("-A")
        document = client.json(*arguments)
        for item in document.get("items", []):
            metadata = item.get("metadata", {})
            annotations = metadata.get("annotations", {})
            owners = metadata.get("ownerReferences", [])
            marked = (
                "tenancy.cnpg-vcluster.io/tenant" in annotations
                or "tenancy.cnpg-vcluster.io/tenant-uid" in annotations
            )
            if policy == "tenant-markers-or-kamaji-owner":
                marked = marked or any(
                    owner.get("kind") == "KamajiControlPlane"
                    for owner in owners
                )
            if policy not in {
                "tenant-markers",
                "tenant-markers-or-kamaji-owner",
            }:
                raise RuntimeError(
                    "management resource catalog inventory policy is invalid"
                )
            if marked:
                raise RuntimeError(
                    f"{resource} residue blocks activation: "
                    f"{metadata.get('name', '<unknown>')}"
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
