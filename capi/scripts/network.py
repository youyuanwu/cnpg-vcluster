from __future__ import annotations

import json
import time
from pathlib import Path

from scripts.endpoint import run_endpoint_gate
from scripts.lib.addons import verify_network, wait_network_ready
from scripts.lib.config import parse_duration
from scripts.lib.controller_scenarios import (
    delete_controller_tenant,
    tenant_document,
    tenant_manifest,
    wait_tenant_ready,
)
from scripts.lib.kube import wait_for
from scripts.lib.tenants import _tenant_kubectl


def _verify_control_plane_active(client, config, tenant) -> None:
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
    if "cluster.x-k8s.io/paused" in (kcp["metadata"].get("annotations") or {}):
        raise RuntimeError("KamajiControlPlane is paused")
    required = (
        (config["CAPI_NAMESPACE"], "capi-controller-manager"),
        (config["CABPK_NAMESPACE"], "capi-kubeadm-bootstrap-controller-manager"),
        (config["CAPD_NAMESPACE"], "capd-controller-manager"),
        (config["KAMAJI_CAPI_NAMESPACE"], "capi-kamaji-controller-manager"),
        (config["MANAGEMENT_NAMESPACE"], "kamaji"),
    )
    for namespace, deployment in required:
        payload = json.loads(
            client.kubectl(
                "-n",
                namespace,
                "get",
                f"deployment/{deployment}",
                "-o",
                "json",
            ).stdout
        )
        if payload["spec"]["replicas"] != payload["status"].get("availableReplicas"):
            raise RuntimeError(
                f"required controller is unavailable: {namespace}/{deployment}"
            )


def _restart_kube_proxy(root: Path, config: dict[str, str], tenant) -> None:
    pods = json.loads(
        _tenant_kubectl(
            root,
            config,
            tenant,
            "-n",
            "kube-system",
            "get",
            "pods",
            "-l",
            "k8s-app=capi-kube-proxy",
            "-o",
            "json",
        ).stdout
    )["items"]
    if len(pods) != tenant.workers:
        raise RuntimeError("expected one kube-proxy Pod per worker")
    old_uids = {pod["metadata"]["uid"] for pod in pods}
    _tenant_kubectl(
        root,
        config,
        tenant,
        "-n",
        "kube-system",
        "delete",
        "pods",
        "-l",
        "k8s-app=capi-kube-proxy",
        "--wait=true",
    )

    def replaced():
        items = json.loads(
            _tenant_kubectl(
                root,
                config,
                tenant,
                "-n",
                "kube-system",
                "get",
                "pods",
                "-l",
                "k8s-app=capi-kube-proxy",
                "-o",
                "json",
            ).stdout
        )["items"]
        if len(items) != tenant.workers:
            return None
        if old_uids & {item["metadata"]["uid"] for item in items}:
            return None
        return items if all(
            next(
                (
                    condition
                    for condition in item.get("status", {}).get("conditions", [])
                    if condition.get("type") == "Ready"
                ),
                {},
            ).get("status")
            == "True"
            for item in items
        ) else None

    replacements = wait_for(
        "replacement kube-proxy Pods",
        parse_duration(config["TENANT_CONTROL_PLANE_TIMEOUT"]),
        parse_duration(config["WAIT_POLL_INTERVAL"]),
        replaced,
    )
    for pod in replacements:
        logs = _tenant_kubectl(
            root,
            config,
            tenant,
            "-n",
            "kube-system",
            "logs",
            f"pod/{pod['metadata']['name']}",
            "--tail=200",
        ).stdout
        if "nf_conntrack_max: permission denied" in logs:
            raise RuntimeError("replacement kube-proxy hit the conntrack permission crash")


def _restart_controller(client, config, tenant) -> None:
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
        f"--timeout={config['PROVIDER_TIMEOUT']}",
    )
    wait_tenant_ready(client.root, config, tenant.name)


