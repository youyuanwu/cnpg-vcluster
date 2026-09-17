#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Mapping, Protocol, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.lib.config import load_env_file
from scripts.lib.locking import (
    e2e_lock,
    profile_lock,
    profile_lock_exists,
    tools_lock,
)
from scripts.lib.redaction import redact
from scripts.lib.tenant_runtime import (
    OperationJournal,
    TenantIdentity,
    TenantRuntime,
    TenantRuntimeError,
)
from scripts.lib.tenant_spec import (
    PROFILES,
    TenantSpec,
    TenantSpecError,
    load_tenant_spec,
    validate_tenant_name,
)
from scripts.lib.tenant_status import TenantStatus
from scripts.lib.tenant_timing import TenantTimings, record_rejected_create


class TenantAdapter(Protocol):
    def foundation_identity(
        self,
        root: Path,
        spec: TenantSpec,
    ) -> Mapping[str, str]: ...

    def intended_resources(self, spec: TenantSpec) -> Sequence[str]: ...

    def create(
        self,
        root: Path,
        spec: TenantSpec,
        runtime: TenantRuntime,
        journal: OperationJournal,
        timings: TenantTimings,
    ) -> Mapping[str, str]: ...

    def status(self, root: Path, tenant: str) -> TenantStatus: ...

    def delete(
        self,
        root: Path,
        spec: TenantSpec,
        identity: TenantIdentity,
        runtime: TenantRuntime,
        journal: OperationJournal,
        timings: TenantTimings,
    ) -> None: ...


def supported_versions(root: Path) -> dict[str, str]:
    local = load_env_file(root / "config" / "versions.env")
    azure = load_env_file(root / "config" / "azure" / "defaults.env")
    return {
        "local": local["KUBERNETES_VERSION"],
        "azure": azure["AZURE_TENANT_KUBERNETES_VERSION"],
    }


def _adapter(
    profile: str,
    adapters: Mapping[str, TenantAdapter],
) -> TenantAdapter:
    try:
        return adapters[profile]
    except KeyError as exc:
        raise RuntimeError(
            f"tenant profile adapter is not implemented: {profile}"
        ) from exc


def _persist_timings(timings: TenantTimings) -> None:
    timings.emit()
    timings.persist()


def _finish_timings(timings: TenantTimings, primary: BaseException | None) -> None:
    try:
        _persist_timings(timings)
    except BaseException as evidence_error:
        if primary is None:
            raise
        primary.add_note(f"tenant timing evidence failed: {redact(str(evidence_error))}")


def _ownership_invalid(profile: str, tenant: str, error: BaseException | str) -> TenantStatus:
    return TenantStatus(
        profile=profile,
        tenant=tenant,
        classification="ownership-invalid",
        foundation_healthy=False,
        blockers=(str(error),),
    )


def _safe_adapter_status(
    adapter: TenantAdapter,
    root: Path,
    profile: str,
    tenant: str,
) -> TenantStatus:
    try:
        status = adapter.status(root, tenant)
    except BaseException as exc:
        return _ownership_invalid(profile, tenant, exc)
    if status.profile != profile or status.tenant != tenant:
        return _ownership_invalid(
            profile,
            tenant,
            "tenant adapter returned status for a different identity",
        )
    return status


