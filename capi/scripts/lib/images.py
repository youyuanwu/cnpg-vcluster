from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import re

from scripts.cache import (
    active_generation,
    canonical_tagged,
    canonical_exact_reference,
    restore_host_image,
    runtime_digest_reference,
)
from scripts.lib.config import parse_duration
from scripts.lib.files import ensure_private_dir, write_private_file
from scripts.lib.kube import wait_for
from scripts.lib.process import run
from scripts.lib.redaction import redact


MANAGEMENT_IMAGE_KEYS = (
    "CAPI_CORE_IMAGE",
    "CAPI_BOOTSTRAP_IMAGE",
    "CAPD_IMAGE",
    "KAMAJI_CAPI_IMAGE",
    "CERT_MANAGER_CONTROLLER_IMAGE",
    "CERT_MANAGER_CAINJECTOR_IMAGE",
    "CERT_MANAGER_WEBHOOK_IMAGE",
    "CERT_MANAGER_STARTUPAPICHECK_IMAGE",
    "METALLB_CONTROLLER_IMAGE",
    "METALLB_SPEAKER_IMAGE",
    "KAMAJI_IMAGE",
    "KAMAJI_ETCD_IMAGE",
    "KAMAJI_ETCD_JOB_IMAGE",
    "KAMAJI_KUBECTL_JOB_IMAGE",
    "KONNECTIVITY_SERVER_IMAGE",
)

WORKER_IMAGE_KEYS = (
    "CALICO_CNI_IMAGE",
    "CALICO_KUBE_CONTROLLERS_IMAGE",
    "CALICO_NODE_IMAGE",
    "KUBE_PROXY_IMAGE",
    "KONNECTIVITY_AGENT_IMAGE",
    "CNPG_CONTROLLER_IMAGE",
    "POSTGRES_IMAGE",
    "VERIFY_IMAGE",
)

TENANT_HOST_IMAGE_KEYS = (
    "KIND_NODE_IMAGE",
    "CAPD_LOAD_BALANCER_IMAGE",
    *WORKER_IMAGE_KEYS,
)


def references(config: dict[str, str], keys: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted(config[key] for key in keys))


def restore_host_images(
    root: Path,
    config: dict[str, str],
    keys: tuple[str, ...],
) -> None:
    timeout = parse_duration(config["DOWNLOAD_TIMEOUT"])
    for key in keys:
        restore_host_image(root, config, key, timeout)


def enforce_offline_node_egress(
    root: Path,
    config: dict[str, str],
    container: str,
) -> None:
    if os.environ.get("CAPI_OFFLINE_ENFORCED") != "1":
        return
    allowed = {"127.0.0.0/8"}
    allowed.update(
        value
        for key, value in config.items()
        if key.endswith("_CIDR") and "/" in value
    )
    network_record = root / ".runtime" / "management" / "network.json"
    if network_record.is_file():
        network = json.loads(network_record.read_text(encoding="utf-8"))
        subnet = network.get("subnet")
        if subnet:
            allowed.add(subnet)
    chain = "CAPI_OFFLINE"
    commands = [
        f"iptables -N {chain} 2>/dev/null || true",
        f"iptables -F {chain}",
        *(
            f"iptables -A {chain} -d {cidr} -j RETURN"
            for cidr in sorted(allowed)
        ),
        f"iptables -A {chain} -d 1.1.1.1/32 -p tcp --dport 443 -j REJECT",
        f"iptables -A {chain} -p tcp -m multiport --dports 80,443 -j REJECT",
        f"iptables -A {chain} -j RETURN",
        f"iptables -C OUTPUT -j {chain} 2>/dev/null || iptables -I OUTPUT 1 -j {chain}",
        f"iptables -C {chain} -p tcp -m multiport --dports 80,443 -j REJECT",
    ]
    run(
        ["docker", "exec", container, "sh", "-ec", "; ".join(commands)],
        timeout=30,
    )
    denied = run(
        [
            "docker",
            "exec",
            container,
            "bash",
            "-c",
            f"iptables -Z {chain}; "
            "timeout 3 bash -c '</dev/tcp/1.1.1.1/443'; rc=$?; "
            f"hits=$(iptables -L {chain} -n -v -x | "
            "awk '$1 ~ /^[0-9]+$/ && $8 == \"1.1.1.1\" {sum += $1} "
            "END {print sum + 0}'); "
            "printf '%s %s\\n' \"$rc\" \"$hits\"",
        ],
        timeout=10,
        check=False,
    )
    try:
        probe_returncode, hits = (int(value) for value in denied.stdout.split())
    except (ValueError, TypeError) as exc:
        raise RuntimeError(
            f"offline egress probe produced invalid evidence for {container}"
        ) from exc
    if probe_returncode == 0 or hits < 1:
        raise RuntimeError(f"offline egress denial was not proven for {container}")
    if probe_returncode not in {1, 124}:
        raise RuntimeError(
            f"offline egress probe could not verify denial for {container}: "
            f"exit {probe_returncode}"
        )


