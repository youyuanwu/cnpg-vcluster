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


def assert_tamper_rejected(
    root: Path,
    config: dict[str, str],
    path: Path,
) -> None:
    original = path.read_bytes()
    original_mode = path.stat().st_mode & 0o777
    before = management_status(root, config)
    try:
        path.write_bytes(original + b"\n# tampered\n")
        path.chmod(original_mode)
        try:
            run_preflight(root, config)
        except RuntimeError as exc:
            if "SHA-256 mismatch" not in str(exc):
                raise
        else:
            raise RuntimeError(f"tampered add-on input was accepted: {path}")
        if management_status(root, config) != before:
            raise RuntimeError("tampered add-on input changed management state")
    finally:
        path.write_bytes(original)
        path.chmod(original_mode)


def main() -> int:
    os.umask(0o077)
    config = load_configuration(ROOT)
    assert_tamper_rejected(
        ROOT,
        config,
        ROOT / "manifests" / "addons" / "kube-proxy.yaml.tpl",
    )
    assert_tamper_rejected(
        ROOT,
        config,
        ROOT / ".tools" / "inputs" / "calico.yaml",
    )
    print("network input tamper checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
