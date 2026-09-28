from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.controller_metrics import (
    MAX_PRODUCTION_LINES,
    production_lines,
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
        self.assertGreater(total, 8050)
        self.assertLessEqual(total, MAX_PRODUCTION_LINES)


if __name__ == "__main__":
    unittest.main()
