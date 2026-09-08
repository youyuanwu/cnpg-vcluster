from __future__ import annotations

import base64
import json
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from .conditions import condition_true
from .config import parse_duration
from .files import IntegrityError, ensure_private_dir, write_private_file
from .kube import ManagementClient, wait_for
from .process import run


@dataclass
class Tenant:
    name: str
    namespace: str
    vip: str
    pod_cidr: str
    service_cidr: str
    dns_ip: str
    domain: str
    storage_host_path: Path
    cnpg_cluster: str
    workers: int


NOT_FOUND = re.compile(r"Error from server \(NotFound\):", re.IGNORECASE)


def inspect_management_resource(
    client: ManagementClient,
    tenant: Tenant,
    resource: str,
) -> dict[str, object] | None:
    response = client.kubectl(
        "-n",
        tenant.namespace,
        "get",
        resource,
        "-o",
        "json",
        check=False,
    )
    if response.returncode == 0:
        return json.loads(response.stdout)
    if NOT_FOUND.search(response.stderr):
        return None
    raise RuntimeError(
        f"tenant management inspection failed for {resource}: {response.stderr}"
    )


def verify_tenant_management_ownership(
    config: dict[str, str],
    client: ManagementClient,
    tenant: Tenant,
) -> dict[str, dict[str, object]]:
    present = {}
    namespace_response = client.kubectl(
        "get",
        f"namespace/{tenant.namespace}",
        "-o",
        "json",
        check=False,
    )
    if namespace_response.returncode == 0:
        namespace = json.loads(namespace_response.stdout)
        labels = namespace["metadata"].get("labels") or {}
        if labels.get(config["OWNERSHIP_LABEL"]) != config["LAB_PREFIX"]:
            raise RuntimeError(f"tenant namespace ownership mismatch: {tenant.name}")
        present["namespace"] = namespace
    elif not NOT_FOUND.search(namespace_response.stderr):
        raise RuntimeError(
            f"tenant namespace inspection failed: {namespace_response.stderr}"
        )
    resources = (
        ("cluster", tenant.name),
        ("devcluster", tenant.name),
        ("kamajicontrolplane", tenant.name),
        ("machinedeployment", f"{tenant.name}-worker"),
        ("kubeadmconfigtemplate", f"{tenant.name}-worker"),
        ("devmachinetemplate", f"{tenant.name}-worker"),
    )
    for kind, name in resources:
        payload = inspect_management_resource(client, tenant, f"{kind}/{name}")
        if payload is None:
            continue
        labels = payload["metadata"].get("labels") or {}
        if labels.get(config["OWNERSHIP_LABEL"]) != config["LAB_PREFIX"]:
            raise RuntimeError(
                f"tenant resource ownership mismatch: {kind}/{name}"
            )
        present[kind] = payload
    machines_response = client.kubectl(
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
    if machines_response.returncode == 0:
        machines = json.loads(machines_response.stdout)["items"]
        deployment = present.get("machinedeployment")
        for machine in machines:
            labels = machine["metadata"].get("labels") or {}
            owners = [
                owner
                for owner in machine["metadata"].get("ownerReferences") or []
                if owner.get("controller") is True
            ]
            if (
                labels.get(config["OWNERSHIP_LABEL"]) != config["LAB_PREFIX"]
                or labels.get("cluster.x-k8s.io/cluster-name") != tenant.name
                or labels.get("cnpg-vcluster.capi/nodepool") != "worker"
                or len(owners) != 1
                or owners[0].get("apiVersion")
                != "cluster.x-k8s.io/v1beta2"
                or owners[0].get("kind") != "MachineSet"
            ):
                raise RuntimeError(
                    f"tenant Machine ownership mismatch: "
                    f"{machine['metadata']['name']}"
                )
            machine_set = inspect_management_resource(
                client,
                tenant,
                f"machineset/{owners[0]['name']}",
            )
            set_owners = (
                machine_set["metadata"].get("ownerReferences") or []
                if machine_set
                else []
            )
            set_controller = [
                owner
                for owner in set_owners
                if owner.get("controller") is True
            ]
            set_labels = machine_set["metadata"].get("labels") or {} if machine_set else {}
            if (
                machine_set is None
                or owners[0].get("uid") != machine_set["metadata"]["uid"]
                or set_labels.get(config["OWNERSHIP_LABEL"])
                != config["LAB_PREFIX"]
                or set_labels.get("cluster.x-k8s.io/cluster-name")
                != tenant.name
                or deployment is None
                or len(set_controller) != 1
                or set_controller[0].get("apiVersion")
                != "cluster.x-k8s.io/v1beta2"
                or set_controller[0].get("kind") != "MachineDeployment"
                or set_controller[0].get("name")
                != f"{tenant.name}-worker"
                or set_controller[0].get("uid")
                != deployment["metadata"]["uid"]
            ):
                raise RuntimeError(
                    f"tenant MachineSet ownership mismatch: "
                    f"{owners[0]['name']}"
                )
        present["machines"] = machines
    elif not NOT_FOUND.search(machines_response.stderr):
        raise RuntimeError(
            f"tenant Machine inspection failed: {machines_response.stderr}"
        )
    return present


def verify_tenant_control_plane_contract(
    config: dict[str, str],
    tenant: Tenant,
    resources: dict[str, dict[str, object]],
) -> None:
    required = ("cluster", "devcluster", "kamajicontrolplane")
    if any(kind not in resources for kind in required):
        raise RuntimeError(
            f"tenant control-plane resources are incomplete: {tenant.name}"
        )
    cluster = resources["cluster"]
    devcluster = resources["devcluster"]
    kcp = resources["kamajicontrolplane"]
    endpoints = (
        cluster["spec"].get("controlPlaneEndpoint", {}),
        devcluster["spec"].get("controlPlaneEndpoint", {}),
        kcp["spec"].get("controlPlaneEndpoint", {}),
    )
    network = cluster["spec"].get("clusterNetwork", {})
    if (
        any(
            endpoint.get("host") != tenant.vip
            or int(endpoint.get("port", 0)) != int(config["SPIKE_API_PORT"])
            for endpoint in endpoints
        )
        or network.get("pods", {}).get("cidrBlocks") != [tenant.pod_cidr]
        or network.get("services", {}).get("cidrBlocks")
        != [tenant.service_cidr]
        or network.get("serviceDomain") != tenant.domain
        or not condition_true(cluster, "Available")
        or not condition_true(kcp, "Available")
        or kcp.get("status", {})
        .get("initialization", {})
        .get("controlPlaneInitialized")
        is not True
        or "cluster.x-k8s.io/paused"
        in (kcp["metadata"].get("annotations") or {})
    ):
        raise RuntimeError(
            f"tenant control-plane contract is unhealthy: {tenant.name}"
        )


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
        cnpg_cluster=config["SPIKE_CNPG_CLUSTER"],
        workers=1,
    )


