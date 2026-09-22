#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.lib.config import load_configuration, parse_duration
from scripts.lib.controller import (
    delete_authorized_controller_tenants,
    delete_tenant_resource,
    reserve_tenant_deletion,
    set_controller_mutation,
)
from scripts.lib.kube import ManagementClient, wait_for
from scripts.lib.locking import profile_lock, tools_lock
from scripts.lib.redaction import redact
from scripts.test_controller_phase2 import _require_clean_cutover


TENANTS = {
    "controller-delete-a": ("10.75.0.0/16", "10.145.0.0/16"),
    "controller-delete-b": ("10.76.0.0/16", "10.146.0.0/16"),
}


def _tenant(client: ManagementClient, name: str) -> dict[str, object] | None:
    response = client.kubectl("get", f"tenant/{name}", "-o", "json", check=False)
    if response.returncode == 0:
        return json.loads(response.stdout)
    output = f"{response.stdout}{response.stderr}".lower()
    if "not found" in output or "notfound" in output:
        return None
    raise RuntimeError(f"failed to inspect Tenant {name}: {output}")


def _ready(client: ManagementClient, name: str) -> dict[str, object] | None:
    tenant = _tenant(client, name)
    if tenant is None:
        return None
    status = tenant.get("status") or {}
    if status.get("phase") == "Ready" and status.get("stage") == "Ready":
        return tenant
    if status.get("phase") in {"Failed", "OwnershipInvalid"}:
        raise RuntimeError(f"Tenant {name} failed: {json.dumps(status, sort_keys=True)}")
    return None


def _apply(client: ManagementClient, config: dict[str, str], name: str) -> None:
    pod_cidr, service_cidr = TENANTS[name]
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
                "metadata": {"name": name},
                "spec": {
                    "kubernetesVersion": config["KUBERNETES_VERSION"].removeprefix("v"),
                    "workers": 1,
                    "databaseCount": 1,
                    "podCIDR": pod_cidr,
                    "serviceCIDR": service_cidr,
                },
            }
        ),
    )


def _prove_garbage_collection(client: ManagementClient, config: dict[str, str]) -> None:
    namespace = "controller-gc-proof"
    client.kubectl("create", "namespace", namespace)
    try:
        client.kubectl("-n", namespace, "create", "configmap", "owner", "--from-literal=value=owner")
        owner = client.json("-n", namespace, "get", "configmap/owner")
        dependent = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": "dependent",
                "namespace": namespace,
                "ownerReferences": [
                    {
                        "apiVersion": "v1",
                        "kind": "ConfigMap",
                        "name": "owner",
                        "uid": owner["metadata"]["uid"],
                    }
                ],
            },
            "stringData": {"value": "dependent"},
        }
        client.kubectl("create", "-f", "-", input_text=json.dumps(dependent))
        client.kubectl("-n", namespace, "delete", "configmap/owner", "--wait=true")
        wait_for(
            "garbage collection positive control",
            parse_duration(config["CONDITION_TIMEOUT"]),
            parse_duration(config["WAIT_POLL_INTERVAL"]),
            lambda: (
                True
                if client.kubectl(
                    "-n", namespace, "get", "secret/dependent", check=False
                ).returncode
                != 0
                else None
            ),
        )
    finally:
        client.kubectl("delete", f"namespace/{namespace}", "--wait=true", check=False)


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
            _prove_garbage_collection(client, config)
            for name in TENANTS:
                _apply(client, config, name)
            timeout = (
                parse_duration(config["TENANT_CONTROL_PLANE_TIMEOUT"])
                + parse_duration(config["WORKER_REGISTRATION_TIMEOUT"])
                + parse_duration(config["CNPG_TIMEOUT"])
            )
            ready = {
                name: wait_for(
                    f"Tenant {name} Ready",
                    timeout,
                    parse_duration(config["WAIT_POLL_INTERVAL"]),
                    lambda name=name: _ready(client, name),
                )
                for name in TENANTS
            }
            denied = client.kubectl(
                "delete",
                "tenant/controller-delete-a",
                "--wait=false",
                check=False,
            )
            if denied.returncode == 0 or _tenant(client, "controller-delete-a")["metadata"].get("deletionTimestamp"):
                raise RuntimeError("unreserved Tenant deletion was admitted")

            delete_tenant_resource(client, "controller-delete-a", wait=False)
            wait_for(
                "target deletion snapshots",
                parse_duration(config["CONDITION_TIMEOUT"]),
                parse_duration(config["WAIT_POLL_INTERVAL"]),
                lambda: (
                    tenant
                    if (tenant := _tenant(client, "controller-delete-a"))
                    and (tenant.get("status") or {}).get("foundationSnapshot")
                    and len((tenant.get("status") or {}).get("survivorSnapshots") or []) == 1
                    else None
                ),
            )
            for arguments in (
                ("get", "namespace/controller-delete-a"),
                ("-n", "controller-delete-a", "get", "cluster/controller-delete-a"),
            ):
                if client.kubectl(*arguments, check=False).returncode != 0:
                    raise RuntimeError(
                        "Tenant deletion cascaded management resources before controller barriers"
                    )
            try:
                reserve_tenant_deletion(client, "controller-delete-b")
            except RuntimeError as exc:
                if (
                    "another targeted deletion" not in str(exc)
                    and "another Tenant deletion is active" not in str(exc)
                ):
                    raise
            else:
                raise RuntimeError("competing targeted deletion reservation succeeded")
            if _tenant(client, "controller-delete-b")["metadata"].get("deletionTimestamp"):
                raise RuntimeError("competing Tenant received a deletion timestamp")

            checkpoint = wait_for(
                "live Tenant API cleanup checkpoint",
                parse_duration(config["DELETE_TIMEOUT"]),
                parse_duration(config["WAIT_POLL_INTERVAL"]),
                lambda: (
                    {"absent": True}
                    if (tenant := _tenant(client, "controller-delete-a")) is None
                    else (
                        tenant
                        if ((tenant.get("status") or {}).get("teardown") or {}).get("authority")
                        == "LiveBootstrapRBACCleanupComplete"
                        else None
                    )
                ),
            )
            if not checkpoint.get("absent"):
                client.kubectl("-n", "tenant-system", "rollout", "restart", "deployment/tenant-controller")
                client.kubectl(
                    "-n",
                    "tenant-system",
                    "rollout",
                    "status",
                    "deployment/tenant-controller",
                    f"--timeout={config['CONDITION_TIMEOUT']}",
                )
            wait_for(
                "first reserved Tenant absence",
                parse_duration(config["DELETE_TIMEOUT"]),
                parse_duration(config["WAIT_POLL_INTERVAL"]),
                lambda: True if _tenant(client, "controller-delete-a") is None else None,
            )
            survivor = _ready(client, "controller-delete-b")
            if survivor is None:
                raise RuntimeError("survivor Tenant lost Ready")
            if survivor["metadata"]["uid"] != ready["controller-delete-b"]["metadata"]["uid"]:
                raise RuntimeError("survivor Tenant identity changed")
            delete_authorized_controller_tenants(config, client)
            if any(_tenant(client, name) is not None for name in TENANTS):
                raise RuntimeError("authorized foundation teardown left Tenant resources")
        finally:
            remaining = client.kubectl("get", "tenants.tenancy.cnpg-vcluster.io", "-o", "name", check=False)
            if remaining.returncode == 0 and remaining.stdout.strip():
                delete_authorized_controller_tenants(config, client)
            set_controller_mutation(config, client, enabled=False)


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        print(redact(str(exc)), file=sys.stderr)
        raise SystemExit(1)
