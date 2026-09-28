#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import os
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.azure import (
    _run_profile_mutation,
)
from scripts.lib.azure.common import (
    _active_subscription,
    _az,
    _json,
    load_azure_configuration,
    names,
    tenant_names,
)
from scripts.lib.azure.foundation import (
    _get_management_resource,
    _inspect_foundation,
    create_foundation,
    create_management,
)
from scripts.lib.azure.deletion import _remove_private_tree
from scripts.lib.azure.gate import (
    build_worker_snapshot,
    refreshed_observed,
    require_owned_resource_delta,
    require_replacement,
)
from scripts.lib.azure.ownership import discover_azure_owned_resources
from scripts.lib.azure.readiness import _collect_ready_observations
from scripts.lib.config import parse_duration
from scripts.lib.files import private_file_exists, read_private_file, write_private_file
from scripts.lib.process import run
from scripts.lib.redaction import redact, redact_value
from scripts.lib.tenant_spec import load_tenant_spec
from scripts.lib.tenant_runtime import TenantRuntime
from scripts.tenant import supported_versions


def _group_exists(group: str) -> bool:
    result = _az(
        "group",
        "exists",
        "--name",
        group,
        "--output",
        "tsv",
    )
    value = result.stdout.strip().lower()
    if value not in {"true", "false"}:
        raise RuntimeError("Azure resource group existence check was invalid")
    return value == "true"


def _recorded_foundation_groups(
    root: Path,
    config: dict[str, str],
) -> dict[str, str] | None:
    path = root / ".runtime" / "azure" / "resources.json"
    if not private_file_exists(path):
        return None
    payload = json.loads(read_private_file(path).decode("utf-8"))
    if (
        not isinstance(payload, dict)
        or payload.get("schema") not in {1, 2}
        or payload.get("subscriptionId") != config["AZURE_SUBSCRIPTION_ID"]
        or payload.get("location") != config["AZURE_LOCATION"]
        or payload.get("prefix") != config["AZURE_PREFIX"]
    ):
        raise RuntimeError(
            "Azure lifecycle gate refuses an unbound legacy foundation inventory"
        )
    outputs = payload.get("outputs")
    if (
        not isinstance(outputs, dict)
        or outputs.get("resourceGroupName") != names(config)["resourceGroup"]
        or not isinstance(outputs.get("resourceGroupId"), str)
    ):
        raise RuntimeError(
            "Azure lifecycle gate legacy foundation identity is incomplete"
        )
    node_group = outputs.get("aksNodeResourceGroup")
    if not isinstance(node_group, str) or not node_group:
        raise RuntimeError(
            "Azure lifecycle gate managed node resource group is unrecorded"
        )
    return {
        "schema": str(payload["schema"]),
        "resourceGroupId": outputs["resourceGroupId"],
        "resourceGroupName": outputs["resourceGroupName"],
        "nodeResourceGroupName": node_group,
    }


def _healthy_schema_v2_foundation(
    root: Path,
    config: dict[str, str],
) -> dict[str, str] | None:
    recorded = _recorded_foundation_groups(root, config)
    if recorded is None or recorded["schema"] != "2":
        return None
    foundation, _, _ = _inspect_foundation(
        root,
        config,
        require_healthy=True,
    )
    return foundation


def _destroy_recorded_foundation(root: Path, config: dict[str, str]) -> None:
    _active_subscription(config)
    recorded = _recorded_foundation_groups(root, config)
    resource_group = names(config)["resourceGroup"]
    exists = _group_exists(resource_group)
    if exists and recorded is None:
        raise RuntimeError(
            "Azure lifecycle gate refuses to delete an unrecorded resource group"
        )
    if exists:
        observed = _az(
            "group",
            "show",
            "--name",
            names(config)["resourceGroup"],
            "--query",
            "id",
            "--output",
            "tsv",
        ).stdout.strip()
        if observed.lower() != recorded["resourceGroupId"].lower():
            raise RuntimeError(
                "Azure lifecycle gate resource group identity changed"
            )
        _az(
            "group",
            "delete",
            "--name",
            names(config)["resourceGroup"],
            "--yes",
            "--no-wait",
            timeout=120,
        )
    deadline = time.monotonic() + parse_duration(config["AZURE_DEPLOY_TIMEOUT"])
    while _group_exists(resource_group) or (
        recorded is not None
        and _group_exists(recorded["nodeResourceGroupName"])
    ):
        if time.monotonic() >= deadline:
            raise RuntimeError("Azure legacy foundation deletion timed out")
        time.sleep(15)
    _remove_private_tree(root / ".runtime" / "azure")
    _remove_private_tree(root / ".runtime" / "lifecycle" / "azure")


