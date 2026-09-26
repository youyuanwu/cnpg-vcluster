from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import time
from pathlib import Path

from .config import parse_duration
from .controller_client import apply_tenant, delete_tenant
from .kube import ManagementClient, wait_for
from .process import run
from .redaction import redact, redact_value
from .tenants import Tenant, export_tenant_kubeconfig, tenant_kubeconfig_path
from scripts.controller_tenant_status import evaluate_tenant


MARKER = "tenancy.cnpg-vcluster.io/"
LEASE_NAMESPACE = "tenant-system"


def tenant_spec_hash(document: dict[str, object]) -> str:
    spec = document.get("spec")
    if not isinstance(spec, dict) or set(spec) != {"kubernetesVersion", "workers", "databases"}:
        raise RuntimeError("Tenant spec is not the v1alpha2 contract")
    version = spec["kubernetesVersion"]
    if not isinstance(version, str) or not re.fullmatch(r"v?[0-9]+\.[0-9]+\.[0-9]+", version):
        raise RuntimeError("Tenant Kubernetes version is invalid")
    if any(type(spec[field]) is not int or not 1 <= spec[field] <= 3 for field in ("workers", "databases")):
        raise RuntimeError("Tenant counts are invalid")
    canonical = {
        "kubernetesVersion": version.removeprefix("v"),
        "workers": spec["workers"],
        "databases": spec["databases"],
    }
    return hashlib.sha256(json.dumps(canonical, separators=(",", ":")).encode()).hexdigest()


