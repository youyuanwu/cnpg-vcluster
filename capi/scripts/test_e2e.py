#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.lib.config import load_configuration, parse_duration
from scripts.cnpg import _sql, verify_restart_persistence
from scripts.lib.host import read_inotify, resolve_host_just
from scripts.lib.kube import ManagementClient
from scripts.lib.locking import e2e_lock
from scripts.lib.process import run
from scripts.lib.redaction import redact
from scripts.tools import verify_all_inputs
from scripts.lib.timing import PhaseTimings
from scripts.lib.registry import registry_name
from scripts.lib.controller_scenarios import (
    delete_controller_tenant,
    tenant_from_document,
    tenant_snapshot,
    verify_allocation_released,
    wait_tenant_ready,
)
from scripts.lib.tenants import export_tenant_kubeconfig


def run_just(
    root: Path,
    config: dict[str, str],
    *arguments: str,
    check: bool = True,
):
    result = run(
        [
            str(resolve_host_just(root, config)),
            "--justfile",
            str(root / "Justfile"),
            *arguments,
        ],
        timeout=parse_duration(config["COMMAND_TIMEOUT"]) * 8,
        cwd=root,
        env={**os.environ, "CAPI_E2E_CHILD": "1"},
        check=check,
    )
    for line in result.stdout.splitlines():
        if line.startswith("CAPI_OFFLINE_"):
            print(line)
    return result


def verify_no_lab_residue(
    config: dict[str, str],
    tenant_names: tuple[str, ...] = (),
) -> None:
    if run(
        ["docker", "inspect", f"{config['KIND_CLUSTER_NAME']}-control-plane"],
        timeout=30,
        check=False,
    ).returncode == 0:
        raise RuntimeError("management container remained after teardown")
    for name in (*tenant_names, config["SPIKE_NAME"]):
        leftovers = run(
            [
                "docker",
                "ps",
                "-aq",
                "--filter",
                f"label=io.x-k8s.kind.cluster={name}",
            ],
            timeout=30,
        ).stdout.split()
        if leftovers:
            raise RuntimeError(f"provider Docker objects remain for {name}")
    volumes = run(
        [
            "docker",
            "volume",
            "ls",
            "-q",
            "--filter",
            f"label={config['OWNERSHIP_LABEL']}={config['LAB_PREFIX']}",
        ],
        timeout=30,
    ).stdout.split()
    if volumes:
        raise RuntimeError(f"owned Docker volumes remain: {volumes}")
    registry = run(
        ["docker", "inspect", registry_name(config)],
        timeout=30,
        check=False,
    )
    if registry.returncode == 0:
        raise RuntimeError("offline registry container remained after teardown")


def verify_no_local_runtime_residue(root: Path) -> None:
    runtime = root / ".runtime"
    if not runtime.exists():
        return
    allowed = {
        "lifecycle",
        "lifecycle/.locks",
        "lifecycle/.locks/azure.lock",
        "lifecycle/rejected",
    }
    allowed_prefixes = (
        "azure",
        "azure-gate",
        "lifecycle/azure",
        "lifecycle/rejected/azure",
    )
    for path in runtime.rglob("*"):
        relative = path.relative_to(runtime).as_posix()
        if relative in allowed or any(
            relative == prefix or relative.startswith(prefix + "/")
            for prefix in allowed_prefixes
        ):
            continue
        raise RuntimeError(
            f"local runtime remained after E2E teardown: {relative}"
        )


def _inspect_management_object(
    client: ManagementClient,
    resource: str,
    namespace: str = "",
) -> dict[str, object] | None:
    arguments = ["-n", namespace] if namespace else []
    response = client.kubectl(
        *arguments, "get", resource, "-o", "json",
        "--ignore-not-found=true", check=False,
    )
    if response.returncode != 0:
        raise RuntimeError(
            f"failed to inspect {resource}: {redact(response.stderr)}"
        )
    if not response.stdout.strip():
        return None
    try:
        document = json.loads(response.stdout)
    except ValueError as exc:
        raise RuntimeError(f"invalid inspection response for {resource}") from exc
    if not isinstance(document, dict):
        raise RuntimeError(f"invalid inspection response for {resource}")
    return document


def capture_tenant_deletion_identity(
    config: dict[str, str],
    client: ManagementClient,
    document: dict[str, object],
) -> dict[str, object]:
    metadata = document.get("metadata", {})
    if not isinstance(metadata, dict):
        raise RuntimeError("Tenant deletion identity is incomplete")
    name, uid = metadata.get("name"), metadata.get("uid")
    if not isinstance(name, str) or not name or not isinstance(uid, str) or not uid:
        raise RuntimeError("Tenant deletion identity is incomplete")
    current = _inspect_management_object(client, f"tenant/{name}")
    current_metadata = current.get("metadata") if current is not None else None
    if not isinstance(current_metadata, dict) or current_metadata.get("uid") != uid:
        raise RuntimeError("Tenant identity changed before deletion")
    identity = tenant_snapshot(config, client, current)
    identity["name"] = name
    cluster_uids = [
        recorded_uid
        for resource, namespace, object_name, recorded_uid in identity["managementResources"]
        if resource == "clusters.cluster.x-k8s.io" and namespace == name and object_name == name
    ]
    if (
        identity["dockerVolume"]["name"] != f"{config['LAB_PREFIX']}-{name}-storage"
        or not identity["workerContainers"]
        or not set(identity["workerContainers"]) <= set(identity["providerContainers"])
        or f"{name}-lb" not in {container.split()[0] for container in identity["providerContainers"]}
        or not identity["clusterUID"]
        or cluster_uids != [identity["clusterUID"]]
    ):
        raise RuntimeError("Tenant provider deletion identity is incomplete")
    return identity


