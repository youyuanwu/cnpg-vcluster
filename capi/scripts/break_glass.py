from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from scripts.lib.files import write_private_file
from scripts.lib.kube import ManagementClient
from scripts.lib.management import (
    require_management_ownership,
    validate_management_kubeconfig,
)
from scripts.lib.process import run
from scripts.lib.tenants import NOT_FOUND


FINALIZERS = {
    "cluster": "cluster.cluster.x-k8s.io",
    "machine": "machine.cluster.x-k8s.io",
    "machinedeployment": "cluster.x-k8s.io/machinedeployment",
    "devcluster": "dockercluster.infrastructure.cluster.x-k8s.io",
    "devmachine": "dockermachine.infrastructure.cluster.x-k8s.io",
    "kamajicontrolplane": "ecr.kamaji.clastix.io/finalizer",
    "configmap": "cnpg-vcluster.capi/break-glass",
}
NAME = re.compile(r"[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?")


def _docker_inventory() -> dict[str, list[str]]:
    return {
        "containers": sorted(
            run(
                ["docker", "ps", "-aq"],
                timeout=30,
            ).stdout.split()
        ),
        "volumes": sorted(
            run(
                ["docker", "volume", "ls", "-q"],
                timeout=30,
            ).stdout.split()
        ),
    }


def break_glass(
    root: Path,
    config: dict[str, str],
    kind: str,
    namespace: str,
    name: str,
    uid: str,
) -> Path:
    kind = kind.lower()
    if kind not in FINALIZERS:
        raise RuntimeError(f"unsupported break-glass kind: {kind}")
    if not all(NAME.fullmatch(value) for value in (namespace, name)):
        raise RuntimeError("break-glass namespace and name must be exact DNS names")
    require_management_ownership(root, config)
    validate_management_kubeconfig(root, config)
    client = ManagementClient(root, config)
    response = client.kubectl(
        "-n",
        namespace,
        "get",
        f"{kind}/{name}",
        "-o",
        "json",
        check=False,
    )
    if response.returncode != 0:
        if NOT_FOUND.search(response.stderr):
            raise RuntimeError("break-glass resource is absent")
        raise RuntimeError(f"break-glass inspection failed: {response.stderr}")
    resource = json.loads(response.stdout)
    metadata = resource["metadata"]
    labels = metadata.get("labels") or {}
    if (
        metadata.get("uid") != uid
        or labels.get(config["OWNERSHIP_LABEL"]) != config["LAB_PREFIX"]
        or not metadata.get("deletionTimestamp")
    ):
        raise RuntimeError(
            "break-glass requires the exact owned deleting resource UID"
        )
    finalizers = metadata.get("finalizers") or []
    finalizer = FINALIZERS[kind]
    if finalizers.count(finalizer) != 1:
        raise RuntimeError(
            f"break-glass expected exactly one {finalizer} finalizer"
        )
    index = finalizers.index(finalizer)
    docker_before = _docker_inventory()
    timestamp = datetime.now(timezone.utc).isoformat()
    evidence = root / ".runtime" / "evidence" / (
        f"break-glass-{kind}-{namespace}-{name}.json"
    )
    write_private_file(
        evidence,
        json.dumps(
            {
                "timestamp": timestamp,
                "resource": {
                    "apiVersion": resource.get("apiVersion"),
                    "kind": resource.get("kind"),
                    "namespace": namespace,
                    "name": name,
                    "uid": uid,
                    "resourceVersion": metadata.get("resourceVersion"),
                    "deletionTimestamp": metadata.get("deletionTimestamp"),
                    "finalizers": finalizers,
                    "conditions": [
                        {
                            key: condition.get(key)
                            for key in ("type", "status")
                        }
                        for condition in resource.get("status", {}).get(
                            "conditions", []
                        )
                    ],
                },
                "selectedFinalizer": finalizer,
                "dockerInventory": docker_before,
            },
            sort_keys=True,
        )
        + "\n",
    )
    patch = [
        {"op": "test", "path": "/metadata/uid", "value": uid},
        {
            "op": "test",
            "path": "/metadata/resourceVersion",
            "value": metadata["resourceVersion"],
        },
        {
            "op": "test",
            "path": f"/metadata/finalizers/{index}",
            "value": finalizer,
        },
        {"op": "remove", "path": f"/metadata/finalizers/{index}"},
    ]
    client.kubectl(
        "-n",
        namespace,
        "patch",
        f"{kind}/{name}",
        "--type=json",
        "-p",
        json.dumps(patch),
    )
    after = client.kubectl(
        "-n",
        namespace,
        "get",
        f"{kind}/{name}",
        "-o",
        "json",
        check=False,
    )
    if after.returncode == 0:
        remaining = json.loads(after.stdout)["metadata"].get("finalizers") or []
        expected = [item for item in finalizers if item != finalizer]
        if remaining != expected:
            raise RuntimeError("break-glass changed unexpected finalizers")
    elif not NOT_FOUND.search(after.stderr):
        raise RuntimeError(f"break-glass result inspection failed: {after.stderr}")
    if _docker_inventory() != docker_before:
        raise RuntimeError("break-glass changed Docker object inventory")
    print(f"removed exact finalizer from {kind}/{namespace}/{name}")
    return evidence
