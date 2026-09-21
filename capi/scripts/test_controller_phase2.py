#!/usr/bin/env python3
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.lib.config import load_configuration, parse_duration
from scripts.lib.controller import set_controller_mutation
from scripts.lib.kube import ManagementClient, wait_for
from scripts.lib.locking import profile_lock, tools_lock
from scripts.lib.process import run
from scripts.lib.redaction import redact


TENANT_NAME = "controller-phase2"


def _require_clean_cutover(
    root: Path,
    config: dict[str, str],
    client: ManagementClient,
) -> None:
    for relative in (
        ".runtime/lifecycle/local",
        ".runtime/rendered/tenants",
        ".runtime/storage",
        ".runtime/kubeconfigs",
    ):
        legacy = root / relative
        if legacy.exists() and any(legacy.rglob("*")):
            raise RuntimeError(
                f"legacy local lifecycle state blocks controller mutation: {relative}"
            )
    tenants = client.kubectl(
        "get",
        "tenants.tenancy.cnpg-vcluster.io",
        "-o",
        "name",
    ).stdout.strip()
    if tenants:
        raise RuntimeError(f"existing Tenant resources block Phase 2 gate: {tenants}")
    clusters = client.json("get", "clusters.cluster.x-k8s.io", "-A")
    if clusters.get("items"):
        raise RuntimeError("existing CAPI Clusters block Phase 2 gate")
    selector = f"{config['OWNERSHIP_LABEL']}={config['LAB_PREFIX']}"
    for resource in (
        "namespaces",
        "devclusters.infrastructure.cluster.x-k8s.io",
        "kamajicontrolplanes.controlplane.cluster.x-k8s.io",
        "kubeadmconfigtemplates.bootstrap.cluster.x-k8s.io",
        "devmachinetemplates.infrastructure.cluster.x-k8s.io",
        "machinedeployments.cluster.x-k8s.io",
    ):
        response = client.kubectl(
            "get",
            resource,
            "-A",
            "-l",
            selector,
            "-o",
            "name",
            check=False,
        )
        if response.returncode != 0:
            raise RuntimeError(f"failed to inspect clean-cutover resource {resource}")
        if response.stdout.strip():
            raise RuntimeError(
                f"owned provider state blocks Phase 2 gate: {response.stdout.strip()}"
            )
    volumes = run(
        [
            "docker",
            "volume",
            "ls",
            "-q",
            "--filter",
            "label=cnpg-vcluster.capi/role=tenant-storage",
        ],
        timeout=30,
    ).stdout.split()
    if volumes:
        raise RuntimeError(f"controller-owned Docker volumes block Phase 2 gate: {volumes}")


def _tenant_manifest(config: dict[str, str]) -> dict[str, object]:
    return {
        "apiVersion": "tenancy.cnpg-vcluster.io/v1alpha1",
        "kind": "Tenant",
        "metadata": {"name": TENANT_NAME},
        "spec": {
            "kubernetesVersion": config["KUBERNETES_VERSION"].removeprefix("v"),
            "workers": 1,
            "databaseCount": 1,
            "podCIDR": "10.73.0.0/16",
            "serviceCIDR": "10.143.0.0/16",
        },
    }


def _tenant(client: ManagementClient) -> dict[str, object] | None:
    response = client.kubectl(
        "get",
        f"tenant/{TENANT_NAME}",
        "-o",
        "json",
        check=False,
    )
    if response.returncode != 0:
        output = f"{response.stdout}{response.stderr}".lower()
        if "not found" in output or "notfound" in output:
            return None
        raise RuntimeError(f"failed to inspect Phase 2 Tenant: {output}")
    return json.loads(response.stdout)


def _workers_applied(client: ManagementClient) -> dict[str, object] | None:
    tenant = _tenant(client)
    if tenant is None:
        return None
    status = tenant.get("status") or {}
    if status.get("phase") in {"Failed", "OwnershipInvalid"}:
        raise RuntimeError(
            f"Phase 2 Tenant failed: {json.dumps(status, sort_keys=True)}"
        )
    accepted_stages = {
        "WorkersApplied",
        "NetworkSourcesApplied",
        "NetworkResourceSetApplied",
        "NetworkProbeCreated",
        "NetworkReady",
        "PostCNIWorkersReady",
        "StorageApplied",
        "StorageProbeCreated",
        "StorageReady",
        "CNPGOperatorApplied",
        "CNPGStoragePrepared",
        "CNPGClusterApplied",
        "DatabaseProbeCreated",
        "DatabaseReady",
        "Ready",
    }
    if status.get("stage") not in accepted_stages:
        return None
    workers = status.get("workerContainers") or []
    if len(workers) != 1 or not workers[0].get("prepared"):
        raise RuntimeError("Phase 2 worker preparation evidence is incomplete")
    if not status.get("dockerVolume") or not status.get("endpoint"):
        raise RuntimeError("Phase 2 endpoint or Docker volume identity is missing")
    ready = next(
        (
            condition
            for condition in status.get("conditions") or []
            if condition.get("type") == "Ready"
        ),
        None,
    )
    if status.get("stage") == "WorkersApplied" and (
        ready is None or ready.get("status") != "False"
    ):
        raise RuntimeError("Phase 2 incorrectly reported the Tenant Ready")
    return tenant


