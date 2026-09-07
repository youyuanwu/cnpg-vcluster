#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.lib.config import load_configuration, parse_duration
from scripts.lib.host import resolve_host_just
from scripts.lib.process import run
from scripts.lib.tenants import storage_volume_name


def main() -> int:
    os.umask(0o077)
    config = load_configuration(ROOT)
    just = resolve_host_just(ROOT, config)
    tenant = SimpleNamespace(name=config["SPIKE_NAME"])
    volume = storage_volume_name(config, tenant)
    existing = run(["docker", "volume", "inspect", volume], timeout=30, check=False)
    if existing.returncode == 0:
        raise RuntimeError("storage negative fixture requires an absent volume")
    run(["docker", "volume", "create", "--label", "foreign=true", volume], timeout=30)
    try:
        result = run(
            [str(just), "--justfile", str(ROOT / "Justfile"), "test-storage"],
            timeout=parse_duration(config["COMMAND_TIMEOUT"]) * 4,
            cwd=ROOT,
            check=False,
        )
        if result.returncode == 0 or "storage volume" not in (
            result.stdout + result.stderr
        ):
            raise RuntimeError("unowned same-name volume did not fail closed")
        observed = run(
            ["docker", "volume", "inspect", volume, "--format", "{{.Name}}"],
            timeout=30,
        ).stdout.strip()
        if observed != volume:
            raise RuntimeError("unowned volume was changed")
    finally:
        run(["docker", "volume", "rm", volume], timeout=30)
        run(
            [str(just), "--justfile", str(ROOT / "Justfile"), "destroy"],
            timeout=parse_duration(config["COMMAND_TIMEOUT"]) * 3,
            cwd=ROOT,
            check=False,
        )
    print("storage ownership negative check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
