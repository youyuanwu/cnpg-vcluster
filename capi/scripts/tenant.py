#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from contextlib import nullcontext
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.lib.config import load_env_file
from scripts.lib.locking import azure_lock, e2e_lock, tools_lock
from scripts.lib.redaction import redact
from scripts.lib.tenant_spec import (
    PROFILE,
    TenantSpecError,
    load_tenant_spec,
    validate_tenant_name,
)


def supported_versions(root: Path) -> dict[str, str]:
    azure = load_env_file(root / "config" / "azure" / "defaults.env")
    return {
        PROFILE: azure["AZURE_SUPPORTED_TENANT_KUBERNETES_VERSION"],
    }


def _tenant_e2e_lock(root: Path):
    if os.environ.get("CAPI_E2E_CHILD") == "1":
        return nullcontext()
    return e2e_lock(root, exclusive=False)


def execute(root: Path, arguments: Sequence[str]) -> int:
    from scripts.lib.azure import operator

    if not arguments:
        raise RuntimeError(
            "usage: tenant.py "
            "<create PROFILE SPEC.json|status PROFILE TENANT|"
            "delete PROFILE TENANT CONFIRMATION>"
        )
    command = arguments[0]
    if command == "create" and len(arguments) == 3:
        profile = arguments[1]
        if profile != PROFILE:
            raise TenantSpecError(f"unsupported tenant profile: {profile}")
        spec = load_tenant_spec(
            Path(arguments[2]),
            expected_profile=PROFILE,
            supported_versions=supported_versions(root),
        )
        with _tenant_e2e_lock(root):
            with azure_lock(root, exclusive=True, create=True) as acquired:
                if not acquired:
                    raise RuntimeError(
                        "tenant profile mutation lock is unavailable"
                    )
                with tools_lock(root, exclusive=True):
                    operator.create_tenant(root, spec)
        return 0
    if command == "status" and len(arguments) == 3:
        profile = arguments[1]
        if profile != PROFILE:
            raise TenantSpecError(f"unsupported tenant profile: {profile}")
        validate_tenant_name(arguments[2])
        status = operator.status_tenant(root, arguments[2])
        print(status.to_json())
        return 0 if status.classification in {"ready", "absent"} else 1
    if command == "delete" and len(arguments) == 4:
        profile = arguments[1]
        if profile != PROFILE:
            raise TenantSpecError(f"unsupported tenant profile: {profile}")
        tenant = validate_tenant_name(arguments[2])
        expected = f"{PROFILE}/{tenant}"
        if arguments[3] != expected:
            raise RuntimeError(
                f"tenant deletion requires confirmation token {expected!r}"
            )
        with _tenant_e2e_lock(root):
            with azure_lock(root, exclusive=True, create=True) as acquired:
                if not acquired:
                    raise RuntimeError(
                        "tenant profile mutation lock is unavailable"
                    )
                with tools_lock(root, exclusive=True):
                    operator.delete_tenant(root, tenant)
        return 0
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
