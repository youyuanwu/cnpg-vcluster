from __future__ import annotations

import json
from pathlib import Path

from scripts.lib.addons import delete_addons, wait_network_ready
from scripts.lib.files import IntegrityError, write_private_file
from scripts.lib.process import run
from scripts.lib.tenants import (
    _tenant_kubectl,
    delete_tenant,
    inspect_storage_volume,
    storage_volume_name,
)
from scripts.machines import _scale_three, worker_snapshot
from scripts.network import run_network_gate
from scripts.status import collect_status, status_healthy


def _volume_identity(config: dict[str, str], tenant) -> dict[str, object]:
    name = storage_volume_name(config, tenant)
    payload = inspect_storage_volume(name)
    if payload is None:
        raise RuntimeError("tenant storage volume is absent")
    labels = payload.get("Labels") or {}
    if (
        labels.get(config["OWNERSHIP_LABEL"]) != config["LAB_PREFIX"]
        or labels.get("cnpg-vcluster.capi/role") != "tenant-storage"
        or labels.get("cnpg-vcluster.capi/tenant") != tenant.name
    ):
        raise RuntimeError("tenant storage volume ownership mismatch")
    return payload


def _render_storage(root: Path, config: dict[str, str], tenant) -> Path:
    source = root / "manifests" / "storage" / "hostpath-smoke.yaml.tpl"
    content = source.read_text(encoding="utf-8")
    replacements = {
        "${STORAGE_CLASS}": config["SPIKE_STORAGE_CLASS"],
        "${PV_NAME}": f"{tenant.name}-storage-smoke",
        "${STORAGE_PATH}": config["SPIKE_STORAGE_CONTAINER_PATH"],
        "${VERIFY_IMAGE}": config["VERIFY_IMAGE"],
    }
    for placeholder, value in replacements.items():
        if content.count(placeholder) < 1:
            raise IntegrityError(f"storage template lacks {placeholder}")
        content = content.replace(placeholder, value)
    if "${" in content:
        raise IntegrityError("storage template contains unresolved placeholders")
    path = root / ".runtime" / "rendered" / "storage" / tenant.name / "smoke.yaml"
    write_private_file(path, content)
    return path


def _wait_smoke(root: Path, config: dict[str, str], tenant) -> dict[str, str]:
    _tenant_kubectl(
        root,
        config,
        tenant,
        "wait",
        "--for=condition=Available",
        "deployment/storage-smoke",
        f"--timeout={config['TENANT_CONTROL_PLANE_TIMEOUT']}",
    )
    pods = json.loads(
        _tenant_kubectl(
            root,
            config,
            tenant,
            "get",
            "pods",
            "-l",
            "app=storage-smoke",
            "-o",
            "json",
        ).stdout
    )["items"]
    if len(pods) != 1:
        raise RuntimeError("expected exactly one storage smoke Pod")
    return {
        "name": pods[0]["metadata"]["name"],
        "uid": pods[0]["metadata"]["uid"],
        "node": pods[0]["spec"]["nodeName"],
    }


def _verify_marker(root: Path, config: dict[str, str], tenant, pod: str) -> None:
    marker = _tenant_kubectl(
        root,
        config,
        tenant,
        "exec",
        f"pod/{pod}",
        "--",
        "cat",
        "/data/marker",
    ).stdout.strip()
    if marker != "machine-independent":
        raise RuntimeError("storage marker is not readable")


def _storage_status(root: Path, config: dict[str, str], tenant) -> dict[str, object]:
    pvc = json.loads(
        _tenant_kubectl(
            root,
            config,
            tenant,
            "get",
            "pvc/storage-smoke",
            "-o",
            "json",
        ).stdout
    )
    pv = json.loads(
        _tenant_kubectl(
            root,
            config,
            tenant,
            "get",
            f"pv/{tenant.name}-storage-smoke",
            "-o",
            "json",
        ).stdout
    )
    return {
        "ready": (
            pvc["status"].get("phase") == "Bound"
            and pv["status"].get("phase") == "Bound"
            and "nodeAffinity" not in pv["spec"]
        ),
        "pvc": pvc["metadata"]["uid"],
        "pv": pv["metadata"]["uid"],
        "path": pv["spec"]["hostPath"]["path"],
    }


