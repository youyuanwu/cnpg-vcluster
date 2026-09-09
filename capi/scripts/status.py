from __future__ import annotations

import json
from pathlib import Path

from scripts.lib.conditions import sanitized_condition_summary, condition_true
from scripts.lib.kube import ManagementClient
from scripts.lib.host import read_inotify
from scripts.lib.management import (
    management_auxiliary_status,
    management_component_status,
    management_status,
)
from scripts.lib.providers import provider_status
from scripts.lib.addons import network_status
from scripts.lib.process import run
from scripts.lib.tenants import (
    NOT_FOUND,
    _tenant_kubectl,
    configured_tenants,
    spike_tenant,
)
from scripts.lib.tenants import (
    inspect_storage_volume,
    storage_record_path,
    storage_volume_name,
)


def _machine_layer_status(root: Path, config: dict[str, str], client, tenant):
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
    desired = deployment["spec"]["replicas"]
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
    devmachines = json.loads(
        client.kubectl(
            "-n",
            tenant.namespace,
            "get",
            "devmachines",
            "-l",
            f"cluster.x-k8s.io/cluster-name={tenant.name}",
            "-o",
            "json",
        ).stdout
    )["items"]
    nodes = json.loads(
        _tenant_kubectl(root, config, tenant, "get", "nodes", "-o", "json").stdout
    )["items"]
    containers = run(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            f"label=io.x-k8s.kind.cluster={tenant.name}",
            "--filter",
            "label=io.x-k8s.kind.role=worker",
            "--format",
            "{{.Names}}",
        ],
        timeout=30,
    ).stdout.split()
    secrets = json.loads(
        client.kubectl(
            "-n",
            tenant.namespace,
            "get",
            "secrets",
            "-o",
            "json",
        ).stdout
    )["items"]
    machine_names = {item["metadata"]["name"] for item in machines}
    bootstrap = sorted(
        item["metadata"]["name"]
        for item in secrets
        if item.get("type") == "cluster.x-k8s.io/secret"
        and item["metadata"]["name"] in machine_names
    )
    layers = {
        "desired": desired,
        "machines": sorted(machine_names),
        "devMachines": sorted(item["metadata"]["name"] for item in devmachines),
        "containers": sorted(containers),
        "nodes": sorted(item["metadata"]["name"] for item in nodes),
        "bootstrapSecrets": bootstrap,
        "machineConditions": {
            item["metadata"]["name"]: sanitized_condition_summary(item)
            for item in machines
        },
        "devMachineConditions": {
            item["metadata"]["name"]: sanitized_condition_summary(item)
            for item in devmachines
        },
    }
    layers["ready"] = (
        len(machine_names) == desired
        and all(condition_true(item, "Ready") for item in machines)
        and len(devmachines) == desired
        and all(condition_true(item, "Ready") for item in devmachines)
        and set(containers) == machine_names
        and {item["metadata"]["name"] for item in nodes} == machine_names
        and len(bootstrap) == desired
    )
    return layers


