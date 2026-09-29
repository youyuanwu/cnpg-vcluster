#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from contextlib import nullcontext
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.lib.config import load_configuration
from scripts.lib.controller_client import apply_tenant
from scripts.lib.controller_scenarios import delete_controller_tenant
from scripts.lib.locking import e2e_lock, tools_lock
from scripts.lib.redaction import redact
from scripts.lib.tenants import (
    clear_all_tenant_kubeconfigs,
    clear_tenant_kubeconfig,
)


def main(arguments: list[str]) -> int:
    if len(arguments) != 2 or arguments[0] not in {
        "apply",
        "delete",
        "clear-cache",
    }:
        print(
            "usage: controller_tenant.py "
            "<apply MANIFEST|delete TENANT|clear-cache TENANT|--all>",
            file=sys.stderr,
        )
        return 1
    with (
        (
            nullcontext()
            if os.environ.get("CAPI_E2E_CHILD") == "1"
            else e2e_lock(ROOT, exclusive=False)
        ),
        tools_lock(ROOT, exclusive=True),
    ):
        if arguments[0] == "clear-cache":
            removed = (
                clear_all_tenant_kubeconfigs(ROOT)
                if arguments[1] == "--all"
                else [arguments[1]]
                if clear_tenant_kubeconfig(ROOT, arguments[1])
                else []
            )
            print(
                "cleared Tenant kubeconfig cache"
                + (f": {', '.join(removed)}" if removed else ": none")
            )
            return 0
        config = load_configuration(ROOT)
        if arguments[0] == "apply":
            apply_tenant(ROOT, config, Path(arguments[1]).resolve())
        else:
            delete_controller_tenant(ROOT, config, arguments[1])
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except RuntimeError as exc:
        print(redact(str(exc)), file=sys.stderr)
        raise SystemExit(1)
