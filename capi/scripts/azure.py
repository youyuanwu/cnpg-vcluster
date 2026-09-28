#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.lib.azure.common import load_azure_configuration
from scripts.lib.azure.foundation import (
    create_foundation,
    create_management,
    destroy,
    foundation_status,
    preflight,
)
from scripts.lib.config import ConfigError
from scripts.lib.locking import azure_lock, azure_lock_exists, e2e_lock, tools_lock
from scripts.lib.redaction import redact


def _run_profile_mutation(root: Path, config: Mapping[str, str], mutation):
    with e2e_lock(root, exclusive=False):
        with azure_lock(
            root,
            exclusive=True,
            create=True,
        ) as acquired:
            if not acquired:
                raise RuntimeError("Azure profile mutation lock is unavailable")
            with tools_lock(root, exclusive=True):
                return mutation(root, config)


def _run_profile_status(root: Path, config: Mapping[str, str]) -> int:
    if not azure_lock_exists(root):
        return foundation_status(root, config)
    with e2e_lock(root, exclusive=False, create=False) as e2e_acquired:
        if not e2e_acquired:
            raise RuntimeError("Azure E2E status lock is missing")
        with azure_lock(
            root,
            exclusive=False,
            create=False,
        ) as acquired:
            if not acquired:
                raise RuntimeError("Azure profile status lock disappeared")
            with tools_lock(root, exclusive=False, create=False) as tools_acquired:
                if not tools_acquired:
                    raise RuntimeError("Azure tools status lock is missing")
                return foundation_status(root, config)


def main(arguments: list[str]) -> int:
    os.umask(0o077)
    config = load_azure_configuration(ROOT)
    if not arguments:
        raise RuntimeError(
            "usage: azure.py "
            "<preflight|create-foundation|create-management|foundation-status|destroy>"
        )
    command = arguments[0]
    if command == "preflight":
        preflight(ROOT, config)
    elif command == "foundation-status":
        return _run_profile_status(ROOT, config)
    else:
        mutations = {
            "create-foundation": create_foundation,
            "create-management": create_management,
            "destroy": destroy,
        }
        try:
            mutation = mutations[command]
        except KeyError as exc:
            raise RuntimeError(f"unknown Azure command: {command}") from exc
        _run_profile_mutation(ROOT, config, mutation)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except (ConfigError, RuntimeError, subprocess.SubprocessError) as exc:
        print(redact(str(exc)), file=sys.stderr)
        raise SystemExit(1)
