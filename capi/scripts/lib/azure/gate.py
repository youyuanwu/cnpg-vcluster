from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Mapping, Sequence


def _normalize_resource_id(value: str) -> str:
    normalized = value.strip()
    if normalized.lower().startswith("azure://"):
        normalized = normalized[len("azure://") :]
    if not normalized.startswith("/"):
        raise RuntimeError("Azure worker provider ID is not an absolute resource ID")
    return normalized.rstrip("/").lower()


@dataclass(frozen=True)
class WorkerMapping:
    node_name: str
    node_uid: str
    instance_id: str
    instance_resource_id: str


@dataclass(frozen=True)
class WorkerSnapshot:
    vmss_id: str
    mappings: tuple[WorkerMapping, ...]

    @property
    def primary(self) -> WorkerMapping:
        return min(self.mappings, key=lambda item: int(item.instance_id))

    @property
    def target(self) -> WorkerMapping:
        return max(self.mappings, key=lambda item: int(item.instance_id))


def build_worker_snapshot(
    readiness: Mapping[str, object],
    vmss_id: str,
    instance_resource_ids: Sequence[str],
) -> WorkerSnapshot:
    canonical_vmss = _normalize_resource_id(vmss_id)
    nodes = readiness.get("nodes")
    node_refs = readiness.get("nodeRefs")
    if (
        readiness.get("requestedWorkers") != 3
        or readiness.get("readyReplicas") != 3
        or not isinstance(nodes, list)
        or len(nodes) != 3
        or not isinstance(node_refs, list)
        or len(node_refs) != 3
    ):
        raise RuntimeError("Azure lifecycle gate requires exactly three Ready workers")
    expected_names = {str(value) for value in node_refs}
    canonical_instances = {
        _normalize_resource_id(str(value)): str(value)
        for value in instance_resource_ids
    }
    if len(canonical_instances) != 3:
        raise RuntimeError("Azure lifecycle gate requires three distinct VMSS instances")
    mappings = []
    for node in nodes:
        if not isinstance(node, dict):
            raise RuntimeError("Azure worker identity is malformed")
        name = node.get("name")
        uid = node.get("uid")
        provider_id = node.get("providerID")
        if not all(isinstance(value, str) and value for value in (name, uid, provider_id)):
            raise RuntimeError("Azure worker identity is incomplete")
        canonical_provider = _normalize_resource_id(provider_id)
        prefix = canonical_vmss + "/virtualmachines/"
        if not canonical_provider.startswith(prefix):
            raise RuntimeError("Azure worker is not backed by the expected VMSS")
        instance_id = canonical_provider[len(prefix) :]
        if not instance_id.isdigit() or "/" in instance_id:
            raise RuntimeError("Azure worker VMSS instance ID is invalid")
        if canonical_provider not in canonical_instances:
            raise RuntimeError("Azure worker provider ID is absent from VMSS instances")
        mappings.append(
            WorkerMapping(
                node_name=name,
                node_uid=uid,
                instance_id=instance_id,
                instance_resource_id=canonical_instances[canonical_provider],
            )
        )
    if {item.node_name for item in mappings} != expected_names:
        raise RuntimeError("Azure tenant Nodes do not match MachinePool nodeRefs")
    if (
        len({item.node_name for item in mappings}) != 3
        or len({item.node_uid for item in mappings}) != 3
        or len({item.instance_id for item in mappings}) != 3
    ):
        raise RuntimeError("Azure worker or VMSS instance identities are duplicated")
    return WorkerSnapshot(
        vmss_id=vmss_id,
        mappings=tuple(sorted(mappings, key=lambda item: int(item.instance_id))),
    )


