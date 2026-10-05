#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import base64
import json
import math
import os
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Mapping, TypeVar

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.azure import _run_profile_mutation
from scripts.lib.azure.common import (
    _az,
    _json,
    _kubectl,
    load_azure_configuration,
    names,
)
from scripts.lib.azure.foundation import (
    ADMIN_SERVICE_PROXY,
    _azure_tenant_api_tunnel,
    _inspect_foundation,
)
from scripts.lib.azure.gate import (
    WorkerSnapshot,
    build_worker_snapshot,
    require_owned_resource_delta,
    require_replacement,
)
from scripts.lib.azure.operator import (
    read_tenant,
    tenant_document,
    tenant_status,
    wait_tenant_absent,
    wait_tenant_ready,
)
from scripts.lib.azure.ownership import observe_azure_owned_resources
from scripts.lib.azure.ownership import tenant_tagged_azure_resources
from scripts.lib.catalog_lifecycle import CatalogClient, ready_entries, require_stale_identity
from scripts.lib.kube import wait_for
from scripts.lib.azure.proof import (
    capture_operator_deletion_proof,
    prove_operator_deletion,
)
from scripts.lib.config import parse_duration
from scripts.lib.files import ensure_private_dir
from scripts.lib.kube import kubeconfig_json_request
from scripts.lib.process import run
from scripts.lib.redaction import redact
from scripts.lib.tenant_spec import load_tenant_spec
from scripts.tenant import supported_versions


T = TypeVar("T")
ADMIN_API_SCHEMA_VERSION = 7


def _tenant_command(*arguments: str) -> str:
    return run(
        [sys.executable, str(ROOT / "scripts" / "tenant.py"), *arguments],
        timeout=2 * 60 * 60,
    ).stdout


def _admin_mutation(
    method: str,
    path: str,
    payload: Mapping[str, object],
) -> dict[str, object]:
    config = _kubectl(
        ROOT,
        "config",
        "view",
        "--raw",
        "--flatten",
        "--minify",
        "-o",
        "json",
        check=False,
    )
    response = kubeconfig_json_request(
        config,
        method,
        f"{ADMIN_SERVICE_PROXY}/{path.lstrip('/')}",
        dict(payload),
        300,
    )
    if response.returncode != 0:
        raise RuntimeError(f"Azure Tenant Admin {method} {path} failed")
    envelope = json.loads(response.stdout)
    if (
        not isinstance(envelope, dict)
        or set(envelope) != {"schemaVersion", "data"}
        or envelope.get("schemaVersion") != ADMIN_API_SCHEMA_VERSION
        or not isinstance(envelope.get("data"), dict)
    ):
        raise RuntimeError("Azure Tenant Admin lifecycle response is invalid")
    return envelope["data"]


def _catalog_client(name: str, uid: str) -> CatalogClient:
    def get(path: str) -> str:
        return _kubectl(
            ROOT, "get", f"--raw={ADMIN_SERVICE_PROXY}/{path}", check=True,
        ).stdout

    def mutate(method: str, path: str, payload: dict[str, object]):
        config = _kubectl(
            ROOT, "config", "view", "--raw", "--flatten", "--minify",
            "-o", "json", check=False,
        )
        return kubeconfig_json_request(
            config, method, f"{ADMIN_SERVICE_PROXY}/{path}", payload, 300,
        )

    return CatalogClient(get, mutate, name, uid)


def _catalog_record(name: str) -> dict:
    return json.loads(_kubectl(
        ROOT, "-n", f"tenant-db-{name}", "get",
        f"tenantdatabasecatalog/{name}", "-o", "json",
    ).stdout)