def verify_tenant_deletion(
    client: ManagementClient,
    identity: dict[str, object],
) -> None:
    name = identity["name"]
    if _inspect_management_object(client, f"tenant/{name}") is not None:
        raise RuntimeError(f"Tenant remained or was recreated after deletion: {name}")
    for resource, namespace, object_name, uid in identity["managementResources"]:
        if _inspect_management_object(
            client, f"{resource}/{object_name}", namespace,
        ) is not None:
            raise RuntimeError(
                f"Tenant management resource remained after deletion: "
                f"{resource}/{object_name} (recorded UID {uid})"
            )
    verify_allocation_released(client, identity)
    containers = set(run(
        ["docker", "ps", "-aq", "--no-trunc"], timeout=30,
    ).stdout.split())
    recorded = {
        container.split()[1] for container in identity["providerContainers"]
    }
    scoped = run(
        [
            "docker", "ps", "-aq",
            "--filter", f"label=io.x-k8s.kind.cluster={name}",
        ],
        timeout=30,
    ).stdout.split()
    if containers & recorded or scoped:
        raise RuntimeError(f"Tenant worker containers remained after deletion: {name}")
    volumes = run(
        ["docker", "volume", "ls", "--format", "{{.Name}}"], timeout=30,
    ).stdout.splitlines()
    if identity["dockerVolume"]["name"] in volumes:
        raise RuntimeError(f"Tenant storage volume remained after deletion: {name}")


def run_e2e() -> int:
    os.umask(0o077)
    config = load_configuration(ROOT)
    original_inotify = {
        "max_user_instances": read_inotify("max_user_instances"),
        "max_user_watches": read_inotify("max_user_watches"),
    }
    failure = None
    timings = PhaseTimings()
    manifest = ROOT / "config" / "tenants" / "examples" / "local.yaml"
    tenant_name = "tenant-example"
    try:
        with timings.phase("tools_cache"):
            run_just(ROOT, config, "tools")
        with timings.phase("initial_cleanup"):
            run_just(ROOT, config, "destroy")
        with timings.phase("host_preparation"):
            run_just(ROOT, config, "prepare-host")
        with timings.phase("management_bootstrap"):
            verify_all_inputs(ROOT, config)
            run_just(ROOT, config, "create-management")

        with timings.phase("tenant_convergence"):
            run_just(ROOT, config, "local-tenant-apply", str(manifest))
            document = wait_tenant_ready(ROOT, config, tenant_name)
        run_just(ROOT, config, "local-tenant-status", tenant_name)
        with timings.phase("tenant_sql_probe"):
            tenant = tenant_from_document(ROOT, config, document)
            export_tenant_kubeconfig(
                ROOT, config, ManagementClient(ROOT, config), tenant,
            )
            if _sql(ROOT, config, tenant, "SELECT 1;") != "1":
                raise RuntimeError("representative tenant PostgreSQL SELECT 1 failed")
            verify_restart_persistence(ROOT, config, tenant)
        print("representative tenant PostgreSQL SELECT 1 succeeded")
        with timings.phase("tenant_deletion_finalization"):
            client = ManagementClient(ROOT, config)
            identity = capture_tenant_deletion_identity(config, client, document)
            delete_controller_tenant(ROOT, config, identity["name"])
            verify_tenant_deletion(client, identity)
            print("exact Tenant/root/Lease/container/volume absence verified before management teardown")
    except BaseException as exc:
        failure = exc
    try:
        with timings.phase("management_teardown_host_restoration"):
            run_just(ROOT, config, "destroy")
            verify_no_lab_residue(config, (tenant_name,))
            verify_no_local_runtime_residue(ROOT)
            for name, expected in original_inotify.items():
                if read_inotify(name) != expected:
                    raise RuntimeError(f"host inotify was not restored: {name}")
            print("final host restoration and local infrastructure absence verified")
    except BaseException as cleanup:
        if failure is None:
            failure = cleanup
        else:
            failure.add_note(f"cleanup also failed: {redact(str(cleanup))}")
    timings.emit()
    if failure is not None:
        raise failure
    return 0


def main() -> int:
    with e2e_lock(ROOT, exclusive=True):
        return run_e2e()


if __name__ == "__main__":
    raise SystemExit(main())
