#!/usr/bin/env python3
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.lib.config import load_configuration, parse_duration
from scripts.lib.controller import delete_tenant_resource, set_controller_mutation
from scripts.lib.controller_cutover import require_clean_controller_cutover
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
    require_clean_controller_cutover(root, config, client)


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
        raise RuntimeError(        f"failed to inspect convergence Tenant: {output}")
    return json.loads(response.stdout)


def _converging(
    client: ManagementClient,
    config: dict[str, str],
) -> dict[str, object] | None:
    tenant = _tenant(client)
    if tenant is None:
        return None
    status = tenant.get("status") or {}
    if status.get("phase") in {"Failed", "OwnershipInvalid"}:
        raise RuntimeError(
            f"convergence Tenant failed: {json.dumps(status, sort_keys=True)}"
        )
    if not all(
        (
            status.get("endpoint"),
            status.get("foundationHash"),
            status.get("clusterUID"),
            status.get("tenantAPICreationAuthorized") is True,
        )
    ):
        return None
    workers = run(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            f"label=io.x-k8s.kind.cluster={TENANT_NAME}",
            "--filter",
            "label=io.x-k8s.kind.role=worker",
            "--format",
            "{{.ID}}",
        ],
        timeout=30,
    ).stdout.split()
    if len(workers) != 1:
        return None
    volume = _volume_name(config)
    if run(
        ["docker", "volume", "inspect", volume],
        timeout=30,
        check=False,
    ).returncode != 0:
        return None
    ready = next(
        (
            condition
            for condition in status.get("conditions") or []
            if condition.get("type") == "Ready"
        ),
        None,
    )
    if status.get("phase") != "Ready" and (
        ready is None or ready.get("status") != "False"
    ):
        raise RuntimeError("converging Tenant incorrectly reported Ready")
    return tenant


def _volume_name(config: dict[str, str]) -> str:
    return f"{config['LAB_PREFIX']}-{TENANT_NAME}-storage"


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
            cleanup = delete_tenant_resource(
                client,
                TENANT_NAME,
                wait=True,
                timeout=config["DELETE_TIMEOUT"],
                check=False,
            )
            if cleanup.returncode != 0 or _tenant(client) is not None:
                cleanup_error = (
                    "convergence cleanup is incomplete; "
                    "Tenant state remains for the next locked recovery"
                )
    except RuntimeError as exc:
        cleanup_error = f"convergence cleanup inspection failed: {exc}"
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
                    "Tenant worker convergence",
                    parse_duration(config["TENANT_CONTROL_PLANE_TIMEOUT"])
                    + parse_duration(config["WORKER_REGISTRATION_TIMEOUT"]),
                    parse_duration(config["WAIT_POLL_INTERVAL"]),
                    lambda: _converging(client, config),
                )
            except RuntimeError as exc:
                tenant = _tenant(client)
                status = (tenant or {}).get("status") or {}
                raise RuntimeError(
                    f"{exc}; last Tenant status: "
                    f"{json.dumps(status, sort_keys=True)}"
                ) from exc
            endpoint = first["status"]["endpoint"]
            cluster_uid = first["status"]["clusterUID"]
            volume = _volume_name(config)
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
                "idempotent Tenant convergence status",
                parse_duration(config["CONDITION_TIMEOUT"]),
                parse_duration(config["WAIT_POLL_INTERVAL"]),
                lambda: _converging(client, config),
            )
            if (
                second["status"]["endpoint"] != endpoint
                or second["status"]["clusterUID"] != cluster_uid
            ):
                raise RuntimeError("idempotent convergence changed stable identity")
            delete_tenant_resource(client, TENANT_NAME, wait=False)
            wait_for(
                "Tenant convergence finalization",
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
                raise RuntimeError("convergence Namespace remained after finalization")
            volume_check = run(
                ["docker", "volume", "inspect", volume],
                timeout=30,
                check=False,
            )
            if volume_check.returncode == 0:
                raise RuntimeError("convergence Docker volume remained after finalization")
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
