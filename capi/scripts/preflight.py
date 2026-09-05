from __future__ import annotations

import ipaddress
import json
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

from scripts.lib.config import parse_duration
from scripts.lib.files import verify_sha256
from scripts.lib.host import read_inotify, resolve_host_just
from scripts.lib.ownership import IdentityRecord, OwnershipError
from scripts.lib.process import CommandError, run
from scripts.tools import verify_all_inputs


class PreflightError(RuntimeError):
    pass


CIDR_KEYS = (
    "MANAGEMENT_POD_CIDR",
    "MANAGEMENT_SERVICE_CIDR",
    "TENANT_A_POD_CIDR",
    "TENANT_A_SERVICE_CIDR",
    "TENANT_B_POD_CIDR",
    "TENANT_B_SERVICE_CIDR",
    "SPIKE_POD_CIDR",
    "SPIKE_SERVICE_CIDR",
)


def configured_networks(config: dict[str, str]) -> dict[str, ipaddress.IPv4Network]:
    networks: dict[str, ipaddress.IPv4Network] = {}
    for key in CIDR_KEYS:
        network = ipaddress.ip_network(config[key])
        if not isinstance(network, ipaddress.IPv4Network):
            raise PreflightError(f"{key} must be IPv4")
        networks[key] = network
    items = list(networks.items())
    for index, (left_name, left) in enumerate(items):
        for right_name, right in items[index + 1 :]:
            if left.overlaps(right):
                raise PreflightError(f"{left_name} {left} overlaps {right_name} {right}")
    return networks


def docker_networks(timeout: int) -> dict[str, ipaddress.IPv4Network]:
    identifiers = run(["docker", "network", "ls", "-q"], timeout=timeout).stdout.split()
    if not identifiers:
        return {}
    payload = json.loads(run(["docker", "network", "inspect", *identifiers], timeout=timeout).stdout)
    networks: dict[str, ipaddress.IPv4Network] = {}
    for item in payload:
        for entry in item.get("IPAM", {}).get("Config", []) or []:
            subnet = entry.get("Subnet")
            if not subnet or ":" in subnet:
                continue
            networks[f"{item['Name']}:{subnet}"] = ipaddress.ip_network(subnet)
    return networks


def verify_no_network_overlap(config: dict[str, str], timeout: int) -> None:
    desired = configured_networks(config)
    existing = docker_networks(timeout)
    for desired_name, desired_network in desired.items():
        for existing_name, existing_network in existing.items():
            if desired_network.overlaps(existing_network):
                raise PreflightError(
                    f"{desired_name} {desired_network} overlaps Docker network "
                    f"{existing_name}"
                )


def _tool_version(path: Path, command: list[str], expected: str, timeout: int) -> None:
    output = run([str(path), *command], timeout=timeout).stdout.strip()
    if expected not in output:
        raise PreflightError(f"{path.name} version mismatch: expected {expected}, got {output!r}")


def verify_tools(root: Path, config: dict[str, str], timeout: int) -> None:
    resolve_host_just(root, config)
    bin_dir = root / ".tools" / "bin"
    verify_sha256(bin_dir / "kind", config["KIND_SHA256"])
    verify_sha256(bin_dir / "kubectl", config["KUBECTL_SHA256"])
    verify_sha256(bin_dir / "clusterctl", config["CLUSTERCTL_SHA256"])
    verify_sha256(bin_dir / "helm", config["HELM_BINARY_SHA256"])
    _tool_version(bin_dir / "kind", ["version"], config["KIND_VERSION"], timeout)
    kubectl = json.loads(
        run(
            [str(bin_dir / "kubectl"), "version", "--client", "-o", "json"],
            timeout=timeout,
        ).stdout
    )
    if kubectl["clientVersion"]["gitVersion"] != config["KUBECTL_VERSION"]:
        raise PreflightError("kubectl version mismatch")
    _tool_version(bin_dir / "helm", ["version", "--short"], config["HELM_VERSION"], timeout)
    _tool_version(
        bin_dir / "clusterctl",
        ["version", "-o", "short"],
        config["CAPI_VERSION"],
        timeout,
    )


def verify_docker(config: dict[str, str], timeout: int) -> None:
    if sys.platform != "linux":
        raise PreflightError("the local CAPI experiment requires Linux")
    if shutil.which("docker") is None:
        raise PreflightError("docker is required")
    info = json.loads(run(["docker", "info", "--format", "{{json .}}"], timeout=timeout).stdout)
    if str(info.get("CgroupVersion")) != "2":
        raise PreflightError("Docker must use cgroup v2")
    security_options = " ".join(info.get("SecurityOptions") or [])
    if "rootless" in security_options.lower():
        raise PreflightError("rootless Docker is not supported")
    if int(info.get("NCPU", 0)) < int(config["MIN_DOCKER_CPUS"]):
        raise PreflightError("Docker CPU capacity is below the configured floor")
    memory_gib = int(info.get("MemTotal", 0)) / (1024**3)
    if memory_gib < float(config["MIN_DOCKER_MEMORY_GIB"]):
        raise PreflightError("Docker memory capacity is below the configured floor")
    docker_root = Path(info["DockerRootDir"])
    free_gib = shutil.disk_usage(docker_root).free / (1024**3)
    if free_gib < float(config["MIN_DOCKER_STORAGE_GIB"]):
        raise PreflightError("Docker storage capacity is below the configured floor")


