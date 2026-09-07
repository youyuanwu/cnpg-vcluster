#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.lib.config import load_configuration
from scripts.lib.management import management_status
from scripts.preflight import run_preflight


def reject(path: Path, config: dict[str, str]) -> None:
    original = path.read_bytes()
    mode = path.stat().st_mode & 0o777
    before = management_status(ROOT, config)
    try:
        path.write_bytes(original + b"\n# tampered\n")
        path.chmod(mode)
        try:
            run_preflight(ROOT, config)
        except RuntimeError as exc:
            if "SHA-256 mismatch" not in str(exc):
                raise
        else:
            raise RuntimeError(f"tampered CNPG input was accepted: {path}")
        if management_status(ROOT, config) != before:
            raise RuntimeError("CNPG tamper check changed management state")
    finally:
        path.write_bytes(original)
        path.chmod(mode)


def main() -> int:
    os.umask(0o077)
    config = load_configuration(ROOT)
    reject(ROOT / ".tools" / "inputs" / "cnpg.yaml", config)
    reject(ROOT / "manifests" / "cnpg" / "cluster.yaml.tpl", config)
    reject(ROOT / "manifests" / "cnpg" / "static-pvs.yaml.tpl", config)
    print("CNPG input tamper checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
