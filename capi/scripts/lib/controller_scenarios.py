from __future__ import annotations

import ipaddress
import json
import re
from pathlib import Path

from .config import parse_duration
from .controller_client import apply_tenant, delete_tenant
from .kube import ManagementClient, wait_for
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
    volume = status.get("dockerVolume")
    volume_path = (
        Path(str(volume.get("mountpoint")))
        if isinstance(volume, dict) and volume.get("mountpoint")
        else Path("/")
    )
    return Tenant(
        name=name,
        namespace=name,
        vip=address,
        pod_cidr=str(spec["podCIDR"]),
        service_cidr=str(spec["serviceCIDR"]),
        dns_ip=dns_ip,
        domain=config["SPIKE_CLUSTER_DOMAIN"],
        storage_host_path=volume_path,
        cnpg_cluster=config["SPIKE_CNPG_CLUSTER"],
        workers=int(spec["workers"]),
        database_count=int(spec["databaseCount"]),
        specification_sha256=str(status.get("specHash", "")),
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


def tenant_snapshot(document: dict[str, object]) -> dict[str, object]:
    metadata = document.get("metadata")
    status = document.get("status")
    if not isinstance(metadata, dict) or not isinstance(status, dict):
        raise RuntimeError("Tenant document is missing metadata or status")
    return {
        "uid": metadata.get("uid"),
        "endpoint": status.get("endpoint"),
        "specHash": status.get("specHash"),
        "foundationHash": status.get("foundationHash"),
        "observedResources": sorted(
            (
                item.get("apiVersion"),
                item.get("kind"),
                item.get("namespace", ""),
                item.get("name"),
                item.get("uid"),
            )
            for item in status.get("observedResources", [])
            if isinstance(item, dict)
        ),
        "tenantResources": sorted(
            (
                item.get("apiVersion"),
                item.get("kind"),
                item.get("namespace", ""),
                item.get("name"),
                item.get("uid"),
            )
            for item in status.get("tenantResources", [])
            if isinstance(item, dict)
        ),
        "dockerVolume": status.get("dockerVolume"),
        "workerContainers": sorted(
            (item.get("name"), item.get("id"))
            for item in status.get("workerContainers", [])
            if isinstance(item, dict)
        ),
    }
