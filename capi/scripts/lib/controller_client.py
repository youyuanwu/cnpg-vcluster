from __future__ import annotations

from pathlib import Path

from scripts.lib.kube import ManagementClient


FIELD_MANAGER = "cnpg-vcluster-tenant-client"


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