def configured_tenants(root: Path, config: dict[str, str]) -> list[Tenant]:
    network_path = root / ".runtime" / "management" / "network.json"
    network = json.loads(network_path.read_text(encoding="utf-8"))
    expected_names = config["TENANT_NAMES"].split()
    if expected_names != ["tenant-a", "tenant-b"]:
        raise IntegrityError("TENANT_NAMES must be exactly: tenant-a tenant-b")
    tenants = []
    for name in expected_names:
        path = root / "manifests" / "tenants" / "overlays" / name / "tenant.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        required = {
            "clusterDomain",
            "cnpgCluster",
            "dnsServiceIP",
            "name",
            "namespace",
            "podCIDRKey",
            "serviceCIDRKey",
            "vipSlot",
            "workers",
        }
        if set(payload) != required or payload["name"] != name:
            raise IntegrityError(f"invalid tenant overlay: {path}")
        pod_key = str(payload["podCIDRKey"])
        service_key = str(payload["serviceCIDRKey"])
        vip_slot = str(payload["vipSlot"])
        if pod_key not in config or service_key not in config:
            raise IntegrityError(f"tenant overlay references unknown CIDR key: {name}")
        if vip_slot not in network["slots"]:
            raise IntegrityError(f"tenant overlay references unknown VIP slot: {name}")
        workers = int(payload["workers"])
        if workers != int(config["WORKERS_PER_TENANT"]):
            raise IntegrityError(f"tenant worker count does not match settings: {name}")
        tenant = Tenant(
            name=name,
            namespace=str(payload["namespace"]),
            vip=str(network["slots"][vip_slot]),
            pod_cidr=config[pod_key],
            service_cidr=config[service_key],
            dns_ip=str(payload["dnsServiceIP"]),
            domain=str(payload["clusterDomain"]),
            storage_host_path=root / ".runtime" / "storage" / name,
            cnpg_cluster=str(payload["cnpgCluster"]),
            workers=workers,
        )
        volume_name = storage_volume_name(config, tenant)
        volume = inspect_storage_volume(volume_name)
        if volume is not None:
            labels = volume.get("Labels") or {}
            record_path = storage_record_path(root, tenant)
            expected_record = {
                "schema": 1,
                "tenant": tenant.name,
                "volumeName": volume_name,
                "createdAt": volume.get("CreatedAt"),
                "mountpoint": volume.get("Mountpoint"),
            }
            if (
                labels.get(config["OWNERSHIP_LABEL"]) != config["LAB_PREFIX"]
                or labels.get("cnpg-vcluster.capi/role") != "tenant-storage"
                or labels.get("cnpg-vcluster.capi/tenant") != tenant.name
                or not record_path.is_file()
                or record_path.is_symlink()
                or record_path.lstat().st_uid != os.getuid()
                or record_path.lstat().st_mode & 0o077
                or json.loads(record_path.read_text(encoding="utf-8"))
                != expected_record
            ):
                raise RuntimeError(
                    f"existing tenant storage identity cannot be proven: {tenant.name}"
                )
            tenant.storage_host_path = Path(str(volume["Mountpoint"]))
        tenants.append(tenant)
    identities = (
        [tenant.name for tenant in tenants],
        [tenant.namespace for tenant in tenants],
        [tenant.vip for tenant in tenants],
        [tenant.pod_cidr for tenant in tenants],
        [tenant.service_cidr for tenant in tenants],
        [tenant.dns_ip for tenant in tenants],
        [tenant.domain for tenant in tenants],
        [tenant.cnpg_cluster for tenant in tenants],
    )
    if any(len(values) != len(set(values)) for values in identities):
        raise IntegrityError("tenant overlays do not define distinct identities")
    return tenants


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
        "WORKER_REPLICAS": str(tenant.workers),
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