def _disk_records(name: str, logical_uids: set[str],
                  group_id: str) -> dict[str, tuple[str, str]]:
    catalog = _catalog_record(name)
    entries = catalog.get("status", {}).get("entries", {})
    if set(entries) != logical_uids:
        raise RuntimeError("Azure catalog observations are incomplete")
    disks = {}
    for uid, entry in entries.items():
        storage = entry.get("storage", [])
        if len(storage) != 3 or {item.get("ordinal") for item in storage} != {1, 2, 3}:
            raise RuntimeError("Azure entry does not record three disks")
        for item in storage:
            disk = item.get("disk")
            arm_id = item.get("armID")
            if not isinstance(disk, dict) or not disk.get("uid") or not disk.get("name"):
                raise RuntimeError("Azure ASO disk identity is absent")
            if not isinstance(arm_id, str) or arm_id.lower() != (
                f"{group_id.rstrip('/')}/providers/Microsoft.Compute/disks/"
                f"{disk['name']}"
            ).lower():
                raise RuntimeError("Azure disk ARM ID is invalid")
            if arm_id.lower() in disks:
                raise RuntimeError("Azure disk ARM IDs overlap")
            disks[arm_id.lower()] = (disk["name"], disk["uid"])
    if len(disks) != 9:
        raise RuntimeError("Azure gate requires nine exact disk IDs")
    return disks


def _require_disks_absent(config: Mapping[str, str], tenant: str,
                          disks: Mapping[str, tuple[str, str]]) -> None:
    namespace = f"tenant-db-storage-{tenant}"
    for arm_id, (name, _) in disks.items():
        result = _kubectl(
            ROOT, "-n", namespace, "get", f"disks.compute.azure.com/{name}",
            "--ignore-not-found=true", "-o", "name",
        )
        if result.stdout.strip():
            raise RuntimeError(f"recorded ASO Disk remained: {name}")
        response = _az(
            "rest", "--method", "get",
            "--url", f"https://management.azure.com{arm_id}?api-version=2024-03-02",
            "--output", "json", check=False,
        )
        if response.returncode == 0 or not any(
            code in response.stderr for code in ("ResourceNotFound", "NotFound", "(404)")
        ):
            raise RuntimeError(f"ARM disk absence is unproven: {name}")


def _require_catalog_absent(name: str) -> None:
    for resource in (
        f"namespace/tenant-db-{name}",
        f"namespace/tenant-db-storage-{name}",
    ):
        response = _kubectl(ROOT, "get", resource, "--ignore-not-found=true", "-o", "name")
        if response.stdout.strip():
            raise RuntimeError(f"Azure database namespace remained: {resource}")


def _tenant_kubectl(tenant: Mapping[str, object], *arguments: str):
    name = tenant["metadata"]["name"]
    provider = tenant["status"]["provider"]
    bound = provider["kubeconfig"]
    secret = json.loads(_kubectl(
        ROOT, "-n", name, "get", f"secret/{name}-kubeconfig", "-o", "json",
    ).stdout)
    data = base64.b64decode(secret["data"]["value"], validate=True)
    if (
        secret["metadata"]["uid"] != bound["secretUID"]
        or hashlib.sha256(data).hexdigest() != bound["contentSha256"]
    ):
        raise RuntimeError("Azure Tenant kubeconfig identity changed")
    scratch = ROOT / ".runtime" / "azure-tenant-lifecycle"
    ensure_private_dir(scratch)
    with tempfile.TemporaryDirectory(dir=scratch) as directory:
        path = Path(directory) / "tenant.kubeconfig"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
        with _azure_tenant_api_tunnel(
            ROOT,
            name,
            name,
            path,
            provider["endpoint"],
        ):
            return run(
                [
                    str(ROOT / ".tools/bin/kubectl"),
                    "--kubeconfig",
                    str(path),
                    *arguments,
                ],
                timeout=300,
            )


def _failover(tenant: Mapping[str, object], entry: Mapping[str, object]) -> None:
    namespace, cluster = entry["namespace"], entry["cluster"]
    reference = f"clusters.postgresql.cnpg.io/{cluster}"
    previous = _tenant_kubectl(
        tenant, "-n", namespace, "get", reference,
        "-o", "jsonpath={.status.currentPrimary}",
    ).stdout.strip()
    if previous not in {item["name"] for item in entry["instanceTopology"]}:
        raise RuntimeError("Azure CNPG primary is not an observed instance")
    _tenant_kubectl(tenant, "-n", namespace, "delete", f"pod/{previous}", "--wait=false")
    wait_for(
        "Azure CNPG failover", 1800, 10,
        lambda: (current if (current := _tenant_kubectl(
            tenant, "-n", namespace, "get", reference,
            "-o", "jsonpath={.status.currentPrimary}",
        ).stdout.strip()) and current != previous else None),
    )


