#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.lib.config import load_configuration
from scripts.lib.kube import ManagementClient
from scripts.lib.redaction import redact, redact_value


def evaluate_tenant(
    document: dict[str, object],
    *,
    foundation_hash: str | None = None,
    now: float | None = None,
) -> dict[str, object]:
    del foundation_hash, now
    metadata = _mapping(document.get("metadata"))
    status = _mapping(document.get("status"))
    blockers: list[str] = []
    if metadata.get("deletionTimestamp"):
        blockers.append("Tenant is deleting")
    generation = metadata.get("generation")
    observed_generation = status.get("observedGeneration")
    if not isinstance(generation, int) or isinstance(generation, bool):
        blockers.append("metadata.generation is missing or invalid")
    elif observed_generation != generation:
        blockers.append("status does not observe the current generation")
    ready = _condition(status, "Ready")
    if ready.get("status") != "True":
        blockers.append("Ready condition is not true")
    if ready.get("observedGeneration") != generation:
        blockers.append("Ready condition does not observe the current generation")
    if status.get("phase") != "Ready":
        blockers.append("Tenant phase is not Ready")
    classification = (
        "deleting"
        if metadata.get("deletionTimestamp")
        else "ready"
        if not blockers
        else _classification(status)
    )
    return {
        "schema": 1,
        "tenant": metadata.get("name", ""),
        "classification": classification,
        "phase": status.get("phase", ""),
        "observedGeneration": observed_generation,
        "conditions": status.get("conditions", []),
        "blockers": blockers,
    }


def _mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _condition(status: dict[str, object], condition_type: str) -> dict[str, object]:
    conditions = status.get("conditions")
    if not isinstance(conditions, list):
        return {}
    for condition in conditions:
        if isinstance(condition, dict) and condition.get("type") == condition_type:
            return condition
    return {}


def _classification(status: dict[str, object]) -> str:
    phase = status.get("phase")
    return {
        "Pending": "progressing",
        "Progressing": "progressing",
        "Ready": "degraded",
        "Deleting": "deleting",
        "Degraded": "degraded",
        "Failed": "failed",
        "OwnershipInvalid": "ownership-invalid",
    }.get(phase, "degraded")


def main(arguments: list[str]) -> int:
    if len(arguments) != 1:
        print("usage: controller_tenant_status.py <tenant>", file=sys.stderr)
        return 1
    config = load_configuration(ROOT)
    client = ManagementClient(ROOT, config)
    result = client.json("get", "tenant", arguments[0])
    if not isinstance(result, dict):
        raise RuntimeError("Tenant API returned an invalid document")
    envelope = redact_value(evaluate_tenant(result))
    print(json.dumps(envelope, sort_keys=True))
    return 0 if envelope["classification"] == "ready" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except RuntimeError as exc:
        print(redact(str(exc)), file=sys.stderr)
        raise SystemExit(1)