def validate_tenant_kubeconfig_view(
    config: dict[str, str],
    tenant: Tenant,
    view: dict[str, object],
    expected_ca: bytes,
) -> None:
    contexts = {
        item["name"]: item["context"] for item in view.get("contexts", [])
    }
    clusters = {
        item["name"]: item["cluster"] for item in view.get("clusters", [])
    }
    current = contexts.get(view.get("current-context"), {})
    selected = clusters.get(current.get("cluster"), {})
    expected_server = f"https://{tenant.vip}:{config['SPIKE_API_PORT']}"
    if (
        selected.get("server") != expected_server
        or not selected.get("certificate-authority-data")
        or base64.b64decode(selected["certificate-authority-data"])
        != expected_ca
    ):
        raise RuntimeError(
            "tenant kubeconfig active context does not match its endpoint and CA"
        )


def validate_tenant_kubeconfig_file(
    root: Path,
    config: dict[str, str],
    client: ManagementClient,
    tenant: Tenant,
    *,
    check_access: bool = True,
) -> Path:
    path = tenant_kubeconfig_path(root, tenant)
    details = path.lstat()
    if (
        path.is_symlink()
        or not path.is_file()
        or details.st_uid != os.getuid()
        or details.st_mode & 0o077
    ):
        raise RuntimeError("tenant kubeconfig is not an owner-only regular file")
    ca_secret = json.loads(
        client.kubectl(
            "-n",
            tenant.namespace,
            "get",
            f"secret/{tenant.name}-ca",
            "-o",
            "json",
        ).stdout
    )
    if ca_secret.get("type") != "Opaque":
        raise RuntimeError("tenant CA Secret type is unexpected")
    view = json.loads(
        run(
            [
                str(root / ".tools" / "bin" / "kubectl"),
                "--kubeconfig",
                str(path),
                "config",
                "view",
                "--raw",
                "-o",
                "json",
            ],
            timeout=parse_duration(config["COMMAND_TIMEOUT"]),
        ).stdout
    )
    validate_tenant_kubeconfig_view(
        config,
        tenant,
        view,
        base64.b64decode(ca_secret.get("data", {}).get("ca.crt", "")),
    )
    if check_access:
        checks = (
            ("patch", "configmaps", "--namespace=kube-system"),
            ("patch", "customresourcedefinitions.apiextensions.k8s.io"),
            ("delete", "namespaces"),
        )
        for verb, resource, *scope in checks:
            authorized = run(
                [
                    str(root / ".tools" / "bin" / "kubectl"),
                    "--kubeconfig",
                    str(path),
                    "--request-timeout",
                    config["KUBECTL_REQUEST_TIMEOUT"],
                    "auth",
                    "can-i",
                    verb,
                    resource,
                    *scope,
                ],
                timeout=parse_duration(config["COMMAND_TIMEOUT"]),
            ).stdout.strip()
            if authorized != "yes":
                raise RuntimeError(
                    f"tenant kubeconfig lacks required {verb} {resource} access"
                )
    return path


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
    if secret.get("type") != "cluster.x-k8s.io/secret":
        raise RuntimeError("tenant kubeconfig Secret type is unexpected")
    validate_tenant_kubeconfig_file(root, config, client, tenant)
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


