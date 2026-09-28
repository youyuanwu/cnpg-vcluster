#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.azure import _run_profile_mutation
from scripts.lib.azure.common import (
    _az,
    _json,
    load_azure_configuration,
    names,
)
from scripts.lib.azure.foundation import _inspect_foundation
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
    wait_tenant_ready,
)
from scripts.lib.azure.ownership import observe_azure_owned_resources
from scripts.lib.azure.proof import (
    AzureDeletionProof,
    capture_operator_deletion_proof,
    prove_operator_deletion,
)
from scripts.lib.config import parse_duration
from scripts.lib.files import private_file_exists, read_private_file, write_private_file
from scripts.lib.process import run
from scripts.lib.redaction import redact, redact_value
from scripts.lib.tenant_spec import load_tenant_spec
from scripts.tenant import supported_versions


SOURCE_PATHS = (
    "scripts/test_azure_tenant_lifecycle.py",
    "scripts/tenant.py",
    "scripts/lib/azure/common.py",
    "scripts/lib/azure/foundation.py",
    "scripts/lib/azure/gate.py",
    "scripts/lib/azure/operator.py",
    "scripts/lib/azure/ownership.py",
    "scripts/lib/azure/proof.py",
)
ORDERED_PHASES = (
    "foundation-readiness",
    "create-ready",
    "worker-identity-verification",
    "worker-instance-deletion",
    "worker-recovery",
    "ordinary-tenant-deletion",
    "external-absence-proof",
    "foundation-verification",
    "recreation",
)


def _tenant_command(*arguments: str) -> str:
    return run(
        [sys.executable, str(ROOT / "scripts" / "tenant.py"), *arguments],
        timeout=2 * 60 * 60,
    ).stdout


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
    digest = hashlib.sha256()
    revision = run(
        ["git", "rev-parse", "HEAD"],
        timeout=30,
        cwd=ROOT.parent,
    ).stdout.strip()
    digest.update(revision.encode())
    for relative in SOURCE_PATHS:
        path = ROOT / relative
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    digest.update(spec_path.read_bytes())
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, object]:
    payload = json.loads(read_private_file(path).decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"invalid Azure lifecycle gate state: {path.name}")
    return payload


def _validate_record(record: object) -> dict[str, object]:
    if not isinstance(record, dict):
        raise RuntimeError("invalid Azure lifecycle gate evidence record")
    status = record.get("status")
    expected = (
        {"phase", "status", "seconds"}
        if status in {"started", "passed"}
        else {"phase", "status", "seconds", "blocker"}
    )
    seconds = record.get("seconds")
    if (
        status not in {"started", "passed", "failed"}
        or set(record) != expected
        or record.get("phase") not in ORDERED_PHASES
        or isinstance(seconds, bool)
        or not isinstance(seconds, (int, float))
        or not math.isfinite(seconds)
        or seconds < 0
        or (
            status == "failed"
            and (
                not isinstance(record.get("blocker"), str)
                or not record["blocker"]
            )
        )
    ):
        raise RuntimeError("invalid Azure lifecycle gate evidence record")
    return record


def _incomplete_gate(
    evidence_dir: Path,
    tenant: str,
    specification_sha256: str,
    source_sha256: str,
) -> dict[str, object] | None:
    candidates = []
    if evidence_dir.is_dir():
        for path in evidence_dir.glob("lifecycle-*.json"):
            payload = _read_json(path)
            if (
                payload.get("tenant") != tenant
                or payload.get("specificationSha256") != specification_sha256
                or payload.get("sourceSha256") != source_sha256
            ):
                continue
            if (
                set(payload)
                != {
                    "schema",
                    "operationId",
                    "tenant",
                    "specificationSha256",
                    "sourceSha256",
                    "records",
                }
                or payload.get("schema") != 2
                or not isinstance(payload.get("operationId"), str)
                or path.stem != f"lifecycle-{payload.get('operationId')}"
                or not isinstance(payload.get("records"), list)
            ):
                raise RuntimeError(
                    f"invalid Azure lifecycle gate evidence: {path.name}"
                )
            records = [_validate_record(record) for record in payload["records"]]
            phase_indices = [
                ORDERED_PHASES.index(str(record["phase"]))
                for record in records
                if record["status"] == "passed"
            ]
            if phase_indices != sorted(phase_indices) or len(phase_indices) != len(
                set(phase_indices)
            ):
                raise RuntimeError(
                    f"Azure lifecycle gate evidence order is invalid: {path.name}"
                )
            passed = {
                str(record["phase"])
                for record in records
                if record["status"] == "passed"
            }
            touched = any(
                record["phase"] == "worker-instance-deletion"
                and record["status"] in {"started", "passed", "failed"}
                for record in records
            )
            if touched and "recreation" not in passed:
                candidates.append(payload)
    if len(candidates) > 1:
        raise RuntimeError("multiple incomplete Azure lifecycle gate attempts exist")
    return candidates[0] if candidates else None


