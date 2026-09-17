from __future__ import annotations

import json
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .files import write_private_file
from .redaction import redact
from .tenant_runtime import TenantRuntime


TENANT_PHASES = frozenset(
    {
        "validation",
        "foundation",
        "control-plane",
        "workers",
        "add-ons",
        "data-services",
        "deletion",
        "absence",
        "foundation-verification",
    }
)


class TenantTimings:
    def __init__(
        self,
        root: Path,
        *,
        profile: str,
        tenant: str,
        operation: str,
        operation_id: str,
    ) -> None:
        self.runtime = TenantRuntime(root, profile, tenant)
        self.profile = profile
        self.tenant = tenant
        self.operation = operation
        self.operation_id = operation_id
        self._records: list[dict[str, object]] = []

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        if name not in TENANT_PHASES:
            raise ValueError(f"unknown tenant lifecycle timing phase: {name}")
        if any(record["phase"] == name for record in self._records):
            raise RuntimeError(f"tenant lifecycle timing phase already recorded: {name}")
        started = time.monotonic()
        try:
            yield
        except BaseException as exc:
            self._records.append(
                {
                    "schema": 1,
                    "phase": name,
                    "status": "failed",
                    "seconds": round(time.monotonic() - started, 3),
                    "blocker": redact(str(exc)),
                }
            )
            raise
        else:
            self._records.append(
                {
                    "schema": 1,
                    "phase": name,
                    "status": "passed",
                    "seconds": round(time.monotonic() - started, 3),
                }
            )

    def records(self) -> list[dict[str, object]]:
        return [dict(record) for record in self._records]

    def emit(self) -> None:
        for record in self._records:
            payload = {
                **record,
                "profile": self.profile,
                "tenant": self.tenant,
                "operation": self.operation,
                "operationId": self.operation_id,
            }
            print(
                "TENANT_TIMING "
                + json.dumps(payload, sort_keys=True, separators=(",", ":"))
            )

    def persist(self) -> Path:
        path = (
            self.runtime.paths.evidence
            / f"{self.operation}-{self.operation_id}.json"
        )
        payload = {
            "schema": 1,
            "profile": self.profile,
            "tenant": self.tenant,
            "operation": self.operation,
            "operationId": self.operation_id,
            "records": self.records(),
        }
        write_private_file(path, json.dumps(payload, sort_keys=True) + "\n")
        return path