def verify_management_name(root: Path, config: dict[str, str], timeout: int) -> None:
    container_name = f"{config['KIND_CLUSTER_NAME']}-control-plane"
    identifiers = run(
        ["docker", "ps", "-aq", "--filter", f"name=^/{container_name}$"],
        timeout=timeout,
    ).stdout.split()
    if not identifiers:
        return
    if len(identifiers) != 1:
        raise PreflightError(f"multiple containers use reserved name {container_name}")
    record_path = root / ".runtime" / "management" / "identity.json"
    if not record_path.exists():
        raise PreflightError(
            f"reserved management container {container_name} exists without ownership record"
        )
    inspect = json.loads(
        run(["docker", "inspect", identifiers[0]], timeout=timeout).stdout
    )[0]
    labels = inspect.get("Config", {}).get("Labels") or {}
    observed = IdentityRecord(
        kind="container",
        name=container_name,
        identifier=inspect["Id"],
        labels={
            config["OWNERSHIP_LABEL"]: labels.get(config["OWNERSHIP_LABEL"], ""),
            "io.x-k8s.kind.cluster": labels.get("io.x-k8s.kind.cluster", ""),
        },
    )
    try:
        IdentityRecord.load(record_path).require_exact(observed)
    except OwnershipError as exc:
        raise PreflightError(str(exc)) from exc


def verify_inotify(config: dict[str, str]) -> None:
    checks = {
        "max_user_instances": int(config["MIN_INOTIFY_INSTANCES"]),
        "max_user_watches": int(config["MIN_INOTIFY_WATCHES"]),
    }
    for name, floor in checks.items():
        current = read_inotify(name)
        if current < floor:
            raise PreflightError(f"fs.inotify.{name}={current} is below required {floor}")


def verify_images(config: dict[str, str], timeout: int) -> None:
    image_keys = sorted(
        key
        for key, value in config.items()
        if key.endswith("_IMAGE") and "@sha256:" in value
    )
    for key in image_keys:
        tagged_key = f"{key}_TAGGED"
        if tagged_key not in config:
            raise PreflightError(f"{key} is missing provenance key {tagged_key}")
        expected = config[key].rsplit("@", 1)[1]
        result = run(
            [
                "docker",
                "buildx",
                "imagetools",
                "inspect",
                config[tagged_key],
                "--format",
                "{{json .Manifest}}",
            ],
            timeout=timeout,
        )
        actual = json.loads(result.stdout)["digest"]
        if actual != expected:
            raise PreflightError(
                f"{tagged_key} resolved to {actual}, expected {expected}"
            )


def verify_privileged_probe(config: dict[str, str]) -> None:
    timeout = parse_duration(config["PREFLIGHT_PROBE_TIMEOUT"])
    name = f"{config['LAB_PREFIX']}-preflight-{uuid.uuid4().hex[:12]}"
    result = None
    with tempfile.TemporaryDirectory(prefix=f"{name}-") as temporary:
        cid_file = Path(temporary) / "container.cid"
        try:
            result = run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--cidfile",
                    str(cid_file),
                    "--name",
                    name,
                    "--label",
                    f"{config['OWNERSHIP_LABEL']}=true",
                    "--label",
                    "cnpg-vcluster.capi/role=probe",
                    "--privileged",
                    "--cgroupns=private",
                    "--network=none",
                    "--entrypoint",
                    "sh",
                    config["VERIFY_IMAGE"],
                    "-ec",
                    "test -r /proc/1/status && test -d /sys/fs/cgroup",
                ],
                timeout=timeout,
                check=False,
            )
        except CommandError as exc:
            raise PreflightError(f"privileged container probe failed: {exc}") from exc
        finally:
            identifier = (
                cid_file.read_text(encoding="utf-8").strip()
                if cid_file.is_file()
                else ""
            )
            candidate = identifier or name
            inspect = run(
                ["docker", "inspect", candidate],
                timeout=30,
                check=False,
            )
            if inspect.returncode == 0:
                payload = json.loads(inspect.stdout)
                if len(payload) != 1:
                    raise PreflightError(f"unexpected probe inventory for {name}")
                container = payload[0]
                labels = container.get("Config", {}).get("Labels") or {}
                if (
                    container.get("Name") != f"/{name}"
                    or labels.get(config["OWNERSHIP_LABEL"]) != "true"
                    or labels.get("cnpg-vcluster.capi/role") != "probe"
                ):
                    raise PreflightError(
                        f"refusing to remove unowned probe candidate {candidate}"
                    )
                run(
                    ["docker", "rm", "-f", container["Id"]],
                    timeout=30,
                )
            remaining = run(
                ["docker", "ps", "-aq", "--filter", f"name=^/{name}$"],
                timeout=30,
            ).stdout.strip()
            if remaining:
                raise PreflightError(f"privileged probe cleanup failed for {name}")
    if result is None or result.returncode != 0:
        raise PreflightError("privileged container probe failed")


def run_preflight(root: Path, config: dict[str, str]) -> None:
    timeout = parse_duration(config["COMMAND_TIMEOUT"])
    verify_all_inputs(root, config)
    verify_tools(root, config, timeout)
    verify_docker(config, timeout)
    verify_management_name(root, config, timeout)
    verify_inotify(config)
    verify_no_network_overlap(config, timeout)
    verify_images(config, timeout)
    verify_privileged_probe(config)
    print("preflight passed")
