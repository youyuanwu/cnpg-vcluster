#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from contextlib import nullcontext
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.lib.config import load_configuration
from scripts.lib.controller_client import apply_tenant, delete_tenant
from scripts.lib.locking import e2e_lock, profile_lock, tools_lock
from scripts.lib.redaction import redact


def main(arguments: list[str]) -> int:
    if len(arguments) != 2 or arguments[0] not in {"apply", "delete"}:
        print(
            "usage: controller_tenant.py <apply MANIFEST|delete TENANT>",
            file=sys.stderr,
        )
        return 1
    config = load_configuration(ROOT)
    with (
        (
            nullcontext()
            if os.environ.get("CAPI_E2E_CHILD") == "1"
            else e2e_lock(ROOT, exclusive=False)
        ),
        profile_lock(ROOT, "local", exclusive=True, create=True),
        tools_lock(ROOT, exclusive=True),
    ):
        if arguments[0] == "apply":
            apply_tenant(ROOT, config, Path(arguments[1]).resolve())
        else:
            delete_tenant(ROOT, config, arguments[1])
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except RuntimeError as exc:
        print(redact(str(exc)), file=sys.stderr)
        raise SystemExit(1)
