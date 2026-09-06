from __future__ import annotations

import base64
import json
import os
import re
import shutil
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from .conditions import condition_summary, condition_true
from .config import parse_duration
from .files import IntegrityError, write_private_file
from .kube import ManagementClient, wait_for
from .process import run


@dataclass(frozen=True)
class Tenant:
    name: str
    namespace: str
    vip: str
    pod_cidr: str
    service_cidr: str
    dns_ip: str
    domain: str
    storage_host_path: Path


def spike_tenant(root: Path, config: dict[str, str]) -> Tenant:
    network_path = root / ".runtime" / "management" / "network.json"
    network = json.loads(network_path.read_text(encoding="utf-8"))
    return Tenant(
        name=config["SPIKE_NAME"],
        namespace=config["SPIKE_NAMESPACE"],
        vip=network["slots"]["spike"],
        pod_cidr=config["SPIKE_POD_CIDR"],
        service_cidr=config["SPIKE_SERVICE_CIDR"],
        dns_ip=config["SPIKE_DNS_SERVICE_IP"],
        domain=config["SPIKE_CLUSTER_DOMAIN"],
        storage_host_path=root / ".runtime" / "storage" / "spike",
    )


def _render_template(
    source: Path,
    destination: Path,
    values: dict[str, str],
) -> None:
    content = source.read_text(encoding="utf-8")
    for key, value in values.items():
        placeholder = "${" + key + "}"
        if placeholder in content:
            content = content.replace(placeholder, value)
    unresolved = sorted(set(re.findall(r"\$\{[A-Z][A-Z0-9_]*\}", content)))
    if unresolved:
        raise IntegrityError(
            f"{source.name} has unresolved placeholders: {', '.join(unresolved)}"
        )
    write_private_file(destination, content)


def _tenant_values(root: Path, config: dict[str, str], tenant: Tenant) -> dict[str, str]:
    return {
        "NAMESPACE": tenant.namespace,
        "CLUSTER_NAME": tenant.name,
        "OWNERSHIP_LABEL": config["OWNERSHIP_LABEL"],
        "LAB_PREFIX": config["LAB_PREFIX"],
        "ADDON_PROFILE": tenant.name,
        "API_VIP": tenant.vip,
        "API_PORT": config["SPIKE_API_PORT"],
        "SERVICE_CIDR": tenant.service_cidr,
        "POD_CIDR": tenant.pod_cidr,
        "CLUSTER_DOMAIN": tenant.domain,
        "DNS_SERVICE_IP": tenant.dns_ip,
        "KUBERNETES_VERSION": config["KUBERNETES_VERSION"],
        "KONNECTIVITY_SERVER_REPOSITORY": config["KONNECTIVITY_SERVER_IMAGE_TAGGED"].split(
            ":", 1
        )[0],
        "KONNECTIVITY_SERVER_VERSION_DIGEST": config["KONNECTIVITY_SERVER_IMAGE"].split(
            ":", 1
        )[1],
        "KONNECTIVITY_AGENT_REPOSITORY": config["KONNECTIVITY_AGENT_IMAGE_TAGGED"].split(
            ":", 1
        )[0],
        "KONNECTIVITY_AGENT_VERSION_DIGEST": config["KONNECTIVITY_AGENT_IMAGE"].split(
            ":", 1
        )[1],
        "KIND_NODE_IMAGE": config["KIND_NODE_IMAGE"],
        "STORAGE_HOST_PATH": str(tenant.storage_host_path),
        "STORAGE_CONTAINER_PATH": config["SPIKE_STORAGE_CONTAINER_PATH"],
    }


def render_tenant_manifests(
    root: Path,
    config: dict[str, str],
    tenant: Tenant,
) -> tuple[Path, Path]:
    directory = root / ".runtime" / "rendered" / "tenants" / tenant.name
    values = _tenant_values(root, config, tenant)
    control_plane = directory / "control-plane.yaml"
    workers = directory / "workers.yaml"
    _render_template(
        root / "manifests" / "tenants" / "base" / "control-plane.yaml.tpl",
        control_plane,
        values,
    )
    _render_template(
        root / "manifests" / "tenants" / "base" / "workers.yaml.tpl",
        workers,
        values,
    )
    return control_plane, workers


