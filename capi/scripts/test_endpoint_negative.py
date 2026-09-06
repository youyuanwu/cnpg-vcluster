#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.create_management import create_management
from scripts.lib.conditions import condition_summary, condition_true
from scripts.lib.config import load_configuration, parse_duration
from scripts.lib.kube import ManagementClient, wait_for
from scripts.lib.files import write_private_file
from scripts.lib.process import run
from scripts.lib.tenants import (
    apply_bootstrap_rbac,
    apply_control_plane,
    delete_tenant,
    export_tenant_kubeconfig,
    prepare_storage_directory,
    render_tenant_manifests,
    spike_tenant,
    verify_load_balancer_runtime,
    verify_worker_runtime,
)


def docker_container(config: dict[str, str], name: str, labels: dict[str, str]) -> str:
    command = [
        "docker",
        "run",
        "-d",
        "--name",
        name,
    ]
    for key, value in labels.items():
        command.extend(("--label", f"{key}={value}"))
    command.extend(("--entrypoint", "sleep", config["KIND_NODE_IMAGE"], "3600"))
    return run(command, timeout=60).stdout.strip()


def wrong_label_load_balancer(
    root: Path,
    config: dict[str, str],
    client: ManagementClient,
) -> None:
    tenant = spike_tenant(root, config)
    delete_tenant(root, config, client, tenant)
    name = f"{tenant.name}-lb"
    identifier = docker_container(config, name, {"foreign": "true"})
    try:
        manifest, _ = render_tenant_manifests(root, config, tenant)
        client.kubectl("apply", "-f", str(manifest))

        def rejected():
            response = client.kubectl(
                "-n",
                tenant.namespace,
                "get",
                f"devcluster/{tenant.name}",
                "-o",
                "json",
                check=False,
            )
            if response.returncode != 0:
                return None
            resource = json.loads(response.stdout)
            conditions = condition_summary(resource)
            return conditions if any(
                item["status"] == "False"
                and "container" in (item.get("message") or "").lower()
                for item in conditions
            ) else None

        wait_for(
            "wrong-label load balancer rejection",
            120,
            2,
            rejected,
        )
        observed = run(
            ["docker", "inspect", name, "--format", "{{.Id}}"],
            timeout=30,
        ).stdout.strip()
        if observed != identifier:
            raise RuntimeError("wrong-label load balancer was adopted or changed")
    finally:
        delete_tenant(root, config, client, tenant)
        observed = run(["docker", "inspect", identifier], timeout=30, check=False)
        if observed.returncode != 0:
            raise RuntimeError("host lifecycle deleted the wrong-label load balancer")
        run(["docker", "rm", "-f", identifier], timeout=30)


def exact_label_load_balancer(
    root: Path,
    config: dict[str, str],
    client: ManagementClient,
) -> None:
    tenant = spike_tenant(root, config)
    delete_tenant(root, config, client, tenant)
    fixture_name = f"{tenant.name}-provider-owned-lb"
    identifier = docker_container(
        config,
        fixture_name,
        {
            "io.x-k8s.kind.cluster": tenant.name,
            "io.x-k8s.kind.role": "external-load-balancer",
        },
    )
    try:
        manifest, _ = render_tenant_manifests(root, config, tenant)
        client.kubectl("apply", "-f", str(manifest))

        def adopted():
            response = client.kubectl(
                "-n",
                tenant.namespace,
                "get",
                f"devcluster/{tenant.name}",
                "-o",
                "json",
                check=False,
            )
            if response.returncode != 0:
                return None
            resource = json.loads(response.stdout)
            return resource if resource.get("status", {}).get("conditions") else None

        wait_for("exact-label load balancer adoption", 120, 2, adopted)
        try:
            verify_load_balancer_runtime(root, config, tenant)
        except RuntimeError:
            pass
        else:
            raise RuntimeError("wrong-image load balancer passed lab runtime health")
    finally:
        delete_tenant(root, config, client, tenant)
        if run(["docker", "inspect", identifier], timeout=30, check=False).returncode == 0:
            raise RuntimeError("CAPD did not delete its exact-label load balancer")


