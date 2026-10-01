from __future__ import annotations

import json
import re
from pathlib import Path

from scripts.lib.kube import ManagementClient


CATALOG_CRD = "tenantdatabasecatalogs.tenancy.cnpg-vcluster.io"
LEGACY_CRD = "tenantdatabases.tenancy.cnpg-vcluster.io"


def _optional_crd(client: ManagementClient, name: str, output: str) -> str:
    result = client.kubectl(
        "get", f"crd/{name}", "--ignore-not-found=true", "-o", output,
        check=False,
    )
    if result.returncode != 0:
        if re.search(r"\bnot\s*found\b|\bnotfound\b", result.stderr, re.I):
            return ""
        raise RuntimeError(f"failed to inspect CRD {name}: {result.stderr}")
    return result.stdout.strip()


def require_absent_legacy_database_crd(client: ManagementClient) -> None:
    if _optional_crd(client, LEGACY_CRD, "name"):
        raise RuntimeError(
            f"Legacy TenantDatabase CRD {LEGACY_CRD} is installed; "
            "clean-install-only cutover is blocked before mutation. "
            "Do not delete the CRD automatically. Separately prove that every "
            "served version has no retained instances, that in-flight CREATE "
            "requests have terminal outcomes on every API server, and that "
            "legacy workloads, storage, and admission resources are safely "
            "retired before removing it through an operator-approved cleanup."
        )


def catalog_manifests(root: Path, *, azure: bool = False) -> tuple[Path, ...]:
    config = root / "database-controller" / "config"
    return (
        config / "crd" / "bases" / "tenancy.cnpg-vcluster.io_tenantdatabasecatalogs.yaml",
        *sorted(
            path for path in (config / "rbac").glob("*.yaml")
            if (azure or path.name != "controller-cluster-role-azure.yaml")
            and (not azure or path.name != "controller-cluster-role.yaml")
        ),
    )


def inspect_catalog_inventory(client: ManagementClient) -> None:
    observed = _optional_crd(client, CATALOG_CRD, "name")
    if not observed:
        return
    result = client.kubectl(
        "get", "--raw=/apis/tenancy.cnpg-vcluster.io/v1alpha1/tenantdatabasecatalogs",
        check=False,
    )
    try:
        listing = json.loads(result.stdout)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("TenantDatabaseCatalog inventory is invalid") from exc
    if (
        result.returncode != 0 or not isinstance(listing, dict)
        or listing.get("apiVersion") != "tenancy.cnpg-vcluster.io/v1alpha1"
        or listing.get("kind") != "TenantDatabaseCatalogList"
        or not isinstance(listing.get("metadata"), dict)
        or listing["metadata"].get("continue", "") != ""
        or not isinstance(listing.get("items"), list)
    ):
        raise RuntimeError("TenantDatabaseCatalog inventory is invalid")
    if listing["items"]:
        raise RuntimeError("retained TenantDatabaseCatalogs block Tenant API cutover")