def _resource(
    client: ManagementClient,
    tenant: Tenant,
    kind: str,
    name: str,
) -> dict[str, object] | None:
    response = client.kubectl(
        "-n",
        tenant.namespace,
        "get",
        f"{kind}/{name}",
        "-o",
        "json",
        check=False,
    )
    return json.loads(response.stdout) if response.returncode == 0 else None


def _wait_resource(
    client: ManagementClient,
    config: dict[str, str],
    tenant: Tenant,
    description: str,
    predicate,
):
    return wait_for(
        description,
        parse_duration(config["TENANT_CONTROL_PLANE_TIMEOUT"]),
        parse_duration(config["WAIT_POLL_INTERVAL"]),
        lambda: (
            predicate()
            if client.kubectl(
                "-n",
                tenant.namespace,
                "get",
                f"cluster/{tenant.name}",
                check=False,
            ).returncode
            == 0
            else None
        ),
    )


def apply_control_plane(
    root: Path,
    config: dict[str, str],
    client: ManagementClient,
    tenant: Tenant,
) -> Path:
    control_plane, _ = render_tenant_manifests(root, config, tenant)
    client.kubectl(
        "apply",
        "--server-side",
        "--field-manager=capi-kamaji-lab",
        "--force-conflicts",
        "-f",
        str(control_plane),
    )

    def ready():
        devcluster = _resource(client, tenant, "devcluster", tenant.name)
        kcp = _resource(client, tenant, "kamajicontrolplane", tenant.name)
        cluster = _resource(client, tenant, "cluster", tenant.name)
        if not devcluster or not kcp or not cluster:
            return None
        provisioned = devcluster.get("status", {}).get("initialization", {}).get(
            "provisioned"
        )
        initialized = kcp.get("status", {}).get("initialization", {}).get(
            "controlPlaneInitialized"
        )
        endpoint = kcp.get("spec", {}).get("controlPlaneEndpoint", {})
        if (
            provisioned is True
            and initialized is True
            and condition_true(kcp, "Available")
            and endpoint.get("host") == tenant.vip
            and int(endpoint.get("port", 0)) == int(config["SPIKE_API_PORT"])
            and cluster["spec"]["controlPlaneEndpoint"]["host"] == tenant.vip
            and devcluster["spec"]["controlPlaneEndpoint"]["host"] == tenant.vip
        ):
            return {"cluster": cluster, "devcluster": devcluster, "kcp": kcp}
        return None

    _wait_resource(client, config, tenant, "hosted control plane readiness", ready)
    return control_plane


def tenant_kubeconfig_path(root: Path, tenant: Tenant) -> Path:
    return root / ".runtime" / "tenants" / tenant.name / "kubeconfig"


def export_tenant_kubeconfig(
    root: Path,
    config: dict[str, str],
    client: ManagementClient,
    tenant: Tenant,
) -> Path:
    secret = json.loads(
        client.kubectl(
            "-n",
            tenant.namespace,
            "get",
            f"secret/{tenant.name}-kubeconfig",
            "-o",
            "json",
        ).stdout
    )
    value = base64.b64decode(secret["data"]["value"])
    path = tenant_kubeconfig_path(root, tenant)
    write_private_file(path, value)
    text = value.decode("utf-8")
    if f"https://{tenant.vip}:{config['SPIKE_API_PORT']}" not in text:
        raise RuntimeError("tenant kubeconfig does not use the authoritative endpoint")
    run(
        [
            str(root / ".tools" / "bin" / "kubectl"),
            "--kubeconfig",
            str(path),
            "--request-timeout",
            config["KUBECTL_REQUEST_TIMEOUT"],
            "get",
            "--raw=/readyz",
        ],
        timeout=parse_duration(config["COMMAND_TIMEOUT"]),
    )
    return path


def _tenant_kubectl(
    root: Path,
    config: dict[str, str],
    tenant: Tenant,
    *arguments: str,
    check: bool = True,
):
    return run(
        [
            str(root / ".tools" / "bin" / "kubectl"),
            "--kubeconfig",
            str(tenant_kubeconfig_path(root, tenant)),
            "--request-timeout",
            config["KUBECTL_REQUEST_TIMEOUT"],
            *arguments,
        ],
        timeout=parse_duration(config["COMMAND_TIMEOUT"]),
        check=check,
    )