def create_tenant(
    root: Path,
    profile: str,
    spec_path: Path,
    adapters: Mapping[str, TenantAdapter],
) -> int:
    with e2e_lock(root, exclusive=False):
        with profile_lock(
            root,
            profile,
            exclusive=True,
            create=True,
        ) as acquired:
            if not acquired:
                raise RuntimeError("tenant profile mutation lock is unavailable")
            with tools_lock(root, exclusive=True):
                operation_id = uuid.uuid4().hex
                validation_started = time.monotonic()
                try:
                    spec = load_tenant_spec(
                        spec_path,
                        expected_profile=profile,
                        supported_versions=supported_versions(root),
                    )
                except BaseException as exc:
                    try:
                        record_rejected_create(
                            root,
                            profile=profile,
                            operation_id=operation_id,
                            seconds=time.monotonic() - validation_started,
                            error=exc,
                        )
                    except BaseException as evidence_error:
                        exc.add_note(
                            "tenant timing evidence failed: "
                            + redact(str(evidence_error))
                        )
                    raise
                adapter = _adapter(profile, adapters)
                timings = TenantTimings(
                    root,
                    profile=profile,
                    tenant=spec.name,
                    operation="create",
                    operation_id=operation_id,
                )
                timings.record_passed(
                    "validation",
                    time.monotonic() - validation_started,
                )
                runtime = TenantRuntime(root, profile, spec.name)
                primary: BaseException | None = None
                try:
                    with timings.phase("foundation"):
                        foundation = adapter.foundation_identity(root, spec)
                    with timings.phase("journal"):
                        runtime.require_compatible_identity(spec, foundation)
                        journal = runtime.start_operation(
                            operation="create",
                            spec=spec,
                            foundation_identity=foundation,
                            intended_resources=adapter.intended_resources(spec),
                            operation_id=operation_id,
                        )
                        timings.bind_operation_id(journal.operation_id)
                    observed = adapter.create(
                        root,
                        spec,
                        runtime,
                        journal,
                        timings,
                    )
                    runtime.complete_create(journal, spec, observed)
                except BaseException as exc:
                    primary = exc
                    timings.record_failure("operation", exc)
                    raise
                finally:
                    _finish_timings(timings, primary)
    return 0


def status_tenant(
    root: Path,
    profile: str,
    tenant: str,
    adapters: Mapping[str, TenantAdapter],
) -> int:
    validate_tenant_name(tenant)
    adapter = _adapter(profile, adapters)
    runtime = TenantRuntime(root, profile, tenant)
    try:
        identity_present = runtime.identity_exists()
        operation_present = runtime.operation_exists()
        if identity_present:
            runtime.load_identity()
        if operation_present:
            runtime.load_operation()
    except BaseException as exc:
        status = _ownership_invalid(profile, tenant, exc)
    else:
        try:
            lock_present = profile_lock_exists(root, profile)
        except BaseException as exc:
            status = _ownership_invalid(profile, tenant, exc)
        else:
            if not lock_present:
                inspected = _safe_adapter_status(
                    adapter,
                    root,
                    profile,
                    tenant,
                )
                if inspected.classification == "ownership-invalid":
                    status = inspected
                elif (
                    identity_present
                    or operation_present
                    or inspected.classification != "absent"
                ):
                    status = _ownership_invalid(
                        profile,
                        tenant,
                        "tenant state exists without its profile lock",
                    )
                else:
                    status = inspected
            else:
                try:
                    with e2e_lock(
                        root,
                        exclusive=False,
                        create=False,
                    ) as e2e_acquired:
                        if not e2e_acquired:
                            raise RuntimeError("tenant E2E status lock is missing")
                        with profile_lock(
                            root,
                            profile,
                            exclusive=False,
                            create=False,
                        ) as acquired:
                            if not acquired:
                                raise RuntimeError(
                                    "tenant profile status lock disappeared"
                                )
                            with tools_lock(
                                root,
                                exclusive=False,
                                create=False,
                            ) as tools_acquired:
                                if not tools_acquired:
                                    raise RuntimeError(
                                        "tenant tools status lock is missing"
                                    )
                                status = _safe_adapter_status(
                                    adapter,
                                    root,
                                    profile,
                                    tenant,
                                )
                except BaseException as exc:
                    status = _ownership_invalid(profile, tenant, exc)
    print(status.to_json())
    return 0 if status.classification in {"ready", "absent"} else 1


