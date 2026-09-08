from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from scripts.lib.timing import PHASES, PhaseTimings


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


if __name__ == "__main__":
    unittest.main()
