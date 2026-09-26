#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.create_management import create_management
from scripts.destroy import destroy
from scripts.lib.config import load_configuration, parse_duration
from scripts.lib.controller_scenarios import (
    tenant_document,
    tenant_snapshot,
    wait_tenant_absent,
    wait_tenant_ready,
)
from scripts.lib.controller_client import apply_tenant_document, tenant_manifest_document
from scripts.test_e2e import capture_tenant_deletion_identity, verify_tenant_deletion
from scripts.lib.host import prepare_inotify
from scripts.lib.kube import ManagementClient, wait_for
from scripts.lib.locking import profile_lock, tools_lock
from scripts.lib.redaction import redact
from scripts.preflight import run_preflight


NAME = "controller-delete-a"


def _controller_pods(client: ManagementClient) -> list[dict[str, object]]:
    return client.json(
        "-n",
        "tenant-system",
        "get",
        "pods",
        "-l",
        "app.kubernetes.io/name=tenant-controller",
    ).get("items", [])


def _apply(client: ManagementClient, config: dict[str, str]) -> None:
    apply_tenant_document(client, tenant_manifest_document(config, NAME))


def main() -> None:
    os.umask(0o077)
    config = load_configuration(ROOT)
    failure = None
    client = None
    try:
        with (
            profile_lock(ROOT, "local", exclusive=True, create=True),
            tools_lock(ROOT, exclusive=True),
        ):
            prepare_inotify(ROOT, config)
            run_preflight(ROOT, config)
            create_management(ROOT, config)
            client = ManagementClient(ROOT, config)
            _apply(client, config)
            first = wait_tenant_ready(ROOT, config, NAME)
            first_uid = first["metadata"]["uid"]
            first_identity = capture_tenant_deletion_identity(config, client, first)
            old_controller_uids = {
                pod["metadata"]["uid"] for pod in _controller_pods(client)
            }
            if not old_controller_uids:
                raise RuntimeError("Tenant controller Pod is absent before restart")

            client.kubectl(
                "-n",
                "tenant-system",
                "scale",
                "deployment/tenant-controller",
                "--replicas=0",
            )
            wait_for(
                "old Tenant controller Pod absence",
                parse_duration(config["CONDITION_TIMEOUT"]),
                parse_duration(config["WAIT_POLL_INTERVAL"]),
                lambda: True if not _controller_pods(client) else None,
            )
            client.kubectl(
                "delete",
                f"tenant/{NAME}",
                "--wait=false",
            )
            wait_for(
                "pending Tenant deletion before controller restart",
                parse_duration(config["CONDITION_TIMEOUT"]),
                parse_duration(config["WAIT_POLL_INTERVAL"]),
                lambda: (
                    tenant
                    if (tenant := tenant_document(client, NAME)) is not None
                    and tenant["metadata"].get("deletionTimestamp")
                    else None
                ),
            )
            pending = tenant_document(client, NAME)
            if tenant_snapshot(config, client, pending) != {
                key: value for key, value in first_identity.items() if key != "name"
            }:
                raise RuntimeError("controller-stopped deletion changed managed identities")
            client.kubectl(
                "-n",
                "tenant-system",
                "scale",
                "deployment/tenant-controller",
                "--replicas=1",
            )
            client.kubectl(
                "-n",
                "tenant-system",
                "rollout",
                "status",
                "deployment/tenant-controller",
                f"--timeout={config['CONDITION_TIMEOUT']}",
            )
            wait_for(
                "replacement Tenant controller Pod",
                parse_duration(config["CONDITION_TIMEOUT"]),
                parse_duration(config["WAIT_POLL_INTERVAL"]),
                lambda: (
                    pods
                    if (pods := _controller_pods(client))
                    and old_controller_uids.isdisjoint(
                        pod["metadata"]["uid"] for pod in pods
                    )
                    and all(
                        next(
                            (
                                condition
                                for condition in pod.get("status", {}).get(
                                    "conditions", []
                                )
                                if condition.get("type") == "Ready"
                            ),
                            {},
                        ).get("status")
                        == "True"
                        for pod in pods
                    )
                    else None
                ),
            )
            wait_tenant_absent(ROOT, config, NAME)
            verify_tenant_deletion(client, first_identity)

            _apply(client, config)
            recreated = wait_tenant_ready(ROOT, config, NAME)
            if recreated["metadata"]["uid"] == first_uid:
                raise RuntimeError("recreated Tenant retained its previous UID")
            recreated_identity = capture_tenant_deletion_identity(config, client, recreated)
            if recreated_identity["allocationLease"]["uid"] == first_identity["allocationLease"]["uid"]:
                raise RuntimeError("recreated Tenant retained its previous Lease UID")
            if (
                recreated_identity["clusterUID"] == first_identity["clusterUID"]
                or recreated_identity["dockerVolume"]["createdAt"] == first_identity["dockerVolume"]["createdAt"]
                or set(recreated_identity["providerContainers"]) & set(first_identity["providerContainers"])
            ):
                raise RuntimeError("same-name recreation reused a deleted provider identity")
            client.kubectl(
                "delete",
                f"tenant/{NAME}",
                "--wait=true",
                f"--timeout={config['DELETE_TIMEOUT']}",
            )
            wait_tenant_absent(ROOT, config, NAME)
            verify_tenant_deletion(client, recreated_identity)
            print("controller deletion restart recovery checks passed")
    except BaseException as exc:
        failure = exc
    try:
        with tools_lock(ROOT, exclusive=True):
            if client is not None:
                deployment = client.json("-n", "tenant-system", "get", "deployment/tenant-controller")
                if deployment["spec"]["replicas"] == 0:
                    client.kubectl("-n", "tenant-system", "scale", "deployment/tenant-controller", "--replicas=1")
                    client.kubectl("-n", "tenant-system", "rollout", "status",
                                   "deployment/tenant-controller", f"--timeout={config['CONDITION_TIMEOUT']}")
            destroy(ROOT, config)
    except BaseException as cleanup:
        if failure is None:
            raise
        failure.add_note(f"cleanup also failed: {redact(str(cleanup))}")
    if failure is not None:
        raise failure


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        print(redact(str(exc)), file=sys.stderr)
        raise SystemExit(1)
