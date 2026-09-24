from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

from .files import IntegrityError, verify_sha256, write_private_file
from .kube import ManagementClient, wait_for
from .tenants import NOT_FOUND, Tenant, _tenant_kubectl
from .config import parse_duration
from .conditions import condition_true


SOURCE_LIMIT = 900 * 1024
REFERENCE_LIMIT = 100


class NetworkProbeCleanupError(RuntimeError):
    pass


def _source_object(
    config: dict[str, str],
    tenant: Tenant,
    name: str,
    content: str,
) -> dict[str, object]:
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": name,
            "namespace": tenant.namespace,
            "labels": {
                config["OWNERSHIP_LABEL"]: config["LAB_PREFIX"],
                "addons.cluster.x-k8s.io/resource-set": "",
            },
        },
        "data": {"addons.yaml": content},
    }


def package_source(
    config: dict[str, str],
    tenant: Tenant,
    base_name: str,
    content: str,
    *,
    limit: int = SOURCE_LIMIT,
) -> list[tuple[str, dict[str, object], str]]:
    documents = [
        document.strip()
        for document in re.split(r"(?m)^---\s*$", content)
        if document.strip()
    ]
    chunks: list[str] = []
    current = ""
    for document in documents:
        candidate = document if not current else current + "\n---\n" + document
        candidate_name = f"{base_name}-{len(chunks):03d}"
        serialized = json.dumps(
            _source_object(config, tenant, candidate_name, candidate),
            separators=(",", ":"),
        ).encode("utf-8")
        if len(serialized) <= limit:
            current = candidate
            continue
        if not current:
            raise IntegrityError(f"single add-on document exceeds source limit: {base_name}")
        chunks.append(current)
        current = document
        serialized = json.dumps(
            _source_object(
                config,
                tenant,
                f"{base_name}-{len(chunks):03d}",
                current,
            ),
            separators=(",", ":"),
        ).encode("utf-8")
        if len(serialized) > limit:
            raise IntegrityError(f"single add-on document exceeds source limit: {base_name}")
    if current:
        chunks.append(current)
    if len(chunks) == 1:
        chunks_with_names = [(base_name, chunks[0])]
    else:
        chunks_with_names = [
            (f"{base_name}-{index:03d}", chunk)
            for index, chunk in enumerate(chunks)
        ]
    return [
        (
            name,
            _source_object(config, tenant, name, chunk),
            hashlib.sha256(chunk.encode("utf-8")).hexdigest(),
        )
        for name, chunk in chunks_with_names
    ]


def validate_inventory(inventory: dict[str, str]) -> None:
    if len(inventory) > REFERENCE_LIMIT:
        raise IntegrityError("ClusterResourceSet source reference limit exceeded")
    if any(not re.fullmatch(r"[0-9a-f]{64}", digest) for digest in inventory.values()):
        raise IntegrityError("ClusterResourceSet inventory contains an invalid SHA-256")


def validate_resource_set_references(
    resource_set: dict[str, object],
    inventory: dict[str, str],
) -> None:
    references = resource_set["spec"]["resources"]
    names = [item["name"] for item in references]
    if (
        names != sorted(inventory)
        or len(names) != len(set(names))
        or any(
            set(item) != {"kind", "name"} or item["kind"] != "ConfigMap"
            for item in references
        )
    ):
        raise IntegrityError("ClusterResourceSet references do not match source inventory")


def validate_manifest_hashes(
    manifest: dict[str, object],
    inventory: dict[str, str],
) -> None:
    entries = [
        (
            item["metadata"]["name"],
            hashlib.sha256(item["data"]["addons.yaml"].encode("utf-8")).hexdigest(),
        )
        for item in manifest["items"]
        if item["kind"] == "ConfigMap"
    ]
    if len(entries) != len(inventory) or len({name for name, _ in entries}) != len(
        entries
    ):
        raise IntegrityError("ClusterResourceSet manifest has duplicate or missing sources")
    observed = dict(entries)
    if observed != inventory:
        raise IntegrityError("ClusterResourceSet manifest content hashes do not match inventory")