def _storage_layer_status(
    root: Path,
    config: dict[str, str],
    tenant,
    *,
    strict: bool = False,
):
    volume_name = storage_volume_name(config, tenant)
    try:
        payload = inspect_storage_volume(volume_name)
    except RuntimeError as exc:
        if strict:
            raise
        return {"ready": False, "reason": "inspection-failed", "message": str(exc)}
    if payload is None:
        return {"ready": False, "reason": "volume-missing"}
    labels = payload.get("Labels") or {}
    record_path = storage_record_path(root, tenant)
    record_ready = False
    if record_path.is_file():
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record_ready = (
            record.get("volumeName") == volume_name
            and record.get("createdAt") == payload.get("CreatedAt")
            and record.get("mountpoint") == payload.get("Mountpoint")
        )
    result = {
        "volume": volume_name,
        "createdAt": payload.get("CreatedAt"),
        "mountpoint": payload.get("Mountpoint"),
        "owned": record_ready and (
            labels.get(config["OWNERSHIP_LABEL"]) == config["LAB_PREFIX"]
            and labels.get("cnpg-vcluster.capi/role") == "tenant-storage"
            and labels.get("cnpg-vcluster.capi/tenant") == tenant.name
        ),
        "pvc": None,
        "pv": None,
    }
    if (root / ".runtime" / "tenants" / tenant.name / "kubeconfig").is_file():
        pvc = _tenant_kubectl(
            root,
            config,
            tenant,
            "get",
            "pvc/storage-smoke",
            "-o",
            "json",
            check=False,
        )
        pv = _tenant_kubectl(
            root,
            config,
            tenant,
            "get",
            f"pv/{tenant.name}-storage-smoke",
            "-o",
            "json",
            check=False,
        )
        if pvc.returncode == 0 and pv.returncode == 0:
            pvc_payload = json.loads(pvc.stdout)
            pv_payload = json.loads(pv.stdout)
            result["pvc"] = {
                "uid": pvc_payload["metadata"]["uid"],
                "phase": pvc_payload["status"].get("phase"),
            }
            result["pv"] = {
                "uid": pv_payload["metadata"]["uid"],
                "phase": pv_payload["status"].get("phase"),
                "nodeAffinity": "nodeAffinity" in pv_payload["spec"],
            }
    storage_objects_ready = (
        result["pvc"] is None
        or (
            result["pvc"]["phase"] == "Bound"
            and result["pv"]["phase"] == "Bound"
            and result["pv"]["nodeAffinity"] is False
        )
    )
    result["ready"] = result["owned"] and storage_objects_ready
    return result


def _cnpg_layer_status(
    root: Path,
    config: dict[str, str],
    tenant,
    *,
    strict: bool = False,
):
    cluster = _tenant_kubectl(
        root,
        config,
        tenant,
        "-n",
        config["DATABASE_NAMESPACE"],
        "get",
        f"cluster/{tenant.cnpg_cluster}",
        "-o",
        "json",
        check=False,
    )
    if cluster.returncode != 0:
        if strict and not NOT_FOUND.search(cluster.stderr):
            raise RuntimeError(
                f"tenant CNPG inspection failed: {tenant.name}: {cluster.stderr}"
            )
        return {"ready": False, "reason": "cluster-missing"}
    cluster_payload = json.loads(cluster.stdout)
    pods = json.loads(
        _tenant_kubectl(
            root,
            config,
            tenant,
            "-n",
            config["DATABASE_NAMESPACE"],
            "get",
            "pods",
            "-l",
            f"cnpg.io/cluster={tenant.cnpg_cluster}",
            "-o",
            "json",
        ).stdout
    )["items"]
    pvcs = json.loads(
        _tenant_kubectl(
            root,
            config,
            tenant,
            "-n",
            config["DATABASE_NAMESPACE"],
            "get",
            "pvc",
            "-l",
            f"cnpg.io/cluster={tenant.cnpg_cluster}",
            "-o",
            "json",
        ).stdout
    )["items"]
    ready_pods = [
        pod
        for pod in pods
        if any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in pod.get("status", {}).get("conditions", [])
        )
    ]
    operator = _tenant_kubectl(
        root,
        config,
        tenant,
        "-n",
        config["CNPG_NAMESPACE"],
        "get",
        "deployment/cnpg-controller-manager",
        "-o",
        "json",
        check=False,
    )
    operator_ready = False
    if operator.returncode == 0:
        payload = json.loads(operator.stdout)
        operator_ready = (
            payload["status"].get("availableReplicas", 0)
            == payload["spec"].get("replicas", 0)
            > 0
            and payload["spec"]["template"]["spec"]["containers"][0]["image"]
            == config["CNPG_CONTROLLER_IMAGE"]
        )
    result = {
        "clusterPhase": cluster_payload.get("status", {}).get("phase"),
        "currentPrimary": cluster_payload.get("status", {}).get("currentPrimary"),
        "operatorReady": operator_ready,
        "pods": sorted(pod["metadata"]["name"] for pod in pods),
        "nodes": sorted({pod["spec"].get("nodeName") for pod in ready_pods}),
        "pvcs": sorted(
            f"{pvc['metadata']['name']}:{pvc['status'].get('phase')}:{pvc['spec'].get('volumeName')}"
            for pvc in pvcs
        ),
    }
    result["ready"] = (
        operator_ready
        and result["clusterPhase"] == "Cluster in healthy state"
        and len(ready_pods) == 3
        and len(result["nodes"]) == 3
        and len(pvcs) == 3
        and all(pvc["status"].get("phase") == "Bound" for pvc in pvcs)
    )
    return result