def ensure_tenant_kubeconfig(
    root: Path,
    config: dict[str, str],
    client: ManagementClient,
    tenant: Tenant,
) -> Path:
    path = tenant_kubeconfig_path(root, tenant)
    if path.exists() or path.is_symlink():
        try:
            validate_tenant_kubeconfig_file(
                root,
                config,
                client,
                tenant,
                check_access=False,
            )
        except RuntimeError:
            return export_tenant_kubeconfig(root, config, client, tenant)
        validate_tenant_kubeconfig_file(root, config, client, tenant)
        return path
    return export_tenant_kubeconfig(root, config, client, tenant)


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
    ensure_private_dir(root / ".runtime" / "storage")
    volume_name = storage_volume_name(config, tenant)
    payload = inspect_storage_volume(volume_name)
    introduced = payload is None
    if payload is None:
        run(
            [
                "docker",
                "volume",
                "create",
                "--label",
                f"{config['OWNERSHIP_LABEL']}={config['LAB_PREFIX']}",
                "--label",
                "cnpg-vcluster.capi/role=tenant-storage",
                "--label",
                f"cnpg-vcluster.capi/tenant={tenant.name}",
                volume_name,
            ],
            timeout=30,
        )
        payload = inspect_storage_volume(volume_name)
        if payload is None:
            raise RuntimeError("tenant storage volume was not created")
    labels = payload.get("Labels") or {}
    if (
        labels.get(config["OWNERSHIP_LABEL"]) != config["LAB_PREFIX"]
        or labels.get("cnpg-vcluster.capi/role") != "tenant-storage"
        or labels.get("cnpg-vcluster.capi/tenant") != tenant.name
    ):
        raise RuntimeError("tenant storage Docker volume ownership cannot be proven")
    tenant.storage_host_path = Path(payload["Mountpoint"])
    record = {
        "schema": 1,
        "tenant": tenant.name,
        "volumeName": volume_name,
        "createdAt": payload["CreatedAt"],
        "mountpoint": payload["Mountpoint"],
    }
    record_path = storage_record_path(root, tenant)
    if introduced:
        if record_path.exists() or record_path.is_symlink():
            raise RuntimeError("stale tenant storage identity record blocks creation")
        write_private_file(record_path, json.dumps(record, sort_keys=True) + "\n")
    elif record_path.exists() or record_path.is_symlink():
        details = record_path.lstat()
        if (
            record_path.is_symlink()
            or not record_path.is_file()
            or details.st_uid != os.getuid()
            or details.st_mode & 0o077
            or json.loads(record_path.read_text(encoding="utf-8")) != record
        ):
            raise RuntimeError("tenant storage volume identity record mismatch")
    else:
        raise RuntimeError("existing tenant storage volume has no identity record")


def storage_volume_name(config: dict[str, str], tenant: Tenant) -> str:
    return f"{config['LAB_PREFIX']}-{tenant.name}-storage"


def storage_record_path(root: Path, tenant: Tenant) -> Path:
    return root / ".runtime" / "storage" / tenant.name / "volume.json"


def inspect_storage_volume(name: str) -> dict[str, object] | None:
    response = run(
        ["docker", "volume", "inspect", name],
        timeout=30,
        check=False,
    )
    if response.returncode == 0:
        payload = json.loads(response.stdout)
        if len(payload) != 1:
            raise RuntimeError(f"unexpected Docker volume inventory for {name}")
        return payload[0]
    if "no such volume" in response.stderr.lower():
        return None
    raise RuntimeError(f"Docker volume inspection failed for {name}: {response.stderr}")


