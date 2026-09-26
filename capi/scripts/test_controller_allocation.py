from __future__ import annotations

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from scripts.lib.config import parse_duration
from scripts.lib.controller import verify_controller_api
from scripts.lib.controller_client import apply_tenant_document, tenant_manifest_document
from scripts.lib.controller_scenarios import (
    MARKER,
    allocation_lease_manifest,
    tenant_document,
    verify_allocation_lease,
    verify_allocation_released,
    wait_tenant_absent,
)
from scripts.lib.kube import wait_for
from scripts.lib.process import run


def controller_replicas(client, config, replicas: int) -> None:
    client.kubectl("-n", "tenant-system", "scale", "deployment/tenant-controller", f"--replicas={replicas}")
    if replicas:
        client.kubectl("-n", "tenant-system", "rollout", "status", "deployment/tenant-controller",
                       f"--timeout={config['CONDITION_TIMEOUT']}")
    else:
        wait_for(
            "Tenant controller Pod absence", parse_duration(config["CONDITION_TIMEOUT"]), 2,
            lambda: True if not client.json(
                "-n", "tenant-system", "get", "pods", "-l",
                "app.kubernetes.io/name=tenant-controller",
            )["items"] else None,
        )


def verify_api_boundaries(client, config) -> None:
    # No manager is running: the admission probe cannot race an allocation.
    verify_controller_api(config, client)
    probe = tenant_manifest_document(config, "allocation-api-probe")
    for field in ("workers", "databases"):
        for value in (0, 4, -1, True, 1.5, "1", None):
            document = copy.deepcopy(probe)
            document["spec"][field] = value
            result = client.kubectl(
                "create", "--dry-run=server", "--validate=strict", "-f", "-",
                input_text=json.dumps(document), check=False,
            )
            if result.returncode == 0 or field not in result.stderr:
                raise RuntimeError(f"Tenant API accepted invalid {field}={value!r}")
    for version in ("1.36", "1.36.4-extra", "", "vv1.36.4"):
        document = copy.deepcopy(probe)
        document["spec"]["kubernetesVersion"] = version
        result = client.kubectl(
            "create", "--dry-run=server", "--validate=strict", "-f", "-",
            input_text=json.dumps(document), check=False,
        )
        if result.returncode == 0 or "kubernetesVersion" not in result.stderr:
            raise RuntimeError(f"Tenant API accepted invalid version {version!r}")
    for version in (config["KUBERNETES_VERSION"].removeprefix("v"),
                    "v" + config["KUBERNETES_VERSION"].removeprefix("v"), "0.0.0"):
        document = copy.deepcopy(probe)
        document["spec"].update(kubernetesVersion=version, workers=3, databases=3)
        client.kubectl("create", "--dry-run=server", "--validate=strict", "-f", "-",
                       input_text=json.dumps(document))
    print("live CEL/count/version and Warn/Ignore/Strict API boundaries passed", flush=True)


def _phase(client, config, name, phase):
    return wait_for(
        f"{name} {phase}", parse_duration(config["CONDITION_TIMEOUT"]), 2,
        lambda: (
            document if (document := tenant_document(client, name))
            and document.get("status", {}).get("phase") == phase else None
        ),
    )


def _assert_no_external_state(client, config, name) -> None:
    for kind in (
        "clusters.cluster.x-k8s.io", "devclusters.infrastructure.cluster.x-k8s.io",
        "kamajicontrolplanes.controlplane.cluster.x-k8s.io", "machines.cluster.x-k8s.io",
        "machinedeployments.cluster.x-k8s.io", "secrets",
    ):
        if client.json("-n", name, "get", kind)["items"]:
            raise RuntimeError(f"allocation-only fixture created {kind}: {name}")
    if run(["docker", "ps", "-aq", "--filter", f"label=io.x-k8s.kind.cluster={name}"], timeout=30).stdout.strip():
        raise RuntimeError(f"allocation-only fixture created containers: {name}")
    if f"{config['LAB_PREFIX']}-{name}-storage" in run(
        ["docker", "volume", "ls", "--format", "{{.Name}}"], timeout=30,
    ).stdout.splitlines():
        raise RuntimeError(f"allocation-only fixture created a volume: {name}")


def _delete_namespace_fixture(client, name, uid) -> None:
    observed = client.json("get", f"namespace/{name}")
    if observed["metadata"]["uid"] != uid or observed["metadata"].get("labels", {}).get("allocation-fixture") != "true":
        raise RuntimeError("foreign Namespace fixture was replaced")
    client.kubectl("delete", f"namespace/{name}", "--wait=true")


def _race_lease_creates(client, lease):
    barrier = Barrier(2)

    def claim(index):
        candidate = copy.deepcopy(lease)
        candidate["metadata"]["annotations"][MARKER + "tenant-uid"] = f"foreign-fixture-{index}"
        barrier.wait(timeout=30)
        return client.kubectl(
            "create", "-f", "-", "-o", "json",
            input_text=json.dumps(candidate), check=False,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(claim, range(2)))
    winners = [result for result in results if result.returncode == 0]
    losers = [result for result in results if result.returncode != 0]
    if len(winners) != 1 or len(losers) != 1 or "AlreadyExists" not in losers[0].stderr:
        raise RuntimeError("concurrent Lease CREATE did not yield one winner and one AlreadyExists")
    return json.loads(winners[0].stdout)


