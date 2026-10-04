#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.lib.config import load_configuration, parse_duration
from scripts.lib.controller_catalog import (
    load_management_resources,
    resource_by_kind,
)
from scripts.lib.catalog_lifecycle import CatalogClient, ready_entries, require_stale_identity
from scripts.lib.host import read_inotify, resolve_host_just
from scripts.lib.kube import ManagementClient, wait_for
from scripts.lib.locking import e2e_lock
from scripts.lib.process import run
from scripts.lib.redaction import redact
from scripts.tools import verify_all_inputs
from scripts.lib.timing import PhaseTimings
from scripts.lib.registry import registry_name
from scripts.lib.admin_local import (
    ADMIN_SERVICE_PROXY,
    _service_proxy,
    create_tenant_via_admin,
    delete_tenant_via_admin,
    verify_admin_api,
)
from scripts.lib.controller_scenarios import (
    tenant_from_document,
    tenant_snapshot,
    verify_allocation_released,
    wait_tenant_absent,
    wait_tenant_ready,
)
from scripts.lib.tenants import export_tenant_kubeconfig
from scripts.lib.tenants import _tenant_kubectl, verify_tenant_management_ownership
from scripts.machines import worker_snapshot
from scripts.lib.addons import wait_network_ready

MANAGEMENT_CATALOG = load_management_resources(ROOT)


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
        "azure",
        "azure/kamaji-provider.yaml",
        "azure/management.kubeconfig",
        "azure/resources.json",
        "lifecycle",
        "lifecycle/.locks",
        "lifecycle/.locks/azure.lock",
        "rendered",
        "rendered/azure-controller",
        "rendered/azure-controller/manager.yaml",
    }
    for path in runtime.rglob("*"):
        relative = path.relative_to(runtime).as_posix()
        if relative in allowed:
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


