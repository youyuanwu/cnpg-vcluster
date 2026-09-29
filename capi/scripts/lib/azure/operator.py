from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Mapping

from scripts.lib.config import parse_duration
from scripts.lib.tenant_spec import TenantSpec
from scripts.lib.tenant_status import TenantStatus

from .common import _kubectl, load_azure_configuration
from scripts.lib.files import (
    private_file_exists,
    read_private_file,
    unlink_private_file,
    write_private_file,
)

from .proof import (
    AzureDeletionProof,
    capture_operator_deletion_proof,
    prove_operator_deletion,
    validate_operator_deletion_proof,
)


FIELD_MANAGER = "cnpg-vcluster-azure-tenant-client"
DELETION_CHECKPOINT_SCHEMA = 1


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


def _deletion_checkpoint_path(root: Path, tenant: str) -> Path:
    if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", tenant):
        raise RuntimeError("Azure Tenant name is invalid")
    return root / ".runtime" / "azure" / "deletion-proofs" / f"{tenant}.json"


def _write_deletion_checkpoint(
    root: Path,
    tenant: str,
    proof: AzureDeletionProof,
) -> None:
    write_private_file(
        _deletion_checkpoint_path(root, tenant),
        json.dumps(
            {
                "schema": DELETION_CHECKPOINT_SCHEMA,
                "proof": proof.to_mapping(),
            },
            sort_keys=True,
        )
        + "\n",
    )


def _load_deletion_checkpoint(
    root: Path,
    config: Mapping[str, str],
    tenant: str,
) -> AzureDeletionProof | None:
    path = _deletion_checkpoint_path(root, tenant)
    if not private_file_exists(path):
        return None
    payload = json.loads(read_private_file(path).decode("utf-8"))
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema", "proof"}
        or payload.get("schema") != DELETION_CHECKPOINT_SCHEMA
        or not isinstance(payload.get("proof"), dict)
    ):
        raise RuntimeError("Azure deletion proof checkpoint is invalid")
    proof = AzureDeletionProof.from_mapping(payload["proof"])
    validate_operator_deletion_proof(root, config, tenant, proof)
    return proof


def _validate_live_tenant_checkpoint(
    tenant: Mapping[str, object],
    proof: AzureDeletionProof,
) -> None:
    metadata = tenant.get("metadata")
    status = tenant.get("status")
    provider = status.get("provider") if isinstance(status, dict) else None
    binding = provider.get("binding") if isinstance(provider, dict) else None
    if (
        not isinstance(metadata, dict)
        or metadata.get("name") != proof.tenant
        or metadata.get("uid") != proof.binding.get("tenantUID")
        or not isinstance(binding, dict)
        or {
            str(key): str(value)
            for key, value in binding.items()
            if isinstance(key, str) and isinstance(value, str)
        }
        != dict(proof.binding)
    ):
        raise RuntimeError(
            "Azure deletion proof checkpoint does not match the live Tenant"
        )


def delete_tenant(root: Path, tenant: str) -> None:
    config = load_azure_configuration(root)
    payload = read_tenant(root, tenant)
    if payload is None:
        proof = _load_deletion_checkpoint(root, config, tenant)
        if proof is None:
            return
        prove_operator_deletion(root, config, proof)
        unlink_private_file(_deletion_checkpoint_path(root, tenant))
        return
    proof = _load_deletion_checkpoint(root, config, tenant)
    if proof is None:
        proof = capture_operator_deletion_proof(root, config, payload)
        _write_deletion_checkpoint(root, tenant, proof)
    else:
        _validate_live_tenant_checkpoint(payload, proof)
    _kubectl(
        root,
        "delete",
        f"tenant/{tenant}",
        "--ignore-not-found=true",
        "--wait=false",
    )
    wait_tenant_absent(root, tenant)
    prove_operator_deletion(root, config, proof)
    unlink_private_file(_deletion_checkpoint_path(root, tenant))