def apply_bootstrap_rbac(
    root: Path,
    config: dict[str, str],
    tenant: Tenant,
) -> None:
    wait_for(
        "tenant administrative RBAC",
        parse_duration(config["TENANT_CONTROL_PLANE_TIMEOUT"]),
        parse_duration(config["WAIT_POLL_INTERVAL"]),
        lambda: (
            _tenant_kubectl(
                root,
                config,
                tenant,
                "auth",
                "can-i",
                "get",
                "roles",
                "-n",
                "kube-system",
                check=False,
            ).stdout.strip()
            == "yes"
        ),
    )
    manifest = root / "manifests" / "tenants" / "base" / "bootstrap-rbac.yaml"
    _tenant_kubectl(root, config, tenant, "apply", "-f", str(manifest))
    for role in ("kubeadm:nodes-kubeadm-config", "kubeadm:kubelet-config"):
        _tenant_kubectl(
            root,
            config,
            tenant,
            "-n",
            "kube-system",
            "get",
            f"role/{role}",
        )


def prepare_storage_directory(root: Path, config: dict[str, str], tenant: Tenant) -> None:
    tenant.storage_host_path.mkdir(parents=True, exist_ok=True, mode=0o700)
    tenant.storage_host_path.chmod(0o700)
    marker = tenant.storage_host_path / ".capi-owner.json"
    expected = {
        "schema": 1,
        "lab": config["LAB_PREFIX"],
        "tenant": tenant.name,
        "uid": os.getuid(),
    }
    if marker.exists():
        if marker.is_symlink() or json.loads(marker.read_text(encoding="utf-8")) != expected:
            raise RuntimeError("tenant storage directory ownership cannot be proven")
    else:
        write_private_file(marker, json.dumps(expected, sort_keys=True) + "\n")


def apply_workers(
    root: Path,
    config: dict[str, str],
    client: ManagementClient,
    tenant: Tenant,
) -> Path:
    prepare_storage_directory(root, config, tenant)
    _, workers = render_tenant_manifests(root, config, tenant)
    client.kubectl(
        "apply",
        "--server-side",
        "--field-manager=capi-kamaji-lab",
        "--force-conflicts",
        "-f",
        str(workers),
    )
    return workers


def _machine(client: ManagementClient, tenant: Tenant) -> dict[str, object] | None:
    response = client.kubectl(
        "-n",
        tenant.namespace,
        "get",
        "machines",
        "-l",
        f"cluster.x-k8s.io/cluster-name={tenant.name}",
        "-o",
        "json",
        check=False,
    )
    if response.returncode != 0:
        return None
    items = json.loads(response.stdout)["items"]
    return items[0] if len(items) == 1 else None


def wait_for_registered_node(
    root: Path,
    config: dict[str, str],
    client: ManagementClient,
    tenant: Tenant,
) -> dict[str, object]:
    def registered():
        machine = _machine(client, tenant)
        if not machine:
            return None
        name = machine["metadata"]["name"]
        devmachine = _resource(client, tenant, "devmachine", name)
        kubeadm = _resource(client, tenant, "kubeadmconfig", name)
        node_ref = machine.get("status", {}).get("nodeRef", {}).get("name")
        if not devmachine or not kubeadm or not node_ref:
            return None
        secret_name = kubeadm.get("status", {}).get("dataSecretName")
        if not secret_name:
            return None
        node = json.loads(
            _tenant_kubectl(
                root,
                config,
                tenant,
                "get",
                f"node/{node_ref}",
                "-o",
                "json",
            ).stdout
        )
        if (
            devmachine.get("status", {}).get("initialization", {}).get("provisioned")
            is True
            and condition_true(devmachine, "BootstrapCompleted")
            and machine.get("status", {})
            .get("initialization", {})
            .get("infrastructureProvisioned")
            is True
        ):
            return {
                "machine": machine,
                "devmachine": devmachine,
                "kubeadm": kubeadm,
                "node": node,
                "secret": secret_name,
            }
        return None

    return wait_for(
        "registered pre-CNI worker Node",
        parse_duration(config["WORKER_REGISTRATION_TIMEOUT"]),
        parse_duration(config["WAIT_POLL_INTERVAL"]),
        registered,
    )


