#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import json
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.lib.config import load_configuration, parse_duration
from scripts.lib.host import resolve_host_just
from scripts.lib.files import write_private_file
from scripts.lib.process import run
from scripts.lib.tenants import storage_record_path, storage_volume_name


def main() -> int:
    os.umask(0o077)
    config = load_configuration(ROOT)
    just = resolve_host_just(ROOT, config)
    tenant = SimpleNamespace(name=config["SPIKE_NAME"])
    volume = storage_volume_name(config, tenant)
    existing = run(["docker", "volume", "inspect", volume], timeout=30, check=False)
    if existing.returncode == 0:
        raise RuntimeError("storage negative fixture requires an absent volume")
    try:
        run(["docker", "volume", "create", "--label", "foreign=true", volume], timeout=30)
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
        run(["docker", "volume", "rm", volume], timeout=30)

        created = run(
            [
                "docker",
                "volume",
                "create",
                "--label",
                f"{config['OWNERSHIP_LABEL']}={config['LAB_PREFIX']}",
                "--label",
                "cnpg-vcluster.capi/role=tenant-storage",
                "--label",
                f"cnpg-vcluster.capi/tenant={tenant.name}",
                volume,
            ],
            timeout=30,
        ).stdout.strip()
        payload = json.loads(
            run(["docker", "volume", "inspect", created], timeout=30).stdout
        )[0]
        missing_record = run(
            [str(just), "--justfile", str(ROOT / "Justfile"), "test-storage"],
            timeout=parse_duration(config["COMMAND_TIMEOUT"]) * 4,
            cwd=ROOT,
            check=False,
        )
        missing_output = missing_record.stdout + missing_record.stderr
        if missing_record.returncode == 0 or not any(
            message in missing_output
            for message in (
                "no identity record",
                "unproven tenant storage volume",
            )
        ):
            raise RuntimeError("existing labelled volume without record was adopted")
        record = storage_record_path(ROOT, tenant)
        write_private_file(
            record,
            json.dumps(
                {
                    "schema": 1,
                    "tenant": tenant.name,
                    "volumeName": volume,
                    "createdAt": "mismatched",
                    "mountpoint": payload["Mountpoint"],
                },
                sort_keys=True,
            )
            + "\n",
        )
        mismatch = run(
            [str(just), "--justfile", str(ROOT / "Justfile"), "test-storage"],
            timeout=parse_duration(config["COMMAND_TIMEOUT"]) * 4,
            cwd=ROOT,
            check=False,
        )
        mismatch_output = mismatch.stdout + mismatch.stderr
        if mismatch.returncode == 0 or not any(
            message in mismatch_output
            for message in ("identity record mismatch", "identity changed")
        ):
            raise RuntimeError("mismatched storage identity record was accepted")
        record.unlink()
        run(["docker", "volume", "rm", volume], timeout=30)
    finally:
        record = storage_record_path(ROOT, tenant)
        record.unlink(missing_ok=True)
        run(["docker", "volume", "rm", volume], timeout=30, check=False)
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
