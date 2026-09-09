from __future__ import annotations

import json
import time
from contextlib import contextmanager


PHASES = (
    "tools_cache",
    "initial_cleanup",
    "host_preparation",
    "management_bootstrap",
    "tenant_control_plane",
    "tenant_workers_network",
    "cnpg_readiness_sql",
    "teardown",
)


class PhaseTimings:
    def __init__(self) -> None:
        self._records: dict[str, dict[str, object]] = {}

    @contextmanager
    def phase(self, name: str):
        if name not in PHASES:
            raise ValueError(f"unknown lifecycle timing phase: {name}")
        if name in self._records:
            raise RuntimeError(f"lifecycle timing phase already recorded: {name}")
        started = time.monotonic()
        try:
            yield
        except BaseException:
            self._records[name] = {
                "schema": 1,
                "phase": name,
                "status": "failed",
                "seconds": round(time.monotonic() - started, 3),
            }
            raise
        else:
            self._records[name] = {
                "schema": 1,
                "phase": name,
                "status": "passed",
                "seconds": round(time.monotonic() - started, 3),
            }

    def records(self) -> list[dict[str, object]]:
        return [
            self._records.get(
                name,
                {"schema": 1, "phase": name, "status": "skipped", "seconds": 0.0},
            )
            for name in PHASES
        ]

    def emit(self) -> None:
        for record in self.records():
            print(
                "CAPI_TIMING "
                + json.dumps(record, sort_keys=True, separators=(",", ":"))
            )