def write_storage_marker(
    config: dict[str, str],
    tenant: Tenant,
    relative_path: str,
    value: str,
) -> None:
    run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{storage_volume_name(config, tenant)}:/data",
            "-e",
            f"CAPI_MARKER_VALUE={value}",
            "--entrypoint",
            "sh",
            config["VERIFY_IMAGE"],
            "-ec",
            f"mkdir -p \"$(dirname '/data/{relative_path}')\"; "
            f"printf '%s' \"$CAPI_MARKER_VALUE\" > '/data/{relative_path}'",
        ],
        timeout=60,
    )


def read_storage_marker(
    config: dict[str, str],
    tenant: Tenant,
    relative_path: str,
) -> str:
    return run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{storage_volume_name(config, tenant)}:/data:ro",
            "--entrypoint",
            "cat",
            config["VERIFY_IMAGE"],
            f"/data/{relative_path}",
        ],
        timeout=60,
    ).stdout


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
    root: Path,
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
        (root / ".runtime" / "management" / "network.json").read_text(
            encoding="utf-8"
        )
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
            "devMachineUID": devmachine["metadata"]["uid"] if devmachine else None,
            "containerID": container.stdout.strip() if container.returncode == 0 else None,
            "nodeUID": node["metadata"]["uid"] if node else None,
        }
    return resources


def remove_tenant_storage_volume(
    root: Path,
    config: dict[str, str],
    tenant: Tenant,
) -> None:
    volume_name = storage_volume_name(config, tenant)
    payload = inspect_storage_volume(volume_name)
    record_path = storage_record_path(root, tenant)
    if payload is not None:
        labels = payload.get("Labels") or {}
        if (
            labels.get(config["OWNERSHIP_LABEL"]) != config["LAB_PREFIX"]
            or labels.get("cnpg-vcluster.capi/role") != "tenant-storage"
            or labels.get("cnpg-vcluster.capi/tenant") != tenant.name
            or not record_path.is_file()
        ):
            raise RuntimeError("refusing to remove unproven tenant storage volume")
        details = record_path.lstat()
        expected_record = {
            "schema": 1,
            "tenant": tenant.name,
            "volumeName": volume_name,
            "createdAt": payload.get("CreatedAt"),
            "mountpoint": payload.get("Mountpoint"),
        }
        if (
            record_path.is_symlink()
            or not record_path.is_file()
            or details.st_uid != os.getuid()
            or details.st_mode & 0o077
            or json.loads(record_path.read_text(encoding="utf-8"))
            != expected_record
        ):
            raise RuntimeError("tenant storage volume identity changed")
        run(["docker", "volume", "rm", volume_name], timeout=30)
        record_path.unlink()
    elif record_path.exists() or record_path.is_symlink():
        details = record_path.lstat()
        record = (
            json.loads(record_path.read_text(encoding="utf-8"))
            if record_path.is_file() and not record_path.is_symlink()
            else {}
        )
        if (
            record_path.is_symlink()
            or not record_path.is_file()
            or details.st_uid != os.getuid()
            or details.st_mode & 0o077
            or record.get("schema") != 1
            or record.get("tenant") != tenant.name
            or record.get("volumeName") != volume_name
            or not record.get("createdAt")
            or not record.get("mountpoint")
        ):
            raise RuntimeError("orphaned tenant storage identity record is invalid")
        record_path.unlink()
    for directory in (record_path.parent, record_path.parent.parent):
        try:
            directory.rmdir()
        except OSError:
            pass


