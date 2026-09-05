#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.lib.config import load_configuration, parse_duration
from scripts.lib.host import read_inotify, resolve_host_just
from scripts.lib.kube import ManagementClient
from scripts.lib.process import run


def run_just(root: Path, config: dict[str, str], *arguments: str, check: bool = True):
    just = resolve_host_just(root, config)
    return run(
        [str(just), "--justfile", str(root / "Justfile"), *arguments],
        timeout=parse_duration(config["COMMAND_TIMEOUT"]) * 3,
        cwd=root,
        check=check,
    )


def fingerprint(root: Path, config: dict[str, str]) -> str:
    client = ManagementClient(root, config)
    resources = client.kubectl(
        "get",
        "deployments,statefulsets,daemonsets",
        "-A",
        "-o",
        "json",
    ).stdout
    payload = json.loads(resources)
    stable = sorted(
        (
            item["kind"],
            item["metadata"]["namespace"],
            item["metadata"]["name"],
            item["metadata"]["uid"],
            item["metadata"].get("generation"),
            tuple(
                container["image"]
                for container in item["spec"]["template"]["spec"].get("containers", [])
            ),
        )
        for item in payload["items"]
    )
    runtime = []
    for path in sorted((root / ".runtime").rglob("*")):
        if path.is_file() and path.name != ".lock":
            runtime.append(
                (
                    path.relative_to(root / ".runtime").as_posix(),
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                    path.stat().st_mode & 0o777,
                )
            )
    container = run(
        [
            "docker",
            "inspect",
            f"{config['KIND_CLUSTER_NAME']}-control-plane",
            "--format",
            "{{.Id}}",
        ],
        timeout=30,
    ).stdout.strip()
    return hashlib.sha256(
        json.dumps(
            {"container": container, "resources": stable, "runtime": runtime},
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def main() -> int:
    os.umask(0o077)
    config = load_configuration(ROOT)
    original_inotify = {
        "max_user_instances": read_inotify("max_user_instances"),
        "max_user_watches": read_inotify("max_user_watches"),
    }
    suffix = uuid.uuid4().hex[:10]
    sentinel_container = f"capi-management-sentinel-{suffix}"
    sentinel_volume = f"capi-management-sentinel-{suffix}"
    unexpected = ROOT / ".runtime" / "foreign"
    sentinel_id = ""
    try:
        run_just(ROOT, config, "destroy")

        run(["docker", "volume", "create", sentinel_volume], timeout=30)
        sentinel_id = run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                sentinel_container,
                "--label",
                "cnpg-vcluster.capi/sentinel=true",
                "-v",
                f"{sentinel_volume}:/sentinel",
                config["VERIFY_IMAGE"],
                "sleep",
                "3600",
            ],
            timeout=60,
        ).stdout.strip()

        reserved_name = f"{config['KIND_CLUSTER_NAME']}-control-plane"
        collision_id = run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                reserved_name,
                "--label",
                "foreign=true",
                config["VERIFY_IMAGE"],
                "sleep",
                "300",
            ],
            timeout=60,
        ).stdout.strip()
        try:
            collision = run_just(ROOT, config, "create-management", check=False)
            if collision.returncode == 0 or "without ownership record" not in (
                collision.stdout + collision.stderr
            ):
                raise RuntimeError("reserved management collision did not fail closed")
            observed = run(
                ["docker", "inspect", reserved_name, "--format", "{{.Id}}"],
                timeout=30,
            ).stdout.strip()
            if observed != collision_id:
                raise RuntimeError("collision fixture was changed")
        finally:
            run(["docker", "rm", "-f", collision_id], timeout=30, check=False)

        run_just(ROOT, config, "prepare-host")
        run_just(ROOT, config, "preflight")
        run_just(ROOT, config, "create-management")
        first = fingerprint(ROOT, config)
        run_just(ROOT, config, "create-management")
        if fingerprint(ROOT, config) != first:
            raise RuntimeError("repeated management creation changed stable identities")

        if run_just(ROOT, config, "status", check=False).returncode != 0:
            raise RuntimeError("healthy management status was nonzero")
        before_observers = fingerprint(ROOT, config)
        run_just(ROOT, config, "diagnose", "management")
        if fingerprint(ROOT, config) != before_observers:
            raise RuntimeError("status or diagnostics mutated management state")

        client = ManagementClient(ROOT, config)
        client.kubectl(
            "-n",
            config["CAPD_NAMESPACE"],
            "scale",
            "deployment/capd-controller-manager",
            "--replicas=0",
        )
        if run_just(ROOT, config, "status", check=False).returncode == 0:
            raise RuntimeError("status accepted an unavailable required controller")
        client.kubectl(
            "-n",
            config["CAPD_NAMESPACE"],
            "scale",
            "deployment/capd-controller-manager",
            "--replicas=1",
        )
        client.kubectl(
            "-n",
            config["CAPD_NAMESPACE"],
            "rollout",
            "status",
            "deployment/capd-controller-manager",
            f"--timeout={config['PROVIDER_TIMEOUT']}",
        )

        unexpected.write_text("foreign\n", encoding="utf-8")
        unexpected.chmod(0o600)
        management_id = run(
            [
                "docker",
                "inspect",
                f"{config['KIND_CLUSTER_NAME']}-control-plane",
                "--format",
                "{{.Id}}",
            ],
            timeout=30,
        ).stdout.strip()
        refused = run_just(ROOT, config, "destroy", check=False)
        if refused.returncode == 0 or "unexpected runtime file" not in (
            refused.stdout + refused.stderr
        ):
            raise RuntimeError("unexpected runtime identity did not block deletion")
        if (
            run(
                [
                    "docker",
                    "inspect",
                    f"{config['KIND_CLUSTER_NAME']}-control-plane",
                    "--format",
                    "{{.Id}}",
                ],
                timeout=30,
            ).stdout.strip()
            != management_id
        ):
            raise RuntimeError("refused deletion changed management identity")
        unexpected.unlink()

        run_just(ROOT, config, "destroy")
        run_just(ROOT, config, "destroy")
        run(["docker", "inspect", sentinel_id], timeout=30)
        run(["docker", "volume", "inspect", sentinel_volume], timeout=30)
        for name, original in original_inotify.items():
            if read_inotify(name) != original:
                raise RuntimeError(f"host inotify {name} was not restored")
        if (ROOT / ".runtime").exists():
            raise RuntimeError("runtime directory remained after management lifecycle")
        print("management lifecycle checks passed")
        return 0
    finally:
        unexpected.unlink(missing_ok=True)
        run_just(ROOT, config, "destroy", check=False)
        if sentinel_id:
            run(["docker", "rm", "-f", sentinel_id], timeout=30, check=False)
        run(["docker", "volume", "rm", sentinel_volume], timeout=30, check=False)


if __name__ == "__main__":
    raise SystemExit(main())
