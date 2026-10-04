from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping, Sequence

from .common import _az, _json, names


AZURE_TAG_KEYS = {
    "tenant": "cnpg-vcluster-tenant",
    "profile": "cnpg-vcluster-profile",
    "specificationSha256": "cnpg-vcluster-spec-sha256",
    "foundationSha256": "cnpg-vcluster-foundation-sha256",
    "operationId": "cnpg-vcluster-operation-id",
}
KNOWN_AZURE_TENANT_TYPES = frozenset(
    {
        "microsoft.compute/virtualmachinescalesets",
        "microsoft.compute/virtualmachinescalesets/virtualmachines",
        "microsoft.network/networkinterfaces",
        "microsoft.network/natgateways",
        "microsoft.network/publicipaddresses",
    }
)


def _required(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"Azure ownership identity is absent: {key}")
    return value


def normalize_resource_id(value: str) -> str:
    normalized = value.strip().rstrip("/")
    if normalized.lower().startswith("azure://"):
        normalized = normalized[len("azure://") :]
    if not normalized.startswith("/"):
        raise RuntimeError("Azure ownership resource ID is invalid")
    return normalized.lower()


def expected_azure_tags(tenant: Mapping[str, object]) -> dict[str, str]:
    metadata = tenant.get("metadata")
    status = tenant.get("status")
    provider = status.get("provider") if isinstance(status, dict) else None
    binding = provider.get("binding") if isinstance(provider, dict) else None
    if not isinstance(metadata, dict) or not isinstance(binding, dict):
        raise RuntimeError("Azure operator ownership binding is absent")
    return {
        AZURE_TAG_KEYS["tenant"]: _required(metadata, "name"),
        AZURE_TAG_KEYS["profile"]: "azure",
        AZURE_TAG_KEYS["specificationSha256"]: _required(
            binding, "specificationSha256"
        ),
        AZURE_TAG_KEYS["foundationSha256"]: _required(
            binding, "foundationSha256"
        ),
        AZURE_TAG_KEYS["operationId"]: _required(binding, "operationId"),
    }


def classify_azure_owned_resources(
    resources: Sequence[Mapping[str, object]],
    expected_tags: Mapping[str, str],
    *,
    verified_ids: Sequence[str] = (),
) -> list[dict[str, object]]:
    verified = {normalize_resource_id(value) for value in verified_ids}
    selected: dict[str, dict[str, object]] = {}
    unknown = []
    for resource in resources:
        identifier = resource.get("id")
        if not isinstance(identifier, str) or not identifier:
            continue
        normalized = normalize_resource_id(identifier)
        tags = resource.get("tags")
        tags = tags if isinstance(tags, dict) else {}
        marked = any(key in tags for key in expected_tags)
        exact = all(tags.get(key) == value for key, value in expected_tags.items())
        if marked and not exact:
            unknown.append({"id": identifier, "reason": "foreign Azure tags"})
            continue
        if normalized not in verified and not exact:
            continue
        resource_type = str(resource.get("type", "")).lower()
        if resource_type not in KNOWN_AZURE_TENANT_TYPES:
            unknown.append(
                {
                    "id": identifier,
                    "type": resource_type,
                    "reason": "unknown Azure tenant resource type",
                }
            )
            continue
        summary: dict[str, object] = {
            "id": identifier,
            "type": resource_type,
        }
        if resource_type == "microsoft.network/networkinterfaces":
            virtual_machine_id = resource.get("virtualMachineId")
            if not isinstance(virtual_machine_id, str) or not virtual_machine_id:
                unknown.append(
                    {
                        "id": identifier,
                        "reason": "missing VMSS instance association",
                    }
                )
                continue
            summary["virtualMachineId"] = virtual_machine_id
        selected[normalized] = summary
    if unknown:
        raise RuntimeError(
            "Azure tenant resource ownership is unknown: "
            + json.dumps(unknown, sort_keys=True)
        )
    missing = sorted(verified - selected.keys())
    if missing:
        raise RuntimeError(
            "recorded Azure tenant resources are absent: "
            + json.dumps(missing)
        )
    return sorted(selected.values(), key=lambda item: str(item["id"]).lower())


