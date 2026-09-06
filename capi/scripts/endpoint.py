from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from scripts.create_management import create_management
from scripts.lib.files import write_private_file
from scripts.lib.kube import ManagementClient
from scripts.lib.management import management_status
from scripts.lib.process import run
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
    tenant_kubeconfig_path,
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
    created = datetime.fromisoformat(
        secret["metadata"]["creationTimestamp"].replace("Z", "+00:00")
    )
    if (
        datetime.now(timezone.utc) - created
    ).total_seconds() > int(config["BOOTSTRAP_SECRET_RETENTION"].removesuffix("s")):
        raise RuntimeError("bootstrap Secret exceeded its configured retention window")

    allowed = set(config["BOOTSTRAP_SECRET_READER_SERVICE_ACCOUNTS"].split())
    service_accounts = json.loads(
        client.kubectl("get", "serviceaccounts", "-A", "-o", "json").stdout
    )["items"]
    identities = {
        f"system:serviceaccount:{item['metadata']['namespace']}:{item['metadata']['name']}"
        for item in service_accounts
    }
    observed_allowed = set()
    for identity in identities:
        result = client.kubectl(
            "auth",
            "can-i",
            "get",
            f"secret/{name}",
            "-n",
            tenant.namespace,
            f"--as={identity}",
            check=False,
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


def run_endpoint_gate(
    root: Path,
    config: dict[str, str],
    *,
    cleanup: bool = True,
):
    if not management_status(root, config).get("apiReady"):
        create_management(root, config)
    client = ManagementClient(root, config)
    tenant = spike_tenant(root, config)
    delete_tenant(root, config, client, tenant)
    evidence = root / ".runtime" / "evidence" / "endpoint-failure.txt"
    success_evidence = root / ".runtime" / "evidence" / "endpoint-success.json"
    evidence.unlink(missing_ok=True)
    success_evidence.unlink(missing_ok=True)
    bootstrap_secret_names: list[str] = []
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
        bootstrap_secret_names.append(str(registered["secret"]))
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
        if (
            not ready
            or ready.get("status") != "False"
            or not re.search(
                r"(?i)(networkpluginnotready|cni plugin.*not initialized)",
                ready.get("message") or "",
            )
        ):
            raise RuntimeError(
                "pre-CNI Node does not report the expected network-plugin NotReady condition"
            )

        marker = tenant.storage_host_path / "phase3-marker"
        write_private_file(marker, "phase3-marker\n")
        old_machine_uid = registered["machine"]["metadata"]["uid"]
        old_machine_name = registered["machine"]["metadata"]["name"]
        client.kubectl(
            "-n",
            tenant.namespace,
            "delete",
            f"machine/{old_machine_name}",
            "--wait=true",
            f"--timeout={config['DELETE_TIMEOUT']}",
        )
        replacement = wait_for_registered_node(root, config, client, tenant)
        if replacement["machine"]["metadata"]["uid"] == old_machine_uid:
            raise RuntimeError("Machine replacement retained the original Machine UID")
        if (
            run(["docker", "inspect", old_machine_name], timeout=30, check=False).returncode
            == 0
        ):
            raise RuntimeError("old CAPD worker container remained after Machine replacement")
        if marker.read_text(encoding="utf-8") != "phase3-marker\n":
            raise RuntimeError("tenant host marker did not survive Machine replacement")
        verify_authoritative_endpoint(root, config, client, tenant, replacement)
        verify_worker_runtime(config, tenant, replacement)
        _verify_bootstrap_secret(config, client, tenant, replacement)
        bootstrap_secret_names.append(str(replacement["secret"]))

        kubeconfig_text = tenant_kubeconfig_path(root, tenant).read_text(encoding="utf-8")
        ca_match = re.search(r"certificate-authority-data:\s*(\S+)", kubeconfig_text)
        if not ca_match:
            raise RuntimeError("tenant kubeconfig has no CA data")
        bootstrap_secret = json.loads(
            client.kubectl(
                "-n",
                tenant.namespace,
                "get",
                f"secret/{replacement['secret']}",
                "-o",
                "json",
            ).stdout
        )
        write_private_file(
            success_evidence,
            json.dumps(
                {
                    "bootstrapSHA256": hashlib.sha256(
                        base64.b64decode(bootstrap_secret["data"]["value"])
                    ).hexdigest(),
                    "caSHA256": hashlib.sha256(
                        base64.b64decode(ca_match.group(1))
                    ).hexdigest(),
                    "cluster": tenant.name,
                    "endpoint": f"{tenant.vip}:{config['SPIKE_API_PORT']}",
                    "machineUID": replacement["machine"]["metadata"]["uid"],
                    "nodeUID": replacement["node"]["metadata"]["uid"],
                },
                sort_keys=True,
            )
            + "\n",
        )
        print(
            json.dumps(
                {
                    "cluster": tenant.name,
                    "endpoint": f"{tenant.vip}:{config['SPIKE_API_PORT']}",
                    "machine": replacement["machine"]["metadata"]["name"],
                    "node": replacement["node"]["metadata"]["name"],
                    "nodeReady": ready.get("status") if ready else "Unknown",
                },
                sort_keys=True,
            )
        )
        return client, tenant, replacement
    except Exception as exc:
        write_private_file(evidence, redact(str(exc)) + "\n")
        raise
    finally:
        if cleanup:
            delete_tenant(root, config, client, tenant)
            for secret_name in bootstrap_secret_names:
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
                    raise RuntimeError(
                        f"bootstrap Secret remained after deletion: {secret_name}"
                    )
