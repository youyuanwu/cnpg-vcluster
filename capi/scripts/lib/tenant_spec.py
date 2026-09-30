from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


TENANT_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,28}[a-z0-9])?$")
KUBERNETES_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
PROFILE = "azure"
COMMON_FIELDS = frozenset(
    {
        "schema",
        "profile",
        "name",
        "kubernetesVersion",
        "workers",
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


@dataclass(frozen=True)
class TenantSpec:
    profile: str
    name: str
    kubernetes_version: str
    workers: int

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, object],
        *,
        expected_profile: str | None = None,
        supported_versions: Mapping[str, str] | None = None,
    ) -> "TenantSpec":
        profile = _required_string(payload, "profile")
        if profile != PROFILE:
            raise TenantSpecError(f"unsupported tenant profile: {profile}")
        if expected_profile is not None and profile != expected_profile:
            raise TenantSpecError(
                f"tenant specification profile {profile!r} does not match "
                f"selected profile {expected_profile!r}"
            )
        expected_fields = COMMON_FIELDS
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
        return cls(
            profile=profile,
            name=name,
            kubernetes_version=version,
            workers=_required_count(payload, "workers"),
        )

    @property
    def namespace(self) -> str:
        return self.name

    @property
    def cluster_domain(self) -> str:
        return "cluster.local"

    def to_mapping(self) -> dict[str, object]:
        result: dict[str, object] = {
            "schema": 1,
            "profile": self.profile,
            "name": self.name,
            "kubernetesVersion": self.kubernetes_version,
            "workers": self.workers,
        }
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
