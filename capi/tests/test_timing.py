from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch

from scripts.lib.timing import PHASES, PhaseTimings
from scripts.test_e2e import run_e2e
from scripts.create import reconcile_tenant


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
        with tempfile.TemporaryDirectory() as temporary:
            with (
                patch("scripts.test_e2e.ROOT", Path(temporary)),
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

    def test_final_cnpg_check_failure_marks_cnpg_phase(self) -> None:
        timings = PhaseTimings()
        tenant = type(
            "Tenant",
            (),
            {"name": "tenant-a", "namespace": "tenant-a", "cnpg_cluster": "postgres"},
        )()
        with (
            patch("scripts.create.tenant_kubeconfig_path") as kubeconfig,
            patch("scripts.create.stable_tenant_snapshot", return_value=None),
            patch("scripts.create.apply_control_plane"),
            patch("scripts.create.export_tenant_kubeconfig"),
            patch("scripts.create.apply_bootstrap_rbac"),
            patch("scripts.create.restore_host_images"),
            patch("scripts.create.apply_workers"),
            patch("scripts.create.preload_worker_images"),
            patch("scripts.create.apply_addons"),
            patch("scripts.create.wait_network_ready"),
            patch("scripts.create.verify_network"),
            patch("scripts.create.worker_snapshot"),
            patch("scripts.create.install_cnpg"),
            patch("scripts.create._write_marker"),
            patch(
                "scripts.create._verify_marker",
                side_effect=RuntimeError("injected final check"),
            ),
        ):
            kubeconfig.return_value.is_file.return_value = False
            with self.assertRaisesRegex(RuntimeError, "final check"):
                reconcile_tenant(
                    Path("."),
                    {},
                    object(),
                    tenant,
                    timings=timings,
                )
        records = {item["phase"]: item for item in timings.records()}
        self.assertEqual(records["tenant_control_plane"]["status"], "passed")
        self.assertEqual(records["tenant_workers_network"]["status"], "passed")
        self.assertEqual(records["cnpg_readiness_sql"]["status"], "failed")

    def test_primary_and_teardown_failures_preserve_both_results(self) -> None:
        calls = 0

        def run_just_failure(*args, **kwargs):
            nonlocal calls
            if args[-1] == "destroy":
                calls += 1
                if calls == 2:
                    raise RuntimeError("injected teardown failure")

        output = io.StringIO()
        with (
            patch("scripts.test_e2e.load_configuration", return_value={}),
            patch("scripts.test_e2e.read_inotify", return_value=1),
            patch("scripts.test_e2e.run_just", side_effect=run_just_failure),
            patch("scripts.test_e2e.verify_all_inputs"),
            patch("scripts.create.ManagementClient", return_value=object()),
            patch(
                "scripts.create.validate_create_inputs",
                side_effect=RuntimeError("injected primary failure"),
            ),
            redirect_stdout(output),
        ):
            with self.assertRaisesRegex(RuntimeError, "primary failure") as raised:
                run_e2e()
        self.assertTrue(
            any("teardown failure" in note for note in raised.exception.__notes__)
        )
        teardown = next(
            json.loads(line.removeprefix("CAPI_TIMING "))
            for line in output.getvalue().splitlines()
            if '"phase":"teardown"' in line
        )
        self.assertEqual(teardown["status"], "failed")


if __name__ == "__main__":
    unittest.main()