def verify_authoritative_endpoint(
    root: Path,
    config: dict[str, str],
    client: ManagementClient,
    tenant: Tenant,
    registered: dict[str, object],
) -> None:
    cluster = _resource(client, tenant, "cluster", tenant.name)
    devcluster = _resource(client, tenant, "devcluster", tenant.name)
    kcp = _resource(client, tenant, "kamajicontrolplane", tenant.name)
    if not cluster or not devcluster or not kcp:
        raise RuntimeError("tenant control-plane resources are missing")
    secret = json.loads(
        client.kubectl(
            "-n",
            tenant.namespace,
            "get",
            f"secret/{registered['secret']}",
            "-o",
            "json",
        ).stdout
    )
    bootstrap = base64.b64decode(secret["data"]["value"]).decode("utf-8", "replace")
    kubeconfig = tenant_kubeconfig_path(root, tenant).read_text(encoding="utf-8")
    expected = f"{tenant.vip}:{config['SPIKE_API_PORT']}"
    sources = (
        f"{cluster['spec']['controlPlaneEndpoint']['host']}:{cluster['spec']['controlPlaneEndpoint']['port']}",
        f"{devcluster['spec']['controlPlaneEndpoint']['host']}:{devcluster['spec']['controlPlaneEndpoint']['port']}",
        f"{kcp['spec']['controlPlaneEndpoint']['host']}:{kcp['spec']['controlPlaneEndpoint']['port']}",
    )
    if any(source != expected for source in sources):
        raise RuntimeError(f"authoritative endpoint mismatch: {sources}")
    if expected not in kubeconfig or expected not in bootstrap:
        raise RuntimeError("kubeconfig or CABPK data does not use the authoritative endpoint")
    payload = verify_load_balancer_runtime(root, config, tenant)
    lb_ip = next(iter(payload["NetworkSettings"]["Networks"].values()))["IPAddress"]
    if lb_ip and (lb_ip in kubeconfig or lb_ip in bootstrap or lb_ip in sources):
        raise RuntimeError("CAPD development load balancer became authoritative")


def verify_load_balancer_runtime(
    root: Path,
    config: dict[str, str],
    tenant: Tenant,
) -> dict[str, object]:
    load_balancer = f"{tenant.name}-lb"
    payload = json.loads(
        run(["docker", "inspect", load_balancer], timeout=30).stdout
    )[0]
    labels = payload.get("Config", {}).get("Labels") or {}
    expected_network = json.loads(
        (root / ".runtime" / "management" / "network.json").read_text(
            encoding="utf-8"
        )
    )["network"]
    tagged = config["CAPD_LOAD_BALANCER_IMAGE_TAGGED"]
    image_details = json.loads(
        run(["docker", "image", "inspect", tagged], timeout=30).stdout
    )[0]
    expected_digest = config["CAPD_LOAD_BALANCER_IMAGE"].rsplit("@", 1)[1]
    repo_digests = image_details.get("RepoDigests") or []
    if (
        payload.get("Name") != f"/{load_balancer}"
        or payload.get("State", {}).get("Running") is not True
        or payload.get("Config", {}).get("Image") != tagged
        or payload.get("Image") != image_details.get("Id")
        or not any(item.endswith(f"@{expected_digest}") for item in repo_digests)
        or labels.get("io.x-k8s.kind.cluster") != tenant.name
        or labels.get("io.x-k8s.kind.role") != "external-load-balancer"
        or set((payload.get("NetworkSettings", {}).get("Networks") or {}))
        != {expected_network}
    ):
        raise RuntimeError("CAPD load balancer runtime does not match the local profile")
    return payload


def verify_worker_runtime(
    config: dict[str, str],
    tenant: Tenant,
    registered: dict[str, object],
) -> None:
    machine_name = registered["machine"]["metadata"]["name"]
    payload = json.loads(run(["docker", "inspect", machine_name], timeout=30).stdout)[0]
    labels = payload.get("Config", {}).get("Labels") or {}
    mounts = {
        mount["Destination"]: mount
        for mount in payload.get("Mounts", [])
    }
    expected_mount = mounts.get(config["SPIKE_STORAGE_CONTAINER_PATH"])
    image = payload.get("Config", {}).get("Image")
    expected_network = json.loads(
        (
            tenant.storage_host_path.parents[1]
            / "management"
            / "network.json"
        ).read_text(encoding="utf-8")
    )["network"]
    networks = payload.get("NetworkSettings", {}).get("Networks") or {}
    if (
        payload.get("State", {}).get("Running") is not True
        or labels.get("io.x-k8s.kind.cluster") != tenant.name
        or labels.get("io.x-k8s.kind.role") != "worker"
        or image != config["KIND_NODE_IMAGE"]
        or not expected_mount
        or Path(expected_mount["Source"]).resolve() != tenant.storage_host_path.resolve()
        or set(networks) != {expected_network}
    ):
        raise RuntimeError("CAPD worker runtime does not match the declared local profile")


