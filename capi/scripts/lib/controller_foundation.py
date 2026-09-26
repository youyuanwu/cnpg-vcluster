from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

from scripts.cache import canonical_tagged
from scripts.lib.images import WORKER_IMAGE_KEYS

if TYPE_CHECKING:
    from scripts.cache import VerifiedCache
    from scripts.lib.ownership import ManagementIdentity


def canonical_hash(payload: str | dict[str, object]) -> str:
    raw = json.loads(payload) if isinstance(payload, str) else payload
    if not isinstance(raw, dict):
        raise ValueError("foundation must be a JSON object")
    immutable = {key: value for key, value in raw.items()
                 if key not in {"mutationEnabled", "controllerImage"}}
    encoded = json.dumps(immutable, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _ipv4_network(value: object) -> ipaddress.IPv4Network:
    if not isinstance(value, str):
        raise ValueError(f"not a canonical IPv4 CIDR: {value}")
    try:
        network = ipaddress.ip_network(value, strict=True)
    except ValueError as exc:
        raise ValueError(f"not a canonical IPv4 CIDR: {value}") from exc
    if not isinstance(network, ipaddress.IPv4Network) or str(network) != value:
        raise ValueError(f"not a canonical IPv4 CIDR: {value}")
    return network


def _ipv4_address(value: object) -> ipaddress.IPv4Address:
    if not isinstance(value, str):
        raise ValueError(f"not an IPv4 address: {value}")
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise ValueError(f"not an IPv4 address: {value}") from exc
    if not isinstance(address, ipaddress.IPv4Address) or str(address) != value:
        raise ValueError(f"not an IPv4 address: {value}")
    return address


def _ordinal(value: object, description: str) -> int:
    if isinstance(value, bool) or not re.fullmatch(r"(0|[1-9][0-9]*)", str(value)):
        raise ValueError(f"{description} must be a nonnegative integer")
    return int(value)


def _reserved(config: dict[str, str]) -> list[str]:
    values = [value for key, value in config.items()
              if key.endswith("_CIDR")]
    networks = [_ipv4_network(value) for value in values]
    for index, network in enumerate(networks):
        if any(network.overlaps(other) for other in networks[:index]):
            raise ValueError("overlapping or duplicate reserved CIDR")
    return sorted(values)


def resolve_slots(
    root: Path, config: dict[str, str], network: dict[str, object],
) -> list[dict[str, object]]:
    catalog = json.loads((root / "config" / "tenant-allocation-slots.json").read_text(
        encoding="utf-8"))
    if not isinstance(catalog, list) or not catalog:
        raise ValueError("allocation slot catalog is empty or malformed")
    subnet = _ipv4_network(network["subnet"])
    start = _ipv4_address(network["pool_start"])
    end = _ipv4_address(network["pool_end"])
    if start not in subnet or end not in subnet or start > end:
        raise ValueError("VIP pool is outside the management subnet or reversed")
    capacity = int(end) - int(start) + 1
    spike = _ordinal(config["SPIKE_API_VIP_SLOT"], "spike VIP ordinal")
    if spike >= capacity or network["slots"] != {"spike": str(start + spike)}:
        raise ValueError("spike VIP reservation does not match the network")
    reserved = [_ipv4_network(value) for value in _reserved(config)]
    if any(cidr.overlaps(subnet) for cidr in reserved):
        raise ValueError("reserved CIDR overlaps the management subnet")
    if any(int(cidr.network_address) <= int(end)
           and int(cidr.broadcast_address) >= int(start) for cidr in reserved):
        raise ValueError("VIP pool overlaps a reserved CIDR")
    ids: set[str] = set()
    ordinals: set[int] = set()
    cidrs: list[ipaddress.IPv4Network] = [subnet, *reserved]
    resolved: list[dict[str, object]] = []
    for entry in catalog:
        if not isinstance(entry, dict) or set(entry) != {
            "slotId", "vipOrdinal", "podCIDR", "serviceCIDR"
        }:
            raise ValueError("malformed allocation slot entry")
        slot_id = entry["slotId"]
        if isinstance(entry["vipOrdinal"], bool) or not isinstance(entry["vipOrdinal"], int):
            raise ValueError("catalog VIP ordinal must be an integer")
        ordinal = _ordinal(entry["vipOrdinal"], "VIP ordinal")
        if (not isinstance(slot_id, str)
                or not re.fullmatch(r"[a-z]([a-z0-9-]{0,61}[a-z0-9])?", slot_id)
                or slot_id in ids or ordinal in ordinals or ordinal >= capacity
                or ordinal == spike):
            raise ValueError("duplicate, reserved, out-of-range, or malformed allocation slot")
        ids.add(slot_id)
        ordinals.add(ordinal)
        address = start + ordinal
        if address not in subnet or any(address in cidr for cidr in reserved):
            raise ValueError("allocation endpoint conflicts with a reserved network")
        for key in ("podCIDR", "serviceCIDR"):
            cidr = _ipv4_network(entry[key])
            if any(cidr.overlaps(previous) for previous in cidrs):
                raise ValueError("allocation CIDR overlaps another network")
            cidrs.append(cidr)
        resolved.append({**entry, "endpoint": str(address)})
    if ordinals != set(range(capacity)) - {spike}:
        raise ValueError("slot catalog does not cover every available VIP ordinal")
    return resolved


def foundation_payload(
    root: Path, config: dict[str, str], network: dict[str, object],
    image: str, verified_cache: VerifiedCache, registry: dict[str, object] | None,
    *, mutation_enabled: bool = True,
) -> dict[str, object]:
    from scripts.lib.management import require_management_ownership

    require_management_ownership(root, config)
    slots = resolve_slots(root, config, network)
    reserved = _reserved(config)
    archives = [
        {
            "key": entry["key"],
            "path": entry["path"],
            "sha256": entry["sha256"],
            "reference": config[entry["key"]],
            "tagged": canonical_tagged(config[f"{entry['key']}_TAGGED"]),
            "worker": entry["key"] in WORKER_IMAGE_KEYS,
        }
        for entry in verified_cache.inventory["imageArchives"]
    ]
    if (not verified_cache.generation.name
            or verified_cache.generation.name in {".", ".."}
            or not re.fullmatch(r"[a-zA-Z0-9._-]+", verified_cache.generation.name)):
        raise ValueError("cache generation is invalid")
    archive_keys: set[str] = set()
    for archive in archives:
        path = archive["path"]
        if (not isinstance(path, str) or "\\" in path
                or any(part in {"", ".", ".."} for part in path.split("/"))
                or not isinstance(archive["sha256"], str)
                or not re.fullmatch(r"[0-9a-fA-F]{64}", archive["sha256"])
                or not archive["key"] or archive["key"] in archive_keys
                or not archive["reference"] or not archive["tagged"]):
            raise ValueError("cache archive is invalid or duplicated")
        archive_keys.add(archive["key"])
    if not set(WORKER_IMAGE_KEYS) <= archive_keys:
        raise ValueError("required worker archive is missing")
    offline = os.environ.get("CAPI_OFFLINE_ENFORCED") == "1"
    if offline:
        if registry is None or not registry.get("address"):
            raise ValueError("offline registry is incomplete")
        if _ipv4_address(registry["address"]) not in _ipv4_network(network["subnet"]):
            raise ValueError("offline registry is outside the management network")
    data = {
        "schema": 3,
        "networkId": network["network_id"],
        "subnet": network["subnet"],
        "reservedCIDRs": reserved,
        "allowedSubnets": sorted({"127.0.0.0/8", str(network["subnet"]), *reserved}),
        "kubernetesVersion": config["KUBERNETES_VERSION"],
        "controllerImage": image,
        "mutationEnabled": mutation_enabled,
        "offlineEnforced": offline,
        "slots": slots,
        "cache": {
            "generation": verified_cache.generation.name,
            "imageArchives": archives,
        },
        "registry": (
            {"address": registry["address"], "port": 5000}
            if registry is not None else None
        ),
        "inputs": {
            "ownershipLabel": config["OWNERSHIP_LABEL"],
            "labPrefix": config["LAB_PREFIX"],
            "apiPort": int(config["SPIKE_API_PORT"]),
            "clusterDomain": config["SPIKE_CLUSTER_DOMAIN"],
            "nodeImage": config["KIND_NODE_IMAGE"],
            "cacheHostPath": str(root / ".tools" / "cache"),
            "cacheContainerPath": "/var/lib/capi-image-cache",
            "storageContainerPath": config["SPIKE_STORAGE_CONTAINER_PATH"],
            "konnectivityServerImage": config["KONNECTIVITY_SERVER_IMAGE"],
            "konnectivityAgentImage": config["KONNECTIVITY_AGENT_IMAGE"],
        },
    }
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "tenant-foundation", "namespace": "tenant-system"},
        "data": {"foundation.json": encoded, "foundation.sha256": canonical_hash(encoded)},
    }
