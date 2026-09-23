#!/usr/bin/env python3
from __future__ import annotations

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


def main() -> int:
    os.umask(0o077)
    config = load_configuration(ROOT)
    name = "tenant-c"
    volume = f"{config['LAB_PREFIX']}-{name}-storage"
    create_management(ROOT, config)
    delete_controller_tenant(ROOT, config, name)
    run(["docker", "volume", "create", "--label", "foreign=true", volume], timeout=30)
    try:
        apply_tenant(ROOT, config, tenant_manifest(ROOT, name))
        client = ManagementClient(ROOT, config)

        def rejected():
            document = tenant_document(client, name)
            if document is None:
                return None
            status = document.get("status")
            return True if (
                isinstance(status, dict)
                and status.get("phase") == "OwnershipInvalid"
            ) else None

        wait_for(
            "foreign tenant storage rejection",
            parse_duration(config["TENANT_CONTROL_PLANE_TIMEOUT"]),
            parse_duration(config["WAIT_POLL_INTERVAL"]),
            rejected,
        )
        labels = run(
            [
                "docker",
                "volume",
                "inspect",
                volume,
                "--format",
                "{{json .Labels}}",
            ],
            timeout=30,
        ).stdout.strip()
        if labels != '{"foreign":"true"}':
            raise RuntimeError("foreign tenant storage volume was changed")
    finally:
        run(["docker", "volume", "rm", volume], timeout=30, check=False)
        delete_controller_tenant(ROOT, config, name)
    print("storage ownership negative check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
