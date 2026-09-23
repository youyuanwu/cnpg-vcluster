#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.create_management import create_management
from scripts.lib.config import load_configuration, parse_duration
from scripts.lib.controller_client import apply_tenant
from scripts.lib.controller_scenarios import (
    delete_controller_tenant,
    tenant_document,
    tenant_manifest,
)
from scripts.lib.kube import ManagementClient, wait_for
from scripts.lib.process import run


def _wait_ownership_invalid(
    root: Path,
    config: dict[str, str],
    name: str,
) -> dict[str, object]:
    client = ManagementClient(root, config)

    def invalid():
        document = tenant_document(client, name)
        if document is None:
            return None
        status = document.get("status")
        return document if (
            isinstance(status, dict)
            and status.get("phase") == "OwnershipInvalid"
        ) else None

    return wait_for(
        f"Tenant {name} ownership rejection",
        parse_duration(config["TENANT_CONTROL_PLANE_TIMEOUT"]),
        parse_duration(config["WAIT_POLL_INTERVAL"]),
        invalid,
    )


def foreign_namespace_rejected(
    root: Path,
    config: dict[str, str],
    client: ManagementClient,
) -> None:
    name = "tenant-example"
    client.kubectl(
        "create",
        "namespace",
        name,
    )
    uid = client.kubectl(
        "get",
        f"namespace/{name}",
        "-o",
        "jsonpath={.metadata.uid}",
    ).stdout
    try:
        apply_tenant(root, config, tenant_manifest(root, name))
        _wait_ownership_invalid(root, config, name)
        observed = client.kubectl(
            "get",
            f"namespace/{name}",
            "-o",
            "jsonpath={.metadata.uid}",
        ).stdout
        if observed != uid:
            raise RuntimeError("foreign namespace was replaced or adopted")
    finally:
        client.kubectl(
            "delete",
            f"namespace/{name}",
            "--ignore-not-found=true",
            "--wait=true",
        )
        delete_controller_tenant(root, config, name)


def foreign_volume_rejected(
    root: Path,
    config: dict[str, str],
) -> None:
    name = "tenant-example"
    volume = f"{config['LAB_PREFIX']}-{name}-storage"
    identifier = run(
        ["docker", "volume", "create", "--label", "foreign=true", volume],
        timeout=30,
    ).stdout.strip()
    try:
        apply_tenant(root, config, tenant_manifest(root, name))
        _wait_ownership_invalid(root, config, name)
        payload = json.loads(
            run(["docker", "volume", "inspect", volume], timeout=30).stdout
        )[0]
        if payload["Name"] != identifier or payload.get("Labels") != {"foreign": "true"}:
            raise RuntimeError("foreign volume was replaced or adopted")
    finally:
        run(["docker", "volume", "rm", volume], timeout=30, check=False)
        delete_controller_tenant(root, config, name)


def main() -> int:
    os.umask(0o077)
    config = load_configuration(ROOT)
    create_management(ROOT, config)
    client = ManagementClient(ROOT, config)
    delete_controller_tenant(ROOT, config, "tenant-example")
    foreign_namespace_rejected(ROOT, config, client)
    foreign_volume_rejected(ROOT, config)
    print("controller ownership negative checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
