#!/usr/bin/env python3
from __future__ import annotations

import json
import hashlib
import ipaddress
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


def evaluate_tenant(
    document: dict[str, object],
    *,
    foundation_hash: str | None,
    now: float | None = None,
) -> dict[str, object]:
    current_time = time.time() if now is None else now
    metadata = _mapping(document.get("metadata"))
    spec = _mapping(document.get("spec"))
    status = _mapping(document.get("status"))
    evidence = _mapping(status.get("functionalEvidence"))
    blockers: list[str] = []
    if metadata.get("deletionTimestamp"):
        blockers.append("Tenant is deleting")
    generation = metadata.get("generation")
    observed_generation = status.get("observedGeneration")
    if not isinstance(generation, int) or isinstance(generation, bool):
        blockers.append("metadata.generation is missing or invalid")
    elif observed_generation != generation:
        blockers.append("status does not observe the current generation")
    try:
        expected_spec_hash = canonical_spec_hash(spec)
    except (KeyError, TypeError, ValueError) as exc:
        blockers.append(f"Tenant spec is invalid: {exc}")
        expected_spec_hash = None
    status_spec_hash = status.get("specHash")
    if expected_spec_hash is None or status_spec_hash != expected_spec_hash:
        blockers.append("status.specHash does not match the current Tenant spec")
    if evidence.get("specHash") != expected_spec_hash:
        blockers.append("functional evidence specHash does not match the current Tenant spec")
    if not isinstance(foundation_hash, str) or not foundation_hash:
        blockers.append("authoritative foundation hash is unavailable")
    elif status.get("foundationHash") != foundation_hash:
        blockers.append("status.foundationHash does not match the live foundation")
    if evidence.get("foundationHash") != foundation_hash:
        blockers.append("functional evidence foundationHash does not match the live foundation")
    try:
        expected_observations_hash = observations_hash(
            status.get("observedResources"),
            status.get("tenantResources"),
            status.get("workerSnapshotHash"),
        )
    except (TypeError, ValueError) as exc:
        blockers.append(f"observed resource identities are invalid: {exc}")
        expected_observations_hash = None
    if status.get("observationsHash") != expected_observations_hash:
        blockers.append("status.observationsHash does not match observed resource identities")
    if evidence.get("observationsHash") != expected_observations_hash:
        blockers.append("functional evidence observationsHash does not match observed resource identities")
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
        elif current_time - verified >= MAX_EVIDENCE_AGE_SECONDS:
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
    if ready.get("observedGeneration") != generation:
        blockers.append("stored Ready condition does not observe the current generation")
    if status.get("phase") != "Ready":
        blockers.append("Tenant phase is not Ready")
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


def canonical_spec_hash(spec: dict[str, object]) -> str:
    required = {
        "kubernetesVersion",
        "workers",
        "databaseCount",
        "podCIDR",
        "serviceCIDR",
    }
    if set(spec) != required:
        raise ValueError("unexpected or missing fields")
    workers = spec["workers"]
    databases = spec["databaseCount"]
    if (
        isinstance(workers, bool)
        or not isinstance(workers, int)
        or not 1 <= workers <= 3
        or isinstance(databases, bool)
        or not isinstance(databases, int)
        or not 1 <= databases <= 3
    ):
        raise ValueError("worker and database counts must be integers from 1 through 3")
    version = spec["kubernetesVersion"]
    if not isinstance(version, str):
        raise ValueError("kubernetesVersion must be a string")
    pod = ipaddress.ip_network(spec["podCIDR"], strict=True)
    service = ipaddress.ip_network(spec["serviceCIDR"], strict=True)
    if pod.version != 4 or service.version != 4 or pod.overlaps(service):
        raise ValueError("Tenant networks must be non-overlapping IPv4 CIDRs")
    canonical = {
        "kubernetesVersion": version.removeprefix("v"),
        "workers": workers,
        "databaseCount": databases,
        "podCIDR": str(pod),
        "serviceCIDR": str(service),
    }
    encoded = json.dumps(canonical, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def observations_hash(
    value: object,
    tenant_value: object = None,
    worker_snapshot: object = None,
) -> str:
    if not isinstance(value, list) or not value:
        raise ValueError("observedResources must be a non-empty list")
    if tenant_value is None:
        raise ValueError("tenantResources must be a non-empty list")
    if not isinstance(tenant_value, list) or not tenant_value:
        raise ValueError("tenantResources must be a non-empty list")
    if not isinstance(worker_snapshot, str) or not worker_snapshot:
        raise ValueError("workerSnapshotHash must be a non-empty string")
    normalized = []
    for item in [*value, *tenant_value]:
        if not isinstance(item, dict):
            raise ValueError("observed resource entry must be an object")
        required = ("apiVersion", "kind", "name", "uid")
        if any(not isinstance(item.get(key), str) or not item[key] for key in required):
            raise ValueError("observed resource identity is incomplete")
        previous_uids = item.get("previousUIDs")
        if previous_uids is not None and (
            not isinstance(previous_uids, list)
            or any(not isinstance(uid, str) or not uid for uid in previous_uids)
        ):
            raise ValueError("observed resource previousUIDs are invalid")
        normalized.append(
            {
                "apiVersion": item["apiVersion"],
                "kind": item["kind"],
                "namespace": item.get("namespace", ""),
                "name": item["name"],
                "uid": item["uid"],
                "contentSHA256": item.get("contentSHA256", ""),
                "previousUIDs": (
                    sorted(previous_uids) if previous_uids is not None else None
                ),
            }
        )
    normalized.append(
        {
            "apiVersion": "tenancy.cnpg-vcluster.io/v1alpha1",
            "kind": "WorkerSnapshot",
            "namespace": "",
            "name": "workers",
            "uid": worker_snapshot,
            "contentSHA256": "",
            "previousUIDs": [],
        }
    )
    normalized.sort(
        key=lambda item: (
            item["apiVersion"],
            item["kind"],
            item["namespace"],
            item["name"],
            item["uid"],
        )
    )
    encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


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
    foundation = client.json(
        "get",
        "configmap",
        "tenant-foundation",
        "-n",
        "tenant-system",
    )
    foundation_data = _mapping(_mapping(foundation).get("data"))
    envelope = redact_value(
        evaluate_tenant(
            result,
            foundation_hash=foundation_data.get("foundation.sha256"),
        )
    )
    print(json.dumps(envelope, sort_keys=True))
    return 0 if envelope["classification"] == "ready" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except RuntimeError as exc:
        print(redact(str(exc)), file=sys.stderr)
        raise SystemExit(1)
