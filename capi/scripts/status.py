from __future__ import annotations

import json
from pathlib import Path

from scripts.lib.kube import ManagementClient
from scripts.lib.management import management_component_status, management_status
from scripts.lib.providers import provider_status


def collect_status(root: Path, config: dict[str, str]) -> dict[str, object]:
    management = management_status(root, config)
    result: dict[str, object] = {"management": management, "providers": []}
    if management.get("apiReady"):
        client = ManagementClient(root, config)
        result["providers"] = provider_status(config, client)
        result["components"] = management_component_status(config, client)
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


def status(root: Path, config: dict[str, str]) -> int:
    result = collect_status(root, config)
    print(json.dumps(result, indent=2, sort_keys=True))
    providers = result.get("providers") or []
    components = result.get("components") or []
    healthy = (
        result["management"].get("apiReady")
        and result.get("kamaji", {}).get("available")
        and result.get("kamaji", {}).get("datastoreReady")
        and len(providers) == 4
        and all(provider.get("available") for provider in providers)
        and len(components) == 7
        and all(component.get("available") for component in components)
    )
    return 0 if healthy else 1
