#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.lib.config import load_configuration, parse_duration
from scripts.lib.controller import delete_tenant_resource, set_controller_mutation
from scripts.lib.controller_scenarios import tenant_snapshot
from scripts.lib.kube import ManagementClient, wait_for
from scripts.lib.locking import profile_lock, tools_lock
from scripts.lib.redaction import redact
from scripts.test_controller_phase2 import _require_clean_cutover


TENANT_NAME = "controller-phase3"


def _tenant(client: ManagementClient) -> dict[str, object] | None:
    response = client.kubectl(
        "get", f"tenant/{TENANT_NAME}", "-o", "json", check=False
    )
    if response.returncode == 0:
        return json.loads(response.stdout)
    output = f"{response.stdout}{response.stderr}".lower()
    if "not found" in output or "notfound" in output:
        return None
    raise RuntimeError(f"failed to inspect Phase 3 Tenant: {output}")


def _ready(client: ManagementClient) -> dict[str, object] | None:
    tenant = _tenant(client)
    if tenant is None:
        return None
    status = tenant.get("status") or {}
    if status.get("phase") in {"Failed", "OwnershipInvalid"}:
        raise RuntimeError(
            f"Phase 3 Tenant failed: {json.dumps(status, sort_keys=True)}"
        )
    if status.get("phase") != "Ready":
        return None
    ready = next(
        (
            condition
            for condition in status.get("conditions", [])
            if condition.get("type") == "Ready"
        ),
        {},
    )
    if ready.get("status") != "True":
        raise RuntimeError("Phase 3 Ready condition is not true")
    metadata = tenant.get("metadata") or {}
    if ready.get("observedGeneration") != metadata.get("generation"):
        raise RuntimeError("Phase 3 Ready condition generation is stale")
    if not status.get("clusterUID") or not status.get("foundationHash"):
        raise RuntimeError("Phase 3 root identity is incomplete")
    return tenant


def main() -> None:
    config = load_configuration(ROOT)
    client = ManagementClient(ROOT, config)
    primary: BaseException | None = None
    with (
        profile_lock(ROOT, "local", exclusive=True, create=True),
        tools_lock(ROOT, exclusive=True),
    ):
        _require_clean_cutover(ROOT, config, client)
        try:
            set_controller_mutation(config, client, enabled=True)
            manifest = {
                "apiVersion": "tenancy.cnpg-vcluster.io/v1alpha1",
                "kind": "Tenant",
                "metadata": {"name": TENANT_NAME},
                "spec": {
                    "kubernetesVersion": config["KUBERNETES_VERSION"].removeprefix("v"),
                    "workers": 1,
                    "databaseCount": 2,
                    "podCIDR": "10.74.0.0/16",
                    "serviceCIDR": "10.144.0.0/16",
                },
            }
            client.kubectl(
                "apply",
                "--server-side",
                "--validate=strict",
                "--field-manager=cnpg-vcluster-controller-phase3-test",
                "-f",
                "-",
                input_text=json.dumps(manifest),
            )
            first = wait_for(
                "Tenant Phase 3 Ready",
                parse_duration(config["TENANT_CONTROL_PLANE_TIMEOUT"])
                + parse_duration(config["WORKER_REGISTRATION_TIMEOUT"])
                + parse_duration(config["CNPG_TIMEOUT"]),
                parse_duration(config["WAIT_POLL_INTERVAL"]),
                lambda: _ready(client),
            )
            identities = tenant_snapshot(config, client, first)
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
            second = wait_for(
                "Tenant Ready after controller restart",
                parse_duration(config["CONDITION_TIMEOUT"]),
                parse_duration(config["WAIT_POLL_INTERVAL"]),
                lambda: _ready(client),
            )
            after = tenant_snapshot(config, client, second)
            if after != identities:
                raise RuntimeError("Phase 3 identities changed across controller restart")
            status = client.kubectl(
                "get", f"tenant/{TENANT_NAME}", "-o", "json"
            )
            from scripts.controller_tenant_status import evaluate_tenant

            foundation = client.json(
                "-n", "tenant-system", "get", "configmap/tenant-foundation"
            )
            evaluation = evaluate_tenant(
                json.loads(status.stdout),
                foundation_hash=foundation["data"]["foundation.sha256"],
            )
            if evaluation["classification"] != "ready":
                raise RuntimeError(
                    "independent Phase 3 status evaluation is not Ready: "
                    + json.dumps(evaluation["blockers"], sort_keys=True)
                )
            delete_tenant_resource(client, TENANT_NAME, wait=False)
            wait_for(
                "Tenant Phase 3 finalization",
                parse_duration(config["DELETE_TIMEOUT"]),
                parse_duration(config["WAIT_POLL_INTERVAL"]),
                lambda: True if _tenant(client) is None else None,
            )
        except BaseException as exc:
            primary = exc
            raise
        finally:
            tenant = _tenant(client)
            if tenant is not None:
                cleanup = delete_tenant_resource(
                    client,
                    TENANT_NAME,
                    wait=True,
                    timeout=config["DELETE_TIMEOUT"],
                    check=False,
                )
                if cleanup.returncode != 0 and primary is not None:
                    primary.add_note("Phase 3 Tenant cleanup is incomplete")
            set_controller_mutation(config, client, enabled=False)


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        print(redact(str(exc)), file=sys.stderr)
        raise SystemExit(1)
