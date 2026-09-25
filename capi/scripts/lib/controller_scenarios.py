from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from pathlib import Path

from .config import parse_duration
from .controller_client import apply_tenant, delete_tenant
from .kube import ManagementClient, wait_for
from .process import run
from .tenants import Tenant, export_tenant_kubeconfig, tenant_kubeconfig_path
from scripts.controller_tenant_status import evaluate_tenant


def tenant_manifest(root: Path, name: str) -> Path:
    if name == "tenant-example":
        return root / "config" / "tenants" / "examples" / "local.yaml"
    return root / "config" / "tenants" / "tests" / f"{name}.yaml"


def manifest_tenant_name(manifest: Path) -> str:
    match = re.search(
        r"(?m)^metadata:\s*\n(?:^[ \t].*\n)*?^[ \t]+name:\s*([a-z0-9-]+)\s*$",
        manifest.read_text(encoding="utf-8"),
    )
    if match is None:
        raise RuntimeError(f"Tenant manifest has no metadata.name: {manifest}")
    return match.group(1)


def tenant_document(
    client: ManagementClient,
    name: str,
) -> dict[str, object] | None:
    response = client.kubectl("get", f"tenant/{name}", "-o", "json", check=False)
    if response.returncode != 0:
        if "NotFound" in response.stderr:
            return None
        raise RuntimeError(
            f"Tenant inspection failed for {name}: {response.stderr}"
        )
    payload = json.loads(response.stdout)
    if not isinstance(payload, dict):
        raise RuntimeError(f"Tenant API returned an invalid document: {name}")
    return payload


def wait_tenant_ready(
    root: Path,
    config: dict[str, str],
    name: str,
) -> dict[str, object]:
    client = ManagementClient(root, config)
    last_result: dict[str, object] = {}

    def ready():
        nonlocal last_result
        document = tenant_document(client, name)
        if document is None:
            last_result = {"classification": "absent", "blockers": ["Tenant is absent"]}
            return None
        result = evaluate_tenant(document)
        last_result = result
        return document if result["classification"] == "ready" else None

    try:
        return wait_for(
            f"Tenant {name} Ready",
            (
                parse_duration(config["TENANT_CONTROL_PLANE_TIMEOUT"])
                + parse_duration(config["WORKER_REGISTRATION_TIMEOUT"])
                + parse_duration(config["CNPG_TIMEOUT"])
            ),
            parse_duration(config["WAIT_POLL_INTERVAL"]),
            ready,
        )
    except RuntimeError as exc:
        raise RuntimeError(
            f"{exc}: {json.dumps(last_result, sort_keys=True)}"
        ) from exc


def wait_tenant_absent(
    root: Path,
    config: dict[str, str],
    name: str,
) -> None:
    client = ManagementClient(root, config)
    last_status: dict[str, object] = {}

    def absent():
        nonlocal last_status
        document = tenant_document(client, name)
        if document is None:
            return True
        status = document.get("status")
        last_status = status if isinstance(status, dict) else {}
        return None

    try:
        wait_for(
            f"Tenant {name} absence",
            parse_duration(config["DELETE_TIMEOUT"]),
            parse_duration(config["WAIT_POLL_INTERVAL"]),
            absent,
        )
    except RuntimeError as exc:
        raise RuntimeError(
            f"{exc}: {json.dumps(last_status, sort_keys=True)}"
        ) from exc


def tenant_from_document(
    root: Path,
    config: dict[str, str],
    document: dict[str, object],
) -> Tenant:
    metadata = document.get("metadata")
    spec = document.get("spec")
    status = document.get("status")
    if not all(isinstance(value, dict) for value in (metadata, spec, status)):
        raise RuntimeError("Tenant document is missing metadata, spec, or status")
    name = str(metadata.get("name", ""))
    endpoint = str(status.get("endpoint", ""))
    try:
        address, port = endpoint.rsplit(":", 1)
        if int(port) != int(config["SPIKE_API_PORT"]):
            raise ValueError
        service = ipaddress.ip_network(str(spec["serviceCIDR"]))
        dns_ip = str(service.network_address + 10)
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"Tenant endpoint or network is invalid: {name}") from exc
    volume_name = f"{config['LAB_PREFIX']}-{name}-storage"
    volume = json.loads(
        run(
            ["docker", "volume", "inspect", volume_name],
            timeout=30,
        ).stdout
    )
    if (
        not isinstance(volume, list)
        or len(volume) != 1
        or not isinstance(volume[0], dict)
        or not volume[0].get("Mountpoint")
    ):
        raise RuntimeError(f"Tenant Docker volume is invalid: {name}")
    canonical = {
        "kubernetesVersion": str(spec["kubernetesVersion"]).removeprefix("v"),
        "workers": int(spec["workers"]),
        "databaseCount": int(spec["databaseCount"]),
        "podCIDR": str(ipaddress.ip_network(str(spec["podCIDR"]))),
        "serviceCIDR": str(ipaddress.ip_network(str(spec["serviceCIDR"]))),
    }
    specification_sha256 = hashlib.sha256(
        json.dumps(canonical, separators=(",", ":")).encode()
    ).hexdigest()
    return Tenant(
        name=name,
        namespace=name,
        vip=address,
        pod_cidr=str(spec["podCIDR"]),
        service_cidr=str(spec["serviceCIDR"]),
        dns_ip=dns_ip,
        domain=config["SPIKE_CLUSTER_DOMAIN"],
        storage_host_path=Path(str(volume[0]["Mountpoint"])),
        cnpg_cluster=config["SPIKE_CNPG_CLUSTER"],
        workers=int(spec["workers"]),
        database_count=int(spec["databaseCount"]),
        specification_sha256=specification_sha256,
    )


