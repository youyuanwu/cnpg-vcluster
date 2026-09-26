#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.lib.config import load_configuration
from scripts.lib.controller import generate_controller


def main(arguments: list[str]) -> int:
    verify = arguments == ["--verify"]
    if arguments and not verify:
        print("usage: generate_controller.py [--verify]", file=sys.stderr)
        return 1
    generate_controller(ROOT, load_configuration(ROOT), check=verify)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
