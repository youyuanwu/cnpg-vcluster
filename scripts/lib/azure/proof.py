from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .common import _json, names
from .foundation import (
    _azure_provider_configuration,
    _inspect_foundation,
    load_inventory,
)
from .ownership import normalize_resource_id, tenant_tagged_azure_resources


def _required(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"Azure operator proof identity is absent: {key}")
    return value


def _specification_sha256(specification: Mapping[str, object]) -> str:
    provider = specification.get("provider")
    if (
        not isinstance(provider, dict)
        or provider.get("type") != "azure"
        or not isinstance(specification.get("kubernetesVersion"), str)
        or type(specification.get("workers")) is not int
        or set(provider) != {"type"}
    ):
        raise RuntimeError("Azure operator Tenant specification is invalid")
    canonical = {
        "kubernetesVersion": specification["kubernetesVersion"].removeprefix("v"),
        "workers": specification["workers"],
        "provider": {"type": "azure"},
    }
    return hashlib.sha256(
        json.dumps(canonical, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True)
class AzureDeletionProof:
    tenant: str
    foundation: Mapping[str, str]
    resource_ids: tuple[str, ...]


def capture_operator_deletion_proof(
    root: Path,
    config: Mapping[str, str],
    tenant: Mapping[str, object],
) -> AzureDeletionProof:
    metadata = tenant.get("metadata")
    status = tenant.get("status")
    provider = status.get("provider") if isinstance(status, dict) else None
    binding = provider.get("binding") if isinstance(provider, dict) else None
    if not isinstance(metadata, dict) or not isinstance(binding, dict):
        raise RuntimeError("Azure operator status binding is absent")
    name = _required(metadata, "name")
    tenant_uid = _required(metadata, "uid")
    if tenant_uid != _required(binding, "tenantUID"):
        raise RuntimeError("Azure operator Tenant UID binding changed")
    specification = tenant.get("spec")
    if not isinstance(specification, dict):
        raise RuntimeError("Azure operator Tenant specification is absent")
    specification_sha256 = _specification_sha256(specification)
    if binding.get("specificationSha256") != specification_sha256:
        raise RuntimeError("Azure operator specification binding changed")
    operation_id = "tenant-" + hashlib.sha256(
        b"azure-tenant-operation-v1\0"
        + tenant_uid.encode()
        + b"\0"
        + specification_sha256.encode()
    ).hexdigest()
    if binding.get("operationId") != operation_id:
        raise RuntimeError("Azure operator operation binding changed")
    foundation, _, _ = _inspect_foundation(root, config, require_healthy=True)
    inventory = load_inventory(root, config)
    provider_config = _azure_provider_configuration(config, inventory)
    provider_config_sha256 = hashlib.sha256(
        json.dumps(
            provider_config,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    expected = {
        "foundationSha256": provider_config["foundationSha256"],
        "foundationDefaultsSha256": foundation["foundationDefaultsSha256"],
        "controllerImage": foundation["controllerImage"],
        "providerConfigUID": foundation["azureProviderConfigUid"],
        "providerConfigSha256": provider_config_sha256,
        "resourceGroupId": foundation["resourceGroupId"],
        "virtualNetworkId": foundation["vnetId"],
        "tenantSubnetId": foundation["tenantSubnetId"],
        "identityId": foundation["identityId"],
    }
    for key, value in expected.items():
        if binding.get(key) != value:
            raise RuntimeError(f"Azure operator foundation binding changed: {key}")
    resource_ids = set()
    vmss = provider.get("vmss") if isinstance(provider, dict) else None
    vmss_id = vmss.get("id") if isinstance(vmss, dict) else None
    if isinstance(vmss_id, str):
        resource_ids.add(vmss_id)
    instance_ids = vmss.get("instanceIds") if isinstance(vmss, dict) else None
    if isinstance(instance_ids, list):
        resource_ids.update(
            value for value in instance_ids if isinstance(value, str) and value
        )
    resources = provider.get("providerResources") if isinstance(provider, dict) else None
    if isinstance(resources, list):
        for resource in resources:
            if isinstance(resource, dict) and isinstance(resource.get("resourceId"), str):
                resource_ids.add(resource["resourceId"])
    deletion = provider.get("deletion") if isinstance(provider, dict) else None
    verified = (
        deletion.get("verifiedAzureResourceIds")
        if isinstance(deletion, dict)
        else None
    )
    if isinstance(verified, list):
        resource_ids.update(value for value in verified if isinstance(value, str))
    foundation_ids = {
        str(value).lower()
        for value in (
            foundation["resourceGroupId"],
            foundation["vnetId"],
            foundation["tenantSubnetId"],
            foundation["identityId"],
        )
    }
    return AzureDeletionProof(
        tenant=name,
        foundation=foundation,
        resource_ids=tuple(
            sorted(
                {
                    normalize_resource_id(value)
                    for value in resource_ids
                    if value
                    and normalize_resource_id(value) not in foundation_ids
                },
                key=str.lower,
            )
        ),
    )


def prove_operator_deletion(
    root: Path,
    config: Mapping[str, str],
    proof: AzureDeletionProof,
) -> None:
    foundation, _, _ = _inspect_foundation(root, config, require_healthy=True)
    if foundation != dict(proof.foundation):
        raise RuntimeError("Azure shared foundation identity changed during tenant deletion")
    residues = tenant_tagged_azure_resources(config, proof.tenant)
    if residues:
        raise RuntimeError("Azure tenant-tagged resources remain after Tenant finalization")
    resources = _json(
        [
            "az",
            "resource",
            "list",
            "--resource-group",
            names(config)["resourceGroup"],
            "--output",
            "json",
        ]
    )
    if not isinstance(resources, list):
        raise RuntimeError("Azure deletion proof returned invalid resources")
    observed = {
        str(item["id"]).lower()
        for item in resources
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    remaining = [value for value in proof.resource_ids if value.lower() in observed]
    if remaining:
        raise RuntimeError("recorded Azure tenant resources remain after finalization")