def apply_controller_tenant(
    root: Path,
    config: dict[str, str],
    manifest: Path,
) -> tuple[ManagementClient, Tenant, dict[str, object]]:
    apply_tenant(root, config, manifest)
    document = wait_tenant_ready(root, config, manifest_tenant_name(manifest))
    tenant = tenant_from_document(root, config, document)
    client = ManagementClient(root, config)
    export_tenant_kubeconfig(root, config, client, tenant)
    return client, tenant, document


def delete_controller_tenant(
    root: Path,
    config: dict[str, str],
    tenant: Tenant | str,
    *,
    wait: bool = True,
) -> None:
    name = tenant if isinstance(tenant, str) else tenant.name
    delete_tenant(root, config, name, wait=False)
    if wait:
        wait_tenant_absent(root, config, name)
    if wait:
        path = (
            tenant_kubeconfig_path(root, tenant)
            if isinstance(tenant, Tenant)
            else root / ".runtime" / "tenants" / name / "kubeconfig"
        )
        path.unlink(missing_ok=True)
        try:
            path.parent.rmdir()
        except OSError:
            pass


def tenant_snapshot(
    config: dict[str, str],
    client: ManagementClient,
    document: dict[str, object],
) -> dict[str, object]:
    metadata = document.get("metadata")
    status = document.get("status")
    if not isinstance(metadata, dict) or not isinstance(status, dict):
        raise RuntimeError("Tenant document is missing metadata or status")
    name = str(metadata.get("name", ""))
    if not name:
        raise RuntimeError("Tenant document has no metadata.name")
    management_resources = []
    for resource, namespace, object_name in (
        ("namespace", None, name),
        ("clusters.cluster.x-k8s.io", name, name),
        ("devclusters.infrastructure.cluster.x-k8s.io", name, name),
        ("kamajicontrolplanes.controlplane.cluster.x-k8s.io", name, name),
        ("kubeadmconfigtemplates.bootstrap.cluster.x-k8s.io", name, f"{name}-worker"),
        ("devmachinetemplates.infrastructure.cluster.x-k8s.io", name, f"{name}-worker"),
        ("machinedeployments.cluster.x-k8s.io", name, f"{name}-worker"),
        ("secret", name, f"{name}-kubeconfig"),
    ):
        arguments = []
        if namespace is not None:
            arguments.extend(["-n", namespace])
        arguments.extend(["get", f"{resource}/{object_name}", "-o", "json"])
        payload = json.loads(client.kubectl(*arguments).stdout)
        item_metadata = payload.get("metadata")
        if not isinstance(item_metadata, dict) or not item_metadata.get("uid"):
            raise RuntimeError(
                f"Tenant management identity is incomplete: {resource}/{object_name}"
            )
        management_resources.append(
            (
                resource,
                namespace or "",
                object_name,
                item_metadata["uid"],
            )
        )
    volume_name = f"{config['LAB_PREFIX']}-{name}-storage"
    volumes = json.loads(
        run(["docker", "volume", "inspect", volume_name], timeout=30).stdout
    )
    if not isinstance(volumes, list) or len(volumes) != 1:
        raise RuntimeError(f"Tenant Docker volume is missing: {name}")
    volume = volumes[0]
    workers = sorted(
        run(
            [
                "docker",
                "ps",
                "-a",
                "--filter",
                f"label=io.x-k8s.kind.cluster={name}",
                "--filter",
                "label=io.x-k8s.kind.role=worker",
                "--format",
                "{{.Names}} {{.ID}}",
            ],
            timeout=30,
        ).stdout.splitlines()
    )
    return {
        "uid": metadata.get("uid"),
        "endpoint": status.get("endpoint"),
        "foundationHash": status.get("foundationHash"),
        "clusterUID": status.get("clusterUID"),
        "tenantAPICreationAuthorized": status.get(
            "tenantAPICreationAuthorized", False
        ),
        "managementResources": sorted(management_resources),
        "dockerVolume": {
            "name": volume.get("Name"),
            "createdAt": volume.get("CreatedAt"),
            "mountpoint": volume.get("Mountpoint"),
            "labels": volume.get("Labels"),
        },
        "workerContainers": workers,
    }
