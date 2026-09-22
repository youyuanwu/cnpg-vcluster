from __future__ import annotations

import json
import re
from pathlib import Path

from scripts.lib.kube import ManagementClient
from scripts.lib.process import run


def controller_mutation_enabled(client: ManagementClient) -> bool:
    response = client.kubectl(
        "-n",
        "tenant-system",
        "get",
        "deployment/tenant-controller",
        "-o",
        "json",
        check=False,
    )
    if response.returncode != 0:
        output = f"{response.stdout}{response.stderr}"
        if re.search(r"not\s*found|notfound", output, re.IGNORECASE):
            return False
        raise RuntimeError(
            f"failed to inspect Tenant controller mutation mode: {response.stderr}"
        )
    deployment = json.loads(response.stdout)
    containers = (
        deployment.get("spec", {})
        .get("template", {})
        .get("spec", {})
        .get("containers", [])
    )
    manager = next(
        (container for container in containers if container.get("name") == "manager"),
        {},
    )
    return "--mutation-enabled=true" in manager.get("args", [])


def require_clean_controller_cutover(
    root: Path,
    config: dict[str, str],
    client: ManagementClient,
) -> None:
    for relative in (
        ".runtime/lifecycle/local",
        ".runtime/rendered/tenants",
        ".runtime/storage",
        ".runtime/kubeconfigs",
        ".runtime/deletions",
    ):
        legacy = root / relative
        if legacy.exists() and any(legacy.rglob("*")):
            raise RuntimeError(
                f"legacy local lifecycle state blocks controller activation: {relative}"
            )
    tenants = client.kubectl(
        "get",
        "tenants.tenancy.cnpg-vcluster.io",
        "-o",
        "name",
        check=False,
    )
    if tenants.returncode == 0 and tenants.stdout.strip():
        raise RuntimeError(
            f"existing Tenant resources block controller activation: {tenants.stdout.strip()}"
        )
    output = f"{tenants.stdout}{tenants.stderr}"
    if tenants.returncode != 0 and not re.search(
        r"not\s*found|notfound|doesn.t have a resource type",
        output,
        re.IGNORECASE,
    ):
        raise RuntimeError(f"failed to inspect Tenant resources: {tenants.stderr}")

    clusters = client.json("get", "clusters.cluster.x-k8s.io", "-A")
    if clusters.get("items"):
        raise RuntimeError("existing CAPI Clusters block controller activation")
    selector = f"{config['OWNERSHIP_LABEL']}={config['LAB_PREFIX']}"
    for resource in (
        "namespaces",
        "devclusters.infrastructure.cluster.x-k8s.io",
        "kamajicontrolplanes.controlplane.cluster.x-k8s.io",
        "kubeadmconfigtemplates.bootstrap.cluster.x-k8s.io",
        "devmachinetemplates.infrastructure.cluster.x-k8s.io",
        "machinedeployments.cluster.x-k8s.io",
    ):
        response = client.kubectl(
            "get",
            resource,
            "-A",
            "-l",
            selector,
            "-o",
            "name",
            check=False,
        )
        if response.returncode != 0:
            raise RuntimeError(f"failed to inspect clean-cutover resource {resource}")
        if response.stdout.strip():
            raise RuntimeError(
                f"owned provider state blocks controller activation: {response.stdout.strip()}"
            )

    allocations = client.kubectl(
        "-n",
        "tenant-system",
        "get",
        "configmap/tenant-endpoint-allocations",
        "-o",
        "json",
        check=False,
    )
    if allocations.returncode == 0:
        document = json.loads(allocations.stdout)
        state = json.loads(document.get("data", {}).get("allocations.json", "{}"))
        if state.get("allocations"):
            raise RuntimeError(
                "endpoint allocations without active controller Tenants block activation"
            )
    elif not re.search(
        r"not\s*found|notfound",
        f"{allocations.stdout}{allocations.stderr}",
        re.IGNORECASE,
    ):
        raise RuntimeError(
            f"failed to inspect endpoint allocations: {allocations.stderr}"
        )

    volumes = run(
        [
            "docker",
            "volume",
            "ls",
            "-q",
            "--filter",
            "label=cnpg-vcluster.capi/role=tenant-storage",
        ],
        timeout=30,
    ).stdout.split()
    if volumes:
        raise RuntimeError(
            f"controller-owned Docker volumes block controller activation: {volumes}"
        )
