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
from scripts.lib.controller_scenarios import (
    apply_controller_tenant,
    delete_controller_tenant,
    manifest_tenant_name,
    tenant_manifest,
)
from scripts.lib.controller_client import delete_tenant as delete_tenant_resource
from scripts.lib.tenants import (
    _tenant_kubectl,
    tenant_kubeconfig_path,
    write_storage_marker,
    read_storage_marker,
    verify_tenant_control_plane_contract,
    verify_tenant_management_ownership,
    verify_authoritative_endpoint,
    verify_worker_runtime,
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
    kubeadm = json.loads(
        client.kubectl(
            "-n",
            tenant.namespace,
            "get",
            f"kubeadmconfig/{name}",
            "-o",
            "json",
        ).stdout
    )
    owner = owners[0] if len(owners) == 1 else {}
    if (
        owner.get("apiVersion") != "bootstrap.cluster.x-k8s.io/v1beta2"
        or owner.get("kind") != "KubeadmConfig"
        or owner.get("name") != name
        or owner.get("uid") != kubeadm["metadata"]["uid"]
        or owner.get("controller") is not True
    ):
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
    manifest: Path | None = None,
):
    selected_manifest = manifest or tenant_manifest(root, "tenant-example")
    selected_name = manifest_tenant_name(selected_manifest)
    status = management_status(root, config)
    if not status.get("apiReady"):
        create_management(root, config)
    else:
        client = ManagementClient(root, config)
        deployment_response = client.kubectl(
            "-n",
            "tenant-system",
            "get",
            "deployment/tenant-controller",
            "-o",
            "json",
            check=False,
        )
        if deployment_response.returncode != 0:
            if "NotFound" not in deployment_response.stderr:
                raise RuntimeError(
                    "Tenant controller inspection failed: "
                    + deployment_response.stderr
                )
            create_management(root, config)
            deployment_response = None
        if deployment_response is None:
            arguments = ["--mutation-enabled=true"]
        else:
            deployment = json.loads(deployment_response.stdout)
            arguments = deployment["spec"]["template"]["spec"]["containers"][0].get(
                "args", []
            )
        if "--mutation-enabled=true" not in arguments:
            client.kubectl(
                "apply",
                "--server-side",
                "--field-manager=cnpg-vcluster-controller-cutover",
                "--force-conflicts",
                "-f",
                str(
                    root
                    / "controller"
                    / "config"
                    / "webhook"
                    / "validating-webhook.yaml"
                ),
            )
            delete_tenant_resource(root, config, selected_name)
            create_management(root, config)
    evidence = root / ".runtime" / "evidence" / "endpoint-failure.txt"
    success_evidence = root / ".runtime" / "evidence" / "endpoint-success.json"
    evidence.unlink(missing_ok=True)
    success_evidence.unlink(missing_ok=True)
    bootstrap_secret_names: list[str] = []
    client = None
    tenant = None
    succeeded = False
    try:
        client, tenant, _ = apply_controller_tenant(
            root, config, selected_manifest
        )
        from scripts.machines import worker_snapshot

        resources = verify_tenant_management_ownership(config, client, tenant)
        verify_tenant_control_plane_contract(config, tenant, resources)
        workers = worker_snapshot(root, config, client, tenant)
        registered = None
        for machine in json.loads(
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
        )["items"]:
            name = machine["metadata"]["name"]
            node_name = machine.get("status", {}).get("nodeRef", {}).get("name")
            if not node_name:
                raise RuntimeError(f"Machine has no Node reference: {name}")
            candidate = {
                "machine": machine,
                "devmachine": json.loads(
                    client.kubectl(
                        "-n",
                        tenant.namespace,
                        "get",
                        f"devmachine/{name}",
                        "-o",
                        "json",
                    ).stdout
                ),
                "node": json.loads(
                    _tenant_kubectl(
                        root,
                        config,
                        tenant,
                        "get",
                        f"node/{node_name}",
                        "-o",
                        "json",
                    ).stdout
                ),
                "secret": name,
            }
            verify_authoritative_endpoint(root, config, client, tenant, candidate)
            verify_worker_runtime(root, config, tenant, candidate)
            _verify_bootstrap_secret(config, client, tenant, candidate)
            bootstrap_secret_names.append(name)
            registered = candidate
        if registered is None:
            raise RuntimeError("Tenant has no registered worker")
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

        write_storage_marker(config, tenant, "phase3-marker", "phase3-marker\n")
        if read_storage_marker(config, tenant, "phase3-marker") != "phase3-marker\n":
            raise RuntimeError("tenant host marker is not readable")

        kubeconfig_text = tenant_kubeconfig_path(root, tenant).read_text(encoding="utf-8")
        ca_match = re.search(r"certificate-authority-data:\s*(\S+)", kubeconfig_text)
        if not ca_match:
            raise RuntimeError("tenant kubeconfig has no CA data")
        bootstrap_secret = json.loads(
            client.kubectl(
                "-n",
                tenant.namespace,
                "get",
                f"secret/{registered['secret']}",
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
                    "machineUID": registered["machine"]["metadata"]["uid"],
                    "nodeUID": registered["node"]["metadata"]["uid"],
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
                    "machine": registered["machine"]["metadata"]["name"],
                    "node": registered["node"]["metadata"]["name"],
                    "workers": sorted(workers),
                },
                sort_keys=True,
            )
        )
        succeeded = True
        return client, tenant, registered
    except Exception as exc:
        write_private_file(evidence, redact(str(exc)) + "\n")
        raise
    finally:
        if (cleanup or not succeeded) and tenant is not None:
            delete_controller_tenant(root, config, tenant)
            if client is not None:
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