def delete_tenant(
    root: Path,
    config: dict[str, str],
    client: ManagementClient,
    tenant: Tenant,
) -> None:
    verify_tenant_management_ownership(config, client, tenant)
    if tenant_kubeconfig_path(root, tenant).is_file():
        addon = _tenant_kubectl(
            root,
            config,
            tenant,
            "-n",
            "kube-system",
            "get",
            "daemonset/capi-kube-proxy",
            check=False,
        )
        if addon.returncode == 0:
            raise RuntimeError(
                "tenant add-ons must be deleted through the live API before Cluster deletion"
            )
        if not re.search(
            r"Error from server \(NotFound\):",
            addon.stderr,
            re.IGNORECASE,
        ):
            raise RuntimeError(
                f"tenant add-on inspection failed before deletion: {addon.stderr}"
            )
        for storage_resource in (
            "deployment/storage-smoke",
            "pvc/storage-smoke",
            f"pv/{tenant.name}-storage-smoke",
            f"storageclass/{config['SPIKE_STORAGE_CLASS']}",
        ):
            response = _tenant_kubectl(
                root,
                config,
                tenant,
                "get",
                storage_resource,
                check=False,
            )
            if response.returncode == 0:
                raise RuntimeError(
                    "tenant storage API resources must be deleted before Cluster deletion"
                )
            if not re.search(
                r"Error from server \(NotFound\):",
                response.stderr,
                re.IGNORECASE,
            ):
                raise RuntimeError(
                    f"tenant storage inspection failed before deletion: "
                    f"{response.stderr}"
                )
        cnpg_resources = (
            (("-n", config["DATABASE_NAMESPACE"]), f"cluster/{tenant.cnpg_cluster}"),
            (("-n", config["DATABASE_NAMESPACE"]), "pvc"),
            ((), f"pv/{tenant.cnpg_cluster}-pv-1"),
            ((), f"pv/{tenant.cnpg_cluster}-pv-2"),
            ((), f"pv/{tenant.cnpg_cluster}-pv-3"),
            (("-n", config["CNPG_NAMESPACE"]), "deployment/cnpg-controller-manager"),
            ((), "crd/clusters.postgresql.cnpg.io"),
        )
        cnpg_present = False
        for scope, resource in cnpg_resources:
            arguments = [*scope, "get", resource]
            is_list = resource == "pvc"
            if is_list:
                arguments.extend(
                    (
                        "-l",
                        f"cnpg.io/cluster={tenant.cnpg_cluster}",
                        "-o",
                        "name",
                    )
                )
            response = _tenant_kubectl(
                root,
                config,
                tenant,
                *arguments,
                check=False,
            )
            if response.returncode == 0 and (
                not is_list or response.stdout.strip()
            ):
                cnpg_present = True
                break
            if (
                response.returncode != 0
                and not re.search(
                    r"Error from server \(NotFound\):",
                    response.stderr,
                    re.IGNORECASE,
                )
            ):
                raise RuntimeError(
                    f"CNPG artifact inspection failed for {resource}: "
                    f"{response.stderr}"
                )
        if cnpg_present:
            raise RuntimeError(
                "tenant CNPG resources must be deleted before Cluster deletion"
            )
    cluster_delete = client.kubectl(
        "-n",
        tenant.namespace,
        "delete",
        f"cluster/{tenant.name}",
        "--ignore-not-found",
        "--wait=true",
        f"--timeout={config['DELETE_TIMEOUT']}",
        check=False,
    )
    if cluster_delete.returncode != 0 and not NOT_FOUND.search(
        cluster_delete.stderr
    ):
        raise RuntimeError(f"tenant Cluster deletion failed: {cluster_delete.stderr}")
    namespace_delete = client.kubectl(
        "delete",
        "namespace",
        tenant.namespace,
        "--ignore-not-found",
        "--wait=true",
        f"--timeout={config['DELETE_TIMEOUT']}",
        check=False,
    )
    if namespace_delete.returncode != 0 and not NOT_FOUND.search(
        namespace_delete.stderr
    ):
        raise RuntimeError(
            f"tenant namespace deletion failed: {namespace_delete.stderr}"
        )

    def namespace_absent():
        response = client.kubectl(
            "get",
            f"namespace/{tenant.namespace}",
            check=False,
        )
        if response.returncode == 0:
            return None
        if NOT_FOUND.search(response.stderr):
            return True
        raise RuntimeError(
            f"tenant namespace deletion inspection failed: {response.stderr}"
        )

    wait_for(
        f"namespace {tenant.namespace} deletion",
        parse_duration(config["DELETE_TIMEOUT"]),
        parse_duration(config["WAIT_POLL_INTERVAL"]),
        namespace_absent,
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
    try:
        kubeconfig.parent.rmdir()
    except OSError:
        pass
    rendered = root / ".runtime" / "rendered" / "tenants" / tenant.name
    shutil.rmtree(rendered, ignore_errors=True)
    remove_tenant_storage_volume(root, config, tenant)
