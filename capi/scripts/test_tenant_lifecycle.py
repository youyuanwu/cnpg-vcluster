from __future__ import annotations

import json
from pathlib import Path

from scripts.cnpg import _verify_marker
from scripts.create import create, stable_tenant_snapshot
from scripts.destroy import destroy
from scripts.destroy_tenant import (
    destroy_tenant_stack,
    prepare_tenant_deletion,
)
from scripts.lib.files import write_private_file
from scripts.lib.kube import ManagementClient
from scripts.lib.redaction import redact
from scripts.lib.tenants import (
    configured_tenants,
    inspect_management_resource,
    storage_record_path,
)
from scripts.repair import repair


def _tenant_map(root: Path, config: dict[str, str]):
    return {tenant.name: tenant for tenant in configured_tenants(root, config)}


def _drift_kube_proxy(root: Path, config: dict[str, str], tenant) -> None:
    from scripts.lib.tenants import _tenant_kubectl

    _tenant_kubectl(
        root,
        config,
        tenant,
        "-n",
        "kube-system",
        "patch",
        "configmap/capi-kube-proxy",
        "--type=merge",
        "-p",
        '{"data":{"config.conf":"apiVersion: kubeproxy.config.k8s.io/v1alpha1\\n'
        'kind: KubeProxyConfiguration\\nconntrack:\\n  maxPerCore: 1\\n"}}',
    )


def _repair_refuses_missing_identity_record(
    root: Path,
    config: dict[str, str],
    client,
    tenant,
    survivor,
) -> None:
    record_path = storage_record_path(root, tenant)
    record = record_path.read_text(encoding="utf-8")
    survivor_before = stable_tenant_snapshot(root, config, client, survivor)
    record_path.unlink()
    try:
        try:
            repair(root, config, tenant.name)
        except RuntimeError:
            pass
        else:
            raise RuntimeError("repair accepted a tenant volume without its identity record")
    finally:
        write_private_file(record_path, record)
    if stable_tenant_snapshot(root, config, client, survivor) != survivor_before:
        raise RuntimeError("failed repair changed the survivor")


def _repair_input_tamper_blocked(
    root: Path,
    config: dict[str, str],
    client,
    tenant,
    survivor,
) -> None:
    path = (
        root
        / "manifests"
        / "tenants"
        / "overlays"
        / tenant.name
        / "tenant.json"
    )
    original = path.read_bytes()
    tenant_before = stable_tenant_snapshot(root, config, client, tenant)
    survivor_before = stable_tenant_snapshot(root, config, client, survivor)
    path.write_bytes(original + b"\n")
    try:
        try:
            repair(root, config, tenant.name)
        except RuntimeError:
            pass
        else:
            raise RuntimeError("repair accepted a tampered immutable tenant input")
    finally:
        path.write_bytes(original)
    if stable_tenant_snapshot(root, config, client, tenant) != tenant_before:
        raise RuntimeError("tampered repair changed the target tenant")
    if stable_tenant_snapshot(root, config, client, survivor) != survivor_before:
        raise RuntimeError("tampered repair changed the survivor tenant")


def _repair_incomplete_control_plane(
    root: Path,
    config: dict[str, str],
    client,
    tenant,
) -> None:
    client.kubectl(
        "-n",
        tenant.namespace,
        "delete",
        f"kamajicontrolplane/{tenant.name}",
        "--wait=true",
        f"--timeout={config['DELETE_TIMEOUT']}",
    )
    repair(root, config, tenant.name)
    _verify_marker(root, config, tenant)


def _repair_refuses_unowned_cluster(
    root: Path,
    config: dict[str, str],
    client,
    tenant,
    survivor,
) -> None:
    survivor_before = stable_tenant_snapshot(root, config, client, survivor)
    client.kubectl(
        "-n",
        tenant.namespace,
        "label",
        f"cluster/{tenant.name}",
        f"{config['OWNERSHIP_LABEL']}=foreign",
        "--overwrite",
    )
    try:
        try:
            repair(root, config, tenant.name)
        except RuntimeError:
            pass
        else:
            raise RuntimeError("repair accepted an unowned same-name Cluster")
    finally:
        client.kubectl(
            "-n",
            tenant.namespace,
            "label",
            f"cluster/{tenant.name}",
            f"{config['OWNERSHIP_LABEL']}={config['LAB_PREFIX']}",
            "--overwrite",
        )
    if stable_tenant_snapshot(root, config, client, survivor) != survivor_before:
        raise RuntimeError("unowned repair attempt changed the survivor")


