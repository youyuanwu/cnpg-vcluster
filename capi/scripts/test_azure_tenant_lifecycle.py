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
)
from scripts.lib.azure.foundation import (
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
    identities = [
        str(item["id"])
        for item in payload
        if isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and item.get("instanceId") is not None
    ]
    if len(identities) != len(payload):
        raise RuntimeError("Azure VMSS instance inventory is incomplete")
    return identities


def _incomplete_gate_records(
    evidence_dir: Path,
    tenant: str,
    specification_sha256: str,
    revision: str,
) -> set[str] | None:
    candidates = []
    if evidence_dir.is_dir():
        for path in evidence_dir.glob("lifecycle-*.json"):
            payload = json.loads(read_private_file(path))
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
                or payload.get("tenant") != tenant
                or payload.get("specificationSha256") != specification_sha256
                or payload.get("revision") != revision
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
            if "recreation" in passed:
                raise RuntimeError(
                    "Azure lifecycle gate already completed for this revision"
                )
            if (
                "worker-instance-deletion" in seen
            ):
                candidates.append(passed | {"worker-instance-deletion-started"})
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

    primary = None
    try:
        foundation_before = _healthy_schema_v2_foundation(ROOT, config)
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
        if runtime.operation_exists():
            pending = runtime.load_operation()
            if pending.operation == "delete":
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
        prior_gate = _incomplete_gate_records(
            evidence.parent,
            spec.name,
            spec.sha256(),
            revision,
        )

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
            before = _recorded_worker_snapshot(identity)
            deadline = time.monotonic() + parse_duration(
                config["AZURE_TENANT_TIMEOUT"]
            )
            last_blockers = ("worker recovery has not been observed",)
            while time.monotonic() < deadline:
                recovered, blockers = _collect_ready_observations(
                    ROOT,
                    config,
                    spec,
                )
                instances = _vmss_instances(config, before.vmss_id)
                if not blockers:
                    try:
                        after = build_worker_snapshot(
                            recovered,
                            before.vmss_id,
                            instances,
                        )
                        deleted, replacement = require_replacement(before, after)
                    except RuntimeError as exc:
                        last_blockers = (str(exc),)
                    else:
                        break
                else:
                    last_blockers = blockers
                time.sleep(10)
            else:
                raise RuntimeError(
                    "Azure worker recovery timed out: "
                    + "; ".join(last_blockers)
                )
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
                    "foundationIdentity": dict(identity.foundation_identity),
                    "observed": observed,
                    "verifiedAt": time.time(),
                    "ready": recovered,
                }
            )
            runtime.compare_and_replace_identity(identity, observed)

        skip_failure_injection = False
        skip_targeted_delete = False
        if prior_gate is not None:
            if "targeted-delete-absent" not in prior_gate:
                if "worker-identity-refresh" not in prior_gate:
                    try:
                        _require_status(spec.name, "ready")
                    except RuntimeError:
                        _run_profile_mutation(
                            ROOT,
                            config,
                            lambda _root, _config: resume_worker_refresh(),
                        )
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
                if "worker-identity-refresh" not in locked_prior:
                    resume_worker_refresh()
                    phase("worker-recovery", lambda: None)
                    phase("worker-identity-refresh", lambda: None)
                return
            runtime = TenantRuntime(ROOT, spec.name)
            if runtime.operation_exists():
                raise RuntimeError(
                    "Azure worker failure injection requires no pending operation"
                )
            identity = runtime.load_identity()
            vmss_id = identity.observed.get("vmssId")
            if not vmss_id:
                raise RuntimeError("Azure tenant VMSS identity is absent")
            readiness, blockers = _collect_ready_observations(ROOT, config, spec)
            if blockers:
                raise RuntimeError(
                    "Azure tenant is not Ready: " + "; ".join(blockers)
                )
            before_instances = _vmss_instances(config, vmss_id)
            before = build_worker_snapshot(readiness, vmss_id, before_instances)
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
            target = before.target
            phase("worker-identity-verification", lambda: before)
            records.append(
                {
                    "phase": "worker-instance-deletion",
                    "status": "failed",
                    "seconds": 0.0,
                    "blocker": "failure injection in progress",
                }
            )
            persist_evidence()
            phase(
                "worker-instance-deletion",
                lambda: _az(
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
                ),
            )
            deadline = (
                time.monotonic()
                + parse_duration(config["AZURE_TENANT_TIMEOUT"])
            )
            last_blockers = ("worker recovery has not been observed",)
            while time.monotonic() < deadline:
                recovered, blockers = _collect_ready_observations(
                    ROOT,
                    config,
                    spec,
                )
                instances = _vmss_instances(config, vmss_id)
                if not blockers:
                    try:
                        after = build_worker_snapshot(
                            recovered,
                            vmss_id,
                            instances,
                        )
                        deleted, replacement = require_replacement(before, after)
                    except RuntimeError as exc:
                        last_blockers = (str(exc),)
                    else:
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
                else:
                    last_blockers = blockers
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
            foundation_after, _, _ = _inspect_foundation(
                ROOT,
                config,
                require_healthy=True,
            )
            if foundation_after != foundation_before:
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
