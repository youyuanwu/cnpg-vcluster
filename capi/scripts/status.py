from __future__ import annotations

import json
import time
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
    verify_tenant_management_ownership,
)
from scripts.controller_tenant_status import evaluate_tenant
from scripts.lib.tenant_runtime import foundation_sha256
from scripts.lib.tenant_status import TenantStatus
from scripts.lib.tenants import (
    inspect_storage_volume,
    storage_record_path,
    storage_volume_name,
)


def collect_management_status(
    root: Path,
    config: dict[str, str],
    *,
    strict: bool = False,
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
        result["providers"] = provider_status(
            config, client, strict=strict
        )
        result["components"] = management_component_status(
            config, client, strict=strict
        )
        result["auxiliary"] = management_auxiliary_status(
            config, client, strict=strict
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
        for name, response in (
            ("Kamaji deployment", kamaji),
            ("Kamaji datastore", datastore),
        ):
            if (
                strict
                and response.returncode != 0
                and not NOT_FOUND.search(response.stderr)
            ):
                raise RuntimeError(
                    f"{name} inspection failed: {response.stderr}"
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
        response = client.kubectl(
            "get",
            "tenants",
            "-o",
            "json",
            check=False,
        )
        if response.returncode == 0:
            payload = json.loads(response.stdout)
            result["tenants"] = {
                item["metadata"]["name"]: evaluate_tenant(item)
                for item in payload.get("items", [])
            }
        elif not NOT_FOUND.search(response.stderr):
            raise RuntimeError(
                f"Tenant status inspection failed: {response.stderr}"
            )
        result["tenantIsolationModel"] = {
            "workers": "exclusive CAPD containers",
            "storage": "distinct Docker volumes",
            "kernel": "shared host kernel",
        }
    return result


def status_healthy(result: dict[str, object]) -> bool:
    tenants = result.get("tenants", {})
    return bool(
        management_status_healthy(result)
        and isinstance(tenants, dict)
        and all(
            isinstance(value, dict) and value.get("classification") == "ready"
            for value in tenants.values()
        )
    )


def status(root: Path, config: dict[str, str]) -> int:
    result = collect_status(root, config)
    print(json.dumps(result, indent=2, sort_keys=True))
    healthy = status_healthy(result)
    return 0 if healthy else 1
