from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from scripts.admin_metrics import (
    ADMIN_BASELINE_LINES,
    ADMIN_SOURCE_ROOTS,
    MAX_ADMIN_PRODUCTION_LINES,
    ROOT,
    main,
    production_lines,
    source_metrics,
)


class AdminMetricsTests(unittest.TestCase):
    def test_counts_through_first_test_only_boundary(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".runtime") as directory:
            source = Path(directory) / "sample.rs"
            source.write_text(
                "one\n\nthree\n#[cfg(test)]\nmod tests {}\n",
                encoding="utf-8",
            )
            self.assertEqual(3, production_lines(source))

    def test_counts_full_file_without_test_only_boundary(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".runtime") as directory:
            source = Path(directory) / "sample.rs"
            source.write_text("one\ntwo\n", encoding="utf-8")
            self.assertEqual(2, production_lines(source))

    def test_scope_baseline_and_ceiling_are_explicit(self) -> None:
        self.assertEqual(
            tuple(
                ROOT / "admin" / crate / "src"
                for crate in ("shared", "server", "web")
            ),
            ADMIN_SOURCE_ROOTS,
        )
        metrics = source_metrics()
        self.assertEqual(
            sorted(path for path, _ in metrics),
            [path for path, _ in metrics],
        )
        self.assertTrue(metrics)
        self.assertTrue(
            all(
                any(path.is_relative_to(root) for root in ADMIN_SOURCE_ROOTS)
                for path, _ in metrics
            )
        )
        total = sum(lines for _, lines in metrics)
        self.assertEqual(3916, ADMIN_BASELINE_LINES)
        self.assertEqual(6000, MAX_ADMIN_PRODUCTION_LINES)
        self.assertLessEqual(total, MAX_ADMIN_PRODUCTION_LINES)

    def test_output_reports_baseline_current_and_delta(self) -> None:
        output = StringIO()
        with (
            patch("scripts.admin_metrics.parse_args") as arguments,
            redirect_stdout(output),
        ):
            arguments.return_value.expect = None
            arguments.return_value.maximum = MAX_ADMIN_PRODUCTION_LINES
            self.assertEqual(0, main())
        rendered = output.getvalue()
        current = sum(lines for _, lines in source_metrics())
        delta = current - ADMIN_BASELINE_LINES
        self.assertIn(
            f"Admin Rust: baseline=3916 current={current} delta={delta:+d}",
            rendered,
        )


if __name__ == "__main__":
    unittest.main()
