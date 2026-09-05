from __future__ import annotations

import base64
import json
from pathlib import Path

from scripts.create_management import create_management
from scripts.lib.files import write_private_file
from scripts.lib.kube import ManagementClient
from scripts.lib.management import management_status
from scripts.lib.redaction import redact
from scripts.lib.tenants import (
    _tenant_kubectl,
    apply_bootstrap_rbac,
    apply_control_plane,
    apply_workers,
    delete_tenant,
    endpoint_snapshot,
    export_tenant_kubeconfig,
    spike_tenant,
    verify_authoritative_endpoint,
    verify_worker_runtime,
    wait_for_registered_node,
)


def _verify_no_worker_state(client: ManagementClient, tenant) -> None:
    for resource in ("machines", "devmachines", "kubeadmconfigs"):
        payload = json.loads(
            client.kubectl(
                "-n",
                tenant.namespace,
                "get",
                resource,
                "-o",
                "json",
            ).stdout
        )
        if payload["items"]:
            raise RuntimeError(f"{resource} existed before bootstrap RBAC")
    secrets = json.loads(
        client.kubectl(
            "-n",
            tenant.namespace,
            "get",
            "secrets",
            "-o",
            "json",
        ).stdout
    )
    if any(
        item.get("type") == "cluster.x-k8s.io/secret"
        and item["metadata"]["name"] not in {f"{tenant.name}-ca", f"{tenant.name}-kubeconfig"}
        for item in secrets["items"]
    ):
        raise RuntimeError("worker bootstrap Secret existed before worker declaration")


def _verify_bootstrap_secret(
    config: dict[str, str],
    client: ManagementClient,
    tenant,
    registered: dict[str, object],
) -> None:
    name = str(registered["secret"])
    secret = json.loads(
        client.kubectl(
            "-n",
            tenant.namespace,
            "get",
            f"secret/{name}",
            "-o",
            "json",
        ).stdout
    )
    owners = secret["metadata"].get("ownerReferences") or []
    if len(owners) != 1 or owners[0].get("kind") != "KubeadmConfig":
        raise RuntimeError("bootstrap Secret lacks one KubeadmConfig owner")
    if secret.get("type") != "cluster.x-k8s.io/secret":
        raise RuntimeError("bootstrap Secret type is unexpected")
    if set(secret.get("data", {})) != {"format", "value"}:
        raise RuntimeError("bootstrap Secret data inventory is unexpected")

    allowed = set(config["BOOTSTRAP_SECRET_READER_SERVICE_ACCOUNTS"].split())
    observed_allowed = set()
    for identity in allowed:
        result = client.kubectl(
            "auth",
            "can-i",
            "get",
            f"secret/{name}",
            "-n",
            tenant.namespace,
            f"--as={identity}",
        ).stdout.strip()
        if result == "yes":
            observed_allowed.add(identity)
    if observed_allowed != allowed:
        raise RuntimeError(
            f"bootstrap Secret reader inventory mismatch: {sorted(observed_allowed)}"
        )
    for denied in (
        f"system:serviceaccount:{tenant.namespace}:default",
        "system:serviceaccount:default:default",
    ):
        result = client.kubectl(
            "auth",
            "can-i",
            "get",
            f"secret/{name}",
            "-n",
            tenant.namespace,
            f"--as={denied}",
            check=False,
        ).stdout.strip()
        if result != "no":
            raise RuntimeError(f"bootstrap Secret is readable by {denied}")


def run_endpoint_gate(root: Path, config: dict[str, str]) -> None:
    if not management_status(root, config).get("apiReady"):
        create_management(root, config)
    client = ManagementClient(root, config)
    tenant = spike_tenant(root, config)
    delete_tenant(root, config, client, tenant)
    evidence = root / ".runtime" / "evidence" / "endpoint-failure.txt"
    evidence.unlink(missing_ok=True)
    try:
        apply_control_plane(root, config, client, tenant)
        export_tenant_kubeconfig(root, config, client, tenant)
        _verify_no_worker_state(client, tenant)
        apply_bootstrap_rbac(root, config, tenant)
        apply_workers(root, config, client, tenant)
        registered = wait_for_registered_node(root, config, client, tenant)
        verify_authoritative_endpoint(root, config, client, tenant, registered)
        verify_worker_runtime(config, tenant, registered)
        _verify_bootstrap_secret(config, client, tenant, registered)
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
        annotations = kcp["metadata"].get("annotations") or {}
        if "cluster.x-k8s.io/paused" in annotations:
            raise RuntimeError("KamajiControlPlane is paused")
        for resource in ("configmap/kube-proxy", "daemonset/kube-proxy"):
            if (
                _tenant_kubectl(
                    root,
                    config,
                    tenant,
                    "-n",
                    "kube-system",
                    "get",
                    resource,
                    check=False,
                )
                .returncode
                == 0
            ):
                raise RuntimeError("Kamaji-managed kube-proxy was not disabled")

        first = endpoint_snapshot(root, config, client, tenant)
        apply_control_plane(root, config, client, tenant)
        apply_workers(root, config, client, tenant)
        second = endpoint_snapshot(root, config, client, tenant)
        if first != second:
            raise RuntimeError("repeated tenant reconciliation changed stable identities")

        node = registered["node"]
        ready = next(
            (
                condition
                for condition in node.get("status", {}).get("conditions", [])
                if condition.get("type") == "Ready"
            ),
            None,
        )
        if ready and ready.get("status") == "True":
            raise RuntimeError("pre-CNI endpoint gate unexpectedly produced a Ready Node")
        print(
            json.dumps(
                {
                    "cluster": tenant.name,
                    "endpoint": f"{tenant.vip}:{config['SPIKE_API_PORT']}",
                    "machine": registered["machine"]["metadata"]["name"],
                    "node": node["metadata"]["name"],
                    "nodeReady": ready.get("status") if ready else "Unknown",
                },
                sort_keys=True,
            )
        )
    except Exception as exc:
        write_private_file(evidence, redact(str(exc)) + "\n")
        raise
    finally:
        delete_tenant(root, config, client, tenant)