def wait_network_ready(
    root: Path,
    config: dict[str, str],
    tenant: Tenant,
) -> None:
    timeout = parse_duration(config["TENANT_CONTROL_PLANE_TIMEOUT"])
    interval = parse_duration(config["WAIT_POLL_INTERVAL"])
    client = ManagementClient(root, config)

    def ready():
        node = json.loads(
            _tenant_kubectl(root, config, tenant, "get", "nodes", "-o", "json").stdout
        )
        deployment = json.loads(
            client.kubectl(
                "-n",
                tenant.namespace,
                "get",
                f"machinedeployment/{tenant.name}-worker",
                "-o",
                "json",
            ).stdout
        )
        desired_workers = deployment["spec"]["replicas"]
        if len(node["items"]) != desired_workers:
            return None
        node_ready = all(
            next(
                (
                    condition
                    for condition in item["status"].get("conditions", [])
                    if condition["type"] == "Ready"
                ),
                {},
            ).get("status")
            == "True"
            for item in node["items"]
        )
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
        machine_ready = len(machines) == desired_workers and all(
            condition_true(machine, "Ready") for machine in machines
        )
        checks = (
            ("daemonset/calico-node", "kube-system"),
            ("deployment/calico-kube-controllers", "kube-system"),
            ("daemonset/capi-kube-proxy", "kube-system"),
            ("deployment/coredns", "kube-system"),
        )
        workloads_ready = True
        for resource, namespace in checks:
            response = _tenant_kubectl(
                root,
                config,
                tenant,
                "-n",
                namespace,
                "get",
                resource,
                "-o",
                "json",
                check=False,
            )
            if response.returncode != 0:
                workloads_ready = False
                break
            payload = json.loads(response.stdout)
            desired = payload.get("spec", {}).get("replicas")
            available = payload.get("status", {}).get("availableReplicas", 0)
            if resource.startswith("daemonset/"):
                desired = payload.get("status", {}).get("desiredNumberScheduled", 0)
                available = payload.get("status", {}).get("numberAvailable", 0)
            if not desired or desired != available:
                workloads_ready = False
                break
        return True if node_ready and machine_ready and workloads_ready else None

    wait_for("tenant network readiness", timeout, interval, ready)


def verify_network(
    root: Path,
    config: dict[str, str],
    tenant: Tenant,
) -> None:
    config_map = _tenant_kubectl(
        root,
        config,
        tenant,
        "-n",
        "kube-system",
        "get",
        "configmap/capi-kube-proxy",
        "-o",
        "json",
    ).stdout
    if not re.search(r"maxPerCore:\s*0", json.loads(config_map)["data"]["config.conf"]):
        raise RuntimeError("kube-proxy conntrack.maxPerCore is not zero")
    image = _tenant_kubectl(
        root,
        config,
        tenant,
        "-n",
        "kube-system",
        "get",
        "daemonset/capi-kube-proxy",
        "-o",
        "jsonpath={.spec.template.spec.containers[0].image}",
    ).stdout
    if image != config["KUBE_PROXY_IMAGE"]:
        raise RuntimeError("kube-proxy image is not digest pinned")
    binding = json.loads(
        _tenant_kubectl(
            root,
            config,
            tenant,
            "get",
            "clusterrolebinding/capi-system:node-proxier",
            "-o",
            "json",
        ).stdout
    )
    if (
        binding["roleRef"] != {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "ClusterRole",
            "name": "system:node-proxier",
        }
        or binding.get("subjects")
        != [
            {
                "kind": "ServiceAccount",
                "name": "capi-kube-proxy",
                "namespace": "kube-system",
            }
        ]
    ):
        raise RuntimeError("kube-proxy RBAC drift detected")
    name = f"network-smoke-{time.time_ns()}"
    try:
        _tenant_kubectl(
            root,
            config,
            tenant,
            "run",
            name,
            "--image",
            config["VERIFY_IMAGE"],
            "--restart=Never",
            "--attach",
            "--command",
            "--",
            "sh",
            "-ec",
            f"nslookup kubernetes.default.svc.{tenant.domain} && "
            "wget -qO- --timeout=5 "
            f"https://kubernetes.default.svc.{tenant.domain}/version "
            "--no-check-certificate >/dev/null",
        )
    finally:
        _tenant_kubectl(
            root,
            config,
            tenant,
            "delete",
            f"pod/{name}",
            "--ignore-not-found",
            "--wait=true",
            check=False,
        )
        remaining = _tenant_kubectl(
            root,
            config,
            tenant,
            "get",
            f"pod/{name}",
            check=False,
        )
        if remaining.returncode == 0 or not NOT_FOUND.search(remaining.stderr):
            raise NetworkProbeCleanupError(
                f"network verification pod cleanup failed: {name}"
            )


