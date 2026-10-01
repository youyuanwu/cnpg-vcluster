#!/usr/bin/env python3
"""Run from capi with PYTHONPATH=. after preparing the verified local tool cache.

Use ``python3 -m scripts.azure_database_runtime --watch`` as an independently
supervised process on a host with access to the tenant control-plane network,
or ``--once`` for a bounded operator-triggered retry.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.lib.azure.common import _kubectl, load_azure_configuration
from scripts.lib.azure.foundation import install_tenant_database_runtime
from scripts.lib.config import ConfigError, load_configuration
from scripts.lib.locking import azure_lock, e2e_lock, tools_lock
from scripts.lib.redaction import redact


def reconcile_once(root: Path, config: dict[str, str]) -> bool:
    successful = True
    with e2e_lock(root, exclusive=False), azure_lock(
        root, exclusive=False, create=True,
    ), tools_lock(root, exclusive=False):
        result = json.loads(_kubectl(root, "get", "tenants", "-o", "json").stdout)
        if (
            not isinstance(result, dict)
            or result.get("kind") != "TenantList"
            or not isinstance(result.get("items"), list)
            or result.get("metadata", {}).get("continue", "") != ""
        ):
            raise RuntimeError("Azure database installer Tenant inventory is invalid")
        for tenant in result["items"]:
            metadata = tenant.get("metadata", {})
            if (
                tenant.get("status", {}).get("phase") != "Ready"
                or tenant.get("status", {}).get("provider", {}).get("type") != "azure"
                or metadata.get("deletionTimestamp")
            ):
                continue
            name = metadata.get("name")
            if not isinstance(name, str):
                raise RuntimeError("Azure database installer Tenant identity is invalid")
            try:
                install_tenant_database_runtime(root, config, name)
            except (RuntimeError, subprocess.SubprocessError) as exc:
                print(redact(f"Azure database installation {name}: {exc}"), file=sys.stderr)
                successful = False
    return successful


def main(arguments: list[str]) -> int:
    os.umask(0o077)
    if arguments not in (["--once"], ["--watch"]):
        raise RuntimeError("usage: python -m scripts.azure_database_runtime <--once|--watch>")
    azure_config = load_azure_configuration(ROOT)
    config = load_configuration(ROOT) | azure_config
    while True:
        try:
            successful = reconcile_once(ROOT, config)
        except (RuntimeError, subprocess.SubprocessError) as exc:
            if arguments == ["--once"]:
                raise
            print(redact(f"Azure database installer retrying: {exc}"), file=sys.stderr)
            successful = False
        if arguments == ["--once"]:
            return 0 if successful else 1
        time.sleep(300)


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except (ConfigError, RuntimeError, subprocess.SubprocessError) as exc:
        print(redact(str(exc)), file=sys.stderr)
        raise SystemExit(1)
