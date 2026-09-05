#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
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


def assert_input_tamper_rejected(
    root: Path,
    config: dict[str, str],
    path: Path,
) -> None:
    original = path.read_bytes()
    original_mode = path.stat().st_mode & 0o777
    before = fingerprint(root, config)
    try:
        path.write_bytes(original + b"\ntampered\n")
        path.chmod(original_mode)
        rejected = run_just(root, config, "create-management", check=False)
        if rejected.returncode == 0 or "SHA-256 mismatch" not in (
            rejected.stdout + rejected.stderr
        ):
            raise RuntimeError(f"tampered management input was accepted: {path.name}")
        if fingerprint(root, config) != before:
            raise RuntimeError(f"tampered management input changed live state: {path.name}")
    finally:
        path.write_bytes(original)
        path.chmod(original_mode)


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
        restore_live = run_just(ROOT, config, "_restore-host", check=False)
        if restore_live.returncode == 0 or "management state exists" not in (
            restore_live.stdout + restore_live.stderr
        ):
            raise RuntimeError("host restoration was allowed with live management state")
        if not (ROOT / ".runtime" / "host" / "inotify.json").is_file():
            raise RuntimeError("refused host restoration removed its state record")
        run_just(ROOT, config, "create-management")
        if fingerprint(ROOT, config) != first:
            raise RuntimeError("repeated management creation changed stable identities")
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    lambda _: run_just(ROOT, config, "create-management"),
                    range(2),
                )
            )
        if any(result.returncode != 0 for result in results):
            raise RuntimeError("concurrent management reconciliation failed")
        if fingerprint(ROOT, config) != first:
            raise RuntimeError("concurrent management reconciliation changed identities")
        assert_input_tamper_rejected(
            ROOT,
            config,
            ROOT / ".tools" / "inputs" / f"cert-manager-{config['CERT_MANAGER_VERSION']}.tgz",
        )
        assert_input_tamper_rejected(
            ROOT,
            config,
            ROOT / ".tools" / "inputs" / "metallb-native.yaml",
        )
        broad_chart_files = [
            path
            for path in (ROOT / ".tools" / "charts" / "kamaji").rglob("*")
            if path.is_file() and path.stat().st_mode & 0o077
        ]
        if broad_chart_files:
            raise RuntimeError(f"Kamaji chart files are not owner-only: {broad_chart_files}")
        broad_chart_directories = [
            path
            for path in (ROOT / ".tools" / "charts" / "kamaji").rglob("*")
            if path.is_dir() and path.stat().st_mode & 0o077
        ]
        if broad_chart_directories:
            raise RuntimeError(
                f"Kamaji chart directories are not owner-only: {broad_chart_directories}"
            )

        if run_just(ROOT, config, "status", check=False).returncode != 0:
            raise RuntimeError("healthy management status was nonzero")
        before_observers = fingerprint(ROOT, config)
        run_just(ROOT, config, "diagnose", "management")
        if fingerprint(ROOT, config) != before_observers:
            raise RuntimeError("status or diagnostics mutated management state")

        client = ManagementClient(ROOT, config)
        deployment = json.loads(
            client.kubectl(
                "-n",
                config["KAMAJI_CAPI_NAMESPACE"],
                "get",
                "deployment/capi-kamaji-controller-manager",
                "-o",
                "json",
            ).stdout
        )
        arguments = deployment["spec"]["template"]["spec"]["containers"][0]["args"]
        gate_index = next(
            index
            for index, argument in enumerate(arguments)
            if argument.startswith("--feature-gates=")
        )
        drifted_gate = arguments[gate_index].replace(
            "ExternalClusterReference=false",
            "ExternalClusterReference=true",
        )
        client.kubectl(
            "-n",
            config["KAMAJI_CAPI_NAMESPACE"],
            "patch",
            "deployment/capi-kamaji-controller-manager",
            "--type=json",
            "-p",
            json.dumps(
                [
                    {
                        "op": "replace",
                        "path": f"/spec/template/spec/containers/0/args/{gate_index}",
                        "value": drifted_gate,
                    }
                ]
            ),
        )
        if run_just(ROOT, config, "status", check=False).returncode == 0:
            raise RuntimeError("status accepted drifted Kamaji feature gates")
        if (
            run_just(ROOT, config, "diagnose", "management", check=False).returncode
            == 0
        ):
            raise RuntimeError("diagnostics accepted drifted Kamaji feature gates")
        run_just(ROOT, config, "create-management")

        client.kubectl(
            "-n",
            config["CAPD_NAMESPACE"],
            "set",
            "image",
            "deployment/capd-controller-manager",
            f"manager={config['VERIFY_IMAGE']}",
        )
        if run_just(ROOT, config, "status", check=False).returncode == 0:
            raise RuntimeError("status accepted a drifted provider image")
        if (
            run_just(ROOT, config, "diagnose", "management", check=False).returncode
            == 0
        ):
            raise RuntimeError("diagnostics accepted a drifted provider image")
        run_just(ROOT, config, "create-management")

        client.kubectl(
            "-n",
            config["CAPD_NAMESPACE"],
            "scale",
            "deployment/capd-controller-manager",
            "--replicas=0",
        )
        if run_just(ROOT, config, "status", check=False).returncode == 0:
            raise RuntimeError("status accepted an unavailable required controller")
        if (
            run_just(ROOT, config, "diagnose", "management", check=False).returncode
            == 0
        ):
            raise RuntimeError("diagnostics accepted an unavailable required controller")
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
        client.kubectl(
            "-n",
            config["CAPD_NAMESPACE"],
            "delete",
            "deployment/capd-controller-manager",
            "--wait=true",
        )
        run_just(ROOT, config, "create-management")
        if run_just(ROOT, config, "status", check=False).returncode != 0:
            raise RuntimeError("management reconciliation did not recover a missing provider")

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

        host_record = ROOT / ".runtime" / "host" / "inotify.json"
        original_host_record = host_record.read_bytes()
        malformed = json.loads(original_host_record)
        malformed["lab_prefix"] = "foreign"
        host_record.write_text(json.dumps(malformed, sort_keys=True) + "\n", encoding="utf-8")
        host_record.chmod(0o600)
        try:
            refused = run_just(ROOT, config, "destroy", check=False)
            if refused.returncode == 0 or "belongs to another lab" not in (
                refused.stdout + refused.stderr
            ):
                raise RuntimeError("foreign host state did not block deletion")
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
                raise RuntimeError("foreign host-state refusal changed management identity")
        finally:
            host_record.write_bytes(original_host_record)
            host_record.chmod(0o600)

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