def _delete_storage(root: Path, config: dict[str, str], tenant) -> None:
    for resource in (
        "deployment/storage-smoke",
        "pvc/storage-smoke",
        f"pv/{tenant.name}-storage-smoke",
        f"storageclass/{config['SPIKE_STORAGE_CLASS']}",
    ):
        _tenant_kubectl(
            root,
            config,
            tenant,
            "delete",
            resource,
            "--ignore-not-found",
            "--wait=true",
            f"--timeout={config['DELETE_TIMEOUT']}",
        )
        if (
            _tenant_kubectl(
                root,
                config,
                tenant,
                "get",
                resource,
                check=False,
            ).returncode
            == 0
        ):
            raise RuntimeError(f"storage resource remained after deletion: {resource}")


def run_storage_gate(
    root: Path,
    config: dict[str, str],
    *,
    cleanup: bool = True,
):
    client, tenant = run_network_gate(root, config, cleanup=False)
    succeeded = False
    try:
        _scale_three(root, config, client, tenant)
        before_workers = worker_snapshot(root, config, client, tenant)
        before_volume = _volume_identity(config, tenant)
        manifest = _render_storage(root, config, tenant)
        _tenant_kubectl(root, config, tenant, "apply", "-f", str(manifest))
        smoke = _wait_smoke(root, config, tenant)
        _verify_marker(root, config, tenant, smoke["name"])
        initial_storage = _storage_status(root, config, tenant)
        if not initial_storage["ready"]:
            raise RuntimeError("static hostPath storage is not ready")
        observed = collect_status(root, config)
        if (
            not status_healthy(observed)
            or observed["spikeStorage"]["pvc"]["phase"] != "Bound"
            or observed["spikeStorage"]["pv"]["nodeAffinity"] is not False
        ):
            raise RuntimeError("status does not expose healthy static storage")

        client.kubectl(
            "-n",
            tenant.namespace,
            "delete",
            f"machine/{smoke['node']}",
            "--wait=true",
            f"--timeout={config['DELETE_TIMEOUT']}",
        )
        _tenant_kubectl(
            root,
            config,
            tenant,
            "delete",
            f"pod/{smoke['name']}",
            "--grace-period=0",
            "--force",
            "--ignore-not-found",
            "--wait=false",
        )
        wait_network_ready(root, config, tenant)
        after_workers = worker_snapshot(root, config, client, tenant)
        replacement = _wait_smoke(root, config, tenant)
        if replacement["uid"] == smoke["uid"]:
            raise RuntimeError("storage workload did not reschedule after Machine replacement")
        _verify_marker(root, config, tenant, replacement["name"])
        after_volume = _volume_identity(config, tenant)
        if (
            after_volume["Name"] != before_volume["Name"]
            or after_volume["CreatedAt"] != before_volume["CreatedAt"]
        ):
            raise RuntimeError("tenant Docker volume identity changed")
        if _storage_status(root, config, tenant) != initial_storage:
            raise RuntimeError("PVC/PV identity changed across Machine replacement")
        if not status_healthy(collect_status(root, config)):
            raise RuntimeError("status is unhealthy after storage Machine replacement")
        if set(before_workers) == set(after_workers):
            raise RuntimeError("Machine replacement did not change worker identity")
        print("Docker-volume-backed hostPath storage checks passed")
        succeeded = True
        return client, tenant, after_workers
    finally:
        if cleanup or not succeeded:
            _delete_storage(root, config, tenant)
            delete_addons(root, config, client, tenant)
            delete_tenant(root, config, client, tenant)