def _verify_static_kube_proxy(
    root: Path,
    config: dict[str, str],
    tenant,
) -> None:
    from scripts.lib.kube import ManagementClient

    client = ManagementClient(root, config)
    arguments = ("-n", "kube-system")
    resource = "configmap/capi-kube-proxy"
    original = json.loads(_tenant_kubectl(
        root, config, tenant, *arguments, "get", resource, "-o", "json",
    ).stdout)
    annotation = "tenancy.cnpg-vcluster.io/tenant-uid"
    uid = original["metadata"]["annotations"][annotation]

    def reconcile():
        # Static objects are not content watches; wake the Tenant without changing its spec.
        client.kubectl("annotate", f"tenant/{tenant.name}", "--overwrite",
                       f"tenancy.cnpg-vcluster.io/live-probe={time.monotonic_ns()}")

    _tenant_kubectl(root, config, tenant, *arguments, "annotate", resource,
                    f"{annotation}=foreign-fixture", "--overwrite")
    reconcile()
    try:
        wait_for(
            "static kube-proxy foreign ownership refusal",
            parse_duration(config["CONDITION_TIMEOUT"]), 2,
            lambda: (
                document if (document := tenant_document(client, tenant.name))
                and document.get("status", {}).get("phase") == "OwnershipInvalid" else None
            ),
        )
        foreign = json.loads(_tenant_kubectl(
            root, config, tenant, *arguments, "get", resource, "-o", "json",
        ).stdout)
        if (
            foreign["metadata"]["uid"] != original["metadata"]["uid"]
            or foreign["metadata"]["annotations"][annotation] != "foreign-fixture"
            or foreign["data"] != original["data"]
        ):
            raise RuntimeError("controller modified a foreign static resource")
    finally:
        _tenant_kubectl(
            root, config, tenant, *arguments, "patch", resource, "--type=json", "-p",
            json.dumps([
                {"op": "test", "path": "/metadata/uid", "value": original["metadata"]["uid"]},
                {"op": "test", "path": "/metadata/annotations/tenancy.cnpg-vcluster.io~1tenant-uid", "value": "foreign-fixture"},
                {"op": "replace", "path": "/metadata/annotations/tenancy.cnpg-vcluster.io~1tenant-uid", "value": uid},
            ]),
        )
        reconcile()
    wait_tenant_ready(root, config, tenant.name)
    _tenant_kubectl(root, config, tenant, *arguments, "delete", resource, "--wait=true")
    reconcile()

    def recreated():
        response = _tenant_kubectl(
            root, config, tenant, *arguments, "get", resource, "-o", "json",
            "--ignore-not-found=true",
        )
        if not response.stdout.strip():
            return None
        observed = json.loads(response.stdout)
        if observed["metadata"]["uid"] == original["metadata"]["uid"]:
            return None
        if (
            observed["data"] != original["data"]
            or observed["metadata"]["annotations"] != original["metadata"]["annotations"]
            or observed["metadata"]["labels"] != original["metadata"]["labels"]
        ):
            raise RuntimeError("recreated static resource content or ownership differs")
        return observed

    wait_for(
        "controller static kube-proxy recreation",
        parse_duration(config["TENANT_CONTROL_PLANE_TIMEOUT"]), 2, recreated,
    )


def _drift_machine_deployment_and_wait_for_repair(
    client,
    config: dict[str, str],
    tenant,
) -> None:
    client.kubectl(
        "-n",
        tenant.namespace,
        "patch",
        f"machinedeployment/{tenant.name}-worker",
        "--type=merge",
        "-p",
        json.dumps({"spec": {"replicas": tenant.workers + 1}}),
    )
    wait_for(
        "controller MachineDeployment drift repair",
        parse_duration(config["WORKER_REGISTRATION_TIMEOUT"]),
        parse_duration(config["WAIT_POLL_INTERVAL"]),
        lambda: (
            True
            if int(
                client.kubectl(
                    "-n",
                    tenant.namespace,
                    "get",
                    f"machinedeployment/{tenant.name}-worker",
                    "-o",
                    "jsonpath={.spec.replicas}",
                ).stdout
            )
            == tenant.workers
            else None
        ),
    )
    wait_tenant_ready(client.root, config, tenant.name)


def run_network_gate(
    root: Path,
    config: dict[str, str],
    *,
    cleanup: bool = True,
    manifest: Path | None = None,
):
    client, tenant, _ = run_endpoint_gate(
        root,
        config,
        cleanup=False,
        manifest=manifest or tenant_manifest(root, "tenant-example"),
    )
    succeeded = False
    try:
        wait_network_ready(root, config, tenant)
        verify_network(root, config, tenant)
        _verify_control_plane_active(client, config, tenant)
        _restart_kube_proxy(root, config, tenant)
        wait_tenant_ready(root, config, tenant.name)
        verify_network(root, config, tenant)
        _verify_static_kube_proxy(root, config, tenant)
        _drift_machine_deployment_and_wait_for_repair(client, config, tenant)
        _restart_controller(client, config, tenant)
        verify_network(root, config, tenant)
        _verify_control_plane_active(client, config, tenant)
        print("tenant networking and controller repair checks passed")
        succeeded = True
        return client, tenant
    finally:
        if cleanup or not succeeded:
            delete_controller_tenant(root, config, tenant)