def stopped_exact_label_load_balancer(
    root: Path,
    config: dict[str, str],
    client: ManagementClient,
) -> None:
    tenant = spike_tenant(root, config)
    delete_tenant(root, config, client, tenant)
    name = f"{tenant.name}-lb"
    identifier = run(
        [
            "docker",
            "create",
            "--name",
            name,
            "--label",
            f"io.x-k8s.kind.cluster={tenant.name}",
            "--label",
            "io.x-k8s.kind.role=external-load-balancer",
            config["CAPD_LOAD_BALANCER_IMAGE"],
        ],
        timeout=60,
    ).stdout.strip()
    try:
        manifest, _ = render_tenant_manifests(root, config, tenant)
        client.kubectl("apply", "-f", str(manifest))

        def observed():
            response = client.kubectl(
                "-n",
                tenant.namespace,
                "get",
                f"devcluster/{tenant.name}",
                "-o",
                "json",
                check=False,
            )
            if response.returncode != 0:
                return None
            resource = json.loads(response.stdout)
            return resource if resource.get("status", {}).get("conditions") else None

        wait_for("stopped exact-label load balancer observation", 120, 2, observed)
        try:
            verify_load_balancer_runtime(root, config, tenant)
        except RuntimeError:
            pass
        else:
            raise RuntimeError("stopped load balancer passed lab runtime health")
    finally:
        delete_tenant(root, config, client, tenant)
        if run(["docker", "inspect", identifier], timeout=30, check=False).returncode == 0:
            raise RuntimeError("CAPD did not delete its stopped exact-label load balancer")


def prepare_paused_worker(
    root: Path,
    config: dict[str, str],
    client: ManagementClient,
) -> tuple[object, str]:
    tenant = spike_tenant(root, config)
    delete_tenant(root, config, client, tenant)
    apply_control_plane(root, config, client, tenant)
    export_tenant_kubeconfig(root, config, client, tenant)
    apply_bootstrap_rbac(root, config, tenant)
    prepare_storage_directory(root, config, tenant)
    _, workers = render_tenant_manifests(root, config, tenant)
    documents = [
        document.strip()
        for document in re.split(r"(?m)^---\s*$", workers.read_text(encoding="utf-8"))
        if document.strip()
    ]
    if len(documents) != 3:
        raise RuntimeError("unexpected worker manifest document inventory")
    template_path = workers.with_name("worker-templates.yaml")
    deployment_path = workers.with_name("worker-deployment.yaml")
    paused_template = documents[1].replace(
        "  template:\n    spec:",
        '  template:\n    metadata:\n      annotations:\n        cluster.x-k8s.io/paused: ""\n    spec:',
        1,
    )
    write_private_file(
        template_path,
        documents[0] + "\n---\n" + paused_template + "\n",
    )
    write_private_file(deployment_path, documents[2] + "\n")
    client.kubectl(
        "apply",
        "--server-side",
        "--field-manager=capi-kamaji-lab",
        "--force-conflicts",
        "-f",
        str(template_path),
    )
    client.kubectl(
        "apply",
        "--server-side",
        "--field-manager=capi-kamaji-lab",
        "--force-conflicts",
        "-f",
        str(deployment_path),
    )

    def generated_machine():
        payload = json.loads(
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
        )
        return payload["items"][0]["metadata"]["name"] if len(payload["items"]) == 1 else None

    return tenant, wait_for("generated Machine", 120, 2, generated_machine)


def partial_label_worker(
    root: Path,
    config: dict[str, str],
    client: ManagementClient,
) -> None:
    tenant, machine_name = prepare_paused_worker(root, config, client)
    identifier = ""
    try:
        identifier = docker_container(
            config,
            machine_name,
            {"io.x-k8s.kind.cluster": tenant.name},
        )
        client.kubectl(
            "-n",
            tenant.namespace,
            "annotate",
            f"devmachine/{machine_name}",
            "cluster.x-k8s.io/paused-",
        )

        def adopted():
            response = client.kubectl(
                "-n",
                tenant.namespace,
                "get",
                f"devmachine/{machine_name}",
                "-o",
                "json",
                check=False,
            )
            if response.returncode != 0:
                return None
            resource = json.loads(response.stdout)
            return resource if condition_true(resource, "ContainerProvisioned") else None

        devmachine = wait_for("partial-label worker adoption", 180, 2, adopted)
        fake = {
            "machine": {"metadata": {"name": machine_name}},
            "devmachine": devmachine,
        }
        try:
            verify_worker_runtime(config, tenant, fake)
        except RuntimeError:
            pass
        else:
            raise RuntimeError("partial-label wrong-role worker passed lab runtime health")
        observed = json.loads(run(["docker", "inspect", identifier], timeout=30).stdout)[0]
        if observed["Config"]["Labels"].get("io.x-k8s.kind.role") == "worker":
            raise RuntimeError("CAPD unexpectedly rewrote the partial-label role")
    finally:
        delete_tenant(root, config, client, tenant)
        if identifier:
            remaining = run(["docker", "inspect", identifier], timeout=30, check=False)
            if remaining.returncode == 0:
                raise RuntimeError("CAPD did not delete its adopted partial-label worker")


