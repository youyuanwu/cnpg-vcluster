from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.controller_metrics import (
    MAX_PRODUCTION_LINES,
    PYTHON_BASELINE_LINES,
    PYTHON_SRC,
    ROOT,
    RUST_BASELINE_LINES,
    main,
    production_lines,
    python_source_metrics,
    source_metrics,
)


class ControllerMetricsTests(unittest.TestCase):
    def test_counts_through_first_test_only_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "sample.rs"
            source.write_text(
                "one\n\nthree\n#[cfg(test)]\nmod tests {}\n",
                encoding="utf-8",
            )
            self.assertEqual(3, production_lines(source))

    def test_counts_full_file_without_test_only_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "sample.rs"
            source.write_text("one\ntwo\n", encoding="utf-8")
            self.assertEqual(2, production_lines(source))

    def test_current_production_ceiling_is_explicit(self) -> None:
        metrics = source_metrics()
        self.assertEqual(sorted(path for path, _ in metrics), [path for path, _ in metrics])
        total = sum(lines for _, lines in metrics)
        self.assertEqual(12000, MAX_PRODUCTION_LINES)
        self.assertEqual(8049, RUST_BASELINE_LINES)
        self.assertEqual(25094, PYTHON_BASELINE_LINES)
        self.assertGreater(total, RUST_BASELINE_LINES)
        self.assertLessEqual(total, MAX_PRODUCTION_LINES)

    def test_python_scope_is_explicit_and_recursive(self) -> None:
        metrics = python_source_metrics()
        self.assertEqual(ROOT / "scripts", PYTHON_SRC)
        self.assertTrue(metrics)
        self.assertTrue(all(path.suffix == ".py" for path, _ in metrics))
        self.assertTrue(
            all(path.is_relative_to(ROOT / "scripts") for path, _ in metrics)
        )

    def test_metric_output_reports_both_reviewed_baselines(self) -> None:
        from contextlib import redirect_stdout
        from io import StringIO
        from unittest.mock import patch

        output = StringIO()
        with (
            patch("scripts.controller_metrics.parse_args") as arguments,
            redirect_stdout(output),
        ):
            arguments.return_value.expect = None
            arguments.return_value.maximum = MAX_PRODUCTION_LINES
            self.assertEqual(0, main())
        rendered = output.getvalue()
        self.assertIn("Rust: baseline=8049", rendered)
        self.assertIn(
            "Python (scripts/**/*.py): baseline=25094",
            rendered,
        )
        self.assertIn("Combined Rust/Python net delta:", rendered)


if __name__ == "__main__":
    unittest.main()
