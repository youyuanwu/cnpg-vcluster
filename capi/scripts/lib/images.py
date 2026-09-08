from __future__ import annotations

import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from scripts.cache import archive_path, restore_host_image
from scripts.lib.config import parse_duration
from scripts.lib.files import ensure_private_dir, write_private_file
from scripts.lib.kube import wait_for
from scripts.lib.process import run


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


def _container_has_image(container: str, reference: str, timeout: int) -> bool:
    return (
        run(
            [
                "docker",
                "exec",
                container,
                "ctr",
                "--namespace",
                "k8s.io",
                "images",
                "inspect",
                reference,
            ],
            timeout=timeout,
            check=False,
        ).returncode
        == 0
    )


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
        source = archive_path(root, config, key)
        destination = f"/tmp/capi-cache-{key.lower()}-{uuid.uuid4().hex}.tar"
        try:
            run(["docker", "cp", str(source), f"{container}:{destination}"], timeout=timeout)
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
        finally:
            run(
                ["docker", "exec", container, "rm", "-f", destination],
                timeout=30,
                check=False,
            )
        if not _container_has_image(container, reference, timeout):
            raise RuntimeError(
                f"container {container} lacks imported exact image {key}"
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
    if any(not item.get("status", {}).get("nodeRef", {}).get("name") for item in machines):
        return None
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
    return names if containers == names else None


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
        except BaseException:
            return {
                "node": container,
                "started": started,
                "finished": time.monotonic(),
                "status": "failed",
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
    failures = [item["node"] for item in ordered if item["status"] != "passed"]
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
            f"worker image preload failed for nodes: {', '.join(sorted(failures))}"
        )
    if len(containers) > 1 and not overlap:
        raise RuntimeError("worker image preload did not overlap across independent nodes")
    return evidence
