from __future__ import annotations

import json
from pathlib import Path

from scripts.lib.addons import delete_addons, verify_network, wait_network_ready
from scripts.lib.conditions import condition_true
from scripts.lib.config import parse_duration
from scripts.lib.files import write_private_file
from scripts.lib.kube import ManagementClient, wait_for
from scripts.lib.process import run
from scripts.lib.tenants import (
    _tenant_kubectl,
    delete_tenant,
    tenant_kubeconfig_path,
    verify_worker_runtime,
)
from scripts.network import run_network_gate


def _machine_items(client: ManagementClient, tenant) -> list[dict[str, object]]:
    return json.loads(
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


def _registered(client: ManagementClient, tenant, machine) -> dict[str, object]:
    name = machine["metadata"]["name"]
    devmachine = json.loads(
        client.kubectl(
            "-n",
            tenant.namespace,
            "get",
            f"devmachine/{name}",
            "-o",
            "json",
        ).stdout
    )
    node_name = machine.get("status", {}).get("nodeRef", {}).get("name")
    if not node_name:
        raise RuntimeError(f"Machine has no Node reference: {name}")
    node = json.loads(
        _tenant_kubectl(
            client.root,
            client.config,
            tenant,
            "get",
            f"node/{node_name}",
            "-o",
            "json",
        ).stdout
    )
    return {"machine": machine, "devmachine": devmachine, "node": node}


def worker_snapshot(
    root: Path,
    config: dict[str, str],
    client: ManagementClient,
    tenant,
) -> dict[str, dict[str, str]]:
    items = _machine_items(client, tenant)
    snapshot: dict[str, dict[str, str]] = {}
    for machine in items:
        registered = _registered(client, tenant, machine)
        verify_worker_runtime(config, tenant, registered)
        if not condition_true(machine, "Ready"):
            raise RuntimeError(f"Machine is not Ready: {machine['metadata']['name']}")
        name = machine["metadata"]["name"]
        container_id = run(
            ["docker", "inspect", name, "--format", "{{.Id}}"],
            timeout=30,
        ).stdout.strip()
        snapshot[name] = {
            "machineUID": machine["metadata"]["uid"],
            "devMachineUID": registered["devmachine"]["metadata"]["uid"],
            "nodeUID": registered["node"]["metadata"]["uid"],
            "containerID": container_id,
        }
    nodes = json.loads(
        _tenant_kubectl(root, config, tenant, "get", "nodes", "-o", "json").stdout
    )["items"]
    if len(snapshot) != 3 or len(nodes) != 3:
        raise RuntimeError("three-worker topology is not exact")
    if {item["metadata"]["name"] for item in nodes} != set(snapshot):
        raise RuntimeError("Machine and Node names do not match exactly")
    return snapshot


def _scale_three(
    root: Path,
    config: dict[str, str],
    client: ManagementClient,
    tenant,
) -> None:
    client.kubectl(
        "-n",
        tenant.namespace,
        "patch",
        f"machinedeployment/{tenant.name}-worker",
        "--type=merge",
        "-p",
        '{"spec":{"replicas":3}}',
    )
    wait_network_ready(root, config, tenant)
    verify_network(root, config, tenant)


def _bootstrap_secrets(client: ManagementClient, tenant) -> set[str]:
    machine_names = {
        machine["metadata"]["name"] for machine in _machine_items(client, tenant)
    }
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
    result = set()
    for secret in secrets:
        if secret.get("type") != "cluster.x-k8s.io/secret":
            continue
        if secret["metadata"]["name"] not in machine_names:
            continue
        owners = secret["metadata"].get("ownerReferences") or []
        if len(owners) != 1 or owners[0].get("kind") != "KubeadmConfig":
            raise RuntimeError("worker bootstrap Secret owner is invalid")
        result.add(secret["metadata"]["name"])
    if len(result) != 3:
        raise RuntimeError("expected one bootstrap Secret per worker")
    return result


def _replace_machine(
    root: Path,
    config: dict[str, str],
    client: ManagementClient,
    tenant,
    before: dict[str, dict[str, str]],
) -> dict[str, dict[str, str]]:
    removed_name = sorted(before)[0]
    removed_uid = before[removed_name]["machineUID"]
    client.kubectl(
        "-n",
        tenant.namespace,
        "delete",
        f"machine/{removed_name}",
        "--wait=true",
        f"--timeout={config['DELETE_TIMEOUT']}",
    )

    def replaced():
        try:
            wait_network_ready(root, config, tenant)
            snapshot = worker_snapshot(root, config, client, tenant)
        except RuntimeError:
            return None
        if removed_name in snapshot or any(
            value["machineUID"] == removed_uid for value in snapshot.values()
        ):
            return None
        return snapshot

    after = wait_for(
        "three-worker Machine replacement",
        parse_duration(config["WORKER_REGISTRATION_TIMEOUT"]),
        parse_duration(config["WAIT_POLL_INTERVAL"]),
        replaced,
    )
    for name in set(before) & set(after):
        if before[name] != after[name]:
            raise RuntimeError(f"unaffected worker identity changed: {name}")
    if run(["docker", "inspect", removed_name], timeout=30, check=False).returncode == 0:
        raise RuntimeError("removed Machine container still exists")
    marker = tenant.storage_host_path / "phase3-marker"
    if marker.read_text(encoding="utf-8") != "phase3-marker\n":
        raise RuntimeError("tenant host marker did not survive worker replacement")
    return after


def _foreign_node_rejected(
    root: Path,
    config: dict[str, str],
    client: ManagementClient,
    tenant,
) -> None:
    manifest = root / ".runtime" / "rendered" / "negative" / "foreign-node.json"
    write_private_file(
        manifest,
        json.dumps(
            {
                "apiVersion": "v1",
                "kind": "Node",
                "metadata": {"name": "foreign-capi-node"},
                "spec": {"providerID": "docker:////foreign-capi-node"},
            }
        )
        + "\n",
    )
    _tenant_kubectl(root, config, tenant, "apply", "-f", str(manifest))
    try:
        try:
            worker_snapshot(root, config, client, tenant)
        except RuntimeError:
            pass
        else:
            raise RuntimeError("foreign fourth Node passed exact topology")
    finally:
        _tenant_kubectl(
            root,
            config,
            tenant,
            "delete",
            "node/foreign-capi-node",
            "--ignore-not-found",
        )
        manifest.unlink(missing_ok=True)


def _interrupted_machine_deletion(
    root: Path,
    config: dict[str, str],
    client: ManagementClient,
    tenant,
) -> None:
    before = worker_snapshot(root, config, client, tenant)
    removed_name = sorted(before)[0]
    client.kubectl(
        "-n",
        config["CAPD_NAMESPACE"],
        "scale",
        "deployment/capd-controller-manager",
        "--replicas=0",
    )
    try:
        client.kubectl(
            "-n",
            tenant.namespace,
            "delete",
            f"machine/{removed_name}",
            "--wait=false",
        )

        def blocked():
            response = client.kubectl(
                "-n",
                tenant.namespace,
                "get",
                f"devmachine/{removed_name}",
                "-o",
                "json",
                check=False,
            )
            if response.returncode != 0:
                return None
            resource = json.loads(response.stdout)
            return resource if (
                resource["metadata"].get("deletionTimestamp")
                and "dockermachine.infrastructure.cluster.x-k8s.io"
                in (resource["metadata"].get("finalizers") or [])
            ) else None

        wait_for("stuck DevMachine finalizer", 120, 2, blocked)
        if run(["docker", "inspect", removed_name], timeout=30, check=False).returncode != 0:
            raise RuntimeError("CAPD outage unexpectedly removed the worker container")
    finally:
        client.kubectl(
            "-n",
            config["CAPD_NAMESPACE"],
            "scale",
            "deployment/capd-controller-manager",
            "--replicas=1",
        )
        client.kubectl(
            "-n",
            config["CAPD_NAMESPACE"],
            "rollout",
            "status",
            "deployment/capd-controller-manager",
            f"--timeout={config['PROVIDER_TIMEOUT']}",
        )
    wait_network_ready(root, config, tenant)
    after = worker_snapshot(root, config, client, tenant)
    if removed_name in after:
        raise RuntimeError("interrupted Machine deletion did not replace the worker")
    if run(["docker", "inspect", removed_name], timeout=30, check=False).returncode == 0:
        raise RuntimeError("interrupted deletion left the old worker container")


def run_machine_gate(root: Path, config: dict[str, str]) -> None:
    client, tenant = run_network_gate(root, config, cleanup=False)
    bootstrap_secrets: set[str] = set()
    try:
        _scale_three(root, config, client, tenant)
        before = worker_snapshot(root, config, client, tenant)
        bootstrap_secrets |= _bootstrap_secrets(client, tenant)
        _scale_three(root, config, client, tenant)
        if worker_snapshot(root, config, client, tenant) != before:
            raise RuntimeError("unchanged three-worker declaration changed identities")
        after = _replace_machine(root, config, client, tenant, before)
        bootstrap_secrets |= _bootstrap_secrets(client, tenant)
        _interrupted_machine_deletion(root, config, client, tenant)
        bootstrap_secrets |= _bootstrap_secrets(client, tenant)
        _foreign_node_rejected(root, config, client, tenant)
        print(
            json.dumps(
                {
                    "workers": sorted(after),
                    "bootstrapSecrets": sorted(bootstrap_secrets),
                },
                sort_keys=True,
            )
        )
    finally:
        delete_addons(root, config, client, tenant)
        delete_tenant(root, config, client, tenant)
        if tenant_kubeconfig_path(root, tenant).exists():
            raise RuntimeError("tenant kubeconfig remained after Machine lifecycle cleanup")
        for secret_name in bootstrap_secrets:
            if (
                client.kubectl(
                    "-n",
                    tenant.namespace,
                    "get",
                    f"secret/{secret_name}",
                    check=False,
                ).returncode
                == 0
            ):
                raise RuntimeError(f"bootstrap Secret remained: {secret_name}")
    from scripts.test_endpoint_negative import partial_label_worker, unlabelled_worker

    unlabelled_worker(root, config, client)
    partial_label_worker(root, config, client)
