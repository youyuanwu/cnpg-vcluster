#!/usr/bin/env python3
from __future__ import annotations

import subprocess
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
    generate_controller(ROOT, load_configuration(ROOT))
    if verify:
        result = subprocess.run(
            [
                "git",
                "diff",
                "--exit-code",
                "--",
                "controller/api/v1alpha1/zz_generated.deepcopy.go",
                "controller/config/crd/bases",
                "controller/config/rbac/role.yaml",
            ],
            cwd=ROOT,
            check=False,
        )
        return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
