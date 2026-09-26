from __future__ import annotations

import json
from pathlib import Path

from scripts.lib.kube import ManagementClient


FIELD_MANAGER = "cnpg-vcluster-tenant-client"


def tenant_manifest_document(
    config: dict[str, str], name: str, *, workers: int = 1, databases: int = 1,
) -> dict[str, object]:
    return {
        "apiVersion": "tenancy.cnpg-vcluster.io/v1alpha2",
        "kind": "Tenant",
        "metadata": {"name": name},
        "spec": {
            "kubernetesVersion": config["KUBERNETES_VERSION"].removeprefix("v"),
            "workers": workers,
            "databases": databases,
        },
    }


def apply_tenant_document(client: ManagementClient, document: dict[str, object]) -> None:
    client.kubectl(
        "apply", "--server-side", "--validate=strict",
        f"--field-manager={FIELD_MANAGER}", "-f", "-",
        input_text=json.dumps(document),
    )


def apply_tenant(
    root: Path,
    config: dict[str, str],
    manifest: Path,
) -> None:
    if not manifest.is_file():
        raise RuntimeError(f"Tenant manifest does not exist: {manifest}")
    client = ManagementClient(root, config)
    client.kubectl(
        "apply",
        "--server-side",
        "--validate=strict",
        f"--field-manager={FIELD_MANAGER}",
        "-f",
        str(manifest),
    )


def delete_tenant(
    root: Path,
    config: dict[str, str],
    tenant_name: str,
    *,
    wait: bool = True,
) -> None:
    client = ManagementClient(root, config)
    client.kubectl(
        "delete",
        f"tenant/{tenant_name}",
        "--ignore-not-found=true",
        f"--wait={'true' if wait else 'false'}",
        f"--timeout={config['DELETE_TIMEOUT']}",
    )
