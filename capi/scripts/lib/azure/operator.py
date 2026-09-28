from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Mapping

from scripts.lib.config import parse_duration
from scripts.lib.tenant_spec import TenantSpec
from scripts.lib.tenant_status import TenantStatus

from .common import _kubectl, load_azure_configuration
from .proof import capture_operator_deletion_proof, prove_operator_deletion


FIELD_MANAGER = "cnpg-vcluster-azure-tenant-client"


def tenant_document(spec: TenantSpec) -> dict[str, object]:
    return {
        "apiVersion": "tenancy.cnpg-vcluster.io/v1alpha2",
        "kind": "Tenant",
        "metadata": {"name": spec.name},
        "spec": {
            "kubernetesVersion": spec.kubernetes_version,
            "workers": spec.workers,
            "provider": {
                "type": "azure",
                "podCIDR": str(spec.pod_network),
                "serviceCIDR": str(spec.service_network),
            },
        },
    }


def read_tenant(root: Path, tenant: str) -> dict[str, object] | None:
    response = _kubectl(
        root,
        "get",
        f"tenant/{tenant}",
        "--ignore-not-found",
        "-o",
        "json",
    )
    if not response.stdout.strip():
        return None
    payload = json.loads(response.stdout)
    if not isinstance(payload, dict):
        raise RuntimeError("Tenant API returned an invalid object")
    return payload


def tenant_status(tenant: str, payload: Mapping[str, object] | None) -> TenantStatus:
    if payload is None:
        return TenantStatus(
            profile="azure",
            tenant=tenant,
            classification="absent",
            foundation_healthy=True,
            components={"operatorObserved": True},
        )
    metadata = payload.get("metadata")
    status = payload.get("status")
    metadata = metadata if isinstance(metadata, dict) else {}
    status = status if isinstance(status, dict) else {}
    generation = metadata.get("generation")
    observed = status.get("observedGeneration")
    fresh = (
        isinstance(generation, int)
        and not isinstance(generation, bool)
        and observed == generation
    )
    conditions = status.get("conditions")
    conditions = conditions if isinstance(conditions, list) else []
    current = [
        condition
        for condition in conditions
        if isinstance(condition, dict)
        and condition.get("observedGeneration") == generation
    ]
    ready = next(
        (
            condition
            for condition in current
            if condition.get("type") == "Ready"
        ),
        None,
    )
    phase = status.get("phase")
    deleting = isinstance(metadata.get("deletionTimestamp"), str)
    classification = "progressing"
    if deleting:
        classification = "deleting"
    elif fresh and phase == "Ready" and isinstance(ready, dict) and ready.get("status") == "True":
        classification = "ready"
    elif fresh and phase == "OwnershipInvalid":
        classification = "ownership-invalid"
    elif fresh and phase == "Failed":
        classification = "failed"
    elif fresh and phase == "Degraded":
        classification = "degraded"
    foundation_ready = next(
        (
            condition
            for condition in current
            if condition.get("type") == "FoundationReady"
        ),
        None,
    )
    blockers = tuple(
        str(condition.get("message"))
        for condition in current
        if condition.get("status") == "False"
        and isinstance(condition.get("message"), str)
    )
    provider = status.get("provider")
    if (
        classification == "ready"
        and (
            not isinstance(provider, dict)
            or provider.get("type") != "azure"
        )
    ):
        classification = "ownership-invalid"
        blockers = ("Azure provider status is absent",)
    return TenantStatus(
        profile="azure",
        tenant=tenant,
        classification=classification,
        foundation_healthy=(
            isinstance(foundation_ready, dict)
            and foundation_ready.get("status") == "True"
        ),
        components={
            "generation": generation,
            "observedGeneration": observed,
            "phase": phase,
            "provider": provider if isinstance(provider, dict) else {},
        },
        blockers=blockers,
    )


def _wait(
    root: Path,
    tenant: str,
    timeout: int,
    predicate,
) -> TenantStatus:
    deadline = time.monotonic() + timeout
    last = tenant_status(tenant, read_tenant(root, tenant))
    while True:
        if predicate(last):
            return last
        if last.classification in {"failed", "ownership-invalid"}:
            raise RuntimeError(
                "Azure Tenant operator blocked: " + "; ".join(last.blockers)
            )
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"timed out waiting for Azure Tenant operator: {last.to_json()}"
            )
        time.sleep(min(5, max(0, deadline - time.monotonic())))
        last = tenant_status(tenant, read_tenant(root, tenant))


def create_tenant(root: Path, spec: TenantSpec) -> None:
    config = load_azure_configuration(root)
    _kubectl(
        root,
        "apply",
        "--server-side",
        "--validate=strict",
        f"--field-manager={FIELD_MANAGER}",
        "-f",
        "-",
        input_text=json.dumps(tenant_document(spec), separators=(",", ":")),
    )
    _wait(
        root,
        spec.name,
        parse_duration(config["AZURE_TENANT_TIMEOUT"]),
        lambda status: status.classification == "ready",
    )


def wait_tenant_ready(root: Path, tenant: str) -> TenantStatus:
    config = load_azure_configuration(root)
    return _wait(
        root,
        tenant,
        parse_duration(config["AZURE_TENANT_TIMEOUT"]),
        lambda status: status.classification == "ready",
    )


def wait_tenant_absent(root: Path, tenant: str) -> TenantStatus:
    config = load_azure_configuration(root)
    return _wait(
        root,
        tenant,
        parse_duration(config["AZURE_TENANT_TIMEOUT"]),
        lambda status: status.classification == "absent",
    )


def status_tenant(root: Path, tenant: str) -> TenantStatus:
    return tenant_status(tenant, read_tenant(root, tenant))


def delete_tenant(root: Path, tenant: str) -> None:
    config = load_azure_configuration(root)
    payload = read_tenant(root, tenant)
    if payload is None:
        return
    proof = capture_operator_deletion_proof(root, config, payload)
    _kubectl(
        root,
        "delete",
        f"tenant/{tenant}",
        "--ignore-not-found=true",
        "--wait=false",
    )
    wait_tenant_absent(root, tenant)
    prove_operator_deletion(root, config, proof)
