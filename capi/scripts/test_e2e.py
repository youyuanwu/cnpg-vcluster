#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
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
from scripts.lib.tenants import _tenant_kubectl

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
            create_tenant_via_admin(
                client,
                tenant_name,
                workers=3,
            )
            document = wait_tenant_ready(ROOT, config, tenant_name)
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
            ready = catalog.wait(
                lambda item: all(
                    entry["phase"] == "ready" for entry in item["databases"]
                ) and len(item["databases"]) == 3,
                parse_duration(config["CNPG_TIMEOUT"]) * 4, catalog_uid,
            )
            observed = ready_entries(ready, entries, "local")
            for name, entry in observed.items():
                catalog.query(catalog_uid, entry, f"marker-{name}", write=True)
                catalog.query(catalog_uid, entry, f"marker-{name}")
            _failover(ROOT, config, tenant, observed["alpha"])
            _restart_database_controller(client)
            ready = catalog.wait(
                lambda item: len(item["databases"]) == 3
                and all(entry["phase"] == "ready" and entry["readyInstances"] == 3
                        for entry in item["databases"]),
                parse_duration(config["CNPG_TIMEOUT"]) * 4, catalog_uid,
            )
            observed = ready_entries(ready, entries, "local")
            for name, entry in observed.items():
                catalog.query(catalog_uid, entry, f"marker-{name}")
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
            ready = catalog.wait(
                lambda item: len(item["databases"]) == 3
                and all(entry["phase"] == "ready" for entry in item["databases"]),
                parse_duration(config["CNPG_TIMEOUT"]) * 4, catalog_uid,
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