def endpoint_snapshot(
    root: Path,
    config: dict[str, str],
    client: ManagementClient,
    tenant: Tenant,
) -> dict[str, object]:
    machine = _machine(client, tenant)
    resources = {}
    for kind, name in (
        ("cluster", tenant.name),
        ("devcluster", tenant.name),
        ("kamajicontrolplane", tenant.name),
    ):
        resource = _resource(client, tenant, kind, name)
        if resource:
            resources[kind] = {
                "uid": resource["metadata"]["uid"],
                "generation": resource["metadata"]["generation"],
                "conditions": condition_summary(resource),
            }
    if machine:
        devmachine = _resource(client, tenant, "devmachine", machine["metadata"]["name"])
        node_name = machine.get("status", {}).get("nodeRef", {}).get("name")
        node = None
        if node_name and tenant_kubeconfig_path(root, tenant).is_file():
            response = _tenant_kubectl(
                root,
                config,
                tenant,
                "get",
                f"node/{node_name}",
                "-o",
                "json",
                check=False,
            )
            if response.returncode == 0:
                node = json.loads(response.stdout)
        container = run(
            ["docker", "inspect", machine["metadata"]["name"], "--format", "{{.Id}}"],
            timeout=30,
            check=False,
        )
        resources["machine"] = {
            "uid": machine["metadata"]["uid"],
            "nodeRef": machine.get("status", {}).get("nodeRef"),
            "conditions": condition_summary(machine),
            "devMachineUID": devmachine["metadata"]["uid"] if devmachine else None,
            "containerID": container.stdout.strip() if container.returncode == 0 else None,
            "nodeUID": node["metadata"]["uid"] if node else None,
        }
    return resources


def delete_tenant(
    root: Path,
    config: dict[str, str],
    client: ManagementClient,
    tenant: Tenant,
) -> None:
    client.kubectl(
        "-n",
        tenant.namespace,
        "delete",
        f"cluster/{tenant.name}",
        "--ignore-not-found",
        "--wait=true",
        f"--timeout={config['DELETE_TIMEOUT']}",
        check=False,
    )
    client.kubectl(
        "delete",
        "namespace",
        tenant.namespace,
        "--ignore-not-found",
        "--wait=true",
        f"--timeout={config['DELETE_TIMEOUT']}",
        check=False,
    )
    wait_for(
        f"namespace {tenant.namespace} deletion",
        parse_duration(config["DELETE_TIMEOUT"]),
        parse_duration(config["WAIT_POLL_INTERVAL"]),
        lambda: (
            True
            if client.kubectl(
                "get",
                f"namespace/{tenant.namespace}",
                check=False,
            ).returncode
            != 0
            else None
        ),
    )
    leftovers = run(
        [
            "docker",
            "ps",
            "-aq",
            "--filter",
            f"label=io.x-k8s.kind.cluster={tenant.name}",
        ],
        timeout=30,
    ).stdout.split()
    if leftovers:
        raise RuntimeError(f"CAPD resources remain after tenant deletion: {leftovers}")
    kubeconfig = tenant_kubeconfig_path(root, tenant)
    kubeconfig.unlink(missing_ok=True)
    rendered = root / ".runtime" / "rendered" / "tenants" / tenant.name
    shutil.rmtree(rendered, ignore_errors=True)
    marker = tenant.storage_host_path / ".capi-owner.json"
    if tenant.storage_host_path.exists():
        details = tenant.storage_host_path.lstat()
        if (
            stat.S_ISLNK(details.st_mode)
            or details.st_uid != os.getuid()
            or not marker.is_file()
        ):
            raise RuntimeError("refusing to remove unproven tenant storage directory")
        shutil.rmtree(tenant.storage_host_path)
