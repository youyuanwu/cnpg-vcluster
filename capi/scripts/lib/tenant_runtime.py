from __future__ import annotations

import json
import os
import stat
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping, Sequence

from .files import (
    IntegrityError,
    private_directory,
    private_file_exists,
    read_private_file,
    write_private_file,
)
from .tenant_spec import TenantSpec, validate_tenant_name


class TenantRuntimeError(RuntimeError):
    pass


def _string_mapping(value: object, field: str) -> dict[str, str]:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str)
        for key, item in value.items()
    ):
        raise TenantRuntimeError(f"invalid tenant runtime field: {field}")
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
    with private_directory(path.parent) as parent_fd:
        try:
            details = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.getuid()
            or details.st_mode & 0o077
        ):
            raise IntegrityError(
                f"private file is not an owner-only regular file: {path}"
            )
        os.unlink(path.name, dir_fd=parent_fd)


@dataclass(frozen=True)
class TenantRuntimePaths:
    directory: Path
    identity: Path
    operation: Path
    evidence: Path


def tenant_runtime_paths(root: Path, profile: str, tenant: str) -> TenantRuntimePaths:
    if profile not in {"local", "azure"}:
        raise TenantRuntimeError(f"unsupported tenant profile: {profile}")
    validate_tenant_name(tenant)
    directory = root / ".runtime" / "lifecycle" / profile / tenant
    return TenantRuntimePaths(
        directory=directory,
        identity=directory / "identity.json",
        operation=directory / "operation.json",
        evidence=directory / "evidence",
    )


@dataclass(frozen=True)
class OperationJournal:
    operation_id: str
    operation: str
    profile: str
    tenant: str
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
            "specificationSha256",
            "foundationIdentity",
            "intendedResources",
            "phase",
            "observed",
        }
        if set(payload) != expected or payload.get("schema") != 1:
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
        return cls(
            operation_id=strings["operationId"],
            operation=strings["operation"],
            profile=strings["profile"],
            tenant=strings["tenant"],
            specification_sha256=strings["specificationSha256"],
            foundation_identity=_string_mapping(
                payload["foundationIdentity"], "foundationIdentity"
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
            "specificationSha256": self.specification_sha256,
            "foundationIdentity": dict(self.foundation_identity),
            "intendedResources": list(self.intended_resources),
            "phase": self.phase,
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

    def load_identity(self) -> dict[str, object]:
        payload = _read_private_json(self.paths.identity)
        if payload.get("schema") != 1:
            raise TenantRuntimeError("invalid tenant identity schema")
        if payload.get("profile") != self.profile or payload.get("tenant") != self.tenant:
            raise TenantRuntimeError("tenant identity does not match runtime path")
        return payload

    def load_operation(self) -> OperationJournal:
        journal = OperationJournal.from_mapping(
            _read_private_json(self.paths.operation)
        )
        if journal.profile != self.profile or journal.tenant != self.tenant:
            raise TenantRuntimeError("tenant operation does not match runtime path")
        return journal

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
        if self.operation_exists():
            existing = self.load_operation()
            if (
                existing.operation != operation
                or existing.specification_sha256 != spec.sha256()
                or dict(existing.foundation_identity) != dict(foundation_identity)
                or tuple(existing.intended_resources) != tuple(intended_resources)
            ):
                raise TenantRuntimeError("conflicting tenant operation already exists")
            return existing
        journal = OperationJournal(
            operation_id=operation_id or uuid.uuid4().hex,
            operation=operation,
            profile=self.profile,
            tenant=self.tenant,
            specification_sha256=spec.sha256(),
            foundation_identity=dict(foundation_identity),
            intended_resources=tuple(intended_resources),
            phase="validated",
            observed={},
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
            "operationId": journal.operation_id,
        }
        if dict(markers) != expected:
            raise TenantRuntimeError(
                "existing resource does not match tenant operation markers"
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
        identity = {
            "schema": 1,
            "profile": self.profile,
            "tenant": self.tenant,
            "specification": spec.to_mapping(),
            "specificationSha256": spec.sha256(),
            "foundationIdentity": dict(current.foundation_identity),
            "observed": dict(observed),
        }
        write_private_file(
            self.paths.identity,
            json.dumps(identity, sort_keys=True) + "\n",
        )
        _unlink_private_file(self.paths.operation)

    def complete_delete(self, journal: OperationJournal) -> None:
        current = self.load_operation()
        if current.operation_id != journal.operation_id:
            raise TenantRuntimeError("tenant operation identity changed")
        _unlink_private_file(self.paths.identity)
        _unlink_private_file(self.paths.operation)