def _absent(client: ManagementClient) -> bool | None:
    return True if _tenant(client) is None else None


def _restore_after_gate(
    config: dict[str, str],
    client: ManagementClient,
    active_error: BaseException | None,
) -> None:
    cleanup_error: str | None = None
    try:
        tenant = _tenant(client)
        if tenant is not None:
            cleanup = client.kubectl(
                "delete",
                f"tenant/{TENANT_NAME}",
                "--wait=true",
                f"--timeout={config['DELETE_TIMEOUT']}",
                check=False,
            )
            if cleanup.returncode != 0 or _tenant(client) is not None:
                cleanup_error = (
                    "Phase 2 cleanup is incomplete; "
                    "Tenant state remains for the next locked recovery"
                )
    except RuntimeError as exc:
        cleanup_error = f"Phase 2 cleanup inspection failed: {exc}"
    try:
        set_controller_mutation(config, client, enabled=False)
    except RuntimeError as disable_error:
        if active_error is not None:
            active_error.add_note(
                f"failed to restore validation-only mode: {disable_error}"
            )
        else:
            raise
    if cleanup_error is not None:
        if active_error is not None:
            active_error.add_note(cleanup_error)
        else:
            raise RuntimeError(cleanup_error)


def main() -> None:
    config = load_configuration(ROOT)
    client = ManagementClient(ROOT, config)
    with (
        profile_lock(ROOT, "local", exclusive=True, create=True),
        tools_lock(ROOT, exclusive=True),
    ):
        _require_clean_cutover(ROOT, config, client)
        try:
            set_controller_mutation(config, client, enabled=True)
            manifest = _tenant_manifest(config)
            client.kubectl(
                "apply",
                "--server-side",
                "--validate=strict",
                "--field-manager=cnpg-vcluster-controller-phase2-test",
                "-f",
                "-",
                input_text=json.dumps(manifest),
            )
            try:
                first = wait_for(
                    "Tenant Phase 2 WorkersApplied",
                    parse_duration(config["TENANT_CONTROL_PLANE_TIMEOUT"])
                    + parse_duration(config["WORKER_REGISTRATION_TIMEOUT"]),
                    parse_duration(config["WAIT_POLL_INTERVAL"]),
                    lambda: _workers_applied(client),
                )
            except RuntimeError as exc:
                tenant = _tenant(client)
                status = (tenant or {}).get("status") or {}
                raise RuntimeError(
                    f"{exc}; last Tenant status: "
                    f"{json.dumps(status, sort_keys=True)}"
                ) from exc
            endpoint = first["status"]["endpoint"]
            volume = first["status"]["dockerVolume"]["name"]
            client.kubectl(
                "apply",
                "--server-side",
                "--validate=strict",
                "--field-manager=cnpg-vcluster-controller-phase2-test",
                "-f",
                "-",
                input_text=json.dumps(manifest),
            )
            second = wait_for(
                "idempotent Tenant Phase 2 status",
                parse_duration(config["CONDITION_TIMEOUT"]),
                parse_duration(config["WAIT_POLL_INTERVAL"]),
                lambda: _workers_applied(client),
            )
            if (
                second["status"]["endpoint"] != endpoint
                or second["status"]["dockerVolume"]["name"] != volume
            ):
                raise RuntimeError("Phase 2 idempotent reconcile changed stable identity")
            client.kubectl("delete", f"tenant/{TENANT_NAME}", "--wait=false")
            wait_for(
                "Tenant Phase 2 finalization",
                parse_duration(config["DELETE_TIMEOUT"]),
                parse_duration(config["WAIT_POLL_INTERVAL"]),
                lambda: _absent(client),
            )
            namespace = client.kubectl(
                "get",
                f"namespace/{TENANT_NAME}",
                check=False,
            )
            if namespace.returncode == 0:
                raise RuntimeError("Phase 2 Namespace remained after finalization")
            volume_check = run(
                ["docker", "volume", "inspect", volume],
                timeout=30,
                check=False,
            )
            if volume_check.returncode == 0:
                raise RuntimeError("Phase 2 Docker volume remained after finalization")
        finally:
            _restore_after_gate(config, client, sys.exc_info()[1])
            shutil.rmtree(
                ROOT / ".runtime" / "rendered" / "controller-phase2",
                ignore_errors=True,
            )


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        print(redact(str(exc)), file=sys.stderr)
        raise SystemExit(1)