def _checkpoint(
    path: Path,
    *,
    operation_id: str,
    spec,
    source_sha256: str,
    tenant: Mapping[str, object],
    foundation: Mapping[str, str],
    before: WorkerSnapshot,
    owned_before: Mapping[str, object],
    deletion_proof: AzureDeletionProof | None = None,
) -> dict[str, object]:
    provider = _provider(tenant)
    binding = provider.get("binding")
    metadata = tenant.get("metadata")
    assert isinstance(binding, dict)
    assert isinstance(metadata, dict)
    payload = {
        "schema": 1,
        "operationId": operation_id,
        "tenant": spec.name,
        "specificationSha256": spec.sha256(),
        "sourceSha256": source_sha256,
        "tenantUid": metadata["uid"],
        "providerOperationId": binding["operationId"],
        "foundation": dict(foundation),
        "before": before.to_mapping(),
        "ownedBefore": dict(owned_before),
        "deletionProof": (
            deletion_proof.to_mapping() if deletion_proof is not None else None
        ),
    }
    write_private_file(
        path,
        json.dumps(redact_value(payload), sort_keys=True) + "\n",
    )
    return payload


def _load_checkpoint(
    path: Path,
    *,
    operation_id: str,
    spec,
    source_sha256: str,
    foundation: Mapping[str, str],
) -> dict[str, object]:
    if not private_file_exists(path):
        raise RuntimeError("Azure lifecycle gate checkpoint is absent")
    payload = _read_json(path)
    if (
        set(payload)
        != {
            "schema",
            "operationId",
            "tenant",
            "specificationSha256",
            "sourceSha256",
            "tenantUid",
            "providerOperationId",
            "foundation",
            "before",
            "ownedBefore",
            "deletionProof",
        }
        or payload.get("schema") != 1
        or payload.get("operationId") != operation_id
        or payload.get("tenant") != spec.name
        or payload.get("specificationSha256") != spec.sha256()
        or payload.get("sourceSha256") != source_sha256
        or payload.get("foundation") != dict(foundation)
        or not isinstance(payload.get("tenantUid"), str)
        or not isinstance(payload.get("providerOperationId"), str)
        or not isinstance(payload.get("before"), dict)
        or not isinstance(payload.get("ownedBefore"), dict)
        or (
            payload.get("deletionProof") is not None
            and not isinstance(payload.get("deletionProof"), dict)
        )
    ):
        raise RuntimeError("Azure lifecycle gate checkpoint is invalid")
    WorkerSnapshot.from_mapping(payload["before"])
    return payload


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
    evidence_dir = ROOT / ".runtime" / "azure-gate" / "evidence"
    state_path = ROOT / ".runtime" / "azure-gate" / "state" / f"{spec.name}.json"
    prior = _incomplete_gate(
        evidence_dir,
        spec.name,
        spec.sha256(),
        source_sha256,
    )
    operation_id = (
        str(prior["operationId"]) if prior is not None else uuid.uuid4().hex
    )
    evidence_path = evidence_dir / f"lifecycle-{operation_id}.json"
    records = list(prior["records"]) if prior is not None else []

    def persist_evidence() -> None:
        write_private_file(
            evidence_path,
            json.dumps(
                redact_value(
                    {
                        "schema": 2,
                        "operationId": operation_id,
                        "tenant": spec.name,
                        "specificationSha256": spec.sha256(),
                        "sourceSha256": source_sha256,
                        "records": records,
                    }
                ),
                sort_keys=True,
            )
            + "\n",
        )

    def passed(name: str) -> bool:
        return any(
            record.get("phase") == name and record.get("status") == "passed"
            for record in records
            if isinstance(record, dict)
        )

    def phase(name: str, operation):
        if passed(name):
            return None
        started = time.monotonic()
        try:
            value = operation()
        except BaseException as exc:
            records.append(
                {
                    "phase": name,
                    "status": "failed",
                    "seconds": round(time.monotonic() - started, 3),
                    "blocker": redact(str(exc)),
                }
            )
            persist_evidence()
            raise
        records.append(
            {
                "phase": name,
                "status": "passed",
                "seconds": round(time.monotonic() - started, 3),
            }
        )
        persist_evidence()
        return value

    primary = None
    try:
        foundation_before = phase(
            "foundation-readiness",
            lambda: _inspect_foundation(ROOT, config, require_healthy=True)[0],
        )
        if foundation_before is None:
            foundation_before = _inspect_foundation(
                ROOT, config, require_healthy=True
            )[0]
        existing = read_tenant(ROOT, spec.name)
        if existing is None:
            phase(
                "create-ready",
                lambda: (
                    _tenant_command("create", "azure", str(spec_path)),
                    _require_status(spec.name, "ready"),
                ),
            )
        else:
            if existing.get("spec") != tenant_document(spec)["spec"]:
                raise RuntimeError(
                    "Azure lifecycle gate found an incompatible existing Tenant"
                )
            phase(
                "create-ready",
                lambda: (
                    wait_tenant_ready(ROOT, spec.name),
                    _require_status(spec.name, "ready"),
                ),
            )

        if prior is None:
            tenant, before, owned_before = _ready_snapshot(config, spec)
            phase("worker-identity-verification", lambda: before)
            checkpoint = _checkpoint(
                state_path,
                operation_id=operation_id,
                spec=spec,
                source_sha256=source_sha256,
                tenant=tenant,
                foundation=foundation_before,
                before=before,
                owned_before=owned_before,
            )
            started = time.monotonic()
            records.append(
                {
                    "phase": "worker-instance-deletion",
                    "status": "started",
                    "seconds": 0.0,
                }
            )
            persist_evidence()
            target = before.target
            try:
                _run_profile_mutation(
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
                        timeout=parse_duration(config["AZURE_TENANT_TIMEOUT"])
                        + 60,
                    ),
                )
            except BaseException as exc:
                records[-1] = {
                    "phase": "worker-instance-deletion",
                    "status": "failed",
                    "seconds": round(time.monotonic() - started, 3),
                    "blocker": redact(str(exc)),
                }
                persist_evidence()
                raise
            records[-1] = {
                "phase": "worker-instance-deletion",
                "status": "passed",
                "seconds": round(time.monotonic() - started, 3),
            }
            persist_evidence()
        else:
            checkpoint = _load_checkpoint(
                state_path,
                operation_id=operation_id,
                spec=spec,
                source_sha256=source_sha256,
                foundation=foundation_before,
            )
            before = WorkerSnapshot.from_mapping(checkpoint["before"])
            owned_before = checkpoint["ownedBefore"]

        proof_payload = checkpoint.get("deletionProof")
        if passed("ordinary-tenant-deletion"):
            if proof_payload is None:
                raise RuntimeError(
                    "Azure deletion proof checkpoint is absent after finalization"
                )
            proof = AzureDeletionProof.from_mapping(proof_payload)
        else:
            if not passed("worker-recovery"):
                tenant_after, after, owned_after = _wait_ready_snapshot(
                    config, spec
                )
                deleted, replacement = require_replacement(before, after)
                require_owned_resource_delta(
                    owned_before,
                    owned_after,
                    deleted,
                    replacement,
                )
                phase("worker-recovery", lambda: after)
            else:
                tenant_after, _, _ = _ready_snapshot(config, spec)

            metadata = tenant_after.get("metadata")
            binding = _provider(tenant_after).get("binding")
            if (
                not isinstance(metadata, dict)
                or metadata.get("uid") != checkpoint["tenantUid"]
                or not isinstance(binding, dict)
                or binding.get("operationId")
                != checkpoint["providerOperationId"]
            ):
                raise RuntimeError(
                    "Azure Tenant identity changed during worker recovery"
                )
            if proof_payload is None:
                proof = capture_operator_deletion_proof(
                    ROOT, config, tenant_after
                )
                checkpoint = _checkpoint(
                    state_path,
                    operation_id=operation_id,
                    spec=spec,
                    source_sha256=source_sha256,
                    tenant=tenant_after,
                    foundation=foundation_before,
                    before=before,
                    owned_before=owned_before,
                    deletion_proof=proof,
                )
            else:
                proof = AzureDeletionProof.from_mapping(proof_payload)

        if not passed("ordinary-tenant-deletion"):
            phase(
                "ordinary-tenant-deletion",
                lambda: (
                    _tenant_command(
                        "delete",
                        "azure",
                        spec.name,
                        f"azure/{spec.name}",
                    ),
                    _require_status(spec.name, "absent"),
                ),
            )
        else:
            _require_status(spec.name, "absent")
        phase(
            "external-absence-proof",
            lambda: prove_operator_deletion(ROOT, config, proof),
        )
        phase(
            "foundation-verification",
            lambda: (
                None
                if _inspect_foundation(ROOT, config, require_healthy=True)[0]
                == foundation_before
                else (_ for _ in ()).throw(
                    RuntimeError(
                        "Azure shared foundation identity changed during the gate"
                    )
                )
            ),
        )
        phase(
            "recreation",
            lambda: (
                _tenant_command("create", "azure", str(spec_path)),
                _require_status(spec.name, "ready"),
                _ready_snapshot(config, spec),
            ),
        )
        state_path.unlink(missing_ok=True)
    except BaseException as exc:
        primary = exc
        raise
    finally:
        try:
            persist_evidence()
            print(f"Azure tenant lifecycle evidence: {evidence_path}")
        except BaseException as evidence_error:
            if primary is None:
                raise
            primary.add_note(
                "Azure lifecycle gate evidence failed: "
                + redact(str(evidence_error))
            )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except Exception as exc:
        print(redact(str(exc)), file=sys.stderr)
        raise SystemExit(1)