def _restart_database_controller() -> None:
    _kubectl(ROOT, "-n", "tenant-system", "rollout", "restart",
             "deployment/database-controller")
    _kubectl(ROOT, "-n", "tenant-system", "rollout", "status",
             "deployment/database-controller", "--timeout=600s")


def _wait_databases_ready(
    catalog: CatalogClient,
    catalog_uid: str,
    timeout: int,
) -> dict[str, dict]:
    deadline = time.monotonic() + timeout
    restarted = False
    while True:
        projected = catalog.read(catalog_uid)
        databases = projected["databases"]
        if (
            len(databases) == 3
            and all(
                entry["phase"] == "ready" and entry["readyInstances"] == 3
                for entry in databases
            )
        ):
            return ready_entries(projected, {"alpha", "beta", "gamma"}, "azure")
        unknown = any(
            blocker.get("code") == "UnknownCreateOutcome"
            for entry in databases
            for blocker in entry["blockers"]
        )
        if unknown and not restarted:
            _restart_database_controller()
            restarted = True
        if time.monotonic() >= deadline:
            raise RuntimeError("catalog did not converge before deadline")
        time.sleep(5)


def _require_clean_tagged_foundation(config: Mapping[str, str], name: str) -> None:
    group = names(config)["resourceGroup"]
    tags = _json(["az", "group", "show", "--name", group, "--query", "tags", "-o", "json"])
    if not isinstance(tags, dict) or any(tags.get(key) != value for key, value in {
        "cnpg-vcluster-experiment": "azure-capi",
        "cnpg-vcluster-prefix": config["AZURE_PREFIX"],
        "cnpg-vcluster-owner": "cnpg-vcluster",
    }.items()):
        raise RuntimeError("Azure destructive gate requires a tagged experiment foundation")
    tenants = json.loads(_kubectl(ROOT, "get", "tenants", "-o", "json").stdout)
    catalogs = json.loads(_kubectl(
        ROOT, "get", "tenantdatabasecatalogs", "--all-namespaces", "-o", "json",
    ).stdout)
    disks = _json([
        "az", "disk", "list", "--resource-group", group, "--output", "json",
    ])
    if (
        not isinstance(tenants, dict) or not isinstance(tenants.get("items"), list)
        or not isinstance(catalogs, dict) or not isinstance(catalogs.get("items"), list)
        or not isinstance(disks, list)
        or tenants["items"] or catalogs["items"]
        or disks
        or read_tenant(ROOT, name) is not None
        or tenant_tagged_azure_resources(config, name)
    ):
        raise RuntimeError("Azure destructive gate requires empty Tenant and catalog inventories")


def _admin_create_tenant(spec) -> dict[str, object]:
    created = _admin_mutation(
        "POST",
        "api/v1/tenants",
        {"name": spec.name, "workers": spec.workers},
    )
    identity = created.get("identity")
    if (
        set(created) != {"identity", "provider", "kubernetesVersion"}
        or not isinstance(identity, dict)
        or set(identity) != {"name", "uid", "generation"}
        or identity.get("name") != spec.name
        or not isinstance(identity.get("uid"), str)
        or not identity["uid"]
        or type(identity.get("generation")) is not int
        or created.get("provider") != "azure"
        or created.get("kubernetesVersion") != spec.kubernetes_version
    ):
        raise RuntimeError("Azure Tenant Admin create response is invalid")
    return created


def _admin_delete_tenant(name: str, uid: str) -> dict[str, object]:
    deleted = _admin_mutation(
        "DELETE",
        f"api/v1/tenants/{name}",
        {"uid": uid, "confirmation": name},
    )
    identity = deleted.get("identity")
    if (
        set(deleted) != {"identity", "state"}
        or not isinstance(identity, dict)
        or set(identity) != {"name", "uid", "generation"}
        or identity.get("name") != name
        or identity.get("uid") != uid
        or (
            identity.get("generation") is not None
            and type(identity.get("generation")) is not int
        )
        or deleted.get("state") not in {"accepted", "completed"}
    ):
        raise RuntimeError("Azure Tenant Admin delete response is invalid")
    return deleted


