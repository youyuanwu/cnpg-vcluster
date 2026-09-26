from __future__ import annotations

import json
import re
from pathlib import Path

from scripts.lib.kube import ManagementClient
from scripts.lib.process import run


LEGACY_WEBHOOK_RESOURCES = (
    (None, "validatingwebhookconfiguration/tenant-controller-validating-webhook"),
    ("tenant-system", "service/tenant-controller-webhook"),
    ("tenant-system", "certificate.cert-manager.io/tenant-controller-serving-cert"),
    ("tenant-system", "issuer.cert-manager.io/tenant-controller-selfsigned"),
    ("tenant-system", "secret/tenant-controller-serving-cert"),
)


def _missing(response, *, undiscovered: bool = False) -> bool:
    pattern = r"not\s*found|notfound"
    if undiscovered:
        pattern += r"|doesn't have a resource type|could not find the requested resource"
    return response.returncode != 0 and bool(
        re.search(pattern, f"{response.stdout}{response.stderr}", re.IGNORECASE)
    )


def verify_absent(client: ManagementClient, namespace: str | None, resource: str) -> None:
    scope = ("-n", namespace) if namespace else ()
    response = client.kubectl(*scope, "get", resource, "-o", "name", check=False)
    if not _missing(response, undiscovered=resource.startswith(
        ("certificate.cert-manager.io/", "issuer.cert-manager.io/")
    )):
        raise RuntimeError(f"cannot prove {resource} absent: {response.stdout}{response.stderr}")


def delete_named(
    config: dict[str, str], client: ManagementClient,
    namespace: str | None, resource: str,
) -> None:
    scope = ("-n", namespace) if namespace else ()
    response = client.kubectl(
        *scope, "delete", resource, "--ignore-not-found=true", "--wait=true",
        "--cascade=foreground",
        f"--timeout={config['DELETE_TIMEOUT']}", check=False,
    )
    if response.returncode != 0 and not _missing(response, undiscovered=resource.startswith(
        ("certificate.cert-manager.io/", "issuer.cert-manager.io/")
    )):
        raise RuntimeError(f"failed to remove {resource}: {response.stderr}")
    verify_absent(client, namespace, resource)


def verify_legacy_webhook_absent(client: ManagementClient) -> None:
    for namespace, resource in LEGACY_WEBHOOK_RESOURCES:
        verify_absent(client, namespace, resource)


def delete_legacy_controller(
    root: Path, config: dict[str, str], client: ManagementClient,
) -> None:
    for namespace, resource in (
        *LEGACY_WEBHOOK_RESOURCES,
        ("tenant-system", "deployment/tenant-controller"),
        ("tenant-system", "configmap/tenant-endpoint-allocations"),
        (None, "crd/tenants.tenancy.cnpg-vcluster.io"),
    ):
        delete_named(config, client, namespace, resource)
    ledger = root / ".runtime" / "management" / "tenant-endpoints.json"
    ledger.unlink(missing_ok=True)
    verify_legacy_webhook_absent(client)


def _controller_manager_args(client: ManagementClient) -> list[str] | None:
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
            return None
        raise RuntimeError(
            f"failed to inspect Tenant controller deployment: {response.stderr}"
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
        None,
    )
    if manager is None:
        raise RuntimeError("Tenant controller manager container is missing")
    return manager.get("args", [])


def controller_mutation_enabled(client: ManagementClient) -> bool:
    args = _controller_manager_args(client)
    return args is not None and "--mutation-enabled=true" in args