def unlabelled_worker(
    root: Path,
    config: dict[str, str],
    client: ManagementClient,
) -> None:
    tenant, machine_name = prepare_paused_worker(root, config, client)
    identifier = docker_container(config, machine_name, {"foreign": "true"})
    try:
        client.kubectl(
            "-n",
            tenant.namespace,
            "annotate",
            f"devmachine/{machine_name}",
            "cluster.x-k8s.io/paused-",
        )

        def rejected():
            response = client.kubectl(
                "-n",
                tenant.namespace,
                "get",
                f"devmachine/{machine_name}",
                "-o",
                "json",
                check=False,
            )
            if response.returncode != 0:
                return None
            resource = json.loads(response.stdout)
            return resource if _current_false_condition(resource) else None

        wait_for("unlabelled worker collision", 180, 2, rejected)
        observed = run(
            ["docker", "inspect", machine_name, "--format", "{{.Id}}"],
            timeout=30,
        ).stdout.strip()
        if observed != identifier:
            raise RuntimeError("unlabelled worker was adopted or changed")
    finally:
        delete_tenant(root, config, client, tenant)
        if run(["docker", "inspect", identifier], timeout=30, check=False).returncode != 0:
            raise RuntimeError("host lifecycle deleted the unlabelled worker")
        run(["docker", "rm", "-f", identifier], timeout=30)


def _current_false_condition(resource: dict[str, object]) -> bool:
    generation = resource["metadata"]["generation"]
    return any(
        condition.get("status") == "False"
        and condition.get("observedGeneration") == generation
        and condition.get("reason")
        and condition.get("message")
        for condition in resource.get("status", {}).get("conditions", [])
    )


def invalid_cluster_condition(
    root: Path,
    config: dict[str, str],
    client: ManagementClient,
) -> None:
    namespace = "capi-invalid-cluster"
    name = "capi-invalid-cluster"
    manifest = root / ".runtime" / "rendered" / "negative" / "invalid-cluster.yaml"
    write_private_file(
        manifest,
        f"""\
apiVersion: v1
kind: Namespace
metadata:
  name: {namespace}
---
apiVersion: cluster.x-k8s.io/v1beta2
kind: Cluster
metadata:
  name: {name}
  namespace: {namespace}
spec:
  controlPlaneEndpoint:
    host: 192.0.2.10
    port: 6443
  infrastructureRef:
    apiGroup: infrastructure.cluster.x-k8s.io
    kind: DevCluster
    name: missing
  controlPlaneRef:
    apiGroup: controlplane.cluster.x-k8s.io
    kind: KamajiControlPlane
    name: missing
""",
    )
    try:
        client.kubectl("apply", "-f", str(manifest))

        def failed():
            response = client.kubectl(
                "-n",
                namespace,
                "get",
                f"cluster/{name}",
                "-o",
                "json",
                check=False,
            )
            if response.returncode != 0:
                return None
            resource = json.loads(response.stdout)
            return resource if _current_false_condition(resource) else None

        wait_for("invalid Cluster condition", 120, 2, failed)
    finally:
        client.kubectl(
            "delete",
            "namespace",
            namespace,
            "--ignore-not-found",
            "--wait=true",
            "--timeout=2m",
            check=False,
        )
        manifest.unlink(missing_ok=True)


