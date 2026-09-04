#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.lib.config import ConfigError, load_configuration
from scripts.lib.host import HostError, prepare_inotify, restore_inotify
from scripts.lib.locking import tools_lock
from scripts.lib.redaction import redact
from scripts.preflight import PreflightError, run_preflight
from scripts.tools import prepare_tools


def unavailable(arguments: list[str]) -> int:
    operation = " ".join(arguments) if arguments else "unknown"
    print(f"{operation} is not available until its implementation phase", file=sys.stderr)
    return 1


def main(arguments: list[str]) -> int:
    os.umask(0o077)
    if not arguments:
        print("usage: lab.py <command>", file=sys.stderr)
        return 1
    config = load_configuration(ROOT)
    command, rest = arguments[0], arguments[1:]
    if command == "tools":
        with tools_lock(ROOT, exclusive=True):
            prepare_tools(ROOT, config)
        return 0
    if command == "prepare-host":
        prepare_inotify(ROOT, config)
        return 0
    if command == "restore-host":
        restore_inotify(ROOT, config)
        return 0
    if command == "preflight":
        with tools_lock(ROOT, exclusive=False):
            run_preflight(ROOT, config)
        return 0
    if command == "unavailable":
        return unavailable(rest)
    print(f"unknown command: {command}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except (ConfigError, HostError, PreflightError, RuntimeError) as exc:
        print(redact(str(exc)), file=sys.stderr)
        raise SystemExit(1)
