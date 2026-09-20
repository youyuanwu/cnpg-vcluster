from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


TENANT_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,28}[a-z0-9])?$")
KUBERNETES_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
PROFILES = frozenset({"local", "azure"})
COMMON_FIELDS = frozenset(
    {
        "schema",
        "profile",
        "name",
        "kubernetesVersion",
        "workers",
        "podCIDR",
        "serviceCIDR",
    }
)


class TenantSpecError(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise TenantSpecError(f"duplicate tenant specification field: {key}")
        result[key] = value
    return result


def validate_tenant_name(name: str) -> str:
    if not TENANT_NAME_RE.fullmatch(name):
        raise TenantSpecError(
            "tenant name must be a 1-30 character lowercase DNS label"
        )
    return name


def _required_string(payload: Mapping[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise TenantSpecError(f"tenant specification field must be a string: {field}")
    return value


def _required_count(payload: Mapping[str, object], field: str) -> int:
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 3:
        raise TenantSpecError(
            f"tenant specification field must be an integer from 1 through 3: {field}"
        )
    return value


def _network(payload: Mapping[str, object], field: str) -> ipaddress.IPv4Network:
    value = _required_string(payload, field)
    try:
        network = ipaddress.ip_network(value, strict=True)
    except ValueError as exc:
        raise TenantSpecError(f"invalid tenant IPv4 network for {field}: {value}") from exc
    if not isinstance(network, ipaddress.IPv4Network):
        raise TenantSpecError(f"tenant network must be IPv4: {field}")
    return network


@dataclass(frozen=True)
class TenantSpec:
    profile: str
    name: str
    kubernetes_version: str
    workers: int
    pod_network: ipaddress.IPv4Network
    service_network: ipaddress.IPv4Network
    database_count: int | None

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, object],
        *,
        expected_profile: str | None = None,
        supported_versions: Mapping[str, str] | None = None,
    ) -> "TenantSpec":
        profile = _required_string(payload, "profile")
        if profile not in PROFILES:
            raise TenantSpecError(f"unsupported tenant profile: {profile}")
        if expected_profile is not None and profile != expected_profile:
            raise TenantSpecError(
                f"tenant specification profile {profile!r} does not match "
                f"selected profile {expected_profile!r}"
            )
        expected_fields = COMMON_FIELDS | (
            frozenset({"databaseCount"}) if profile == "local" else frozenset()
        )
        fields = set(payload)
        unknown = sorted(fields - expected_fields)
        missing = sorted(expected_fields - fields)
        if unknown:
            raise TenantSpecError(
                f"unknown tenant specification fields: {', '.join(unknown)}"
            )
        if missing:
            raise TenantSpecError(
                f"missing tenant specification fields: {', '.join(missing)}"
            )
        if isinstance(payload["schema"], bool) or payload["schema"] != 1:
            raise TenantSpecError("tenant specification schema must be 1")
        name = validate_tenant_name(_required_string(payload, "name"))
        version = _required_string(payload, "kubernetesVersion").removeprefix("v")
        if not KUBERNETES_VERSION_RE.fullmatch(version):
            raise TenantSpecError(
                "tenant Kubernetes version must use major.minor.patch"
            )
        if supported_versions is not None:
            supported = supported_versions.get(profile, "").removeprefix("v")
            if version != supported:
                raise TenantSpecError(
                    f"unsupported {profile} tenant Kubernetes version: {version}"
                )
        pod_network = _network(payload, "podCIDR")
        service_network = _network(payload, "serviceCIDR")
        if pod_network.overlaps(service_network):
            raise TenantSpecError("tenant Pod and Service CIDRs overlap")
        if service_network.num_addresses <= 11:
            raise TenantSpecError(
                "tenant Service CIDR is too small for the derived DNS service IP"
            )
        return cls(
            profile=profile,
            name=name,
            kubernetes_version=version,
            workers=_required_count(payload, "workers"),
            pod_network=pod_network,
            service_network=service_network,
            database_count=(
                _required_count(payload, "databaseCount")
                if profile == "local"
                else None
            ),
        )

    @property
    def namespace(self) -> str:
        return self.name

    @property
    def dns_service_ip(self) -> str:
        return str(self.service_network.network_address + 10)

    @property
    def cluster_domain(self) -> str:
        return f"{self.name}.capi.local" if self.profile == "local" else "cluster.local"

    @property
    def database_name(self) -> str | None:
        return f"{self.name}-postgres" if self.profile == "local" else None

    def to_mapping(self) -> dict[str, object]:
        result: dict[str, object] = {
            "schema": 1,
            "profile": self.profile,
            "name": self.name,
            "kubernetesVersion": self.kubernetes_version,
            "workers": self.workers,
            "podCIDR": str(self.pod_network),
            "serviceCIDR": str(self.service_network),
        }
        if self.database_count is not None:
            result["databaseCount"] = self.database_count
        return result

    def canonical_json(self) -> str:
        return json.dumps(
            self.to_mapping(),
            sort_keys=True,
            separators=(",", ":"),
        )

    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


def load_tenant_spec(
    path: Path,
    *,
    expected_profile: str | None = None,
    supported_versions: Mapping[str, str] | None = None,
) -> TenantSpec:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TenantSpecError(f"unable to read tenant specification {path}: {exc}") from exc
    try:
        payload = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise TenantSpecError(f"invalid tenant specification JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise TenantSpecError("tenant specification must be a JSON object")
    return TenantSpec.from_mapping(
        payload,
        expected_profile=expected_profile,
        supported_versions=supported_versions,
    )


def require_non_overlapping_networks(
    spec: TenantSpec,
    networks: Mapping[str, ipaddress.IPv4Network],
) -> None:
    for label, network in networks.items():
        if spec.pod_network.overlaps(network):
            raise TenantSpecError(f"tenant Pod CIDR overlaps {label}")
        if spec.service_network.overlaps(network):
            raise TenantSpecError(f"tenant Service CIDR overlaps {label}")