def delete_tenant(
    root: Path,
    profile: str,
    tenant: str,
    confirmation: str,
    adapters: Mapping[str, TenantAdapter],
) -> int:
    validate_tenant_name(tenant)
    expected_confirmation = f"{profile}/{tenant}"
    if confirmation != expected_confirmation:
        raise RuntimeError(
            f"tenant deletion requires confirmation token {expected_confirmation!r}"
        )
    adapter = _adapter(profile, adapters)
    operation_id = uuid.uuid4().hex
    with e2e_lock(root, exclusive=False):
        with profile_lock(
            root,
            profile,
            exclusive=True,
            create=True,
        ) as acquired:
            if not acquired:
                raise RuntimeError("tenant profile mutation lock is unavailable")
            with tools_lock(root, exclusive=True):
                runtime = TenantRuntime(root, profile, tenant)
                timings = TenantTimings(
                    root,
                    profile=profile,
                    tenant=tenant,
                    operation="delete",
                    operation_id=operation_id,
                )
                primary: BaseException | None = None
                try:
                    with timings.phase("validation"):
                        identity = (
                            runtime.load_identity()
                            if runtime.identity_exists()
                            else None
                        )
                        pending = (
                            runtime.load_operation()
                            if runtime.operation_exists()
                            else None
                        )
                        if pending is not None:
                            timings.bind_operation_id(pending.operation_id)
                    if identity is None:
                        with timings.phase("absence"):
                            inspected = _safe_adapter_status(
                                adapter,
                                root,
                                profile,
                                tenant,
                            )
                            if inspected.classification != "absent":
                                raise TenantRuntimeError(
                                    "tenant resources exist without an "
                                    "authoritative identity"
                                )
                        if pending is not None:
                            if pending.operation != "delete":
                                raise TenantRuntimeError(
                                    "tenant create operation exists without an identity"
                                )
                            runtime.complete_delete(pending)
                        return 0
                    spec = TenantSpec.from_mapping(
                        identity.specification.to_mapping(),
                        expected_profile=profile,
                        supported_versions=supported_versions(root),
                    )
                    with timings.phase("foundation"):
                        observed_foundation = adapter.foundation_identity(root, spec)
                        if dict(observed_foundation) != dict(
                            identity.foundation_identity
                        ):
                            raise TenantRuntimeError(
                                "tenant foundation binding changed"
                            )
                    with timings.phase("journal"):
                        journal = runtime.start_operation(
                            operation="delete",
                            spec=spec,
                            foundation_identity=identity.foundation_identity,
                            intended_resources=adapter.intended_resources(spec),
                            operation_id=operation_id,
                        )
                        timings.bind_operation_id(journal.operation_id)
                    adapter.delete(
                        root,
                        spec,
                        identity,
                        runtime,
                        journal,
                        timings,
                    )
                    runtime.complete_delete(journal)
                except BaseException as exc:
                    primary = exc
                    timings.record_failure("operation", exc)
                    raise
                finally:
                    _finish_timings(timings, primary)
    return 0


def execute(
    root: Path,
    arguments: Sequence[str],
    *,
    adapters: Mapping[str, TenantAdapter] | None = None,
) -> int:
    available = {} if adapters is None else adapters
    if not arguments:
        raise RuntimeError(
            "usage: tenant.py "
            "<create PROFILE SPEC.json|status PROFILE TENANT|"
            "delete PROFILE TENANT CONFIRMATION>"
        )
    command = arguments[0]
    if command == "create" and len(arguments) == 3:
        profile = arguments[1]
        if profile not in PROFILES:
            raise TenantSpecError(f"unsupported tenant profile: {profile}")
        return create_tenant(root, profile, Path(arguments[2]), available)
    if command == "status" and len(arguments) == 3:
        profile = arguments[1]
        if profile not in PROFILES:
            raise TenantSpecError(f"unsupported tenant profile: {profile}")
        return status_tenant(root, profile, arguments[2], available)
    if command == "delete" and len(arguments) == 4:
        profile = arguments[1]
        if profile not in PROFILES:
            raise TenantSpecError(f"unsupported tenant profile: {profile}")
        return delete_tenant(
            root,
            profile,
            arguments[2],
            arguments[3],
            available,
        )
    raise RuntimeError(f"invalid tenant command arguments: {json.dumps(arguments)}")


def main(arguments: Sequence[str]) -> int:
    os.umask(0o077)
    return execute(ROOT, arguments)


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except (TenantSpecError, RuntimeError) as exc:
        print(redact(str(exc)), file=sys.stderr)
        raise SystemExit(1)