def _require_status(tenant: str, classification: str) -> dict[str, object]:
    output = _tenant_command("status", "azure", tenant)
    lines = [line for line in output.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("Azure tenant status produced no output")
    payload = json.loads(lines[-1])
    if payload.get("classification") != classification:
        raise RuntimeError(
            f"Azure tenant status is {payload.get('classification')}, "
            f"expected {classification}: {payload.get('blockers')}"
        )
    return payload


def _ensure_tenant_ready(config: Mapping[str, str], spec) -> dict[str, object]:
    if read_tenant(ROOT, spec.name) is not None:
        raise RuntimeError("Azure lifecycle gate requires an absent Tenant")
    _admin_create_tenant(spec)
    wait_tenant_ready(ROOT, spec.name)
    return _require_status(spec.name, "ready")


def _vmss_instances(config: Mapping[str, str], vmss_id: str) -> list[str]:
    pool = vmss_id.rstrip("/").split("/")[-1]
    payload = _json(
        [
            "az",
            "vmss",
            "list-instances",
            "--resource-group",
            names(config)["resourceGroup"],
            "--name",
            pool,
            "--query",
            "[].{id:id,instanceId:instanceId}",
            "--output",
            "json",
        ]
    )
    if not isinstance(payload, list):
        raise RuntimeError("Azure VMSS instance inventory is invalid")
    identities = []
    normalized = set()
    for item in payload:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("id"), str)
            or isinstance(item.get("instanceId"), bool)
            or not str(item.get("instanceId", "")).isdigit()
            or item["id"].rstrip("/").split("/")[-1]
            != str(item["instanceId"])
        ):
            raise RuntimeError("Azure VMSS instance inventory is incomplete")
        key = item["id"].rstrip("/").lower()
        if key in normalized:
            raise RuntimeError("Azure VMSS instance inventory contains duplicates")
        normalized.add(key)
        identities.append(item["id"])
    return identities


def _provider(tenant: Mapping[str, object]) -> Mapping[str, object]:
    status = tenant.get("status")
    provider = status.get("provider") if isinstance(status, dict) else None
    if not isinstance(provider, dict) or provider.get("type") != "azure":
        raise RuntimeError("Azure operator provider status is absent")
    return provider


def _ready_snapshot(
    config: Mapping[str, str],
    spec,
) -> tuple[Mapping[str, object], WorkerSnapshot, dict[str, object]]:
    tenant = read_tenant(ROOT, spec.name)
    status = tenant_status(spec.name, tenant)
    if tenant is None or status.classification != "ready":
        raise RuntimeError(
            "Azure Tenant operator is not Ready: " + "; ".join(status.blockers)
        )
    if tenant.get("spec") != tenant_document(spec)["spec"]:
        raise RuntimeError("Azure Tenant specification changed during the gate")
    metadata = tenant.get("metadata")
    if (
        not isinstance(metadata, dict)
        or not isinstance(metadata.get("uid"), str)
        or not metadata["uid"]
    ):
        raise RuntimeError("Azure Tenant UID is absent")
    provider = _provider(tenant)
    binding = provider.get("binding")
    vmss = provider.get("vmss")
    nodes = provider.get("nodes")
    if (
        not isinstance(binding, dict)
        or not isinstance(binding.get("operationId"), str)
        or not binding["operationId"]
        or not isinstance(vmss, dict)
        or not isinstance(vmss.get("id"), str)
        or not isinstance(vmss.get("instanceIds"), list)
        or not isinstance(nodes, list)
    ):
        raise RuntimeError("Azure operator worker identity is incomplete")
    live_instances = _vmss_instances(config, vmss["id"])
    if {
        str(value).rstrip("/").lower() for value in vmss["instanceIds"]
    } != {value.rstrip("/").lower() for value in live_instances}:
        raise RuntimeError("Azure operator and VMSS instance inventories differ")
    readiness = {
        "requestedWorkers": spec.workers,
        "readyReplicas": len(nodes),
        "nodeRefs": [
            node.get("name") for node in nodes if isinstance(node, dict)
        ],
        "nodes": nodes,
    }
    snapshot = build_worker_snapshot(readiness, vmss["id"], live_instances)
    owned = observe_azure_owned_resources(ROOT, config, tenant)
    return tenant, snapshot, owned


