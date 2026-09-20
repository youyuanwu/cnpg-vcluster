#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.lib.config import load_configuration
from scripts.lib.controller import build_controller_binary, build_controller_image


def main(arguments: list[str]) -> int:
    config = load_configuration(ROOT)
    if arguments == ["--binary"]:
        print(build_controller_binary(ROOT, config))
        return 0
    if arguments:
        print("usage: build_controller.py [--binary]", file=sys.stderr)
        return 1
    print(build_controller_image(ROOT, config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
