from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts.lib.timing import PHASES, PhaseTimings
from scripts.test_e2e import run_e2e, verify_no_local_runtime_residue


class TimingTests(unittest.TestCase):
    def test_e2e_runtime_check_preserves_only_azure_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            azure = root / ".runtime/azure/resources.json"
            lifecycle = (
                root
                / ".runtime/lifecycle/azure/tenant-example/ready.json"
            )
            gate = (
                root
                / ".runtime/azure-gate/evidence/lifecycle-operation.json"
            )
            for path in (azure, lifecycle, gate):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}\n", encoding="utf-8")
            verify_no_local_runtime_residue(root)
            local = root / ".runtime/tenant-endpoints.json"
            local.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "tenant-endpoints"):
                verify_no_local_runtime_residue(root)

    def test_records_passed_failed_and_skipped_without_error_text(self) -> None:
        values = iter((1.0, 2.25, 3.0, 3.5))
        timings = PhaseTimings()
        with patch(
            "scripts.lib.timing.time.monotonic",
            side_effect=lambda: next(values),
        ):
            with timings.phase("tools_cache"):
                pass
            with self.assertRaises(RuntimeError):
                with timings.phase("initial_cleanup"):
                    raise RuntimeError("******")
        records = timings.records()
        self.assertEqual([item["phase"] for item in records], list(PHASES))
        self.assertEqual(records[0]["status"], "passed")
        self.assertEqual(records[1]["status"], "failed")
        self.assertTrue(
            all(item["status"] == "skipped" for item in records[2:])
        )
        self.assertNotIn("secret", json.dumps(records))

    def test_emit_produces_eight_structured_records(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            PhaseTimings().emit()
        payloads = [
            json.loads(line.removeprefix("CAPI_TIMING "))
            for line in output.getvalue().splitlines()
        ]
        self.assertEqual([item["phase"] for item in payloads], list(PHASES))

    @staticmethod
    def _root(temporary: str) -> Path:
        root = Path(temporary)
        spec = root / "config" / "tenants" / "examples" / "local.json"
        spec.parent.mkdir(parents=True)
        spec.write_text('{"name":"tenant-example"}\n')
        return root

    def test_e2e_tenant_create_failure_still_runs_teardown(self) -> None:
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            root = self._root(temporary)

            def run_just(*args, **_kwargs):
                if "tenant-create" in args:
                    raise RuntimeError("injected tenant setup failure")

            with (
                patch("scripts.test_e2e.ROOT", root),
                patch("scripts.test_e2e.load_configuration", return_value={}),
                patch("scripts.test_e2e.read_inotify", return_value=1),
                patch("scripts.test_e2e.run_just", side_effect=run_just),
                patch("scripts.test_e2e.verify_all_inputs"),
                patch("scripts.test_e2e.verify_no_lab_residue"),
                redirect_stdout(output),
            ):
                with self.assertRaisesRegex(RuntimeError, "tenant setup failure"):
                    run_e2e()
        teardown = next(
            json.loads(line.removeprefix("CAPI_TIMING "))
            for line in output.getvalue().splitlines()
            if '"phase":"teardown"' in line
        )
        self.assertEqual(teardown["status"], "passed")

    def test_primary_and_teardown_failures_preserve_both_results(self) -> None:
        calls = 0
        with tempfile.TemporaryDirectory() as temporary:
            root = self._root(temporary)

            def run_just_failure(*args, **_kwargs):
                nonlocal calls
                if "tenant-create" in args:
                    raise RuntimeError("injected primary failure")
                if args[-1] == "destroy":
                    calls += 1
                    if calls == 2:
                        raise RuntimeError("injected teardown failure")

            with (
                patch("scripts.test_e2e.ROOT", root),
                patch("scripts.test_e2e.load_configuration", return_value={}),
                patch("scripts.test_e2e.read_inotify", return_value=1),
                patch(
                    "scripts.test_e2e.run_just",
                    side_effect=run_just_failure,
                ),
                patch("scripts.test_e2e.verify_all_inputs"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "primary failure",
                ) as raised:
                    run_e2e()
        self.assertTrue(
            any(
                "teardown failure" in note
                for note in raised.exception.__notes__
            )
        )


if __name__ == "__main__":
    unittest.main()
