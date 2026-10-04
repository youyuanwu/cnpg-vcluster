from __future__ import annotations

import base64
import json
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from .config import parse_duration
from .process import run


T = TypeVar("T")


def kubeconfig_json_request(
    config_response: subprocess.CompletedProcess[str],
    method: str,
    path: str,
    payload: dict[str, object],
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    if config_response.returncode != 0:
        return config_response
    try:
        config = json.loads(config_response.stdout)
        cluster = config["clusters"][0]["cluster"]
        user = config["users"][0]["user"]
        server = cluster["server"].rstrip("/")
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        return subprocess.CompletedProcess(
            ["kubernetes-json-request"],
            1,
            "",
            f"management kubeconfig is invalid: {exc}",
        )
    headers = {"Content-Type": "application/json"}
    token = user.get("token")
    if isinstance(token, str) and token:
        headers["Authorization"] = "Bearer" + " " + token
    request = urllib.request.Request(
        f"{server}/{path.lstrip('/')}",
        data=json.dumps(payload, separators=(",", ":")).encode(),
        headers=headers,
        method=method,
    )
    try:
        with tempfile.TemporaryDirectory() as directory:
            context = ssl.create_default_context()
            ca_data = cluster.get("certificate-authority-data")
            if isinstance(ca_data, str) and ca_data:
                context.load_verify_locations(
                    cadata=base64.b64decode(ca_data).decode()
                )
            cert_data = user.get("client-certificate-data")
            key_data = user.get("client-key-data")
            if isinstance(cert_data, str) and isinstance(key_data, str):
                cert = Path(directory) / "client.crt"
                key = Path(directory) / "client.key"
                cert.write_bytes(base64.b64decode(cert_data))
                key.write_bytes(base64.b64decode(key_data))
                key.chmod(0o600)
                context.load_cert_chain(cert, key)
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}),
                urllib.request.HTTPSHandler(context=context),
            )
            with opener.open(request, timeout=timeout) as response:
                return subprocess.CompletedProcess(
                    ["kubernetes-json-request"],
                    0,
                    response.read().decode(errors="replace"),
                    "",
                )
    except urllib.error.HTTPError as exc:
        return subprocess.CompletedProcess(
            ["kubernetes-json-request"],
            1,
            exc.read().decode(errors="replace"),
            f"HTTP {exc.code}: {exc.reason}",
        )
    except (OSError, ValueError, urllib.error.URLError) as exc:
        return subprocess.CompletedProcess(
            ["kubernetes-json-request"],
            1,
            "",
            f"Kubernetes JSON request failed: {exc}",
        )


def wait_for(
    description: str,
    timeout: int,
    interval: int,
    predicate: Callable[[], T | None],
) -> T:
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

    def kubectl(
        self,
        *arguments: str,
        check: bool = True,
        input_text: str | None = None,
        timeout: int | None = None,
    ):
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
            timeout=self.timeout if timeout is None else timeout,
            check=check,
            input_text=input_text,
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

    def request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, object],
    ) -> subprocess.CompletedProcess[str]:
        config_response = self.kubectl(
            "config",
            "view",
            "--raw",
            "--flatten",
            "--minify",
            "-o",
            "json",
            check=False,
        )
        return kubeconfig_json_request(
            config_response,
            method,
            path,
            payload,
            self.timeout,
        )