def _tenant_command(*arguments: str) -> str:
    result = run(
        [sys.executable, str(ROOT / "scripts" / "tenant.py"), *arguments],
        timeout=2 * 60 * 60,
    )
    return result.stdout


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


def _vmss_instances(config: dict[str, str], vmss_id: str) -> list[str]:
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


def _require_three_worker_pool(spec, *, require_ready: bool) -> None:
    pool = _get_management_resource(
        ROOT,
        spec.namespace,
        f"machinepool/{tenant_names(spec)['pool']}",
    )
    if (
        pool is None
        or pool.get("spec", {}).get("replicas") != 3
        or (
            require_ready
            and pool.get("status", {}).get("readyReplicas") != 3
        )
    ):
        raise RuntimeError(
            "Azure lifecycle gate requires a three-replica Ready MachinePool"
        )


def _incomplete_gate_records(
    evidence_dir: Path,
    tenant: str,
    specification_sha256: str,
    revision: str,
) -> set[str] | None:
    candidates = []
    if evidence_dir.is_dir():
        for path in evidence_dir.glob("lifecycle-*.json"):
            try:
                payload = json.loads(read_private_file(path))
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"invalid Azure lifecycle gate evidence: {path.name}"
                ) from exc
            if not isinstance(payload, dict):
                continue
            if (
                payload.get("tenant") != tenant
                or payload.get("specificationSha256") != specification_sha256
                or payload.get("revision") != revision
            ):
                continue
            if (
                set(payload)
                != {
                    "schema",
                    "operationId",
                    "tenant",
                    "specificationSha256",
                    "revision",
                    "records",
                }
                or payload.get("schema") != 1
                or not isinstance(payload.get("operationId"), str)
                or not payload["operationId"]
                or not isinstance(payload.get("records"), list)
            ):
                raise RuntimeError(
                    f"invalid Azure lifecycle gate evidence: {path.name}"
                )
            valid_records = True
            for record in payload["records"]:
                if not isinstance(record, dict):
                    valid_records = False
                    break
                status = record.get("status")
                expected_keys = (
                    {"phase", "status", "seconds"}
                    if status == "passed"
                    else {"phase", "status", "seconds", "blocker"}
                )
                seconds = record.get("seconds")
                if (
                    status not in {"passed", "failed"}
                    or set(record) != expected_keys
                    or not isinstance(record.get("phase"), str)
                    or not record["phase"]
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
                    valid_records = False
                    break
            if not valid_records:
                raise RuntimeError(
                    f"invalid Azure lifecycle gate evidence records: {path.name}"
                )
            if path.stem != f"lifecycle-{payload['operationId']}":
                raise RuntimeError(
                    f"Azure lifecycle gate evidence identity changed: {path.name}"
                )
            passed = {
                record.get("phase")
                for record in payload["records"]
                if isinstance(record, dict) and record.get("status") == "passed"
            }
            seen = {
                record.get("phase")
                for record in payload["records"]
                if isinstance(record, dict)
            }
            if "targeted-delete-absent" in passed and not {
                "worker-instance-deletion",
                "worker-recovery",
                "worker-identity-refresh",
            }.issubset(passed):
                raise RuntimeError(
                    f"Azure lifecycle gate evidence phase order is invalid: {path.name}"
                )
            if "recreation" in passed and not {
                "targeted-delete-absent",
                "foundation-verification",
            }.issubset(passed):
                raise RuntimeError(
                    f"Azure lifecycle gate evidence completion is invalid: {path.name}"
                )
            if "recreation" in passed:
                continue
            if (
                "worker-instance-deletion" in seen
            ):
                candidates.append(
                    passed
                    | {
                        "worker-instance-deletion-started",
                        f"gate-operation:{payload['operationId']}",
                    }
                )
    if len(candidates) > 1:
        raise RuntimeError("multiple incomplete Azure lifecycle gate attempts exist")
    return candidates[0] if candidates else None


def _recorded_worker_snapshot(identity) -> object:
    try:
        nodes = json.loads(identity.observed["nodeIdentities"])
        instances = json.loads(identity.observed["vmssInstanceIds"])
        vmss_id = identity.observed["vmssId"]
    except (KeyError, json.JSONDecodeError) as exc:
        raise RuntimeError("recorded Azure worker identities are invalid") from exc
    readiness = {
        "requestedWorkers": 3,
        "readyReplicas": 3,
        "nodeRefs": [item.get("name") for item in nodes if isinstance(item, dict)],
        "nodes": nodes,
    }
    return build_worker_snapshot(readiness, vmss_id, instances)


def _require_authenticated_worker_deletion(phases: set[str]) -> None:
    if not {
        "worker-instance-deletion",
        "worker-instance-deletion-started",
    } & phases:
        raise RuntimeError(
            "Azure worker deletion outcome is ambiguous; refusing to continue"
        )


def _gate_attempt_id(phases: set[str]) -> str:
    values = {
        value.removeprefix("gate-operation:")
        for value in phases
        if value.startswith("gate-operation:")
    }
    if len(values) != 1 or not next(iter(values)):
        raise RuntimeError("Azure lifecycle gate attempt identity is ambiguous")
    return next(iter(values))


def _complete_prior_gate_attempt(
    evidence_dir: Path,
    phases: set[str],
    continuation_records: list[dict[str, object]],
) -> None:
    attempt_id = _gate_attempt_id(phases)
    path = evidence_dir / f"lifecycle-{attempt_id}.json"
    payload = json.loads(read_private_file(path))
    records = payload["records"]
    passed = {
        record.get("phase")
        for record in records
        if isinstance(record, dict) and record.get("status") == "passed"
    }
    for record in continuation_records:
        if (
            isinstance(record, dict)
            and record.get("status") == "passed"
            and record.get("phase") not in passed
        ):
            records.append(dict(record))
            passed.add(record.get("phase"))
    write_private_file(
        path,
        json.dumps(redact_value(payload), sort_keys=True) + "\n",
    )


def _incompatible_existing_identity(runtime: TenantRuntime, spec):
    if not runtime.identity_exists():
        return None
    identity = runtime.load_identity()
    return identity if identity.specification_sha256 != spec.sha256() else None


def main(arguments: list[str]) -> int:
    os.umask(0o077)
    revision = run(
        ["git", "rev-parse", "HEAD"],
        timeout=30,
        cwd=ROOT.parent,
    ).stdout.strip()
    tracked_changes = run(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        timeout=30,
        cwd=ROOT.parent,
    ).stdout.strip()
    if tracked_changes:
        raise RuntimeError(
            "Azure lifecycle gate requires a clean committed worktree"
        )
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
        raise RuntimeError(
            "Azure lifecycle gate requires exactly three workers"
        )
    operation_id = uuid.uuid4().hex
    records = []
    evidence = (
        ROOT
        / ".runtime"
        / "azure-gate"
        / "evidence"
        / f"lifecycle-{operation_id}.json"
    )
    foundation_checkpoint = (
        ROOT / ".runtime" / "azure-gate" / "state" / f"{spec.name}-foundation.json"
    )
    worker_checkpoint = (
        ROOT / ".runtime" / "azure-gate" / "state" / f"{spec.name}-worker.json"
    )

    def persist_evidence():
        write_private_file(
            evidence,
            json.dumps(
                redact_value(
                    {
                        "schema": 1,
                        "operationId": operation_id,
                        "tenant": spec.name,
                        "specificationSha256": spec.sha256(),
                        "revision": revision,
                        "records": records,
                    }
                ),
                sort_keys=True,
            )
            + "\n",
        )

    def phase(name, operation):
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

    initial_prior_gate = _run_profile_mutation(
        ROOT,
        config,
        lambda _root, _config: _incomplete_gate_records(
            evidence.parent,
            spec.name,
            spec.sha256(),
            revision,
        ),
    )
    active_gate_operation_id = (
        _gate_attempt_id(initial_prior_gate)
        if initial_prior_gate is not None
        else operation_id
    )
    primary = None
    try:
        foundation_before = _healthy_schema_v2_foundation(ROOT, config)
        if initial_prior_gate is not None and foundation_before is None:
            raise RuntimeError(
                "Azure lifecycle gate cannot resume without its foundation"
            )
        if foundation_before is None:
            phase(
                "destroy-legacy-foundation",
                lambda: _run_profile_mutation(
                    ROOT,
                    config,
                    _destroy_recorded_foundation,
                ),
            )
            phase(
                "create-schema-v2-foundation",
                lambda: _run_profile_mutation(ROOT, config, create_foundation),
            )
            phase(
                "create-management",
                lambda: _run_profile_mutation(ROOT, config, create_management),
            )
        else:
            phase("reuse-schema-v2-foundation", lambda: foundation_before)
        foundation_before = phase(
            "foundation-snapshot",
            lambda: _inspect_foundation(
                ROOT,
                config,
                require_healthy=True,
            )[0],
        )
        runtime = TenantRuntime(ROOT, spec.name)
        prior_gate = initial_prior_gate
        if runtime.operation_exists():
            pending = runtime.load_operation()
            if pending.operation == "delete":
                if prior_gate is not None:
                    _require_authenticated_worker_deletion(prior_gate)
                    if not private_file_exists(foundation_checkpoint):
                        raise RuntimeError(
                            "Azure foundation checkpoint is absent during delete resume"
                        )
                    checkpoint = json.loads(
                        read_private_file(foundation_checkpoint)
                    )
                    if (
                        checkpoint.get("gateOperationId")
                        != active_gate_operation_id
                        or checkpoint.get("foundation") != foundation_before
                    ):
                        raise RuntimeError(
                            "Azure foundation changed before delete resume"
                        )
                phase(
                    "resume-pending-delete",
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
                if prior_gate is not None:
                    prior_gate = prior_gate | {"targeted-delete-absent"}
            elif prior_gate is not None:
                raise RuntimeError(
                    "Azure lifecycle gate conflicts with a pending create"
                )
        if _incompatible_existing_identity(runtime, spec) is not None:
            if prior_gate is not None:
                raise RuntimeError(
                    "Azure lifecycle gate evidence conflicts with tenant identity"
                )
            phase(
                "delete-incompatible-existing-tenant",
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
        if (
            prior_gate is not None
            and not runtime.operation_exists()
            and not runtime.identity_exists()
            and "targeted-delete-absent" not in prior_gate
        ):
            _require_status(spec.name, "absent")
            prior_gate = prior_gate | {"targeted-delete-absent"}
        def resume_worker_refresh():
            if _incomplete_gate_records(
                evidence.parent,
                spec.name,
                spec.sha256(),
                revision,
            ) is None:
                raise RuntimeError(
                    "Azure worker recovery evidence changed before resume"
                )
            runtime = TenantRuntime(ROOT, spec.name)
            if runtime.operation_exists():
                raise RuntimeError(
                    "Azure worker recovery cannot resume with a pending operation"
                )
            identity = runtime.load_identity()
            if not private_file_exists(worker_checkpoint):
                raise RuntimeError(
                    "Azure worker recovery checkpoint is absent"
                )
            checkpoint = json.loads(read_private_file(worker_checkpoint))
            if (
                set(checkpoint)
                != {
                    "schema",
                    "tenant",
                    "specificationSha256",
                    "revision",
                    "gateOperationId",
                    "markerOperationId",
                    "observed",
                }
                or checkpoint.get("schema") != 1
                or checkpoint.get("tenant") != spec.name
                or checkpoint.get("specificationSha256") != spec.sha256()
                or checkpoint.get("revision") != revision
                or checkpoint.get("gateOperationId")
                != _gate_attempt_id(
                    _incomplete_gate_records(
                        evidence.parent,
                        spec.name,
                        spec.sha256(),
                        revision,
                    )
                    or set()
                )
                or checkpoint.get("markerOperationId")
                != identity.observed.get("markerOperationId")
                or not isinstance(checkpoint.get("observed"), dict)
            ):
                raise RuntimeError("Azure worker recovery checkpoint is invalid")
            baseline_observed = checkpoint["observed"]
            baseline_identity = type(identity)(
                profile=identity.profile,
                tenant=identity.tenant,
                specification=identity.specification,
                specification_sha256=identity.specification_sha256,
                foundation_identity=identity.foundation_identity,
                observed=baseline_observed,
            )
            before = _recorded_worker_snapshot(baseline_identity)
            deadline = time.monotonic() + parse_duration(
                config["AZURE_TENANT_TIMEOUT"]
            )
            last_blockers = ("worker recovery has not been observed",)
            while time.monotonic() < deadline:
                try:
                    _require_three_worker_pool(spec, require_ready=False)
                    recovered, blockers = _collect_ready_observations(
                        ROOT,
                        config,
                        spec,
                    )
                    instances = _vmss_instances(config, before.vmss_id)
                    if blockers:
                        raise RuntimeError("; ".join(blockers))
                    else:
                        after = build_worker_snapshot(
                            recovered,
                            before.vmss_id,
                            instances,
                        )
                        deleted, replacement = require_replacement(before, after)
                        discovery = discover_azure_owned_resources(
                            ROOT,
                            config,
                            spec,
                            identity,
                        )
                        require_owned_resource_delta(
                            baseline_observed["azureResources"],
                            discovery,
                            deleted,
                            replacement,
                        )
                        break
                except RuntimeError as exc:
                    last_blockers = (str(exc),)
                time.sleep(10)
            else:
                raise RuntimeError(
                    "Azure worker recovery timed out: "
                    + "; ".join(last_blockers)
                )
            observed = refreshed_observed(
                baseline_observed,
                recovered,
                instances,
                discovery,
            )
            ready_payload = (
                {
                    "schema": 1,
                    "profile": "azure",
                    "tenant": spec.name,
                    "specificationSha256": spec.sha256(),
                    "foundationIdentity": dict(identity.foundation_identity),
                    "observed": observed,
                    "verifiedAt": time.time(),
                    "ready": recovered,
                }
            )
            runtime.write_ready_evidence(ready_payload)
            if dict(identity.observed) != observed:
                runtime.compare_and_replace_identity(identity, observed)
            elif runtime.load_ready_evidence() != ready_payload:
                raise RuntimeError(
                    "Azure worker Ready evidence changed during recovery"
                )

        skip_failure_injection = False
        skip_targeted_delete = False
        if prior_gate is not None:
            _require_authenticated_worker_deletion(prior_gate)
            if "targeted-delete-absent" not in prior_gate:
                if "worker-identity-refresh" not in prior_gate:
                    _run_profile_mutation(
                        ROOT,
                        config,
                        lambda _root, _config: resume_worker_refresh(),
                    )
                    phase("worker-recovery", lambda: None)
                    phase("worker-identity-refresh", lambda: None)
                _require_status(spec.name, "ready")
            skip_failure_injection = True
            skip_targeted_delete = "targeted-delete-absent" in prior_gate
        else:
            phase(
                "create-ready",
                lambda: (
                    _tenant_command("create", "azure", str(spec_path)),
                    _require_status(spec.name, "ready"),
                ),
            )

        def verify_and_replace_worker():
            locked_prior = _incomplete_gate_records(
                evidence.parent,
                spec.name,
                spec.sha256(),
                revision,
            )
            if locked_prior is not None:
                _require_authenticated_worker_deletion(locked_prior)
                raise RuntimeError(
                    "another Azure lifecycle gate attempt is already active"
                )
            runtime = TenantRuntime(ROOT, spec.name)
            if runtime.operation_exists():
                raise RuntimeError(
                    "Azure worker failure injection requires no pending operation"
                )
            identity = runtime.load_identity()
            recorded_before = _recorded_worker_snapshot(identity)
            vmss_id = identity.observed.get("vmssId")
            if not vmss_id:
                raise RuntimeError("Azure tenant VMSS identity is absent")
            _require_three_worker_pool(spec, require_ready=True)
            readiness, blockers = _collect_ready_observations(ROOT, config, spec)
            if blockers:
                raise RuntimeError(
                    "Azure tenant is not Ready: " + "; ".join(blockers)
                )
            before_instances = _vmss_instances(config, vmss_id)
            before = build_worker_snapshot(readiness, vmss_id, before_instances)
            if before != recorded_before:
                raise RuntimeError(
                    "Azure worker identities changed before failure injection"
                )
            recorded_resources = json.loads(identity.observed["azureResources"])
            live_resources = discover_azure_owned_resources(
                ROOT,
                config,
                spec,
                identity,
            )
            if live_resources != recorded_resources:
                raise RuntimeError(
                    "Azure owned resources changed before failure injection"
                )
            _require_three_worker_pool(spec, require_ready=True)
            reread, blockers = _collect_ready_observations(ROOT, config, spec)
            reread_instances = _vmss_instances(config, vmss_id)
            if blockers or build_worker_snapshot(
                reread,
                vmss_id,
                reread_instances,
            ) != before:
                raise RuntimeError(
                    "Azure worker identities changed before failure injection"
                )
            if (
                discover_azure_owned_resources(
                    ROOT,
                    config,
                    spec,
                    identity,
                )
                != live_resources
            ):
                raise RuntimeError(
                    "Azure owned resources changed before failure injection"
                )
            target = before.target
            phase("worker-identity-verification", lambda: before)
            write_private_file(
                worker_checkpoint,
                json.dumps(
                    {
                        "schema": 1,
                        "tenant": spec.name,
                        "specificationSha256": spec.sha256(),
                        "revision": revision,
                        "gateOperationId": active_gate_operation_id,
                        "markerOperationId": identity.observed[
                            "markerOperationId"
                        ],
                        "observed": dict(identity.observed),
                    },
                    sort_keys=True,
                )
                + "\n",
            )
            deletion_started = time.monotonic()
            deletion_record = len(records)
            records.append(
                {
                    "phase": "worker-instance-deletion",
                    "status": "failed",
                    "seconds": 0.0,
                    "blocker": "failure injection in progress",
                }
            )
            persist_evidence()
            try:
                _az(
                    "vmss",
                    "delete-instances",
                    "--resource-group",
                    names(config)["resourceGroup"],
                    "--name",
                    vmss_id.rstrip("/").split("/")[-1],
                    "--instance-ids",
                    target.instance_id,
                    "--output",
                    "none",
                    timeout=parse_duration(config["AZURE_TENANT_TIMEOUT"]) + 60,
                )
            except BaseException as exc:
                records[deletion_record] = {
                    "phase": "worker-instance-deletion",
                    "status": "failed",
                    "seconds": round(time.monotonic() - deletion_started, 3),
                    "blocker": redact(str(exc)),
                }
                persist_evidence()
                raise
            records[deletion_record] = {
                "phase": "worker-instance-deletion",
                "status": "passed",
                "seconds": round(time.monotonic() - deletion_started, 3),
            }
            persist_evidence()
            deadline = (
                time.monotonic()
                + parse_duration(config["AZURE_TENANT_TIMEOUT"])
            )
            last_blockers = ("worker recovery has not been observed",)
            while time.monotonic() < deadline:
                try:
                    _require_three_worker_pool(spec, require_ready=False)
                    recovered, blockers = _collect_ready_observations(
                        ROOT,
                        config,
                        spec,
                    )
                    instances = _vmss_instances(config, vmss_id)
                    if blockers:
                        raise RuntimeError("; ".join(blockers))
                    else:
                        after = build_worker_snapshot(
                            recovered,
                            vmss_id,
                            instances,
                        )
                        deleted, replacement = require_replacement(before, after)
                        discovery = discover_azure_owned_resources(
                            ROOT,
                            config,
                            spec,
                            identity,
                        )
                        require_owned_resource_delta(
                            identity.observed["azureResources"],
                            discovery,
                            deleted,
                            replacement,
                        )
                        observed = refreshed_observed(
                            identity.observed,
                            recovered,
                            instances,
                            discovery,
                        )
                        runtime.write_ready_evidence(
                            {
                                "schema": 1,
                                "profile": "azure",
                                "tenant": spec.name,
                                "specificationSha256": spec.sha256(),
                                "foundationIdentity": dict(
                                    identity.foundation_identity
                                ),
                                "observed": observed,
                                "verifiedAt": time.time(),
                                "ready": recovered,
                            }
                        )
                        runtime.compare_and_replace_identity(
                            identity,
                            observed,
                        )
                        phase("worker-recovery", lambda: after)
                        phase("worker-identity-refresh", lambda: None)
                        return
                except RuntimeError as exc:
                    last_blockers = (str(exc),)
                time.sleep(10)
            raise RuntimeError(
                "Azure worker recovery timed out: "
                + "; ".join(last_blockers)
            )

        if not skip_failure_injection:
            _run_profile_mutation(
                ROOT,
                config,
                lambda _root, _config: verify_and_replace_worker(),
            )
            _require_status(spec.name, "ready")
        if not skip_targeted_delete:
            if private_file_exists(foundation_checkpoint):
                checkpoint = json.loads(read_private_file(foundation_checkpoint))
                if (
                    checkpoint.get("gateOperationId")
                    != active_gate_operation_id
                    or checkpoint.get("foundation") != foundation_before
                ):
                    raise RuntimeError(
                        "Azure foundation checkpoint conflicts with gate attempt"
                    )
            else:
                write_private_file(
                    foundation_checkpoint,
                    json.dumps(
                        {
                            "schema": 1,
                            "gateOperationId": active_gate_operation_id,
                            "foundation": foundation_before,
                        },
                        sort_keys=True,
                    )
                    + "\n",
                )
            phase(
                "targeted-delete-absent",
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

        def verify_foundation():
            if not private_file_exists(foundation_checkpoint):
                raise RuntimeError(
                    "Azure foundation checkpoint is absent during gate resume"
                )
            checkpoint = json.loads(
                read_private_file(foundation_checkpoint)
            )
            if (
                set(checkpoint)
                != {"schema", "gateOperationId", "foundation"}
                or checkpoint.get("schema") != 1
                or checkpoint.get("gateOperationId")
                != active_gate_operation_id
                or not isinstance(checkpoint.get("foundation"), dict)
            ):
                raise RuntimeError("Azure foundation checkpoint is invalid")
            expected_foundation = checkpoint["foundation"]
            foundation_after, _, _ = _inspect_foundation(
                ROOT,
                config,
                require_healthy=True,
            )
            if foundation_after != expected_foundation:
                raise RuntimeError(
                    "Azure shared foundation identity changed during targeted deletion"
                )

        phase("foundation-verification", verify_foundation)
        phase(
            "recreation",
            lambda: (
                _tenant_command("create", "azure", str(spec_path)),
                _require_status(spec.name, "ready"),
            ),
        )
        if initial_prior_gate is not None:
            _complete_prior_gate_attempt(
                evidence.parent,
                initial_prior_gate,
                records,
            )
        foundation_checkpoint.unlink(missing_ok=True)
        worker_checkpoint.unlink(missing_ok=True)
    except BaseException as exc:
        primary = exc
        raise
    finally:
        try:
            persist_evidence()
            print(f"Azure tenant lifecycle evidence: {evidence}")
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
