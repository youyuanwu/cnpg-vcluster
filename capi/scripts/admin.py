#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.lib.admin import (
    build_admin,
    build_admin_image,
    fetch_admin_dependencies,
    generate_admin_resources,
    test_admin,
    verify_reproducible_admin_build,
    vet_admin,
)
from scripts.lib.config import load_configuration
from scripts.lib.admin_local import admin_port_forward, collect_local_admin_status


def main(arguments: list[str]) -> int:
    config = load_configuration(ROOT)
    if arguments == ["fetch"]:
        fetch_admin_dependencies(ROOT, config)
    elif arguments == ["generate-check"]:
        generate_admin_resources(root=ROOT, check=True)
    elif arguments == ["test"]:
        test_admin(ROOT, config)
    elif arguments == ["lint"]:
        vet_admin(ROOT, config)
    elif arguments == ["build"]:
        server, web = build_admin(ROOT, config)
        print(server)
        print(web)
    elif arguments == ["package-check"]:
        server, web = verify_reproducible_admin_build(ROOT, config)
        print(server)
        print(web)
    elif arguments == ["image"]:
        print(build_admin_image(ROOT, config))
    elif arguments == ["status"]:
        print(
            json.dumps(
                collect_local_admin_status(ROOT, config),
                indent=2,
                sort_keys=True,
            )
        )
    elif arguments == ["port-forward"]:
        return admin_port_forward(ROOT, config)
    else:
        print(
            "usage: admin.py "
            "{fetch|generate-check|test|lint|build|package-check|image|status|"
            "port-forward}",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