def _require_runtime_identity(
    spec, expected: Mapping[str, object], current: Mapping[str, object] | None,
) -> Mapping[str, object]:
    if not isinstance(current, dict):
        raise RuntimeError("Azure database runtime Tenant disappeared or is invalid")
    metadata = current.get("metadata")
    previous = expected.get("metadata")
    if (
        not isinstance(metadata, dict)
        or not isinstance(previous, dict)
        or metadata.get("name") != spec.name
        or not isinstance(previous.get("uid"), str)
        or not previous["uid"]
        or metadata.get("uid") != previous["uid"]
        or metadata.get("deletionTimestamp")
        or current.get("spec") != tenant_document(spec)["spec"]
        or tenant_status(spec.name, current).classification != "ready"
    ):
        raise RuntimeError("Azure database runtime Tenant identity or readiness changed")
    provider = _provider(current)
    original = _provider(expected)
    if any(
        not isinstance(original.get(key), dict)
        or not original[key]
        or provider.get(key) != original[key]
        for key in ("binding", "management", "kubeconfig")
    ):
        raise RuntimeError("Azure database runtime Tenant binding changed")
    return current


def _require_only_runtime_tenant(spec, expected: Mapping[str, object]) -> None:
    inventory = json.loads(_kubectl(ROOT, "get", "tenants", "-o", "json").stdout)
    if (
        not isinstance(inventory, dict)
        or inventory.get("kind") not in {"List", "TenantList"}
        or not isinstance(inventory.get("metadata"), dict)
        or inventory["metadata"].get("continue", "") != ""
        or not isinstance(inventory.get("items"), list)
        or len(inventory["items"]) != 1
    ):
        raise RuntimeError("Azure database runtime requires one complete Tenant inventory")
    _require_runtime_identity(spec, expected, inventory["items"][0])


def _remaining_runtime_seconds(deadline: float) -> int:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RuntimeError("Azure database runtime and capability timed out")
    return math.ceil(remaining)


def _install_database_runtime(
    spec, tenant: Mapping[str, object], deadline: float,
) -> None:
    _remaining_runtime_seconds(deadline)
    _require_only_runtime_tenant(spec, tenant)
    attempt_seconds = min(420, _remaining_runtime_seconds(deadline))
    run(
        [
            "timeout", "--kill-after=5s", f"{attempt_seconds}s",
            "just", "azure-database-runtime-once",
        ],
        cwd=ROOT,
        timeout=attempt_seconds + 15,
    )
    _require_only_runtime_tenant(spec, tenant)


def _wait_database_capability(
    spec, tenant: Mapping[str, object], deadline: float,
) -> None:
    def available() -> Mapping[str, object] | None:
        current = _require_runtime_identity(
            spec, tenant, read_tenant(ROOT, spec.name),
        )
        return (
            current
            if current.get("status", {}).get("databaseCapability", {}).get("available")
            else None
        )

    wait_for(
        "Azure catalog capability",
        _remaining_runtime_seconds(deadline),
        10,
        available,
    )


def _wait_ready_snapshot(
    config: Mapping[str, str],
    spec,
) -> tuple[Mapping[str, object], WorkerSnapshot, dict[str, object]]:
    wait_tenant_ready(ROOT, spec.name)
    deadline = time.monotonic() + parse_duration(config["AZURE_TENANT_TIMEOUT"])
    last = "Azure external ownership has not converged"
    while time.monotonic() < deadline:
        try:
            return _ready_snapshot(config, spec)
        except RuntimeError as exc:
            last = str(exc)
            time.sleep(10)
    raise RuntimeError("Azure worker recovery timed out: " + last)