def _inspect_catalog_object(
    client: ManagementClient,
    api_version: str,
    kind: str,
    namespace: str,
    name: str,
) -> dict[str, object] | None:
    resource = next(
        resource
        for resource in MANAGEMENT_CATALOG
        if resource.api_version == api_version and resource.kind == kind
    )
    response = client.kubectl(
        "get",
        f"--raw={resource.object_path(namespace or None, name)}",
        check=False,
    )
    if response.returncode != 0:
        if "NotFound" in response.stderr:
            return None
        raise RuntimeError(f"failed to inspect {kind}/{name}: {redact(response.stderr)}")
    document = json.loads(response.stdout)
    if (
        not isinstance(document, dict)
        or document.get("apiVersion") != api_version
        or document.get("kind") != kind
    ):
        raise RuntimeError(f"invalid inspection response for {kind}/{name}")
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
    cluster = resource_by_kind(
        MANAGEMENT_CATALOG,
        "Cluster",
    )
    cluster_uids = [
        recorded_uid
        for api_version, kind, namespace, object_name, recorded_uid
        in identity["managementResources"]
        if api_version == cluster.api_version
        and kind == cluster.kind
        and namespace == name
        and object_name == cluster.expected_name(name)
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
    for api_version, kind, namespace, object_name, uid in identity["managementResources"]:
        if _inspect_catalog_object(
            client, api_version, kind, namespace, object_name,
        ) is not None:
            raise RuntimeError(
                f"Tenant management resource remained after deletion: "
                f"{kind}/{object_name} (recorded UID {uid})"
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


def catalog_client(client: ManagementClient, name: str, uid: str) -> CatalogClient:
    return CatalogClient(
        lambda path: _service_proxy(client, path),
        lambda method, path, payload: client.request_json(
            method, f"{ADMIN_SERVICE_PROXY}/{path}", payload,
        ),
        name, uid,
    )


def _catalog_record(client: ManagementClient, name: str) -> dict[str, object]:
    document = client.json(
        "-n", f"tenant-db-{name}", "get", f"tenantdatabasecatalog/{name}",
    )
    if not isinstance(document, dict) or not isinstance(document.get("status"), dict):
        raise RuntimeError("catalog status is absent")
    return document


def _verify_entry_gone(root, config, client, tenant, catalog_uid, logical_uid, recorded):
    current = _catalog_record(client, tenant.name)
    if current["metadata"]["uid"] != catalog_uid or logical_uid in current["spec"]["entries"]:
        raise RuntimeError("deleted catalog entry remains")
    if logical_uid in current["status"]["entries"]:
        raise RuntimeError("deleted catalog observation remains")
    status = recorded["status"]["entries"][logical_uid]
    namespace = status["namespace"]["name"]
    cluster = status["cnpgCluster"]["name"]
    for namespace_arg, resource in (
        ((), f"namespace/{namespace}"),
        (("-n", namespace), f"clusters.postgresql.cnpg.io/{cluster}"),
        (("-n", namespace), f"secret/{cluster}-superuser"),
        *(((), f"pv/{item['pv']['name']}") for item in status["storage"]),
        *(
            (("-n", namespace), f"pvc/{item['pvc']['name']}")
            for item in status["storage"]
        ),
    ):
        response = _tenant_kubectl(
            root, config, tenant, *namespace_arg, "get", resource,
            "--ignore-not-found=true", "-o", "name",
        )
        if response.stdout.strip():
            raise RuntimeError(f"deleted entry resource remained: {resource}")
    volume = f"{config['LAB_PREFIX']}-{tenant.name}-storage"
    result = run(
        ["docker", "run", "--rm", "--network", "none", "--pull", "never",
         "--mount", f"type=volume,src={volume},dst=/data,readonly",
         "--entrypoint", "/bin/sh", config["POSTGRES_IMAGE"],
         "-c", 'test ! -e "/data/volumes/cnpg/$1/$2"', "sh",
         catalog_uid, logical_uid],
        timeout=60,
    )
    if result.returncode:
        raise RuntimeError("deleted entry local storage path remained")


def _failover(root, config, tenant, entry):
    namespace, cluster = entry["namespace"], entry["cluster"]
    before = _tenant_kubectl(
        root, config, tenant, "-n", namespace, "get", f"clusters.postgresql.cnpg.io/{cluster}",
        "-o", "jsonpath={.status.currentPrimary}",
    ).stdout.strip()
    if before not in {item["name"] for item in entry["instanceTopology"]}:
        raise RuntimeError("CNPG primary identity is missing")
    _tenant_kubectl(
        root, config, tenant, "-n", namespace, "delete", f"pod/{before}", "--wait=false",
    )
    wait_for(
        f"{cluster} primary failover", parse_duration(config["CNPG_TIMEOUT"]), 5,
        lambda: (
            current
            if (current := _tenant_kubectl(
                root, config, tenant, "-n", namespace, "get", f"clusters.postgresql.cnpg.io/{cluster}",
                "-o", "jsonpath={.status.currentPrimary}", check=False,
            ).stdout.strip()) and current != before else None
        ),
    )


def _restart_database_controller(client: ManagementClient) -> None:
    client.kubectl(
        "-n", "tenant-system", "rollout", "restart",
        "deployment/database-controller",
    )
    client.kubectl(
        "-n", "tenant-system", "rollout", "status",
        "deployment/database-controller", "--timeout=300s",
    )


def _wait_databases_ready(
    client: ManagementClient,
    catalog: CatalogClient,
    catalog_uid: str,
    names: set[str],
    timeout: int,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    restarted = False
    while True:
        projected = catalog.read(catalog_uid)
        databases = projected["databases"]
        if (
            projected["catalogUid"] == catalog_uid
            and projected["closed"] is False
            and projected["capabilityAvailable"] is True
            and len(databases) == len(names)
            and {entry["name"] for entry in databases} == names
            and all(
                entry["phase"] == "ready"
                and entry["readyInstances"] == 3
                for entry in databases
            )
        ):
            return projected
        unknown = any(
            blocker.get("code") == "UnknownCreateOutcome"
            for entry in databases
            for blocker in entry["blockers"]
        )
        if unknown and not restarted:
            _restart_database_controller(client)
            restarted = True
        if time.monotonic() >= deadline:
            raise RuntimeError("catalog did not converge before deadline")
        time.sleep(5)


def _restart_worker_and_verify_markers(
    root: Path, config: dict[str, str], client: ManagementClient,
    tenant, tenant_uid: str, catalog: CatalogClient, catalog_uid: str,
    observed: dict[str, dict[str, object]],
) -> dict[str, dict[str, object]]:
    names = {"alpha", "beta", "gamma"}
    if set(observed) != names or tenant.name != "tenant-example" or tenant.workers != 3:
        raise RuntimeError("worker restart requires the disposable three-database Tenant")

    def require_tenant() -> None:
        current = _inspect_management_object(client, f"tenant/{tenant.name}")
        metadata = current.get("metadata") if current is not None else None
        spec = current.get("spec") if current is not None else None
        if (
            not isinstance(metadata, dict)
            or not isinstance(spec, dict)
            or metadata.get("name") != tenant.name
            or metadata.get("uid") != tenant_uid
            or metadata.get("deletionTimestamp")
            or spec.get("workers") != 3
            or spec.get("provider") != {"type": "local"}
        ):
            raise RuntimeError("disposable Tenant identity changed before worker restart")

    require_tenant()
    verify_tenant_management_ownership(config, client, tenant)
    volume_name = f"{config['LAB_PREFIX']}-{tenant.name}-storage"
    volume = json.loads(run(["docker", "volume", "inspect", volume_name], timeout=30).stdout)
    details = volume[0] if isinstance(volume, list) and len(volume) == 1 else None
    labels = details.get("Labels") if isinstance(details, dict) else None
    if (
        not isinstance(details, dict) or not isinstance(labels, dict)
        or details.get("Name") != volume_name
        or labels.get(config["OWNERSHIP_LABEL"]) != config["LAB_PREFIX"]
        or labels.get("cnpg-vcluster.capi/role") != "tenant-storage"
        or labels.get("cnpg-vcluster.capi/tenant") != tenant.name
        or Path(details.get("Mountpoint", "")).resolve() != tenant.storage_host_path.resolve()
    ):
        raise RuntimeError("disposable Tenant storage ownership changed before worker restart")
    before = worker_snapshot(root, config, client, tenant)
    name = sorted(before)[0]
    identity = before[name]
    container_id = identity["containerID"]
    if not all(identity.get(key) for key in ("machineUID", "devMachineUID", "nodeUID")) or not (
        isinstance(container_id, str) and len(container_id) == 64
        and all(character in "0123456789abcdef" for character in container_id)
    ):
        raise RuntimeError("recorded Tenant worker/node identity is incomplete")

    def lease_renewal() -> tuple[str, datetime]:
        resource = resource_by_kind(MANAGEMENT_CATALOG, "Lease")
        result = _tenant_kubectl(
            root, config, tenant, "-n", "kube-node-lease",
            "get", f"{resource.kubectl_resource}/{name}", "-o", "json", check=False,
        )
        if result.returncode != 0:
            raise RuntimeError("recorded Tenant worker kubelet Lease is unavailable")
        lease = json.loads(result.stdout)
        metadata = lease.get("metadata", {})
        spec = lease.get("spec", {})
        if (
            metadata.get("name") != name
            or metadata.get("namespace") != "kube-node-lease"
            or not metadata.get("uid")
            or spec.get("holderIdentity") != name
        ):
            raise RuntimeError("recorded Tenant worker kubelet Lease identity changed")
        renew_time = spec.get("renewTime")
        if not isinstance(renew_time, str):
            raise RuntimeError("recorded Tenant worker kubelet Lease has no renewal")
        return metadata["uid"], datetime.fromisoformat(renew_time.replace("Z", "+00:00"))

    lease_uid, previous_renewal = lease_renewal()

    def inspect_worker() -> dict[str, object]:
        payload = json.loads(run(["docker", "inspect", container_id], timeout=30).stdout)
        if not isinstance(payload, list) or len(payload) != 1:
            raise RuntimeError("recorded Tenant worker container is missing")
        worker = payload[0]
        labels = worker.get("Config", {}).get("Labels") or {}
        if (
            worker.get("Id") != container_id or worker.get("Name") != f"/{name}"
            or labels.get("io.x-k8s.kind.cluster") != tenant.name
            or labels.get("io.x-k8s.kind.role") != "worker"
            or worker.get("State", {}).get("Running") is not True
            or not worker["State"].get("StartedAt")
        ):
            raise RuntimeError("recorded Tenant worker container identity changed")
        return worker

    started_at = inspect_worker()["State"]["StartedAt"]
    require_tenant()
    run(
        ["docker", "restart", container_id],
        timeout=parse_duration(config["COMMAND_TIMEOUT"]) + 60,
    )
    restarted_at = inspect_worker()["State"]["StartedAt"]
    if restarted_at == started_at:
        raise RuntimeError("recorded Tenant worker did not restart")
    restart_time = datetime.fromisoformat(restarted_at.replace("Z", "+00:00"))

    def node_ready() -> bool | None:
        result = _tenant_kubectl(
            root, config, tenant, "get", f"node/{name}", "-o", "json", check=False,
        )
        if result.returncode != 0:
            return None
        node = json.loads(result.stdout)
        metadata = node.get("metadata", {})
        if metadata.get("name") != name or metadata.get("uid") != identity["nodeUID"]:
            raise RuntimeError("recorded Tenant worker Node identity changed")
        if not any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in node.get("status", {}).get("conditions", [])
        ):
            return None
        current_uid, current_renewal = lease_renewal()
        if current_uid != lease_uid:
            raise RuntimeError("recorded Tenant worker kubelet Lease identity changed")
        return current_renewal > max(previous_renewal, restart_time) or None

    wait_for(
        f"recorded Tenant worker Node/{name} recovery",
        parse_duration(config["WORKER_REGISTRATION_TIMEOUT"]),
        parse_duration(config["WAIT_POLL_INTERVAL"]), node_ready,
    )
    wait_network_ready(root, config, tenant)
    require_tenant()
    if worker_snapshot(root, config, client, tenant) != before:
        raise RuntimeError("recorded Tenant worker/node identity changed after restart")

    ready = catalog.wait(
        lambda item: item["catalogUid"] == catalog_uid
        and item["closed"] is False
        and item["capabilityAvailable"] is True
        and len(item["databases"]) == 3
        and {entry["name"] for entry in item["databases"]} == names
        and all(
            entry["phase"] == "ready" and entry["readyInstances"] == 3
            for entry in item["databases"]
        ),
        parse_duration(config["CNPG_TIMEOUT"]) * 4,
        catalog_uid,
    )
    recovered = ready_entries(ready, names, "local")
    for entry_name in sorted(names):
        def marker_restored():
            current = catalog.read(catalog_uid)
            entries = current["databases"]
            if (
                current["catalogUid"] != catalog_uid
                or current["closed"]
                or {entry["name"] for entry in entries} != names
                or len(entries) != 3
            ):
                raise RuntimeError("catalog identity changed after worker restart")
            if not current["capabilityAvailable"] or any(
                entry["phase"] != "ready" or entry["readyInstances"] != 3
                for entry in entries
            ):
                return None
            current_entries = ready_entries(current, names, "local")
            entry = current_entries[entry_name]
            if (
                entry["logicalUid"] != observed[entry_name]["logicalUid"]
                or entry["clusterUid"] != observed[entry_name]["clusterUid"]
            ):
                raise RuntimeError(
                    f"catalog entry identity changed after worker restart: {entry_name}"
                )
            try:
                catalog.query(catalog_uid, entry, f"marker-{entry_name}")
            except RuntimeError as error:
                if str(error).startswith("catalog query failed: HTTP 503:"):
                    return None
                raise
            return entry

        recovered[entry_name] = wait_for(
            f"exact marker for {entry_name} after worker restart",
            parse_duration(config["CNPG_TIMEOUT"]),
            parse_duration(config["WAIT_POLL_INTERVAL"]),
            marker_restored,
        )
    return recovered


def _verify_restarts_and_markers(
    root: Path, config: dict[str, str], client: ManagementClient,
    tenant, tenant_uid: str, catalog: CatalogClient, catalog_uid: str,
    observed: dict[str, dict[str, object]],
) -> dict[str, dict[str, object]]:
    names = {"alpha", "beta", "gamma"}
    for name, entry in observed.items():
        catalog.query(catalog_uid, entry, f"marker-{name}", write=True)
        catalog.query(catalog_uid, entry, f"marker-{name}")
    _failover(root, config, tenant, observed["alpha"])
    _restart_database_controller(client)
    ready = catalog.wait(
        lambda item: len(item["databases"]) == 3
        and all(entry["phase"] == "ready" and entry["readyInstances"] == 3
                for entry in item["databases"]),
        parse_duration(config["CNPG_TIMEOUT"]) * 4, catalog_uid,
    )
    observed = ready_entries(ready, names, "local")
    for name, entry in observed.items():
        catalog.query(catalog_uid, entry, f"marker-{name}")
    return _restart_worker_and_verify_markers(
        root, config, client, tenant, tenant_uid, catalog, catalog_uid, observed,
    )


def run_e2e() -> int:
    os.umask(0o077)
    config = load_configuration(ROOT)
    original_inotify = {
        "max_user_instances": read_inotify("max_user_instances"),
        "max_user_watches": read_inotify("max_user_watches"),
    }
    failure = None
    timings = PhaseTimings()
    tenant_name = "tenant-example"
    entries = {"alpha", "beta", "gamma"}
    try:
        with timings.phase("tools_cache"):
            run_just(ROOT, config, "tools")
        with timings.phase("initial_cleanup"):
            run_just(ROOT, config, "destroy")
            original_inotify = {
                "max_user_instances": read_inotify("max_user_instances"),
                "max_user_watches": read_inotify("max_user_watches"),
            }
        with timings.phase("host_preparation"):
            run_just(ROOT, config, "prepare-host")
        with timings.phase("management_bootstrap"):
            verify_all_inputs(ROOT, config)
            run_just(ROOT, config, "create-management")
            verify_admin_api(
                ManagementClient(ROOT, config),
                expected_tenant_names=(),
            )

        with timings.phase("tenant_convergence"):
            client = ManagementClient(ROOT, config)
            created = create_tenant_via_admin(
                client,
                tenant_name,
                workers=3,
            )
            document = wait_tenant_ready(ROOT, config, tenant_name)
            if document.get("metadata", {}).get("uid") != created["identity"]["uid"]:
                raise RuntimeError("disposable Tenant identity changed after creation")
            wait_for(
                "Tenant database catalog capability",
                parse_duration(config["CNPG_TIMEOUT"]) * 4, 5,
                lambda: (
                    current if (
                        (current := _inspect_management_object(client, f"tenant/{tenant_name}"))
                        and current.get("status", {}).get("databaseCapability", {}).get("available")
                        and current.get("metadata", {}).get("uid") == document["metadata"]["uid"]
                    ) else None
                ),
            )
            verify_admin_api(
                ManagementClient(ROOT, config),
                expected_tenant_names=(tenant_name,),
                require_available_databases=True,
            )
        run_just(ROOT, config, "local-tenant-status", tenant_name)
        with timings.phase("tenant_sql_probe"):
            tenant = tenant_from_document(ROOT, config, document)
            export_tenant_kubeconfig(
                ROOT, config, ManagementClient(ROOT, config), tenant,
            )
            catalog = catalog_client(client, tenant_name, document["metadata"]["uid"])
            initial = catalog.read()
            if initial["databases"]:
                raise RuntimeError("Tenant implicitly created a database")
            catalog_uid = initial["catalogUid"]
            logical_uids = {name: catalog.add(catalog_uid, name) for name in sorted(entries)}
            ready = _wait_databases_ready(
                client,
                catalog,
                catalog_uid,
                entries,
                parse_duration(config["CNPG_TIMEOUT"]) * 4,
            )
            observed = ready_entries(ready, entries, "local")
            observed = _verify_restarts_and_markers(
                ROOT, config, client, tenant, document["metadata"]["uid"],
                catalog, catalog_uid, observed,
            )
            recorded = _catalog_record(client, tenant_name)
            old_uid = logical_uids["beta"]
            old_entry = observed["beta"]
            catalog.delete(catalog_uid, old_uid, "beta")
            catalog.wait(
                lambda item: not any(entry["logicalUid"] == old_uid
                                     for entry in item["databases"]),
                parse_duration(config["CNPG_TIMEOUT"]) * 4, catalog_uid,
            )
            _verify_entry_gone(ROOT, config, client, tenant, catalog_uid, old_uid, recorded)
            replacement_uid = catalog.add(catalog_uid, "beta")
            if replacement_uid == old_uid:
                raise RuntimeError("recreated entry reused the deleted logical UID")
            catalog.require_stale_query(catalog_uid, old_entry)
            ready = _wait_databases_ready(
                client,
                catalog,
                catalog_uid,
                entries,
                parse_duration(config["CNPG_TIMEOUT"]) * 4,
            )
            recreated = ready_entries(ready, entries, "local")
            if recreated["beta"]["logicalUid"] != replacement_uid:
                raise RuntimeError("recreated local entry identity changed")
            stale = client.request_json(
                "DELETE", f"{ADMIN_SERVICE_PROXY}/{catalog.path}/{old_uid}",
                {"catalogUid": catalog_uid, "logicalUid": old_uid, "confirmation": "beta"},
            )
            require_stale_identity(stale)
            for name, entry in recreated.items():
                if name == "beta":
                    catalog.assert_fresh(catalog_uid, entry)
                    catalog.query(catalog_uid, entry, f"marker-{name}", write=True)
                catalog.query(catalog_uid, entry, f"marker-{name}")
            print("three independent three-instance databases and entry recreation verified")
        with timings.phase("tenant_deletion_finalization"):
            client = ManagementClient(ROOT, config)
            identity = capture_tenant_deletion_identity(config, client, document)
            delete_tenant_via_admin(client, identity["name"], identity["uid"])
            wait_tenant_absent(ROOT, config, identity["name"])
            verify_tenant_deletion(client, identity)
            catalog_response = client.kubectl(
                "-n", f"tenant-db-{tenant_name}", "get",
                f"tenantdatabasecatalog/{tenant_name}",
                "--ignore-not-found=true", "-o", "name",
            )
            if catalog_response.stdout.strip():
                raise RuntimeError("Tenant catalog remained after cascade")
            namespace_response = client.kubectl(
                "get", f"namespace/tenant-db-{tenant_name}",
                "--ignore-not-found=true", "-o", "name",
            )
            if namespace_response.stdout.strip():
                raise RuntimeError("Tenant database catalog namespace remained")
            verify_admin_api(client, expected_tenant_names=())
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
