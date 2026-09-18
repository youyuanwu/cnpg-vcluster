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
        "journal",
        "control-plane",
        "workers",
        "add-ons",
        "data-services",
        "deletion",
        "deletion-validation",
        "controller-cleanup",
        "orchestration-cleanup",
        "azure-absence",
        "runtime-cleanup",
        "absence",
        "foundation-verification",
        "recreation",
        "operation",
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
        self._started = time.monotonic()

    def bind_operation_id(self, operation_id: str) -> None:
        self.operation_id = operation_id

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

    def record_passed(self, name: str, seconds: float) -> None:
        if name not in TENANT_PHASES:
            raise ValueError(f"unknown tenant lifecycle timing phase: {name}")
        if any(record["phase"] == name for record in self._records):
            raise RuntimeError(f"tenant lifecycle timing phase already recorded: {name}")
        self._records.append(
            {
                "schema": 1,
                "phase": name,
                "status": "passed",
                "seconds": round(seconds, 3),
            }
        )

    def record_failure(self, name: str, error: BaseException) -> None:
        if any(record["status"] == "failed" for record in self._records):
            return
        if name not in TENANT_PHASES:
            raise ValueError(f"unknown tenant lifecycle timing phase: {name}")
        self._records.append(
            {
                "schema": 1,
                "phase": name,
                "status": "failed",
                "seconds": round(time.monotonic() - self._started, 3),
                "blocker": redact(str(error)),
            }
        )

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


def record_rejected_create(
    root: Path,
    *,
    profile: str,
    operation_id: str,
    seconds: float,
    error: BaseException,
) -> None:
    record = {
        "schema": 1,
        "profile": profile,
        "tenant": None,
        "operation": "create",
        "operationId": operation_id,
        "records": [
            {
                "schema": 1,
                "phase": "validation",
                "status": "failed",
                "seconds": round(seconds, 3),
                "blocker": redact(str(error)),
            }
        ],
    }
    print(
        "TENANT_TIMING "
        + json.dumps(
            {
                **record["records"][0],
                "profile": profile,
                "tenant": None,
                "operation": "create",
                "operationId": operation_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    write_private_file(
        root
        / ".runtime"
        / "lifecycle"
        / "rejected"
        / profile
        / f"create-{operation_id}.json",
        json.dumps(record, sort_keys=True) + "\n",
    )