def observe_azure_owned_resources(
    root: Path,
    config: Mapping[str, str],
    tenant: Mapping[str, object],
) -> dict[str, object]:
    del root
    status = tenant.get("status")
    provider = status.get("provider") if isinstance(status, dict) else None
    if not isinstance(provider, dict) or provider.get("type") != "azure":
        raise RuntimeError("Azure operator provider status is absent")
    expected_tags = expected_azure_tags(tenant)
    vmss = provider.get("vmss")
    vmss = vmss if isinstance(vmss, dict) else {}
    vmss_id = vmss.get("id")
    instance_ids = vmss.get("instanceIds")
    if not isinstance(vmss_id, str) or not vmss_id:
        raise RuntimeError("Azure operator VMSS identity is absent")
    if not isinstance(instance_ids, list) or not all(
        isinstance(value, str) and value for value in instance_ids
    ):
        raise RuntimeError("Azure operator VMSS instance identity is invalid")
    provider_resources = provider.get("providerResources")
    if not isinstance(provider_resources, list):
        raise RuntimeError("Azure operator provider resource inventory is absent")
    recorded_provider = []
    verified_ids = [vmss_id, *instance_ids]
    binding = provider.get("binding")
    if not isinstance(binding, dict):
        raise RuntimeError("Azure operator ownership binding is absent")
    foundation_ids = {
        str(binding.get(key, "")).rstrip("/").lower()
        for key in (
            "resourceGroupId",
            "virtualNetworkId",
            "tenantSubnetId",
            "identityId",
        )
        if isinstance(binding.get(key), str)
    }
    for resource in provider_resources:
        if (
            not isinstance(resource, dict)
            or not isinstance(resource.get("apiVersion"), str)
            or not isinstance(resource.get("kind"), str)
            or not isinstance(resource.get("name"), str)
            or not isinstance(resource.get("uid"), str)
        ):
            raise RuntimeError("Azure operator provider resource identity is invalid")
        summary = {
            key: resource[key]
            for key in (
                "apiVersion",
                "kind",
                "name",
                "uid",
            )
        }
        if isinstance(resource.get("namespace"), str):
            summary["namespace"] = resource["namespace"]
        if isinstance(resource.get("resourceId"), str):
            summary["resourceId"] = resource["resourceId"]
            if resource["resourceId"].rstrip("/").lower() not in foundation_ids:
                verified_ids.append(resource["resourceId"])
        recorded_provider.append(summary)
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
        raise RuntimeError("Azure tenant resource discovery returned invalid resources")
    pool = vmss_id.rstrip("/").split("/")[-1]
    instances = _json(
        [
            "az",
            "vmss",
            "list-instances",
            "--resource-group",
            names(config)["resourceGroup"],
            "--name",
            pool,
            "--query",
            "[].{id:id,type:type,tags:tags}",
            "--output",
            "json",
        ]
    )
    if not isinstance(instances, list):
        raise RuntimeError("Azure VMSS instance discovery returned invalid resources")
    for instance in instances:
        if not isinstance(instance, dict):
            raise RuntimeError("Azure VMSS instance discovery returned invalid resources")
        if not isinstance(instance.get("type"), str):
            instance["type"] = (
                "Microsoft.Compute/virtualMachineScaleSets/virtualMachines"
            )
        resources.append(instance)
    nic_response = _az(
        "vmss",
        "nic",
        "list",
        "--resource-group",
        names(config)["resourceGroup"],
        "--vmss-name",
        pool,
        "--query",
        "[].{id:id,type:type,tags:tags,virtualMachineId:virtualMachine.id}",
        "--output",
        "json",
    )
    nics = json.loads(nic_response.stdout)
    if not isinstance(nics, list):
        raise RuntimeError("Azure VMSS NIC discovery returned invalid resources")
    for nic in nics:
        if not isinstance(nic, dict):
            raise RuntimeError("Azure VMSS NIC discovery returned invalid resources")
        if not isinstance(nic.get("type"), str):
            nic["type"] = "Microsoft.Network/networkInterfaces"
        resources.append(nic)
        identifier = nic.get("id")
        if isinstance(identifier, str):
            verified_ids.append(identifier)
    return {
        "azure": classify_azure_owned_resources(
            resources,
            expected_tags,
            verified_ids=verified_ids,
        ),
        "provider": sorted(
            recorded_provider,
            key=lambda item: (
                str(item["apiVersion"]),
                str(item["kind"]),
                str(item.get("namespace", "")),
                str(item["name"]),
                str(item["uid"]),
            ),
        ),
    }


def tenant_tagged_azure_resources(
    config: Mapping[str, str],
    tenant: str,
) -> list[dict[str, object]]:
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
        raise RuntimeError("Azure tenant absence discovery returned invalid resources")
    residues = []
    for item in resources:
        if not isinstance(item, dict):
            raise RuntimeError("Azure tenant absence discovery returned invalid resources")
        tags = item.get("tags")
        tags = tags if isinstance(tags, dict) else {}
        if tags.get(AZURE_TAG_KEYS["tenant"]) != tenant:
            continue
        identifier = item.get("id")
        if not isinstance(identifier, str) or not identifier:
            raise RuntimeError("Azure tenant residue has no resource ID")
        residues.append(
            {
                "id": identifier,
                "type": str(item.get("type", "")).lower(),
            }
        )
    return sorted(residues, key=lambda item: str(item["id"]).lower())
