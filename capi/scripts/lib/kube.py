from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from .config import parse_duration
from .process import run


T = TypeVar("T")


def wait_for(description: str, timeout: int, interval: int, predicate: Callable[[], T | None]) -> T:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(interval)
    raise RuntimeError(f"timed out waiting for {description}")


class ManagementClient:
    def __init__(self, root: Path, config: dict[str, str]):
        self.root = root
        self.config = config
        self.kubeconfig = root / ".runtime" / "management" / "kubeconfig"
        self.context = f"kind-{config['KIND_CLUSTER_NAME']}"
        self.timeout = parse_duration(config["COMMAND_TIMEOUT"])

    @property
    def kubectl_path(self) -> Path:
        return self.root / ".tools" / "bin" / "kubectl"

    @property
    def helm_path(self) -> Path:
        return self.root / ".tools" / "bin" / "helm"

    def kubectl(self, *arguments: str, check: bool = True):
        return run(
            [
                str(self.kubectl_path),
                "--kubeconfig",
                str(self.kubeconfig),
                "--context",
                self.context,
                "--request-timeout",
                self.config["KUBECTL_REQUEST_TIMEOUT"],
                *arguments,
            ],
            timeout=self.timeout,
            check=check,
        )

    def helm(self, *arguments: str, check: bool = True):
        return run(
            [
                str(self.helm_path),
                "--kubeconfig",
                str(self.kubeconfig),
                "--kube-context",
                self.context,
                *arguments,
            ],
            timeout=self.timeout,
            check=check,
        )

    def json(self, *arguments: str) -> object:
        return json.loads(self.kubectl(*arguments, "-o", "json").stdout)
