from __future__ import annotations

import json
from pathlib import Path

from scripts.endpoint import run_endpoint_gate
from scripts.lib.addons import verify_network, wait_network_ready
from scripts.lib.config import parse_duration
from scripts.lib.controller_scenarios import (
    delete_controller_tenant,
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


def _drift_kube_proxy_and_wait_for_repair(
    root: Path,
    config: dict[str, str],
    tenant,
) -> None:
    _tenant_kubectl(
        root,
        config,
        tenant,
        "-n",
        "kube-system",
        "patch",
        "configmap/capi-kube-proxy",
        "--type=merge",
        "-p",
        '{"data":{"config.conf":"apiVersion: kubeproxy.config.k8s.io/v1alpha1\\n'
        'kind: KubeProxyConfiguration\\nconntrack:\\n  maxPerCore: 1\\n"}}',
    )
    try:
        verify_network(root, config, tenant)
    except RuntimeError:
        pass
    else:
        raise RuntimeError("kube-proxy drift was not detected")

    def repaired():
        try:
            verify_network(root, config, tenant)
            return True
        except RuntimeError:
            return None

    wait_for(
        "controller kube-proxy drift repair",
        parse_duration(config["TENANT_CONTROL_PLANE_TIMEOUT"]),
        parse_duration(config["WAIT_POLL_INTERVAL"]),
        repaired,
    )


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
        _drift_kube_proxy_and_wait_for_repair(root, config, tenant)
        _restart_controller(client, config, tenant)
        verify_network(root, config, tenant)
        _verify_control_plane_active(client, config, tenant)
        print("tenant networking and controller repair checks passed")
        succeeded = True
        return client, tenant
    finally:
        if cleanup or not succeeded:
            delete_controller_tenant(root, config, tenant)
