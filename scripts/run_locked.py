#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.lib.locking import e2e_lock


def main(arguments: list[str]) -> int:
    if not arguments:
        print("usage: run_locked.py <script> [arguments...]", file=sys.stderr)
        return 1
    command = [sys.executable, *arguments]
    if os.environ.get("CAPI_E2E_CHILD") == "1":
        return subprocess.run(command, cwd=ROOT, check=False).returncode
    with e2e_lock(ROOT, exclusive=False):
        return subprocess.run(command, cwd=ROOT, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
