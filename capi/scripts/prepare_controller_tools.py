#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from contextlib import nullcontext
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.lib.config import load_configuration, parse_duration
from scripts.lib.locking import e2e_lock, tools_lock
from scripts.tools import _ensure_download, _install_envtest, _install_go


def prepare_controller_tools(root: Path, config: dict[str, str]) -> None:
    with (
        (
            nullcontext()
            if os.environ.get("CAPI_E2E_CHILD") == "1"
            else e2e_lock(root, exclusive=False)
        ),
        tools_lock(root, exclusive=True),
    ):
        inputs = root / ".tools" / "controller-inputs"
        timeout = parse_duration(config["DOWNLOAD_TIMEOUT"])
        go = _ensure_download(
            inputs, "go-linux-amd64.tar.gz",
            config["GO_URL"], config["GO_SHA256"], timeout,
        )
        envtest = _ensure_download(
            inputs, "envtest-linux-amd64.tar.gz",
            config["ENVTEST_URL"], config["ENVTEST_SHA256"], timeout,
        )
        _install_go(root, go, root / ".tools" / "bin")
        _install_envtest(root, envtest)
    print("installed checksum-verified Go and envtest tools (no OCI acquisition)")


if __name__ == "__main__":
    prepare_controller_tools(ROOT, load_configuration(ROOT))