def network_status(
    root: Path,
    config: dict[str, str],
    client: ManagementClient,
    tenant: Tenant,
    *,
    strict: bool = False,
) -> dict[str, object]:
    result: dict[str, object] = {"ready": False}
    if not (root / ".runtime" / "tenants" / tenant.name / "kubeconfig").is_file():
        result["reason"] = "kubeconfig-missing"
        return result
    try:
        nodes = json.loads(
            _tenant_kubectl(
                root, config, tenant, "get", "nodes", "-o", "json"
            ).stdout
        )["items"]
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
        deployment = json.loads(
            client.kubectl(
                "-n",
                tenant.namespace,
                "get",
                f"machinedeployment/{tenant.name}-worker",
                "-o",
                "json",
            ).stdout
        )
        desired_workers = deployment["spec"]["replicas"]
        config_map = json.loads(
            _tenant_kubectl(
                root,
                config,
                tenant,
                "-n",
                "kube-system",
                "get",
                "configmap/capi-kube-proxy",
                "-o",
                "json",
            ).stdout
        )
        daemonset = json.loads(
            _tenant_kubectl(
                root,
                config,
                tenant,
                "-n",
                "kube-system",
                "get",
                "daemonset/capi-kube-proxy",
                "-o",
                "json",
            ).stdout
        )
        calico_node = json.loads(
            _tenant_kubectl(
                root,
                config,
                tenant,
                "-n",
                "kube-system",
                "get",
                "daemonset/calico-node",
                "-o",
                "json",
            ).stdout
        )
        calico_controllers = json.loads(
            _tenant_kubectl(
                root,
                config,
                tenant,
                "-n",
                "kube-system",
                "get",
                "deployment/calico-kube-controllers",
                "-o",
                "json",
            ).stdout
        )
        binding = json.loads(
            _tenant_kubectl(
                root,
                config,
                tenant,
                "get",
                "clusterrolebinding/capi-system:node-proxier",
                "-o",
                "json",
            ).stdout
        )
        kcp = json.loads(
            client.kubectl(
                "-n",
                tenant.namespace,
                "get",
                f"kamajicontrolplane/{tenant.name}",
                "-o",
                "json",
            ).stdout
        )
        node_ready = len(nodes) == desired_workers and all(
            any(
                condition.get("type") == "Ready"
                and condition.get("status") == "True"
                for condition in node.get("status", {}).get("conditions", [])
            )
            for node in nodes
        )
        machine_ready = len(machines) == desired_workers and all(
            condition_true(machine, "Ready") for machine in machines
        )
        proxy_ready = (
            daemonset["status"].get("desiredNumberScheduled", 0)
            == daemonset["status"].get("numberAvailable", 0)
            > 0
            and daemonset["spec"]["template"]["spec"]["containers"][0]["image"]
            == config["KUBE_PROXY_IMAGE"]
            and re.search(
                r"maxPerCore:\s*0",
                config_map["data"]["config.conf"],
            )
            is not None
        )
        calico_ready = (
            calico_node["status"].get("desiredNumberScheduled", 0)
            == calico_node["status"].get("numberAvailable", 0)
            > 0
            and calico_node["spec"]["template"]["spec"]["containers"][0]["image"]
            == config["CALICO_NODE_IMAGE"]
            and calico_controllers["status"].get("availableReplicas", 0)
            == calico_controllers["spec"].get("replicas", 0)
            > 0
            and calico_controllers["spec"]["template"]["spec"]["containers"][0][
                "image"
            ]
            == config["CALICO_KUBE_CONTROLLERS_IMAGE"]
        )
        rbac_ready = (
            binding["roleRef"].get("name") == "system:node-proxier"
            and binding.get("subjects")
            == [
                {
                    "kind": "ServiceAccount",
                    "name": "capi-kube-proxy",
                    "namespace": "kube-system",
                }
            ]
        )
        control_plane_active = "cluster.x-k8s.io/paused" not in (
            kcp["metadata"].get("annotations") or {}
        )
        result.update(
            {
                "ready": (
                    node_ready
                    and machine_ready
                    and proxy_ready
                    and calico_ready
                    and rbac_ready
                    and control_plane_active
                ),
                "nodeReady": node_ready,
                "machineReady": machine_ready,
                "kubeProxyReady": proxy_ready,
                "calicoReady": calico_ready,
                "kubeProxyRBACReady": rbac_ready,
                "controlPlaneActive": control_plane_active,
            }
        )
    except RuntimeError as exc:
        if strict and not NOT_FOUND.search(str(exc)):
            raise
        result["reason"] = str(exc)
    return result
