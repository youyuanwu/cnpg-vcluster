from __future__ import annotations

import json
from pathlib import Path

from scripts.lib.kube import ManagementClient
from scripts.lib.host import read_inotify
from scripts.lib.management import (
    management_auxiliary_status,
    management_component_status,
    management_status,
)
from scripts.lib.providers import provider_status
from scripts.lib.addons import network_status
from scripts.lib.tenants import spike_tenant


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
    )


def status(root: Path, config: dict[str, str]) -> int:
    result = collect_status(root, config)
    print(json.dumps(result, indent=2, sort_keys=True))
    healthy = status_healthy(result)
    return 0 if healthy else 1
