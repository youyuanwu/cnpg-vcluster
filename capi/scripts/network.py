from __future__ import annotations

import json
from pathlib import Path

from scripts.endpoint import run_endpoint_gate
from scripts.lib.addons import (
    apply_addons,
    delete_addons,
    render_calico,
    render_kube_proxy,
    render_resource_set,
    verify_network,
    wait_network_ready,
)
from scripts.lib.config import parse_duration
from scripts.lib.kube import wait_for
from scripts.lib.tenants import _tenant_kubectl, delete_tenant


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
            raise RuntimeError(f"required controller is unavailable: {namespace}/{deployment}")


def _restart_kube_proxy(root: Path, config: dict[str, str], tenant) -> None:
    pod = json.loads(
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
    if len(pod) != 1:
        raise RuntimeError("expected exactly one kube-proxy Pod")
    old_uid = pod[0]["metadata"]["uid"]
    old_name = pod[0]["metadata"]["name"]
    _tenant_kubectl(
        root,
        config,
        tenant,
        "-n",
        "kube-system",
        "delete",
        f"pod/{old_name}",
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
        if len(items) != 1 or items[0]["metadata"]["uid"] == old_uid:
            return None
        ready = next(
            (
                condition
                for condition in items[0]["status"].get("conditions", [])
                if condition["type"] == "Ready"
            ),
            {},
        )
        return items[0] if ready.get("status") == "True" else None

    replacement = wait_for(
        "replacement kube-proxy Pod",
        parse_duration(config["TENANT_CONTROL_PLANE_TIMEOUT"]),
        parse_duration(config["WAIT_POLL_INTERVAL"]),
        replaced,
    )
    logs = _tenant_kubectl(
        root,
        config,
        tenant,
        "-n",
        "kube-system",
        "logs",
        f"pod/{replacement['metadata']['name']}",
        "--tail=200",
    ).stdout
    if "nf_conntrack_max: permission denied" in logs:
        raise RuntimeError("replacement kube-proxy hit the conntrack permission crash")


def _repair_addons(root: Path, config: dict[str, str], client, tenant) -> None:
    for path in (render_calico(root, config, tenant), render_kube_proxy(root, config, tenant)):
        _tenant_kubectl(
            root,
            config,
            tenant,
            "apply",
            "--server-side",
            "--field-manager=capi-kamaji-lab-repair",
            "--force-conflicts",
            "-f",
            str(path),
        )


def _wait_network_verified(root: Path, config: dict[str, str], tenant) -> None:
    def verified():
        try:
            verify_network(root, config, tenant)
            return True
        except RuntimeError:
            return None

    wait_for(
        "tenant add-on source reconciliation",
        parse_duration(config["TENANT_CONTROL_PLANE_TIMEOUT"]),
        parse_duration(config["WAIT_POLL_INTERVAL"]),
        verified,
    )


def _source_change_reconcile(root: Path, config: dict[str, str], client, tenant) -> None:
    source_name = f"{tenant.name}-kube-proxy"
    source = json.loads(
        client.kubectl(
            "-n",
            tenant.namespace,
            "get",
            f"configmap/{source_name}",
            "-o",
            "json",
        ).stdout
    )
    original = source["data"]["addons.yaml"]
    revision = original + """\
\n---
apiVersion: v1
kind: ConfigMap
metadata:
  name: capi-source-revision
  namespace: kube-system
data:
  revision: phase4
"""
    client.kubectl(
        "-n",
        tenant.namespace,
        "patch",
        f"configmap/{source_name}",
        "--type=merge",
        "-p",
        json.dumps({"data": {"addons.yaml": revision}}),
    )
    wait_for(
        "ClusterResourceSet source update",
        parse_duration(config["TENANT_CONTROL_PLANE_TIMEOUT"]),
        parse_duration(config["WAIT_POLL_INTERVAL"]),
        lambda: (
            True
            if _tenant_kubectl(
                root,
                config,
                tenant,
                "-n",
                "kube-system",
                "get",
                "configmap/capi-source-revision",
                check=False,
            ).returncode
            == 0
            else None
        ),
    )


def _handoff_addon_ownership(root: Path, config: dict[str, str], client, tenant) -> None:
    _, inventory = render_resource_set(root, config, tenant)
    client.kubectl(
        "-n",
        tenant.namespace,
        "delete",
        f"clusterresourceset/{tenant.name}-network",
        "--ignore-not-found",
        "--wait=true",
        check=False,
    )
    client.kubectl(
        "-n",
        tenant.namespace,
        "delete",
        f"clusterresourcesetbinding/{tenant.name}",
        "--ignore-not-found",
        "--wait=true",
        check=False,
    )
    for source_name in inventory:
        client.kubectl(
            "-n",
            tenant.namespace,
            "delete",
            f"configmap/{source_name}",
            "--ignore-not-found",
            "--wait=true",
            check=False,
        )
    wait_network_ready(root, config, tenant)
    verify_network(root, config, tenant)


def _assert_drift_and_repair(root: Path, config: dict[str, str], client, tenant) -> None:
    drift_operations = (
        (
            "daemonset/capi-kube-proxy",
            "kube-system",
            "json",
            [
                {
                    "op": "replace",
                    "path": "/spec/template/spec/containers/0/image",
                    "value": config["VERIFY_IMAGE"],
                }
            ],
        ),
        (
            "clusterrolebinding/capi-system:node-proxier",
            None,
            "merge",
            {
                "subjects": [
                    {
                        "kind": "ServiceAccount",
                        "name": "default",
                        "namespace": "default",
                    }
                ]
            },
        ),
    )
    for resource, namespace, patch_type, patch in drift_operations:
        namespace_arguments = ["-n", namespace] if namespace else []
        wait_for(
            f"{resource} availability before drift fixture",
            parse_duration(config["TENANT_CONTROL_PLANE_TIMEOUT"]),
            parse_duration(config["WAIT_POLL_INTERVAL"]),
            lambda: (
                True
                if _tenant_kubectl(
                    root,
                    config,
                    tenant,
                    *namespace_arguments,
                    "get",
                    resource,
                    check=False,
                ).returncode
                == 0
                else None
            ),
        )
        patch_result = _tenant_kubectl(
            root,
            config,
            tenant,
            *namespace_arguments,
            "patch",
            resource,
            f"--type={patch_type}",
            "-p",
            json.dumps(patch),
            check=False,
        )
        if patch_result.returncode != 0:
            raise RuntimeError(f"failed to inject drift for {resource}: {patch_result.stderr}")
        try:
            verify_network(root, config, tenant)
        except RuntimeError:
            pass
        else:
            raise RuntimeError(f"add-on drift was not detected for {resource}")
        _repair_addons(root, config, client, tenant)
        wait_network_ready(root, config, tenant)
        _wait_network_verified(root, config, tenant)


def _wait_addons_absent(root: Path, config: dict[str, str], tenant) -> None:
    resources = (
        ("kube-system", "daemonset/capi-kube-proxy"),
        ("kube-system", "configmap/capi-kube-proxy"),
        ("kube-system", "daemonset/calico-node"),
        ("kube-system", "deployment/calico-kube-controllers"),
    )
    wait_for(
        "tenant add-on deletion",
        parse_duration(config["TENANT_CONTROL_PLANE_TIMEOUT"]),
        parse_duration(config["WAIT_POLL_INTERVAL"]),
        lambda: (
            True
            if all(
                _tenant_kubectl(
                    root,
                    config,
                    tenant,
                    "-n",
                    namespace,
                    "get",
                    resource,
                    check=False,
                ).returncode
                != 0
                for namespace, resource in resources
            )
            else None
        ),
    )


def run_network_gate(root: Path, config: dict[str, str]) -> None:
    client, tenant, _ = run_endpoint_gate(root, config, cleanup=False)
    try:
        apply_addons(root, config, client, tenant)
        wait_network_ready(root, config, tenant)
        verify_network(root, config, tenant)
        _verify_control_plane_active(client, config, tenant)
        _restart_kube_proxy(root, config, tenant)
        verify_network(root, config, tenant)
        _source_change_reconcile(root, config, client, tenant)
        wait_network_ready(root, config, tenant)
        verify_network(root, config, tenant)
        _handoff_addon_ownership(root, config, client, tenant)
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
        _repair_addons(root, config, client, tenant)
        wait_network_ready(root, config, tenant)
        _wait_network_verified(root, config, tenant)
        _assert_drift_and_repair(root, config, client, tenant)
        _verify_control_plane_active(client, config, tenant)
        print("tenant networking and kube-proxy reconciliation checks passed")
    finally:
        delete_addons(root, config, client, tenant)
        _wait_addons_absent(root, config, tenant)
        delete_tenant(root, config, client, tenant)
