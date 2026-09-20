#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.azure import (
    _active_subscription,
    _az,
    _inspect_foundation,
    _remove_private_tree,
    _run_profile_mutation,
    create_foundation,
    create_management,
    load_azure_configuration,
    names,
)
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
    operation_id = uuid.uuid4().hex
    records = []
    evidence = (
        ROOT
        / ".runtime"
        / "azure-gate"
        / "evidence"
        / f"lifecycle-{operation_id}.json"
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
            raise
        records.append(
            {
                "phase": name,
                "status": "passed",
                "seconds": round(time.monotonic() - started, 3),
            }
        )
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
        runtime = TenantRuntime(ROOT, "azure", spec.name)
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
        phase(
            "create-ready",
            lambda: (
                _tenant_command("create", "azure", str(spec_path)),
                _require_status(spec.name, "ready"),
            ),
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
        payload = {
            "schema": 1,
            "operationId": operation_id,
            "tenant": spec.name,
            "specificationSha256": spec.sha256(),
            "revision": revision,
            "records": records,
        }
        try:
            write_private_file(
                evidence,
                json.dumps(redact_value(payload), sort_keys=True) + "\n",
            )
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