def _repair_refuses_unowned_machine(
    root: Path,
    config: dict[str, str],
    client,
    tenant,
    survivor,
) -> None:
    target_before = stable_tenant_snapshot(root, config, client, tenant)
    survivor_before = stable_tenant_snapshot(root, config, client, survivor)
    machine_name = sorted(target_before["workers"])[0]
    machine_uid = target_before["workers"][machine_name]["machineUID"]
    machine = json.loads(
        client.kubectl(
            "-n",
            tenant.namespace,
            "get",
            f"machine/{machine_name}",
            "-o",
            "json",
        ).stdout
    )
    machine_set = next(
        owner["name"]
        for owner in machine["metadata"].get("ownerReferences") or []
        if owner.get("kind") == "MachineSet"
        and owner.get("controller") is True
    )
    paused_resources = (
        f"cluster/{tenant.name}",
        f"machinedeployment/{tenant.name}-worker",
        f"machineset/{machine_set}",
    )
    for resource in paused_resources:
        client.kubectl(
            "-n",
            tenant.namespace,
            "annotate",
            resource,
            "cluster.x-k8s.io/paused=true",
            "--overwrite",
        )
    client.kubectl(
        "-n",
        tenant.namespace,
        "label",
        f"machine/{machine_name}",
        f"{config['OWNERSHIP_LABEL']}=foreign",
        "--overwrite",
    )
    try:
        observed_label = client.kubectl(
            "-n",
            tenant.namespace,
            "get",
            f"machine/{machine_name}",
            "-o",
            "json",
        ).stdout
        observed_label = json.loads(observed_label)["metadata"]["labels"].get(
            config["OWNERSHIP_LABEL"]
        )
        if observed_label != "foreign":
            raise RuntimeError("foreign Machine ownership fixture did not persist")
        try:
            repair(root, config, tenant.name)
        except RuntimeError:
            pass
        else:
            raise RuntimeError("repair accepted an incorrectly owned Machine")
        observed_uid = client.kubectl(
            "-n",
            tenant.namespace,
            "get",
            f"machine/{machine_name}",
            "-o",
            "jsonpath={.metadata.uid}",
        ).stdout
        if observed_uid != machine_uid:
            raise RuntimeError("refused Machine changed identity")
    finally:
        client.kubectl(
            "-n",
            tenant.namespace,
            "label",
            f"machine/{machine_name}",
            f"{config['OWNERSHIP_LABEL']}={config['LAB_PREFIX']}",
            "--overwrite",
        )
        for resource in reversed(paused_resources):
            client.kubectl(
                "-n",
                tenant.namespace,
                "annotate",
                resource,
                "cluster.x-k8s.io/paused-",
            )
    if stable_tenant_snapshot(root, config, client, survivor) != survivor_before:
        raise RuntimeError("unowned Machine repair attempt changed the survivor")


def _interrupt_before_cluster_deletion(
    root: Path,
    config: dict[str, str],
    client,
    tenant,
) -> None:
    cluster = inspect_management_resource(client, tenant, f"cluster/{tenant.name}")
    if cluster is None:
        raise RuntimeError("interruption fixture tenant is absent")
    prepare_tenant_deletion(root, config, client, tenant, cluster)
    destroy_tenant_stack(root, config, tenant.name)


def _interrupt_after_cluster_deletion(
    root: Path,
    config: dict[str, str],
    client,
    tenant,
) -> None:
    cluster = inspect_management_resource(client, tenant, f"cluster/{tenant.name}")
    if cluster is None:
        raise RuntimeError("interruption fixture tenant is absent")
    prepare_tenant_deletion(root, config, client, tenant, cluster)
    client.kubectl(
        "-n",
        tenant.namespace,
        "delete",
        f"cluster/{tenant.name}",
        "--wait=true",
        f"--timeout={config['DELETE_TIMEOUT']}",
    )
    destroy_tenant_stack(root, config, tenant.name)


def run_tenant_lifecycle(root: Path, config: dict[str, str]) -> None:
    failure = None
    try:
        create(root, config)
        client = ManagementClient(root, config)
        tenants = _tenant_map(root, config)
        tenant_a = tenants["tenant-a"]
        tenant_b = tenants["tenant-b"]

        repair(root, config, tenant_a.name)
        _drift_kube_proxy(root, config, tenant_a)
        repair(root, config, tenant_a.name)
        _verify_marker(root, config, tenant_a)
        _verify_marker(root, config, tenant_b)
        _repair_input_tamper_blocked(
            root, config, client, tenant_a, tenant_b
        )
        _repair_refuses_unowned_cluster(
            root, config, client, tenant_a, tenant_b
        )
        _repair_refuses_unowned_machine(
            root, config, client, tenant_a, tenant_b
        )
        _repair_incomplete_control_plane(
            root, config, client, tenant_a
        )
        _repair_refuses_missing_identity_record(
            root, config, client, tenant_a, tenant_b
        )

        _interrupt_before_cluster_deletion(
            root, config, client, tenant_a
        )
        destroy_tenant_stack(root, config, tenant_a.name)
        _verify_marker(root, config, tenant_b)

        create(root, config)
        client = ManagementClient(root, config)
        tenants = _tenant_map(root, config)
        _interrupt_after_cluster_deletion(
            root, config, client, tenants["tenant-b"]
        )
        destroy_tenant_stack(root, config, "tenant-b")
        _verify_marker(root, config, tenants["tenant-a"])
        print(
            json.dumps(
                {
                    "repaired": "tenant-a",
                    "deletedBeforeClusterInterruption": "tenant-a",
                    "deletedAfterClusterInterruption": "tenant-b",
                },
                sort_keys=True,
            )
        )
    except BaseException as exc:
        failure = exc
    try:
        destroy(root, config)
    except BaseException as cleanup:
        if failure is None:
            raise
        failure.add_note(f"cleanup also failed: {redact(str(cleanup))}")
    if failure is not None:
        raise failure
