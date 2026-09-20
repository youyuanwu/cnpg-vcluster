from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping, Sequence

from .files import (
    IntegrityError,
    private_directory,
    private_file_exists,
    read_private_file,
    unlink_private_file,
    write_private_file,
)
from .tenant_spec import TenantSpec, validate_tenant_name


class TenantRuntimeError(RuntimeError):
    pass


def _string_mapping(
    value: object,
    field: str,
    *,
    allow_empty: bool = True,
) -> dict[str, str]:
    if not isinstance(value, dict) or not all(
        isinstance(key, str)
        and bool(key)
        and isinstance(item, str)
        and bool(item)
        for key, item in value.items()
    ):
        raise TenantRuntimeError(f"invalid tenant runtime field: {field}")
    if not allow_empty and not value:
        raise TenantRuntimeError(f"tenant runtime field must not be empty: {field}")
    return dict(value)


def _read_private_json(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(read_private_file(path).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TenantRuntimeError(f"invalid tenant runtime JSON {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise TenantRuntimeError(f"tenant runtime JSON must be an object: {path}")
    return payload


def _unlink_private_file(path: Path) -> None:
    unlink_private_file(path)


@dataclass(frozen=True)
class TenantRuntimePaths:
    directory: Path
    identity: Path
    operation: Path
    ready: Path
    evidence: Path


def foundation_sha256(identity: Mapping[str, str]) -> str:
    payload = json.dumps(dict(identity), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def tenant_runtime_paths(root: Path, profile: str, tenant: str) -> TenantRuntimePaths:
    if profile not in {"local", "azure"}:
        raise TenantRuntimeError(f"unsupported tenant profile: {profile}")
    validate_tenant_name(tenant)
    directory = root / ".runtime" / "lifecycle" / profile / tenant
    return TenantRuntimePaths(
        directory=directory,
        identity=directory / "identity.json",
        operation=directory / "operation.json",
        ready=directory / "ready.json",
        evidence=directory / "evidence",
    )


def recorded_tenant_names(root: Path, profile: str) -> tuple[str, ...]:
    if profile not in {"local", "azure"}:
        raise TenantRuntimeError(f"unsupported tenant profile: {profile}")
    directory = root / ".runtime" / "lifecycle" / profile
    if not directory.exists():
        return ()
    details = directory.lstat()
    if (
        directory.is_symlink()
        or not directory.is_dir()
        or details.st_uid != os.getuid()
        or details.st_mode & 0o077
    ):
        raise TenantRuntimeError(
            "tenant lifecycle profile directory is not owner-only"
        )
    names = []
    for candidate in directory.iterdir():
        details = candidate.lstat()
        if (
            candidate.is_symlink()
            or not candidate.is_dir()
            or details.st_uid != os.getuid()
            or details.st_mode & 0o077
        ):
            raise TenantRuntimeError(
                "tenant lifecycle directory is not owner-only"
            )
        validate_tenant_name(candidate.name)
        names.append(candidate.name)
    return tuple(sorted(names))


@dataclass(frozen=True)
class OperationJournal:
    operation_id: str
    operation: str
    profile: str
    tenant: str
    specification: Mapping[str, object]
    specification_sha256: str
    foundation_identity: Mapping[str, str]
    intended_resources: Sequence[str]
    phase: str
    observed: Mapping[str, str]

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "OperationJournal":
        expected = {
            "schema",
            "operationId",
            "operation",
            "profile",
            "tenant",
            "specification",
            "specificationSha256",
            "foundationIdentity",
            "intendedResources",
            "phase",
            "observed",
        }
        if (
            set(payload) != expected
            or isinstance(payload.get("schema"), bool)
            or payload.get("schema") != 1
        ):
            raise TenantRuntimeError("invalid tenant operation journal schema")
        intended = payload["intendedResources"]
        if not isinstance(intended, list) or not all(
            isinstance(item, str) for item in intended
        ):
            raise TenantRuntimeError("invalid tenant intended resource list")
        strings = {
            key: payload[key]
            for key in (
                "operationId",
                "operation",
                "profile",
                "tenant",
                "specificationSha256",
                "phase",
            )
        }
        if not all(isinstance(value, str) and value for value in strings.values()):
            raise TenantRuntimeError("invalid tenant operation journal value")
        if strings["operation"] not in {"create", "delete"}:
            raise TenantRuntimeError("invalid tenant operation")
        if not isinstance(payload["specification"], dict):
            raise TenantRuntimeError("invalid tenant operation specification")
        specification = TenantSpec.from_mapping(payload["specification"])
        if (
            specification.profile != strings["profile"]
            or specification.name != strings["tenant"]
            or specification.sha256() != strings["specificationSha256"]
        ):
            raise TenantRuntimeError("tenant operation specification binding changed")
        return cls(
            operation_id=strings["operationId"],
            operation=strings["operation"],
            profile=strings["profile"],
            tenant=strings["tenant"],
            specification=specification.to_mapping(),
            specification_sha256=strings["specificationSha256"],
            foundation_identity=_string_mapping(
                payload["foundationIdentity"],
                "foundationIdentity",
                allow_empty=False,
            ),
            intended_resources=tuple(intended),
            phase=strings["phase"],
            observed=_string_mapping(payload["observed"], "observed"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema": 1,
            "operationId": self.operation_id,
            "operation": self.operation,
            "profile": self.profile,
            "tenant": self.tenant,
            "specification": dict(self.specification),
            "specificationSha256": self.specification_sha256,
            "foundationIdentity": dict(self.foundation_identity),
            "intendedResources": list(self.intended_resources),
            "phase": self.phase,
            "observed": dict(self.observed),
        }


@dataclass(frozen=True)
class TenantIdentity:
    profile: str
    tenant: str
    specification: TenantSpec
    specification_sha256: str
    foundation_identity: Mapping[str, str]
    observed: Mapping[str, str]

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "TenantIdentity":
        expected = {
            "schema",
            "profile",
            "tenant",
            "specification",
            "specificationSha256",
            "foundationIdentity",
            "observed",
        }
        if (
            set(payload) != expected
            or isinstance(payload.get("schema"), bool)
            or payload.get("schema") != 1
        ):
            raise TenantRuntimeError("invalid tenant identity schema")
        if not isinstance(payload["specification"], dict):
            raise TenantRuntimeError("invalid tenant identity specification")
        spec = TenantSpec.from_mapping(payload["specification"])
        specification_sha256 = payload["specificationSha256"]
        if (
            not isinstance(specification_sha256, str)
            or specification_sha256 != spec.sha256()
        ):
            raise TenantRuntimeError("tenant identity specification checksum changed")
        profile = payload["profile"]
        tenant = payload["tenant"]
        if profile != spec.profile or tenant != spec.name:
            raise TenantRuntimeError("tenant identity specification binding changed")
        return cls(
            profile=spec.profile,
            tenant=spec.name,
            specification=spec,
            specification_sha256=specification_sha256,
            foundation_identity=_string_mapping(
                payload["foundationIdentity"],
                "foundationIdentity",
                allow_empty=False,
            ),
            observed=_string_mapping(
                payload["observed"],
                "observed",
                allow_empty=False,
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema": 1,
            "profile": self.profile,
            "tenant": self.tenant,
            "specification": self.specification.to_mapping(),
            "specificationSha256": self.specification_sha256,
            "foundationIdentity": dict(self.foundation_identity),
            "observed": dict(self.observed),
        }


class TenantRuntime:
    def __init__(self, root: Path, profile: str, tenant: str) -> None:
        self.paths = tenant_runtime_paths(root, profile, tenant)
        self.profile = profile
        self.tenant = tenant

    def identity_exists(self) -> bool:
        return private_file_exists(self.paths.identity)

    def operation_exists(self) -> bool:
        return private_file_exists(self.paths.operation)

    def load_identity(self) -> TenantIdentity:
        identity = TenantIdentity.from_mapping(_read_private_json(self.paths.identity))
        if identity.profile != self.profile or identity.tenant != self.tenant:
            raise TenantRuntimeError("tenant identity does not match runtime path")
        return identity

    def require_compatible_identity(
        self,
        spec: TenantSpec,
        foundation_identity: Mapping[str, str],
    ) -> TenantIdentity | None:
        if not self.identity_exists():
            return None
        identity = self.load_identity()
        if identity.specification_sha256 != spec.sha256():
            raise TenantRuntimeError("existing tenant specification changed")
        if dict(identity.foundation_identity) != dict(foundation_identity):
            raise TenantRuntimeError("existing tenant foundation binding changed")
        return identity

    def load_operation(self) -> OperationJournal:
        journal = OperationJournal.from_mapping(
            _read_private_json(self.paths.operation)
        )
        if journal.profile != self.profile or journal.tenant != self.tenant:
            raise TenantRuntimeError("tenant operation does not match runtime path")
        return journal

    def load_ready_evidence(self) -> dict[str, object]:
        return _read_private_json(self.paths.ready)

    def write_ready_evidence(self, payload: Mapping[str, object]) -> None:
        write_private_file(
            self.paths.ready,
            json.dumps(dict(payload), sort_keys=True) + "\n",
        )

    def remove_ready_evidence(self) -> None:
        _unlink_private_file(self.paths.ready)

    def start_operation(
        self,
        *,
        operation: str,
        spec: TenantSpec,
        foundation_identity: Mapping[str, str],
        intended_resources: Sequence[str],
        operation_id: str | None = None,
    ) -> OperationJournal:
        if operation not in {"create", "delete"}:
            raise TenantRuntimeError(f"unsupported tenant operation: {operation}")
        if spec.profile != self.profile or spec.name != self.tenant:
            raise TenantRuntimeError("tenant specification does not match runtime path")
        validated_foundation = _string_mapping(
            dict(foundation_identity),
            "foundationIdentity",
            allow_empty=False,
        )
        if not intended_resources or not all(
            isinstance(item, str) and item for item in intended_resources
        ):
            raise TenantRuntimeError("tenant intended resources must not be empty")
        identity = self.require_compatible_identity(
            spec,
            validated_foundation,
        )
        if self.operation_exists():
            existing = self.load_operation()
            if (
                existing.operation != operation
                or existing.specification_sha256 != spec.sha256()
                or dict(existing.foundation_identity) != validated_foundation
                or tuple(existing.intended_resources) != tuple(intended_resources)
            ):
                raise TenantRuntimeError("conflicting tenant operation already exists")
            if identity is None:
                return existing
            conflicts = sorted(
                key
                for key, value in identity.observed.items()
                if key in existing.observed and existing.observed[key] != value
            )
            if conflicts:
                raise TenantRuntimeError(
                    "tenant observed identity changed: " + ", ".join(conflicts)
                )
            merged = {**identity.observed, **existing.observed}
            if merged != dict(existing.observed):
                existing = replace(existing, observed=merged)
                write_private_file(
                    self.paths.operation,
                    json.dumps(existing.to_mapping(), sort_keys=True) + "\n",
                )
            return existing
        journal = OperationJournal(
            operation_id=operation_id or uuid.uuid4().hex,
            operation=operation,
            profile=self.profile,
            tenant=self.tenant,
            specification=spec.to_mapping(),
            specification_sha256=spec.sha256(),
            foundation_identity=validated_foundation,
            intended_resources=tuple(intended_resources),
            phase="validated",
            observed={} if identity is None else dict(identity.observed),
        )
        write_private_file(
            self.paths.operation,
            json.dumps(journal.to_mapping(), sort_keys=True) + "\n",
        )
        return journal

    def update_operation(
        self,
        journal: OperationJournal,
        *,
        phase: str,
        observed: Mapping[str, str] | None = None,
    ) -> OperationJournal:
        current = self.load_operation()
        if current.operation_id != journal.operation_id:
            raise TenantRuntimeError("tenant operation identity changed")
        merged = dict(current.observed)
        if observed is not None:
            identity = self.load_identity() if self.identity_exists() else None
            durable = {} if identity is None else dict(identity.observed)
            conflicts = sorted(
                key
                for key, value in observed.items()
                if (
                    key in merged
                    and merged[key] != value
                )
                or (
                    key in durable
                    and durable[key] != value
                )
            )
            if conflicts:
                raise TenantRuntimeError(
                    "tenant observed identity changed: " + ", ".join(conflicts)
                )
            merged.update(observed)
        updated = replace(current, phase=phase, observed=merged)
        write_private_file(
            self.paths.operation,
            json.dumps(updated.to_mapping(), sort_keys=True) + "\n",
        )
        return updated

    def require_recoverable_markers(
        self,
        journal: OperationJournal,
        markers: Mapping[str, str],
    ) -> None:
        expected = {
            "tenant": journal.tenant,
            "profile": journal.profile,
            "specificationSha256": journal.specification_sha256,
            "operationId": journal.observed.get(
                "markerOperationId",
                journal.operation_id,
            ),
            "foundationSha256": foundation_sha256(journal.foundation_identity),
        }
        if dict(markers) != expected:
            raise TenantRuntimeError(
                "existing resource does not match tenant operation markers"
            )

    def recover_observed_identity(
        self,
        journal: OperationJournal,
        *,
        resource: str,
        identifier: str,
        markers: Mapping[str, str],
        phase: str,
    ) -> OperationJournal:
        self.require_recoverable_markers(journal, markers)
        return self.update_operation(
            journal,
            phase=phase,
            observed={resource: identifier},
        )

    def complete_create(
        self,
        journal: OperationJournal,
        spec: TenantSpec,
        observed: Mapping[str, str],
    ) -> None:
        current = self.load_operation()
        if current.operation_id != journal.operation_id:
            raise TenantRuntimeError("tenant operation identity changed")
        existing = self.require_compatible_identity(
            spec,
            current.foundation_identity,
        )
        prior_observed = {} if existing is None else dict(existing.observed)
        validated_observed = _string_mapping(
            dict(observed),
            "observed",
            allow_empty=False,
        )
        conflicts = sorted(
            {
                key
                for key, value in current.observed.items()
                if key in prior_observed and prior_observed[key] != value
            }
            | {
                key
                for key, value in validated_observed.items()
                if (
                    key in current.observed
                    and current.observed[key] != value
                )
                or (
                    key in prior_observed
                    and prior_observed[key] != value
                )
            }
        )
        if conflicts:
            raise TenantRuntimeError(
                "tenant observed identity changed: " + ", ".join(conflicts)
            )
        identity = TenantIdentity(
            profile=self.profile,
            tenant=self.tenant,
            specification=spec,
            specification_sha256=spec.sha256(),
            foundation_identity=dict(current.foundation_identity),
            observed={**prior_observed, **current.observed, **validated_observed},
        )
        write_private_file(
            self.paths.identity,
            json.dumps(identity.to_mapping(), sort_keys=True) + "\n",
        )
        _unlink_private_file(self.paths.operation)

    def complete_delete(self, journal: OperationJournal) -> None:
        current = self.load_operation()
        if current.operation_id != journal.operation_id:
            raise TenantRuntimeError("tenant operation identity changed")
        _unlink_private_file(self.paths.ready)
        _unlink_private_file(self.paths.identity)
        _unlink_private_file(self.paths.operation)