def invalid_control_plane_condition(
    root: Path,
    config: dict[str, str],
    client: ManagementClient,
) -> None:
    tenant = spike_tenant(root, config)
    delete_tenant(root, config, client, tenant)
    manifest, _ = render_tenant_manifests(root, config, tenant)
    invalid = manifest.with_name("invalid-control-plane.yaml")
    write_private_file(
        invalid,
        manifest.read_text(encoding="utf-8").replace(
            "  dataStoreName: default\n",
            "  dataStoreName: missing-datastore\n",
            1,
        ),
    )
    try:
        client.kubectl("apply", "-f", str(invalid))

        def failed():
            response = client.kubectl(
                "-n",
                tenant.namespace,
                "get",
                f"kamajicontrolplane/{tenant.name}",
                "-o",
                "json",
                check=False,
            )
            if response.returncode != 0:
                return None
            resource = json.loads(response.stdout)
            return resource if _current_false_condition(resource) else None

        wait_for("invalid control-plane condition", 120, 2, failed)
    finally:
        response = client.kubectl(
            "-n",
            tenant.namespace,
            "get",
            f"kamajicontrolplane/{tenant.name}",
            check=False,
        )
        if response.returncode == 0:
            client.kubectl(
                "-n",
                tenant.namespace,
                "patch",
                f"kamajicontrolplane/{tenant.name}",
                "--type=merge",
                "-p",
                '{"spec":{"dataStoreName":"default"}}',
            )

            def repaired():
                current = client.kubectl(
                    "-n",
                    tenant.namespace,
                    "get",
                    f"kamajicontrolplane/{tenant.name}",
                    "-o",
                    "json",
                    check=False,
                )
                if current.returncode != 0:
                    return None
                return (
                    json.loads(current.stdout)
                    if condition_true(json.loads(current.stdout), "Available")
                    else None
                )

            wait_for("invalid control-plane repair", 180, 2, repaired)
        delete_tenant(root, config, client, tenant)


def invalid_worker_condition(
    root: Path,
    config: dict[str, str],
    client: ManagementClient,
) -> None:
    tenant = spike_tenant(root, config)
    delete_tenant(root, config, client, tenant)
    apply_control_plane(root, config, client, tenant)
    export_tenant_kubeconfig(root, config, client, tenant)
    apply_bootstrap_rbac(root, config, tenant)
    _, workers = render_tenant_manifests(root, config, tenant)
    invalid = workers.with_name("invalid-worker.yaml")
    missing_path = root / ".runtime" / "intentionally-missing" / "worker"
    write_private_file(
        invalid,
        workers.read_text(encoding="utf-8").replace(
            str(tenant.storage_host_path),
            str(missing_path),
        ),
    )
    try:
        client.kubectl("apply", "-f", str(invalid))

        def failed():
            response = client.kubectl(
                "-n",
                tenant.namespace,
                "get",
                "devmachines",
                "-o",
                "json",
                check=False,
            )
            if response.returncode != 0:
                return None
            items = json.loads(response.stdout)["items"]
            return items[0] if len(items) == 1 and _current_false_condition(items[0]) else None

        wait_for("invalid worker condition", 180, 2, failed)
    finally:
        delete_tenant(root, config, client, tenant)


def main() -> int:
    os.umask(0o077)
    config = load_configuration(ROOT)
    template = ROOT / "manifests" / "tenants" / "base" / "control-plane.yaml.tpl"
    original = template.read_bytes()
    try:
        template.write_bytes(original + b"\n# tampered\n")
        try:
            create_management(ROOT, config)
        except RuntimeError as exc:
            if "SHA-256 mismatch" not in str(exc):
                raise
        else:
            raise RuntimeError("tenant source tampering was accepted")
    finally:
        template.write_bytes(original)
    create_management(ROOT, config)
    client = ManagementClient(ROOT, config)
    invalid_cluster_condition(ROOT, config, client)
    invalid_control_plane_condition(ROOT, config, client)
    invalid_worker_condition(ROOT, config, client)
    wrong_label_load_balancer(ROOT, config, client)
    exact_label_load_balancer(ROOT, config, client)
    stopped_exact_label_load_balancer(ROOT, config, client)
    unlabelled_worker(ROOT, config, client)
    partial_label_worker(ROOT, config, client)
    print("endpoint ownership and collision checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