def _control_plane_layer_status(config: dict[str, str], client, tenant, cluster):
    devcluster = json.loads(
        client.kubectl(
            "-n",
            tenant.namespace,
            "get",
            f"devcluster/{tenant.name}",
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
    endpoints = (
        cluster["spec"].get("controlPlaneEndpoint", {}),
        devcluster["spec"].get("controlPlaneEndpoint", {}),
        kcp["spec"].get("controlPlaneEndpoint", {}),
    )
    endpoint_ready = all(
        endpoint.get("host") == tenant.vip
        and int(endpoint.get("port", 0)) == int(config["SPIKE_API_PORT"])
        for endpoint in endpoints
    )
    return {
        "ready": (
            condition_true(cluster, "Available")
            and condition_true(kcp, "Available")
            and kcp.get("status", {})
            .get("initialization", {})
            .get("controlPlaneInitialized")
            is True
            and endpoint_ready
            and "cluster.x-k8s.io/paused"
            not in (kcp["metadata"].get("annotations") or {})
        ),
        "clusterAvailable": condition_true(cluster, "Available"),
        "controlPlaneAvailable": condition_true(kcp, "Available"),
        "controlPlaneInitialized": kcp.get("status", {})
        .get("initialization", {})
        .get("controlPlaneInitialized")
        is True,
        "paused": "cluster.x-k8s.io/paused"
        in (kcp["metadata"].get("annotations") or {}),
        "authoritativeEndpoints": endpoint_ready,
        "clusterConditions": sanitized_condition_summary(cluster),
        "controlPlaneConditions": sanitized_condition_summary(kcp),
    }


def collect_tenant_status(
    root: Path,
    config: dict[str, str],
    client: ManagementClient,
    tenant,
    cluster_payload: dict[str, object],
    *,
    strict: bool = False,
) -> dict[str, object]:
    result: dict[str, object] = {
        "endpoint": f"{tenant.vip}:{config['SPIKE_API_PORT']}",
        "domain": tenant.domain,
        "database": tenant.cnpg_cluster,
        "controlPlane": _control_plane_layer_status(
            config, client, tenant, cluster_payload
        ),
        "network": network_status(
            root, config, client, tenant, strict=strict
        ),
    }
    try:
        result["machines"] = _machine_layer_status(
            root, config, client, tenant
        )
    except RuntimeError as exc:
        if strict:
            raise
        result["machines"] = {"ready": False, "reason": str(exc)}
    result["storage"] = _storage_layer_status(
        root, config, tenant, strict=strict
    )
    result["cnpg"] = _cnpg_layer_status(
        root, config, tenant, strict=strict
    )
    result["ready"] = all(
        result[layer].get("ready")
        for layer in (
            "controlPlane",
            "network",
            "machines",
            "storage",
            "cnpg",
        )
    )
    return result


def collect_management_status(
    root: Path, config: dict[str, str]
) -> dict[str, object]:
    management = management_status(root, config)
    host = {
        "maxUserInstances": read_inotify("max_user_instances"),
        "maxUserInstancesFloor": int(config["MIN_INOTIFY_INSTANCES"]),
        "maxUserWatches": read_inotify("max_user_watches"),
        "maxUserWatchesFloor": int(config["MIN_INOTIFY_WATCHES"]),
        "prepared": (root / ".runtime" / "host" / "inotify.json").is_file(),
    }
    host["ready"] = (
        host["maxUserInstances"] >= host["maxUserInstancesFloor"]
        and host["maxUserWatches"] >= host["maxUserWatchesFloor"]
        and host["prepared"]
    )
    result: dict[str, object] = {
        "host": host,
        "management": management,
        "providers": [],
    }
    if management.get("apiReady"):
        client = ManagementClient(root, config)
        result["providers"] = provider_status(config, client)
        result["components"] = management_component_status(config, client)
        result["auxiliary"] = management_auxiliary_status(config, client)
        kamaji = client.kubectl(
            "-n",
            config["MANAGEMENT_NAMESPACE"],
            "get",
            "deployment/kamaji",
            "-o",
            "json",
            check=False,
        )
        datastore = client.kubectl(
            "get",
            "datastore/default",
            "-o",
            "jsonpath={.status.ready}",
            check=False,
        )
        result["kamaji"] = {
            "available": False,
            "datastoreReady": datastore.returncode == 0 and datastore.stdout == "true",
        }
        if kamaji.returncode == 0:
            payload = json.loads(kamaji.stdout)
            result["kamaji"]["available"] = (
                payload.get("spec", {}).get("replicas", 0)
                == payload.get("status", {}).get("availableReplicas", 0)
                > 0
            )
    return result


def management_status_healthy(result: dict[str, object]) -> bool:
    providers = result.get("providers") or []
    components = result.get("components") or []
    return bool(
        result["management"].get("apiReady")
        and result["host"].get("ready")
        and result.get("kamaji", {}).get("available")
        and result.get("kamaji", {}).get("datastoreReady")
        and result.get("auxiliary", {}).get("ready")
        and len(providers) == 4
        and all(provider.get("available") for provider in providers)
        and len(components) == 7
        and all(component.get("available") for component in components)
    )


def collect_status(root: Path, config: dict[str, str]) -> dict[str, object]:
    result = collect_management_status(root, config)
    management = result["management"]
    if management.get("apiReady"):
        client = ManagementClient(root, config)
        spike = spike_tenant(root, config)
        spike_cluster = client.kubectl(
            "-n",
            spike.namespace,
            "get",
            f"cluster/{spike.name}",
            check=False,
        )
        if spike_cluster.returncode == 0:
            result["spikeNetwork"] = network_status(root, config, client, spike)
            try:
                result["spikeMachines"] = _machine_layer_status(
                    root, config, client, spike
                )
            except RuntimeError as exc:
                result["spikeMachines"] = {"ready": False, "reason": str(exc)}
            result["spikeStorage"] = _storage_layer_status(
                root, config, spike
            )
            cnpg = client.kubectl(
                "-n",
                spike.namespace,
                "get",
                f"cluster/{spike.name}",
                check=False,
            )
            if cnpg.returncode == 0 and (
                _tenant_kubectl(
                    root,
                    config,
                    spike,
                    "-n",
                    config["DATABASE_NAMESPACE"],
                    "get",
                    f"cluster/{spike.cnpg_cluster}",
                    check=False,
                ).returncode
                == 0
            ):
                result["spikeCNPG"] = _cnpg_layer_status(root, config, spike)
        result["tenants"] = {}
        configured_cluster_present = False
        for tenant in configured_tenants(root, config):
            cluster = client.kubectl(
                "-n",
                tenant.namespace,
                "get",
                f"cluster/{tenant.name}",
                "-o",
                "json",
                check=False,
            )
            if cluster.returncode != 0:
                result["tenants"][tenant.name] = {
                    "ready": False,
                    "reason": "cluster-missing",
                }
                continue
            configured_cluster_present = True
            cluster_payload = json.loads(cluster.stdout)
            result["tenants"][tenant.name] = collect_tenant_status(
                root,
                config,
                client,
                tenant,
                cluster_payload,
            )
        result["tenantModeExpected"] = (
            configured_cluster_present
            or (root / ".runtime" / "evidence" / "create-success.json").is_file()
        )
        result["tenantIsolationModel"] = {
            "workers": "exclusive CAPD containers",
            "storage": "distinct Docker volumes",
            "kernel": "shared host kernel",
        }
    return result


def status_healthy(result: dict[str, object]) -> bool:
    return bool(
        management_status_healthy(result)
        and (
            "spikeNetwork" not in result
            or result["spikeNetwork"].get("ready")
        )
        and (
            "spikeMachines" not in result
            or result["spikeMachines"].get("ready")
        )
        and (
            "spikeStorage" not in result
            or result["spikeStorage"].get("ready")
        )
        and (
            "spikeCNPG" not in result
            or result["spikeCNPG"].get("ready")
        )
        and (
            not result.get("tenantModeExpected")
            or (
                len(result.get("tenants") or {}) == 2
                and all(
                    tenant.get("ready")
                    for tenant in (result.get("tenants") or {}).values()
                )
            )
        )
    )


def status(root: Path, config: dict[str, str]) -> int:
    result = collect_status(root, config)
    print(json.dumps(result, indent=2, sort_keys=True))
    healthy = status_healthy(result)
    return 0 if healthy else 1