def controller_lifecycle_epoch(client: ManagementClient) -> str | None:
    args = _controller_manager_args(client)
    if args is None:
        return None
    prefix = "--lifecycle-epoch="
    values = [value.removeprefix(prefix) for value in args if value.startswith(prefix)]
    if len(values) > 1:
        raise RuntimeError("Tenant controller lifecycle epoch argument is duplicated")
    return values[0] if values else None


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
        ".runtime/tenants",
        ".runtime/deletions",
    ):
        legacy = root / relative
        if legacy.exists() and any(legacy.rglob("*")):
            raise RuntimeError(
                f"legacy local lifecycle state blocks controller activation: {relative}"
            )
    legacy_allocations = (
        root / ".runtime" / "management" / "tenant-endpoints.json"
    )
    if legacy_allocations.exists():
        try:
            state = json.loads(legacy_allocations.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                "legacy tenant endpoint allocation record is invalid"
            ) from exc
        if (
            set(state) != {"schema", "networkId", "allocations"}
            or state.get("schema") != 1
            or not isinstance(state.get("networkId"), str)
            or not state["networkId"]
            or not isinstance(state.get("allocations"), dict)
        ):
            raise RuntimeError(
                "legacy tenant endpoint allocation record is invalid"
            )
        if state["allocations"]:
            raise RuntimeError(
                "legacy tenant endpoint allocations block controller activation"
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
    if tenants.returncode != 0:
        verify_absent(client, None, "crd/tenants.tenancy.cnpg-vcluster.io")

    clusters = client.json("get", "clusters.cluster.x-k8s.io", "-A")
    if not isinstance(clusters.get("items"), list):
        raise RuntimeError("failed to inspect CAPI Cluster inventory")
    if clusters["items"]:
        raise RuntimeError("existing CAPI Clusters block controller activation")
    selector = f"{config['OWNERSHIP_LABEL']}={config['LAB_PREFIX']}"
    for resource in (
        "namespaces",
        "devclusters.infrastructure.cluster.x-k8s.io",
        "kamajicontrolplanes.controlplane.cluster.x-k8s.io",
        "kubeadmconfigtemplates.bootstrap.cluster.x-k8s.io",
        "devmachinetemplates.infrastructure.cluster.x-k8s.io",
        "machinedeployments.cluster.x-k8s.io",
        "machines.cluster.x-k8s.io",
        "machinesets.cluster.x-k8s.io",
        "kubeadmconfigs.bootstrap.cluster.x-k8s.io",
        "devmachines.infrastructure.cluster.x-k8s.io",
        "tenantcontrolplanes.kamaji.clastix.io",
    ):
        response = client.kubectl(
            "get",
            resource,
            "-A",
            *(["-l", selector] if resource == "namespaces" else []),
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
        try:
            state = json.loads(document["data"]["allocations.json"])
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("legacy endpoint allocation ledger is malformed") from exc
        if (
            not isinstance(state, dict) or state.get("schema") != 1
            or not isinstance(state.get("allocations"), dict)
            or any(not isinstance(state.get(key), str) or not state[key] for key in (
                "foundationHash", "networkId", "poolStart", "poolEnd",
            ))
        ):
            raise RuntimeError("legacy endpoint allocation ledger is malformed")
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

    leases = client.json("-n", "tenant-system", "get", "leases.coordination.k8s.io")
    for lease in leases["items"]:
        metadata = lease["metadata"]
        if (
            metadata["name"] != "tenant-controller.tenancy.cnpg-vcluster.io"
            or "tenancy.cnpg-vcluster.io/slot-id" in metadata.get("labels", {})
            or metadata.get("annotations", {}).get("tenancy.cnpg-vcluster.io/resource")
            == "allocation-lease"
        ):
            raise RuntimeError("allocation Lease residue blocks controller activation")

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
    owned_containers = run(
        [
            "docker",
            "ps",
            "-aq",
            "--filter",
            f"label={config['OWNERSHIP_LABEL']}={config['LAB_PREFIX']}",
            "--filter",
            "label=io.x-k8s.kind.cluster",
        ],
        timeout=30,
    ).stdout.split()
    load_balancers = run(
        [
            "docker",
            "ps",
            "-aq",
            "--filter",
            "label=io.x-k8s.kind.role=external-load-balancer",
        ],
        timeout=30,
    ).stdout.split()
    workers = run(
        ["docker", "ps", "-aq", "--filter", "label=io.x-k8s.kind.role=worker"],
        timeout=30,
    ).stdout.split()
    tenant_containers = sorted(set(owned_containers) | set(load_balancers) | set(workers))
    if tenant_containers:
        raise RuntimeError(
            "CAPD tenant containers block controller activation: "
            f"{tenant_containers}"
        )