def tenant_allocation(document: dict[str, object]) -> dict[str, str]:
    status = document.get("status")
    allocation = status.get("allocation") if isinstance(status, dict) else None
    try:
        if not isinstance(allocation, dict) or set(allocation) != {"slotId", "endpoint", "podCIDR", "serviceCIDR"}:
            raise ValueError
        if not isinstance(allocation["slotId"], str) or not re.fullmatch(
            r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", allocation["slotId"],
        ):
            raise ValueError
        address = ipaddress.IPv4Address(allocation["endpoint"])
        pod = ipaddress.IPv4Network(allocation["podCIDR"])
        service = ipaddress.IPv4Network(allocation["serviceCIDR"])
        if (
            str(address) != allocation["endpoint"]
            or str(pod) != allocation["podCIDR"]
            or str(service) != allocation["serviceCIDR"]
            or pod.overlaps(service) or address in pod or address in service
            or service.num_addresses <= 10
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("Tenant status allocation is invalid or absent") from exc
    return dict(allocation)


def allocation_lease_name(slot_id: str) -> str:
    return "tenant-slot-" + hashlib.sha256(slot_id.encode()).hexdigest()[:51]


def allocation_lease_manifest(
    config: dict[str, str], document: dict[str, object],
) -> dict[str, object]:
    allocation = tenant_allocation(document)
    metadata = document["metadata"]
    foundation_hash = document["status"].get("foundationHash")
    if not metadata.get("name") or not metadata.get("uid") or not foundation_hash:
        raise RuntimeError("Tenant allocation identity is incomplete")
    return {
        "apiVersion": "coordination.k8s.io/v1",
        "kind": "Lease",
        "metadata": {
            "name": allocation_lease_name(allocation["slotId"]),
            "namespace": LEASE_NAMESPACE,
            "labels": {
                config["OWNERSHIP_LABEL"]: config["LAB_PREFIX"],
                MARKER + "slot-id": allocation["slotId"],
                MARKER + "tenant": metadata["name"],
            },
            "annotations": {
                MARKER + "tenant": metadata["name"],
                MARKER + "tenant-uid": metadata["uid"],
                MARKER + "spec-hash": tenant_spec_hash(document),
                MARKER + "foundation-hash": foundation_hash,
                MARKER + "resource": "allocation-lease",
                MARKER + "slot-id": allocation["slotId"],
                MARKER + "endpoint": allocation["endpoint"],
                MARKER + "pod-cidr": allocation["podCIDR"],
                MARKER + "service-cidr": allocation["serviceCIDR"],
            },
        },
    }


def allocation_leases(client: ManagementClient) -> list[dict[str, object]]:
    payload = client.json("-n", LEASE_NAMESPACE, "get", "leases.coordination.k8s.io")
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise RuntimeError("invalid allocation Lease inventory")
    if any(
        not isinstance(item, dict)
        or not isinstance(item.get("metadata"), dict)
        or not item["metadata"].get("name")
        or not item["metadata"].get("uid")
        or not isinstance(item["metadata"].get("annotations", {}), dict)
        for item in payload["items"]
    ):
        raise RuntimeError("invalid allocation Lease identity")
    return payload["items"]


def verify_allocation_lease(
    config: dict[str, str], client: ManagementClient, document: dict[str, object],
) -> dict[str, object]:
    expected = allocation_lease_manifest(config, document)["metadata"]
    inventory = allocation_leases(client)
    same_uid = [
        lease for lease in inventory
        if lease["metadata"].get("annotations", {}).get(MARKER + "tenant-uid")
        == document["metadata"]["uid"]
    ]
    matches = [lease for lease in inventory if lease["metadata"]["name"] == expected["name"]]
    if len(matches) != 1 or same_uid != matches:
        raise RuntimeError("Tenant allocation Lease is absent, replaced, or duplicated")
    lease = matches[0]
    metadata = lease["metadata"]
    if (
        any(metadata.get(field) != expected[field] for field in ("name", "namespace", "labels", "annotations"))
        or not metadata.get("resourceVersion") or metadata.get("ownerReferences")
        or metadata.get("deletionTimestamp") or lease.get("spec") not in (None, {})
    ):
        raise RuntimeError("Tenant allocation Lease identity changed")
    return {
        "name": metadata["name"], "uid": metadata["uid"],
        "namespace": metadata["namespace"],
        "labels": metadata["labels"], "annotations": metadata["annotations"],
    }


def verify_allocation_released(client: ManagementClient, identity: dict[str, object]) -> None:
    lease_identity = identity["allocationLease"]
    for lease in allocation_leases(client):
        metadata = lease["metadata"]
        annotations = metadata.get("annotations", {})
        if (
            metadata["name"] == lease_identity["name"]
            or metadata["uid"] == lease_identity["uid"]
            or annotations.get(MARKER + "tenant-uid") == identity["uid"]
            or annotations.get(MARKER + "tenant") == identity["name"]
        ):
            raise RuntimeError(f"Tenant allocation Lease remained after deletion: {identity['name']}")


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
    last_transition: dict[str, object] | None = None
    started = time.monotonic()

    def ready():
        nonlocal last_result, last_transition
        document = tenant_document(client, name)
        if document is None:
            last_result = {"classification": "absent", "blockers": ["Tenant is absent"]}
        else:
            last_result = evaluate_tenant(document)
        raw_conditions = last_result.get("conditions", [])
        conditions = sorted(
            (
                {
                    field: condition.get(field)
                    for field in ("type", "status", "reason", "observedGeneration")
                }
                for condition in (
                    raw_conditions if isinstance(raw_conditions, list) else []
                )
                if isinstance(condition, dict)
            ),
            key=lambda condition: str(condition["type"]),
        )
        metadata = document.get("metadata") if document else None
        if not isinstance(metadata, dict):
            metadata = {}
        transition = {
            "classification": last_result["classification"],
            "generation": metadata.get("generation"),
            "observedGeneration": last_result.get("observedGeneration"),
            "conditions": redact_value(conditions),
        }
        if transition != last_transition:
            print(
                "CAPI_TENANT_TRANSITION "
                + json.dumps(
                    redact_value({
                        "schema": 1,
                        "tenant": name,
                        "seconds": round(time.monotonic() - started, 3),
                        **transition,
                    }),
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                flush=True,
            )
            last_transition = transition
        return document if last_result["classification"] == "ready" else None

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
            f"{exc}: {json.dumps(redact_value(last_result), sort_keys=True)}"
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
    allocation = tenant_allocation(document)
    specification_sha256 = tenant_spec_hash(document)
    try:
        address = allocation["endpoint"]
        service = ipaddress.ip_network(allocation["serviceCIDR"])
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
    return Tenant(
        name=name,
        namespace=name,
        vip=address,
        pod_cidr=allocation["podCIDR"],
        service_cidr=allocation["serviceCIDR"],
        dns_ip=dns_ip,
        domain=config["SPIKE_CLUSTER_DOMAIN"],
        storage_host_path=Path(str(volume[0]["Mountpoint"])),
        cnpg_cluster=config["SPIKE_CNPG_CLUSTER"],
        workers=int(spec["workers"]),
        database_count=int(spec["databases"]),
        specification_sha256=specification_sha256,
    )


def apply_controller_tenant(
    root: Path,
    config: dict[str, str],
    manifest: Path,
) -> tuple[ManagementClient, Tenant, dict[str, object]]:
    apply_tenant(root, config, manifest)
    document = wait_tenant_ready(root, config, manifest_tenant_name(manifest))
    verify_allocation_lease(config, ManagementClient(root, config), document)
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
                "--no-trunc",
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
    containers = sorted(run(
        [
            "docker", "ps", "-a", "--no-trunc", "--filter",
            f"label=io.x-k8s.kind.cluster={name}", "--format", "{{.Names}} {{.ID}}",
        ],
        timeout=30,
    ).stdout.splitlines())
    return {
        "uid": metadata.get("uid"),
        "allocation": tenant_allocation(document),
        "allocationLease": verify_allocation_lease(config, client, document),
        "foundationHash": status.get("foundationHash"),
        "clusterUID": status.get("clusterUID"),
        "managementResources": sorted(management_resources),
        "dockerVolume": {
            "name": volume.get("Name"),
            "createdAt": volume.get("CreatedAt"),
            "mountpoint": volume.get("Mountpoint"),
            "labels": volume.get("Labels"),
        },
        "workerContainers": workers,
        "providerContainers": containers,
    }
