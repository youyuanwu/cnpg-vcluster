#!/usr/bin/env python3
from __future__ import annotations

import json
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
    wait_tenant_absent,
    wait_tenant_ready,
)
from scripts.lib.host import prepare_inotify
from scripts.lib.kube import ManagementClient, wait_for
from scripts.lib.locking import profile_lock, tools_lock
from scripts.lib.redaction import redact
from scripts.preflight import run_preflight


NAME = "controller-delete-a"


def _apply(client: ManagementClient, config: dict[str, str]) -> None:
    client.kubectl(
        "apply",
        "--server-side",
        "--validate=strict",
        "--field-manager=cnpg-vcluster-controller-deletion-test",
        "-f",
        "-",
        input_text=json.dumps(
            {
                "apiVersion": "tenancy.cnpg-vcluster.io/v1alpha1",
                "kind": "Tenant",
                "metadata": {"name": NAME},
                "spec": {
                    "kubernetesVersion": config["KUBERNETES_VERSION"].removeprefix("v"),
                    "workers": 1,
                    "databaseCount": 1,
                    "podCIDR": "10.75.0.0/16",
                    "serviceCIDR": "10.145.0.0/16",
                },
            }
        ),
    )


def main() -> None:
    os.umask(0o077)
    config = load_configuration(ROOT)
    failure = None
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

            client.kubectl(
                "delete",
                f"tenant/{NAME}",
                "--wait=false",
            )

            checkpoint = wait_for(
                "live Tenant cleanup checkpoint",
                parse_duration(config["DELETE_TIMEOUT"]),
                parse_duration(config["WAIT_POLL_INTERVAL"]),
                lambda: (
                    {"absent": True}
                    if (tenant := tenant_document(client, NAME)) is None
                    else tenant
                    if ((tenant.get("status") or {}).get("teardown") or {}).get(
                        "authority"
                    )
                    == "LiveBootstrapRBACCleanupComplete"
                    else None
                ),
            )
            if not checkpoint.get("absent"):
                client.kubectl(
                    "-n",
                    "tenant-system",
                    "rollout",
                    "restart",
                    "deployment/tenant-controller",
                )
                client.kubectl(
                    "-n",
                    "tenant-system",
                    "rollout",
                    "status",
                    "deployment/tenant-controller",
                    f"--timeout={config['CONDITION_TIMEOUT']}",
                )
            wait_tenant_absent(ROOT, config, NAME)

            _apply(client, config)
            recreated = wait_tenant_ready(ROOT, config, NAME)
            if recreated["metadata"]["uid"] == first_uid:
                raise RuntimeError("recreated Tenant retained its previous UID")
            client.kubectl(
                "delete",
                f"tenant/{NAME}",
                "--wait=true",
                f"--timeout={config['DELETE_TIMEOUT']}",
            )
            wait_tenant_absent(ROOT, config, NAME)
            print("controller deletion restart recovery checks passed")
    except BaseException as exc:
        failure = exc
    try:
        with tools_lock(ROOT, exclusive=True):
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
