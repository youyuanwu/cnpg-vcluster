from __future__ import annotations

import base64
import ipaddress
import json
import os
import re
import shutil
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from .conditions import condition_true
from .config import parse_duration
from .files import (
    IntegrityError,
    ensure_private_dir,
    private_file_exists,
    read_private_file,
    write_private_file,
)
from .kube import ManagementClient, wait_for
from .process import run
from .images import WORKER_IMAGE_KEYS, verify_container_images
from .tenant_spec import (
    TenantSpec,
    load_tenant_spec,
    require_non_overlapping_networks,
)
from .tenant_runtime import (
    OperationJournal,
    TenantRuntime,
    foundation_sha256,
    recorded_tenant_names,
)


LIFECYCLE_MARKERS = {
    "tenant": "lifecycle.cnpg-vcluster.capi/tenant",
    "profile": "lifecycle.cnpg-vcluster.capi/profile",
    "specificationSha256": "lifecycle.cnpg-vcluster.capi/specification-sha256",
    "foundationSha256": "lifecycle.cnpg-vcluster.capi/foundation-sha256",
    "operationId": "lifecycle.cnpg-vcluster.capi/operation-id",
}


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
    database_count: int = 3
    specification_sha256: str = ""
    lifecycle_markers: Mapping[str, str] = field(default_factory=dict)


def lifecycle_markers(
    spec: TenantSpec,
    journal: OperationJournal,
) -> dict[str, str]:
    marker_operation = journal.observed.get(
        "markerOperationId",
        journal.operation_id,
    )
    return {
        "tenant": spec.name,
        "profile": spec.profile,
        "specificationSha256": spec.sha256(),
        "foundationSha256": foundation_sha256(journal.foundation_identity),
        "operationId": marker_operation,
    }


def resource_lifecycle_markers(payload: Mapping[str, object]) -> dict[str, str]:
    metadata = payload.get("metadata")
    annotations = (
        metadata.get("annotations")
        if isinstance(metadata, dict)
        else None
    )
    values = annotations if isinstance(annotations, dict) else {}
    return {
        name: str(values.get(key, ""))
        for name, key in LIFECYCLE_MARKERS.items()
    }


def _require_resource_markers(
    payload: Mapping[str, object],
    expected: Mapping[str, str],
    description: str,
) -> None:
    if resource_lifecycle_markers(payload) != dict(expected):
        raise RuntimeError(
            f"tenant lifecycle marker mismatch: {description}"
        )


NOT_FOUND = re.compile(r"Error from server \(NotFound\):", re.IGNORECASE)
MANAGEMENT_IDENTITY_KEYS = {
    "namespace": "namespaceUID",
    "cluster": "clusterUID",
    "devcluster": "devClusterUID",
    "kamajicontrolplane": "controlPlaneUID",
    "machinedeployment": "machineDeploymentUID",
    "kubeadmconfigtemplate": "kubeadmTemplateUID",
    "devmachinetemplate": "devMachineTemplateUID",
}


def management_resource_identities(
    resources: Mapping[str, object],
) -> dict[str, str]:
    return {
        observed_key: str(resources[kind]["metadata"]["uid"])
        for kind, observed_key in MANAGEMENT_IDENTITY_KEYS.items()
        if isinstance(resources.get(kind), dict)
    }


def require_recorded_management_identities(
    resources: Mapping[str, object],
    observed: Mapping[str, str],
    *,
    require_present: bool,
) -> None:
    current = management_resource_identities(resources)
    changed = []
    absent = []
    for observed_key in MANAGEMENT_IDENTITY_KEYS.values():
        recorded = observed.get(observed_key)
        if recorded is None:
            continue
        actual = current.get(observed_key)
        if actual is None:
            if require_present:
                absent.append(observed_key)
        elif actual != recorded:
            changed.append(observed_key)
    if changed:
        raise RuntimeError(
            "tenant management identity changed: " + ", ".join(sorted(changed))
        )
    if absent:
        raise RuntimeError(
            "recorded tenant management resource is absent: "
            + ", ".join(sorted(absent))
        )


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
    *,
    expected_markers: Mapping[str, str] | None = None,
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
        if expected_markers is not None:
            _require_resource_markers(
                namespace,
                expected_markers,
                f"namespace/{tenant.namespace}",
            )
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
        if expected_markers is not None:
            _require_resource_markers(
                payload,
                expected_markers,
                f"{kind}/{name}",
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
    check_access: bool = False,
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
    validate_tenant_kubeconfig_file(
        root,
        config,
        client,
        tenant,
        check_access=False,
    )
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
            export_tenant_kubeconfig(root, config, client, tenant)
        else:
            validate_tenant_kubeconfig_file(
                root,
                config,
                client,
                tenant,
                check_access=True,
            )
            return path
    else:
        export_tenant_kubeconfig(root, config, client, tenant)
    validate_tenant_kubeconfig_file(
        root,
        config,
        client,
        tenant,
        check_access=True,
    )
    return path


def _tenant_kubectl(
    root: Path,
    config: dict[str, str],
    tenant: Tenant,
    *arguments: str,
    check: bool = True,
    input_text: str | None = None,
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
        input_text=input_text,
        check=check,
    )


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
    resources: dict[str, dict[str, object]] | None = None,
) -> None:
    resources = resources or {}
    cluster = resources.get("cluster") or _resource(
        client, tenant, "cluster", tenant.name
    )
    devcluster = resources.get("devcluster") or _resource(
        client, tenant, "devcluster", tenant.name
    )
    kcp = resources.get("kamajicontrolplane") or _resource(
        client, tenant, "kamajicontrolplane", tenant.name
    )
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
    verify_container_images(config, machine_name, WORKER_IMAGE_KEYS)


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
