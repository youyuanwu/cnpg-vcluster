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
from scripts.lib.tenants import (
    NOT_FOUND,
    configured_tenants,
    inspect_storage_volume,
    spike_tenant,
    storage_volume_name,
    verify_tenant_management_ownership,
)


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
APIS = {
    "cluster": ("cluster.x-k8s.io/v1beta2", "Cluster"),
    "machine": ("cluster.x-k8s.io/v1beta2", "Machine"),
    "machinedeployment": (
        "cluster.x-k8s.io/v1beta2",
        "MachineDeployment",
    ),
    "devcluster": (
        "infrastructure.cluster.x-k8s.io/v1beta2",
        "DevCluster",
    ),
    "devmachine": (
        "infrastructure.cluster.x-k8s.io/v1beta2",
        "DevMachine",
    ),
    "kamajicontrolplane": (
        "controlplane.cluster.x-k8s.io/v1alpha2",
        "KamajiControlPlane",
    ),
    "configmap": ("v1", "ConfigMap"),
}


def _resolve_tenant(root: Path, config: dict[str, str], namespace: str):
    tenants = [spike_tenant(root, config), *configured_tenants(root, config)]
    matches = [tenant for tenant in tenants if tenant.namespace == namespace]
    if len(matches) != 1:
        raise RuntimeError(
            "break-glass namespace is not an exact configured tenant"
        )
    return matches[0]


def _verify_resource_graph(
    config: dict[str, str],
    client: ManagementClient,
    tenant,
    kind: str,
    resource: dict[str, object],
) -> None:
    expected_api, expected_kind = APIS[kind]
    metadata = resource["metadata"]
    name = metadata["name"]
    if (
        resource.get("apiVersion") != expected_api
        or resource.get("kind") != expected_kind
    ):
        raise RuntimeError("break-glass resource API identity is unexpected")
    owned = verify_tenant_management_ownership(config, client, tenant)
    exact_names = {
        "cluster": tenant.name,
        "machinedeployment": f"{tenant.name}-worker",
        "devcluster": tenant.name,
        "kamajicontrolplane": tenant.name,
    }
    if kind in exact_names:
        if name != exact_names[kind]:
            raise RuntimeError("break-glass resource name is outside tenant graph")
        observed = owned.get(kind)
        if not observed or observed["metadata"]["uid"] != metadata["uid"]:
            raise RuntimeError("break-glass resource is not the owned tenant object")
        return
    machines = {
        machine["metadata"]["name"]: machine
        for machine in owned.get("machines", [])
    }
    if kind == "machine":
        machine = machines.get(name)
        if not machine or machine["metadata"]["uid"] != metadata["uid"]:
            raise RuntimeError("break-glass Machine is outside the owned worker graph")
        return
    if kind == "devmachine":
        machine = machines.get(name)
        reference = (machine or {}).get("spec", {}).get(
            "infrastructureRef", {}
        )
        owners = [
            owner
            for owner in metadata.get("ownerReferences") or []
            if owner.get("controller") is True
        ]
        if (
            machine is None
            or reference.get("apiGroup")
            != "infrastructure.cluster.x-k8s.io"
            or reference.get("kind") != "DevMachine"
            or reference.get("name") != name
            or len(owners) != 1
            or owners[0].get("apiVersion")
            != "cluster.x-k8s.io/v1beta2"
            or owners[0].get("kind") != "Machine"
            or owners[0].get("name") != name
            or owners[0].get("uid") != machine["metadata"]["uid"]
            or (metadata.get("labels") or {}).get(
                "cluster.x-k8s.io/cluster-name"
            )
            != tenant.name
        ):
            raise RuntimeError(
                "break-glass DevMachine is outside the owned worker graph"
            )
        return
    if kind == "configmap" and re.fullmatch(
        r"break-glass-fixture-[0-9a-f]{8}", name
    ):
        return
    raise RuntimeError("break-glass resource is outside the tenant object graph")


def _docker_inventory(config: dict[str, str], tenant) -> dict[str, object]:
    volume = inspect_storage_volume(storage_volume_name(config, tenant))
    return {
        "containers": sorted(
            run(
                [
                    "docker",
                    "ps",
                    "-aq",
                    "--filter",
                    f"label=io.x-k8s.kind.cluster={tenant.name}",
                ],
                timeout=30,
            ).stdout.split()
        ),
        "volume": (
            {
                "name": volume.get("Name"),
                "createdAt": volume.get("CreatedAt"),
                "mountpoint": volume.get("Mountpoint"),
            }
            if volume
            else None
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
    tenant = _resolve_tenant(root, config, namespace)
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
    _verify_resource_graph(config, client, tenant, kind, resource)
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
    docker_before = _docker_inventory(config, tenant)
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
    if _docker_inventory(config, tenant) != docker_before:
        raise RuntimeError("break-glass changed Docker object inventory")
    print(f"removed exact finalizer from {kind}/{namespace}/{name}")
    return evidence