def require_replacement(
    before: WorkerSnapshot,
    after: WorkerSnapshot,
) -> tuple[WorkerMapping, WorkerMapping]:
    if _normalize_resource_id(before.vmss_id) != _normalize_resource_id(after.vmss_id):
        raise RuntimeError("Azure worker VMSS identity changed during recovery")
    deleted = before.target
    if deleted == before.primary:
        raise RuntimeError("Azure lifecycle gate did not select a non-primary instance")
    before_pairs = {
        (item.node_name, item.node_uid, _normalize_resource_id(item.instance_resource_id))
        for item in before.mappings
    }
    after_pairs = {
        (item.node_name, item.node_uid, _normalize_resource_id(item.instance_resource_id))
        for item in after.mappings
    }
    deleted_pair = (
        deleted.node_name,
        deleted.node_uid,
        _normalize_resource_id(deleted.instance_resource_id),
    )
    survivors = before_pairs - {deleted_pair}
    if deleted_pair in after_pairs or not survivors.issubset(after_pairs):
        raise RuntimeError("Azure worker survivor or deletion identity changed")
    replacements = after_pairs - survivors
    if len(replacements) != 1:
        raise RuntimeError("Azure worker recovery did not produce one replacement")
    replacement_pair = next(iter(replacements))
    if (
        replacement_pair[0] == deleted.node_name
        or replacement_pair[1] == deleted.node_uid
        or replacement_pair[2] == deleted_pair[2]
    ):
        raise RuntimeError("Azure worker replacement reused a deleted identity")
    replacement = next(
        item
        for item in after.mappings
        if (
            item.node_name,
            item.node_uid,
            _normalize_resource_id(item.instance_resource_id),
        )
        == replacement_pair
    )
    return deleted, replacement


def refreshed_observed(
    observed: Mapping[str, str],
    readiness: Mapping[str, object],
    instance_resource_ids: Sequence[str],
    discovery: Mapping[str, object],
) -> dict[str, str]:
    refreshed = dict(observed)
    refreshed["vmssInstanceIds"] = json.dumps(
        sorted(str(value) for value in instance_resource_ids),
        separators=(",", ":"),
    )
    refreshed["nodeIdentities"] = json.dumps(
        readiness["nodes"],
        sort_keys=True,
        separators=(",", ":"),
    )
    refreshed["azureResources"] = json.dumps(
        dict(discovery),
        sort_keys=True,
        separators=(",", ":"),
    )
    return refreshed


def require_owned_resource_delta(
    recorded_json: str,
    discovered: Mapping[str, object],
    deleted: WorkerMapping,
    replacement: WorkerMapping,
) -> None:
    recorded = json.loads(recorded_json)
    if not isinstance(recorded, dict):
        raise RuntimeError("recorded Azure resource inventory is invalid")
    if recorded.get("aso") != discovered.get("aso"):
        raise RuntimeError("Azure ASO ownership changed during worker recovery")
    if recorded.get("unknown") != discovered.get("unknown"):
        raise RuntimeError("unknown Azure ownership changed during worker recovery")

    def resources(payload):
        return {
            _normalize_resource_id(str(item["id"])): (
                str(item.get("type", "")).lower(),
                json.dumps(item, sort_keys=True, separators=(",", ":")),
            )
            for item in payload.get("azure", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }

    before = resources(recorded)
    after = resources(discovered)
    removed = set(before) - set(after)
    added = set(after) - set(before)
    changed = {
        item
        for item in set(before) & set(after)
        if before[item][1] != after[item][1]
    }
    if changed:
        raise RuntimeError("retained Azure ownership changed during worker recovery")
    deleted_id = _normalize_resource_id(deleted.instance_resource_id)
    replacement_id = _normalize_resource_id(replacement.instance_resource_id)
    if deleted_id not in removed or replacement_id not in added:
        raise RuntimeError("Azure VMSS replacement resource delta is incomplete")
    allowed_types = {
        "microsoft.compute/virtualmachinescalesets/virtualmachines",
        "microsoft.network/networkinterfaces",
    }
    if any(before[item][0] not in allowed_types for item in removed) or any(
        after[item][0] not in allowed_types for item in added
    ):
        raise RuntimeError("unrelated Azure ownership changed during worker recovery")
    removed_vms = {
        item
        for item in removed
        if before[item][0]
        == "microsoft.compute/virtualmachinescalesets/virtualmachines"
    }
    added_vms = {
        item
        for item in added
        if after[item][0]
        == "microsoft.compute/virtualmachinescalesets/virtualmachines"
    }
    if removed_vms != {deleted_id} or added_vms != {replacement_id}:
        raise RuntimeError("Azure VMSS replacement ownership delta is ambiguous")
    removed_nics = {
        item
        for item in removed
        if before[item][0] == "microsoft.network/networkinterfaces"
    }
    added_nics = {
        item
        for item in added
        if after[item][0] == "microsoft.network/networkinterfaces"
    }
    if len(removed_nics) != len(added_nics) or len(removed_nics) > 1:
        raise RuntimeError("Azure VMSS replacement NIC delta is ambiguous")