def run_allocation_gate(root, config, client) -> None:
    names = ("allocation-a", "allocation-b")
    namespaces = {}
    claims = {}
    primary = None
    replacement_restore = None
    controller_replicas(client, config, 0)
    try:
        verify_api_boundaries(client, config)
        for name in names:
            created = client.kubectl(
                "create", "-f", "-", "-o", "json", input_text=json.dumps({
                    "apiVersion": "v1", "kind": "Namespace",
                    "metadata": {"name": name, "labels": {"allocation-fixture": "true"}},
                }),
            )
            namespaces[name] = json.loads(created.stdout)["metadata"]["uid"]
        barrier = Barrier(len(names))

        def create(name):
            barrier.wait(timeout=30)
            apply_tenant_document(client, tenant_manifest_document(config, name))

        with ThreadPoolExecutor(max_workers=len(names)) as executor:
            list(executor.map(create, names))
        controller_replicas(client, config, 1)
        documents = [_phase(client, config, name, "OwnershipInvalid") for name in names]
        for document in documents:
            name = document["metadata"]["name"]
            claims[name] = verify_allocation_lease(config, client, document)
            _assert_no_external_state(client, config, name)
        for field in ("slotId", "endpoint", "podCIDR", "serviceCIDR"):
            if len({document["status"]["allocation"][field] for document in documents}) != len(names):
                raise RuntimeError(f"concurrent Tenants share {field}")

        controller_replicas(client, config, 0)
        # Rewind only the observation, with no owned roots present. The durable
        # claims are real controller writes; this recreates its pre-status crash window.
        for document in documents:
            client.kubectl(
                "patch", f"tenant/{document['metadata']['name']}", "--subresource=status",
                "--type=json", "-p", json.dumps([
                    {"op": "test", "path": "/metadata/uid", "value": document["metadata"]["uid"]},
                    {"op": "remove", "path": "/status/allocation"},
                    {"op": "replace", "path": "/status/phase", "value": "Pending"},
                ]),
            )
        controller_replicas(client, config, 1)
        for original in documents:
            name = original["metadata"]["name"]
            recovered = _phase(client, config, name, "OwnershipInvalid")
            if verify_allocation_lease(config, client, recovered) != claims[name]:
                raise RuntimeError("restart changed a pre-status Lease claim")
            _assert_no_external_state(client, config, name)

        controller_replicas(client, config, 0)
        original = documents[0]
        lease = allocation_lease_manifest(config, original)
        resource = f"lease/{lease['metadata']['name']}"
        client.kubectl("-n", "tenant-system", "delete", resource, "--wait=true")
        created = _race_lease_creates(client, lease)
        replacement_restore = (created, lease)
        if created["metadata"]["uid"] == claims[names[0]]["uid"]:
            raise RuntimeError("replacement Lease did not acquire a new UID")
        claims[names[0]] = {
            key: created["metadata"][key] for key in ("name", "namespace", "uid", "labels", "annotations")
        }
        client.kubectl("patch", f"tenant/{names[0]}", "--subresource=status", "--type=merge",
                       "-p", '{"status":{"phase":"Pending"}}')
        controller_replicas(client, config, 1)
        rejected = _phase(client, config, names[0], "OwnershipInvalid")
        if not any(
            "Lease" in condition.get("message", "") for condition in rejected["status"].get("conditions", [])
        ):
            raise RuntimeError("foreign/replaced Lease was not the ownership refusal")
        observed = client.json("-n", "tenant-system", "get", resource)
        if observed != created:
            raise RuntimeError("controller changed a foreign replacement Lease")
        _assert_no_external_state(client, config, names[0])
        controller_replicas(client, config, 0)
        observed["metadata"]["annotations"] = lease["metadata"]["annotations"]
        restored = json.loads(client.kubectl(
            "replace", "-f", "-", "-o", "json", input_text=json.dumps(observed),
        ).stdout)
        replacement_restore = None
        claims[names[0]] = {
            key: restored["metadata"][key] for key in ("name", "namespace", "uid", "labels", "annotations")
        }

        unsupported = tenant_manifest_document(config, "allocation-version")
        unsupported["spec"]["kubernetesVersion"] = "0.0.0"
        apply_tenant_document(client, unsupported)
        controller_replicas(client, config, 1)
        rejected = _phase(client, config, "allocation-version", "Failed")
        if rejected.get("status", {}).get("allocation") or rejected["metadata"].get("finalizers"):
            raise RuntimeError("unsupported version acquired managed resources")
        _assert_no_external_state(client, config, "allocation-version")
        client.kubectl("delete", "tenant/allocation-version", "--wait=true")
        print("concurrent unique claims, pre-status restart recovery and foreign/replaced Lease refusal passed", flush=True)
    except BaseException as exc:
        primary = exc
        raise
    finally:
        try:
            controller_replicas(client, config, 0)
            if replacement_restore is not None:
                created, lease = replacement_restore
                current = client.json("-n", "tenant-system", "get", f"lease/{created['metadata']['name']}")
                if current != created:
                    raise RuntimeError("replacement Lease changed before fixture restoration")
                current["metadata"]["annotations"] = lease["metadata"]["annotations"]
                client.kubectl("replace", "-f", "-", input_text=json.dumps(current))
            for name in names:
                client.kubectl("delete", f"tenant/{name}", "--ignore-not-found=true", "--wait=false")
            for name, uid in namespaces.items():
                _delete_namespace_fixture(client, name, uid)
            controller_replicas(client, config, 1)
            for name in names:
                wait_tenant_absent(root, config, name)
                _assert_no_external_state(client, config, name)
                if name in claims:
                    verify_allocation_released(client, {
                        "name": name,
                        "uid": claims[name]["annotations"][MARKER + "tenant-uid"],
                        "allocationLease": claims[name],
                    })
            client.kubectl("delete", "tenant/allocation-version", "--ignore-not-found=true", "--wait=true")
        except BaseException as cleanup:
            if primary is None:
                raise
            primary.add_note(f"allocation cleanup failed: {cleanup}")
