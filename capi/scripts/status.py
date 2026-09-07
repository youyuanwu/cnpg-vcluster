from __future__ import annotations

import json
from pathlib import Path

from scripts.lib.conditions import condition_true
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
from scripts.lib.tenants import _tenant_kubectl, spike_tenant
from scripts.lib.tenants import storage_volume_name


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


def _storage_layer_status(root: Path, config: dict[str, str], tenant):
    volume_name = storage_volume_name(config, tenant)
    volume = run(
        ["docker", "volume", "inspect", volume_name],
        timeout=30,
        check=False,
    )
    if volume.returncode != 0:
        return {"ready": False, "reason": "volume-missing"}
    payload = json.loads(volume.stdout)[0]
    labels = payload.get("Labels") or {}
    result = {
        "volume": volume_name,
        "createdAt": payload.get("CreatedAt"),
        "mountpoint": payload.get("Mountpoint"),
        "owned": (
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


def collect_status(root: Path, config: dict[str, str]) -> dict[str, object]:
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


def status_healthy(result: dict[str, object]) -> bool:
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
    )


def status(root: Path, config: dict[str, str]) -> int:
    result = collect_status(root, config)
    print(json.dumps(result, indent=2, sort_keys=True))
    healthy = status_healthy(result)
    return 0 if healthy else 1