def _require_recreated_identity(
    recreated: Mapping[str, object],
    old_tenant_uid: object,
    old_lease_uid: str,
) -> None:
    metadata = recreated.get("metadata")
    allocation = _provider(recreated).get("networkAllocation")
    new_lease_uid = (
        allocation.get("leaseUID") if isinstance(allocation, dict) else None
    )
    if (
        not isinstance(metadata, dict)
        or not isinstance(metadata.get("uid"), str)
        or not metadata["uid"]
        or metadata.get("uid") == old_tenant_uid
        or not isinstance(new_lease_uid, str)
        or not new_lease_uid
        or new_lease_uid == old_lease_uid
    ):
        raise RuntimeError("Azure Tenant recreation retained old identity")


def _require_allocation_lease_absent(lease_name: str) -> None:
    response = _kubectl(
        ROOT,
        "-n",
        "tenant-system",
        "get",
        f"lease/{lease_name}",
        "--ignore-not-found=true",
        "-o",
        "name",
    )
    if response.stdout.strip():
        raise RuntimeError("Azure Tenant allocation Lease remained after deletion")


def _source_sha256(spec_path: Path) -> str:
    tracked = run(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        timeout=30,
        cwd=ROOT,
    ).stdout
    if tracked.strip():
        raise RuntimeError(
            "Azure lifecycle gate requires a clean tracked worktree"
        )
    digest = hashlib.sha256()
    revision = run(
        ["git", "rev-parse", "HEAD"],
        timeout=30,
        cwd=ROOT,
    ).stdout.strip()
    digest.update(revision.encode())
    digest.update(b"\0")
    digest.update(str(spec_path.resolve()).encode())
    digest.update(b"\0")
    digest.update(spec_path.read_bytes())
    return digest.hexdigest()


