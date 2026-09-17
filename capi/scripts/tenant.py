#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
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
from scripts.lib.tenant_runtime import OperationJournal, TenantRuntime
from scripts.lib.tenant_spec import (
    PROFILES,
    TenantSpec,
    TenantSpecError,
    load_tenant_spec,
    validate_tenant_name,
)
from scripts.lib.tenant_status import TenantStatus
from scripts.lib.tenant_timing import TenantTimings


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
        identity: Mapping[str, object],
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


def create_tenant(
    root: Path,
    profile: str,
    spec_path: Path,
    adapters: Mapping[str, TenantAdapter],
) -> int:
    spec = load_tenant_spec(
        spec_path,
        expected_profile=profile,
        supported_versions=supported_versions(root),
    )
    adapter = _adapter(profile, adapters)
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
                foundation = adapter.foundation_identity(root, spec)
                runtime = TenantRuntime(root, profile, spec.name)
                journal = runtime.start_operation(
                    operation="create",
                    spec=spec,
                    foundation_identity=foundation,
                    intended_resources=adapter.intended_resources(spec),
                )
                timings = TenantTimings(
                    root,
                    profile=profile,
                    tenant=spec.name,
                    operation="create",
                    operation_id=journal.operation_id,
                )
                primary: BaseException | None = None
                try:
                    with timings.phase("validation"):
                        pass
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
    if not profile_lock_exists(root, profile):
        status = adapter.status(root, tenant)
    else:
        with e2e_lock(root, exclusive=False):
            with profile_lock(
                root,
                profile,
                exclusive=False,
                create=False,
            ) as acquired:
                if not acquired:
                    raise RuntimeError("tenant profile status lock disappeared")
                with tools_lock(root, exclusive=False):
                    status = adapter.status(root, tenant)
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
                identity = runtime.load_identity()
                specification = identity.get("specification")
                if not isinstance(specification, dict):
                    raise RuntimeError("tenant identity lacks its specification")
                spec = TenantSpec.from_mapping(
                    specification,
                    expected_profile=profile,
                    supported_versions=supported_versions(root),
                )
                foundation = identity.get("foundationIdentity")
                if not isinstance(foundation, dict) or not all(
                    isinstance(key, str) and isinstance(value, str)
                    for key, value in foundation.items()
                ):
                    raise RuntimeError("tenant identity has invalid foundation binding")
                journal = runtime.start_operation(
                    operation="delete",
                    spec=spec,
                    foundation_identity=foundation,
                    intended_resources=adapter.intended_resources(spec),
                )
                timings = TenantTimings(
                    root,
                    profile=profile,
                    tenant=tenant,
                    operation="delete",
                    operation_id=journal.operation_id,
                )
                primary: BaseException | None = None
                try:
                    with timings.phase("validation"):
                        pass
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
