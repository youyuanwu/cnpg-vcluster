#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.lib.config import load_configuration
from scripts.lib.kube import ManagementClient
from scripts.lib.redaction import redact, redact_value


FUNCTIONAL_CATEGORIES = {
    "clusterAccess",
    "workers",
    "network",
    "storage",
    "database",
}
MAX_EVIDENCE_AGE_SECONDS = 24 * 60 * 60


def evaluate_tenant(document: dict[str, object], *, now: float | None = None) -> dict[str, object]:
    current_time = time.time() if now is None else now
    metadata = _mapping(document.get("metadata"))
    status = _mapping(document.get("status"))
    evidence = _mapping(status.get("functionalEvidence"))
    blockers: list[str] = []
    generation = metadata.get("generation")
    observed_generation = status.get("observedGeneration")
    if not isinstance(generation, int) or isinstance(generation, bool):
        blockers.append("metadata.generation is missing or invalid")
    elif observed_generation != generation:
        blockers.append("status does not observe the current generation")
    for key in ("specHash", "foundationHash", "observationsHash"):
        status_value = status.get(key)
        evidence_value = evidence.get(key)
        if not isinstance(status_value, str) or not status_value:
            blockers.append(f"status.{key} is missing")
        elif evidence_value != status_value:
            blockers.append(f"functional evidence {key} does not match status")
    verified_at = evidence.get("verifiedAt")
    expires_at = evidence.get("expiresAt")
    if (
        isinstance(verified_at, bool)
        or not isinstance(verified_at, (int, float))
        or not math.isfinite(float(verified_at))
    ):
        blockers.append("functional evidence verifiedAt is invalid")
    else:
        verified = float(verified_at)
        if verified > current_time:
            blockers.append("functional evidence is future-dated")
        elif current_time - verified > MAX_EVIDENCE_AGE_SECONDS:
            blockers.append(
                f"functional evidence is stale by {current_time - verified - MAX_EVIDENCE_AGE_SECONDS:.0f}s"
            )
        expected_expiry = verified + MAX_EVIDENCE_AGE_SECONDS
        if (
            isinstance(expires_at, bool)
            or not isinstance(expires_at, (int, float))
            or not math.isfinite(float(expires_at))
            or float(expires_at) != expected_expiry
        ):
            blockers.append("functional evidence expiresAt is inconsistent")
    categories = evidence.get("categories")
    if not isinstance(categories, dict) or set(categories) != FUNCTIONAL_CATEGORIES:
        blockers.append("functional evidence categories are incomplete")
    elif any(value is not True for value in categories.values()):
        blockers.append("one or more functional evidence categories failed")
    ready = _condition(status, "Ready")
    if ready.get("status") != "True":
        blockers.append("stored Ready condition is not true")
    classification = "ready" if not blockers else _classification(status)
    return {
        "schema": 1,
        "tenant": metadata.get("name", ""),
        "classification": classification,
        "phase": status.get("phase", ""),
        "observedGeneration": observed_generation,
        "verifiedAt": verified_at,
        "expiresAt": expires_at,
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