def main(arguments: list[str]) -> int:
    os.umask(0o077)
    spec_path = (
        Path(arguments[0])
        if arguments
        else ROOT / "config" / "tenants" / "examples" / "azure.json"
    )
    if not spec_path.is_absolute():
        spec_path = ROOT / spec_path
    config = load_azure_configuration(ROOT)
    spec = load_tenant_spec(
        spec_path,
        expected_profile="azure",
        supported_versions=supported_versions(ROOT),
    )
    if spec.workers != 3:
        raise RuntimeError("Azure lifecycle gate requires exactly three workers")
    source_sha256 = _source_sha256(spec_path)

    def phase(name: str, operation: Callable[[], T]) -> T:
        started = time.monotonic()
        try:
            value = operation()
        except BaseException as exc:
            print(
                f"Azure tenant lifecycle phase failed: {name}: "
                f"{redact(str(exc))}",
                file=sys.stderr,
            )
            raise
        print(
            f"Azure tenant lifecycle phase passed: {name} "
            f"({time.monotonic() - started:.3f}s)"
        )
        return value

    foundation_before = phase(
        "foundation-readiness",
        lambda: _inspect_foundation(ROOT, config, require_healthy=True)[0],
    )
    phase("clean-tagged-foundation", lambda: _require_clean_tagged_foundation(config, spec.name))
    phase("create-ready", lambda: _ensure_tenant_ready(config, spec))

    tenant, before, owned_before = phase(
        "worker-identity-verification",
        lambda: _ready_snapshot(config, spec),
    )
    runtime_deadline = time.monotonic() + parse_duration(config["AZURE_TENANT_TIMEOUT"])
    phase(
        "database-runtime-install",
        lambda: _install_database_runtime(spec, tenant, runtime_deadline),
    )
    phase(
        "database-capability-readiness",
        lambda: _wait_database_capability(spec, tenant, runtime_deadline),
    )
    catalog = _catalog_client(spec.name, tenant["metadata"]["uid"])
    initial = phase("empty-catalog", catalog.read)
    if initial["databases"]:
        raise RuntimeError("Azure Tenant implicitly created database entries")
    catalog_uid = initial["catalogUid"]
    names_by_uid = {
        name: phase(f"add-{name}", lambda name=name: catalog.add(catalog_uid, name))
        for name in ("alpha", "beta", "gamma")
    }
    def wait_database_ready():
        return _wait_databases_ready(
            catalog,
            catalog_uid,
            parse_duration(config["AZURE_TENANT_TIMEOUT"]),
        )

    databases = phase("nine-instance-readiness", wait_database_ready)
    for name, entry in databases.items():
        phase(f"write-{name}", lambda name=name, entry=entry:
              catalog.query(catalog_uid, entry, f"marker-{name}", write=True))
        phase(f"query-{name}", lambda name=name, entry=entry:
              catalog.query(catalog_uid, entry, f"marker-{name}"))
    _failover(tenant, databases["alpha"])
    phase("database-controller-restart", _restart_database_controller)
    databases = phase("post-restart-readiness", wait_database_ready)
    for name, entry in databases.items():
        catalog.query(catalog_uid, entry, f"marker-{name}")
    old_disks = phase(
        "nine-exact-disk-identities",
        lambda: _disk_records(
            spec.name, set(names_by_uid.values()), foundation_before["resourceGroupId"],
        ),
    )
    old_beta_names = {
        item["disk"]["name"]
        for item in _catalog_record(spec.name)["status"]["entries"][
            names_by_uid["beta"]
        ]["storage"]
    }
    old_beta_entry = databases["beta"]
    catalog.delete(catalog_uid, names_by_uid["beta"], "beta")
    catalog.wait(
        lambda item: not any(entry["logicalUid"] == names_by_uid["beta"]
                             for entry in item["databases"]),
        parse_duration(config["AZURE_TENANT_TIMEOUT"]), catalog_uid,
    )
    old_beta_disks = {
        arm: identity for arm, identity in old_disks.items()
        if identity[0] in old_beta_names
    }
    if len(old_beta_disks) != 3:
        raise RuntimeError("deleted Azure entry disk identities are incomplete")
    phase("deleted-entry-disk-absence", lambda:
          _require_disks_absent(config, spec.name, old_beta_disks))
    replacement = catalog.add(catalog_uid, "beta")
    if replacement == names_by_uid["beta"]:
        raise RuntimeError("recreated Azure entry reused stale logical UID")
    catalog.require_stale_query(catalog_uid, old_beta_entry)
    names_by_uid["beta"] = replacement
    databases = phase("entry-recreation", wait_database_ready)
    if databases["beta"]["logicalUid"] != replacement:
        raise RuntimeError("recreated Azure entry identity changed")
    require_stale_identity(catalog.mutate(
        "DELETE", f"{catalog.path}/{old_beta_entry['logicalUid']}", {
            "catalogUid": catalog_uid,
            "logicalUid": old_beta_entry["logicalUid"],
            "confirmation": "beta",
        },
    ))
    for name, entry in databases.items():
        if name == "beta":
            catalog.assert_fresh(catalog_uid, entry)
            catalog.query(catalog_uid, entry, f"marker-{name}", write=True)
        catalog.query(catalog_uid, entry, f"marker-{name}")
    disks = phase(
        "recreated-nine-disk-identities",
        lambda: _disk_records(
            spec.name, set(names_by_uid.values()), foundation_before["resourceGroupId"],
        ),
    )
    if set(disks) & set(old_beta_disks):
        raise RuntimeError("recreated Azure entry reused stale disks")
    metadata = tenant.get("metadata")
    binding = _provider(tenant).get("binding")
    if not isinstance(metadata, dict) or not isinstance(binding, dict):
        raise RuntimeError("Azure Tenant identity is incomplete before injection")
    tenant_uid = metadata.get("uid")
    provider_operation_id = binding.get("operationId")
    allocation = _provider(tenant).get("networkAllocation")
    if (
        not isinstance(allocation, dict)
        or not isinstance(allocation.get("slotId"), str)
        or not allocation["slotId"]
        or not isinstance(allocation.get("podCIDR"), str)
        or not isinstance(allocation.get("serviceCIDR"), str)
    ):
        raise RuntimeError("Azure Tenant allocation status is incomplete")
    allocation_lease_uid = allocation.get("leaseUID")
    allocation_lease_name = allocation.get("leaseName")
    if (
        not isinstance(allocation_lease_uid, str)
        or not allocation_lease_uid
        or not isinstance(allocation_lease_name, str)
        or not allocation_lease_name
    ):
        raise RuntimeError("Azure Tenant allocation Lease identity is incomplete")

    if _source_sha256(spec_path) != source_sha256:
        raise RuntimeError(
            "Azure lifecycle gate source changed before failure injection"
        )

    target = before.target
    phase(
        "worker-instance-deletion",
        lambda: _run_profile_mutation(
            ROOT,
            config,
            lambda _root, _config: _az(
                "vmss",
                "delete-instances",
                "--resource-group",
                names(config)["resourceGroup"],
                "--name",
                before.vmss_id.rstrip("/").split("/")[-1],
                "--instance-ids",
                target.instance_id,
                "--output",
                "none",
                timeout=parse_duration(config["AZURE_TENANT_TIMEOUT"]) + 60,
            ),
        ),
    )

    def verify_worker_recovery() -> Mapping[str, object]:
        tenant_after, after, owned_after = _wait_ready_snapshot(config, spec)
        deleted, replacement = require_replacement(before, after)
        require_owned_resource_delta(
            owned_before,
            owned_after,
            deleted,
            replacement,
        )
        return tenant_after

    tenant_after = phase("worker-recovery", verify_worker_recovery)
    metadata_after = tenant_after.get("metadata")
    binding_after = _provider(tenant_after).get("binding")
    if (
        not isinstance(metadata_after, dict)
        or metadata_after.get("uid") != tenant_uid
        or not isinstance(binding_after, dict)
        or binding_after.get("operationId") != provider_operation_id
    ):
        raise RuntimeError("Azure Tenant identity changed during worker recovery")
    databases = phase("database-recovery-after-worker-replacement", wait_database_ready)
    for name, entry in databases.items():
        catalog.query(catalog_uid, entry, f"marker-{name}")
    if _disk_records(
        spec.name, set(names_by_uid.values()), foundation_before["resourceGroupId"],
    ) != disks:
        raise RuntimeError("Azure worker replacement changed retained disk identities")
    proof = capture_operator_deletion_proof(ROOT, config, tenant_after)

    phase(
        "ordinary-tenant-deletion",
        lambda: (
            _admin_delete_tenant(spec.name, str(tenant_uid)),
            wait_tenant_absent(ROOT, spec.name),
            _require_status(spec.name, "absent"),
        ),
    )
    phase(
        "external-absence-proof",
        lambda: prove_operator_deletion(ROOT, config, proof),
    )
    phase("nine-exact-disk-absence", lambda: _require_disks_absent(config, spec.name, disks))
    phase("catalog-and-storage-namespace-absence", lambda: _require_catalog_absent(spec.name))
    phase(
        "allocation-release-proof",
        lambda: _require_allocation_lease_absent(allocation_lease_name),
    )

    def verify_foundation() -> None:
        foundation_after = _inspect_foundation(
            ROOT, config, require_healthy=True
        )[0]
        if foundation_after != foundation_before:
            raise RuntimeError(
                "Azure shared foundation identity changed during the gate"
            )

    phase("foundation-verification", verify_foundation)
    def verify_recreation() -> None:
        _admin_create_tenant(spec)
        wait_tenant_ready(ROOT, spec.name)
        recreated, _, _ = _ready_snapshot(config, spec)
        _require_recreated_identity(
            recreated,
            tenant_uid,
            allocation_lease_uid,
        )

    phase("recreation", verify_recreation)
    recreated = read_tenant(ROOT, spec.name)
    phase(
        "recreated-tenant-cascade-cleanup",
        lambda: (
            _admin_delete_tenant(spec.name, recreated["metadata"]["uid"]),
            wait_tenant_absent(ROOT, spec.name),
            _require_status(spec.name, "absent"),
        ),
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except Exception as exc:
        print(redact(str(exc)), file=sys.stderr)
        raise SystemExit(1)
