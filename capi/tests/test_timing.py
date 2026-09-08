from __future__ import annotations

import io
import json
import unittest
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch

from scripts.lib.timing import PHASES, PhaseTimings
from scripts.test_e2e import run_e2e


class TimingTests(unittest.TestCase):
    def test_records_passed_failed_and_skipped_without_error_text(self) -> None:
        values = iter((1.0, 2.25, 3.0, 3.5))
        timings = PhaseTimings()
        with patch("scripts.lib.timing.time.monotonic", side_effect=lambda: next(values)):
            with timings.phase("tools_cache"):
                pass
            with self.assertRaisesRegex(RuntimeError, "password=secret"):
                with timings.phase("initial_cleanup"):
                    raise RuntimeError("password=secret")
        records = timings.records()
        self.assertEqual([item["phase"] for item in records], list(PHASES))
        self.assertEqual(records[0]["status"], "passed")
        self.assertEqual(records[1]["status"], "failed")
        self.assertTrue(all(item["status"] == "skipped" for item in records[2:]))
        self.assertNotIn("secret", json.dumps(records))

    def test_emit_produces_eight_structured_records(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            PhaseTimings().emit()
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 8)
        payloads = [json.loads(line.removeprefix("CAPI_TIMING ")) for line in lines]
        self.assertEqual([item["phase"] for item in payloads], list(PHASES))

    def test_e2e_tenant_setup_failure_marks_phase_and_runs_teardown(self) -> None:
        output = io.StringIO()
        config = {}
        with (
            patch("scripts.test_e2e.load_configuration", return_value=config),
            patch("scripts.test_e2e.read_inotify", return_value=1),
            patch("scripts.test_e2e.run_just"),
            patch("scripts.test_e2e.verify_all_inputs"),
            patch("scripts.test_e2e.verify_no_lab_residue"),
            patch("scripts.create.ManagementClient", return_value=object()),
            patch(
                "scripts.create.validate_create_inputs",
                side_effect=RuntimeError("injected tenant setup failure"),
            ),
            redirect_stdout(output),
        ):
            with self.assertRaisesRegex(RuntimeError, "tenant setup failure"):
                run_e2e()
        records = {
            item["phase"]: item
            for item in (
                json.loads(line.removeprefix("CAPI_TIMING "))
                for line in output.getvalue().splitlines()
                if line.startswith("CAPI_TIMING ")
            )
        }
        self.assertEqual(records["tenant_control_plane"]["status"], "failed")
        self.assertEqual(records["tenant_workers_network"]["status"], "skipped")
        self.assertEqual(records["cnpg_readiness_sql"]["status"], "skipped")
        self.assertEqual(records["teardown"]["status"], "passed")


if __name__ == "__main__":
    unittest.main()
