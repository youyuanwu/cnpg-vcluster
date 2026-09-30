#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import sys
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
from scripts.lib.azure.foundation import ADMIN_SERVICE_PROXY, _inspect_foundation
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
from scripts.lib.azure.proof import (
    capture_operator_deletion_proof,
    prove_operator_deletion,
)
from scripts.lib.config import parse_duration
from scripts.lib.process import run
from scripts.lib.redaction import redact
from scripts.lib.tenant_spec import load_tenant_spec
from scripts.tenant import supported_versions


T = TypeVar("T")
ADMIN_API_SCHEMA_VERSION = 4


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
    command = "create" if method == "POST" else "delete"
    response = _kubectl(
        ROOT,
        command,
        "--raw",
        f"{ADMIN_SERVICE_PROXY}/{path.lstrip('/')}",
        "-f",
        "-",
        input_text=json.dumps(payload),
        check=False,
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


def _admin_create_tenant(spec) -> dict[str, object]:
    created = _admin_mutation(
        "POST",
        "api/v1/tenants",
        {"name": spec.name, "workers": spec.workers, "databases": None},
    )
    identity = created.get("identity")
    if (
        set(created) != {"identity", "provider", "kubernetesVersion"}
        or not isinstance(identity, dict)
        or identity.get("name") != spec.name
        or not isinstance(identity.get("uid"), str)
        or not identity["uid"]
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
        or identity.get("name") != name
        or identity.get("uid") != uid
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
    existing = read_tenant(ROOT, spec.name)
    metadata = existing.get("metadata") if isinstance(existing, dict) else None
    if isinstance(metadata, dict) and metadata.get("deletionTimestamp"):
        wait_tenant_absent(ROOT, spec.name)
        existing = None
    if existing is None:
        _admin_create_tenant(spec)
    else:
        if existing.get("spec") != tenant_document(spec)["spec"]:
            raise RuntimeError(
                "Azure lifecycle gate found an incompatible existing Tenant"
            )
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


def _source_sha256(spec_path: Path) -> str:
    tracked = run(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        timeout=30,
        cwd=ROOT.parent,
    ).stdout
    if tracked.strip():
        raise RuntimeError(
            "Azure lifecycle gate requires a clean tracked worktree"
        )
    digest = hashlib.sha256()
    revision = run(
        ["git", "rev-parse", "HEAD"],
        timeout=30,
        cwd=ROOT.parent,
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
    phase("create-ready", lambda: _ensure_tenant_ready(config, spec))

    tenant, before, owned_before = phase(
        "worker-identity-verification",
        lambda: _ready_snapshot(config, spec),
    )
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
    proof = capture_operator_deletion_proof(ROOT, config, tenant_after)

    phase(
        "ordinary-tenant-deletion",
        lambda: (
            _admin_delete_tenant(spec.name, str(tenant_uid)),
            _require_status(spec.name, "absent"),
        ),
    )
    phase(
        "external-absence-proof",
        lambda: prove_operator_deletion(ROOT, config, proof),
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
    phase(
        "recreation",
        lambda: (
            _admin_create_tenant(spec),
            _require_status(spec.name, "ready"),
            _ready_snapshot(config, spec),
        ),
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except Exception as exc:
        print(redact(str(exc)), file=sys.stderr)
        raise SystemExit(1)