def _container_has_image(container: str, reference: str, timeout: int) -> bool:
    runtime_reference = runtime_digest_reference(reference)
    result = run(
        [
            "docker",
            "exec",
            container,
            "ctr",
            "--namespace",
            "k8s.io",
            "images",
            "inspect",
            runtime_reference,
        ],
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        return False
    match = re.search(
        r"(?m)^[└├]──[^\n]*@(sha256:[0-9a-f]{64})",
        result.stdout,
    )
    return match is not None and match.group(1) == reference.rsplit("@", 1)[1]


def import_container_images(
    root: Path,
    config: dict[str, str],
    container: str,
    keys: tuple[str, ...],
) -> None:
    timeout = parse_duration(config["DOWNLOAD_TIMEOUT"])
    for key in keys:
        reference = config[key]
        if _container_has_image(container, reference, timeout):
            continue
        generation = active_generation(root).name
        destination = (
            f"/var/lib/capi-image-cache/generations/{generation}/"
            f"images/{key.lower()}.tar"
        )
        if (
            run(
                ["docker", "exec", container, "test", "-f", destination],
                timeout=30,
                check=False,
            ).returncode
            != 0
        ):
            raise RuntimeError(
                f"container {container} cannot read cache archive {key}"
            )
        run(
                [
                    "docker",
                    "exec",
                    container,
                    "ctr",
                    "--namespace",
                    "k8s.io",
                    "images",
                    "import",
                    "--digests",
                    destination,
                ],
                timeout=timeout,
        )
        run(
                [
                    "docker",
                    "exec",
                    container,
                    "ctr",
                    "--namespace",
                    "k8s.io",
                    "images",
                    "tag",
                    "--force",
                    canonical_tagged(config[f"{key}_TAGGED"]),
                    reference,
                ],
                timeout=timeout,
        )
        run(
                [
                    "docker",
                    "exec",
                    container,
                    "ctr",
                    "--namespace",
                    "k8s.io",
                    "images",
                    "tag",
                    "--force",
                    canonical_tagged(config[f"{key}_TAGGED"]),
                    canonical_exact_reference(reference),
                ],
                timeout=timeout,
        )
        run(
                [
                    "docker",
                    "exec",
                    container,
                    "ctr",
                    "--namespace",
                    "k8s.io",
                    "images",
                    "tag",
                    "--force",
                    canonical_tagged(config[f"{key}_TAGGED"]),
                    runtime_digest_reference(reference),
                ],
                timeout=timeout,
        )
        if not _container_has_image(container, reference, timeout):
            raise RuntimeError(
                f"container {container} lacks imported exact image {key}"
            )


def verify_container_images(
    config: dict[str, str],
    container: str,
    keys: tuple[str, ...],
) -> None:
    timeout = parse_duration(config["COMMAND_TIMEOUT"])
    missing = [
        key
        for key in keys
        if not _container_has_image(container, config[key], timeout)
    ]
    if missing:
        raise RuntimeError(
            f"container {container} lacks exact images: {', '.join(sorted(missing))}"
        )


def _pre_cni_worker_names(client, tenant) -> tuple[str, ...] | None:
    machines = json.loads(
        client.kubectl(
            "-n",
            tenant.namespace,
            "get",
            "machines",
            "-l",
            f"cluster.x-k8s.io/cluster-name={tenant.name}",
            "-o",
            "json",
        ).stdout
    )["items"]
    if len(machines) != tenant.workers:
        return None
    names = tuple(sorted(item["metadata"]["name"] for item in machines))
    containers = tuple(
        sorted(
            run(
                [
                    "docker",
                    "ps",
                    "--filter",
                    f"label=io.x-k8s.kind.cluster={tenant.name}",
                    "--filter",
                    "label=io.x-k8s.kind.role=worker",
                    "--format",
                    "{{.Names}}",
                ],
                timeout=30,
            ).stdout.split()
        )
    )
    if containers != names:
        return None
    for container in containers:
        if (
            run(
                [
                    "docker",
                    "exec",
                    container,
                    "test",
                    "-S",
                    "/run/containerd/containerd.sock",
                ],
                timeout=30,
                check=False,
            ).returncode
            != 0
        ):
            return None
    return names


def wait_pre_cni_workers(root: Path, config: dict[str, str], client, tenant) -> tuple[str, ...]:
    return wait_for(
        f"all pre-CNI worker containers for {tenant.name}",
        parse_duration(config["WORKER_REGISTRATION_TIMEOUT"]),
        parse_duration(config["WAIT_POLL_INTERVAL"]),
        lambda: _pre_cni_worker_names(client, tenant),
    )


def preload_worker_images(
    root: Path,
    config: dict[str, str],
    client,
    tenant,
) -> dict[str, object]:
    containers = wait_pre_cni_workers(root, config, client, tenant)
    results: dict[str, dict[str, object]] = {}

    def preload(container: str) -> dict[str, object]:
        started = time.monotonic()
        try:
            import_container_images(root, config, container, WORKER_IMAGE_KEYS)
            enforce_offline_node_egress(root, config, container)
        except BaseException as exc:
            return {
                "node": container,
                "started": started,
                "finished": time.monotonic(),
                "status": "failed",
                "error": redact(str(exc)),
            }
        return {
            "node": container,
            "started": started,
            "finished": time.monotonic(),
            "status": "passed",
        }

    with ThreadPoolExecutor(max_workers=len(containers)) as executor:
        futures = {executor.submit(preload, name): name for name in containers}
        for future in as_completed(futures):
            result = future.result()
            results[result["node"]] = result

    ordered = [results[name] for name in sorted(results)]
    failures = [item for item in ordered if item["status"] != "passed"]
    intervals = [
        (float(item["started"]), float(item["finished"])) for item in ordered
    ]
    overlap = any(
        left_start < right_end and right_start < left_end
        for index, (left_start, left_end) in enumerate(intervals)
        for right_start, right_end in intervals[index + 1 :]
    )
    evidence = {
        "schema": 1,
        "tenant": tenant.name,
        "nodes": ordered,
        "overlap": overlap,
    }
    evidence_dir = root / ".runtime" / "evidence"
    ensure_private_dir(evidence_dir)
    write_private_file(
        evidence_dir / f"preload-{tenant.name}.json",
        json.dumps(evidence, sort_keys=True) + "\n",
    )
    if failures:
        raise RuntimeError(
            "worker image preload failed: "
            + "; ".join(
                f"{item['node']}: {item.get('error', 'unknown error')}"
                for item in failures
            )
        )
    if len(containers) > 1 and not overlap:
        raise RuntimeError("worker image preload did not overlap across independent nodes")
    return evidence
