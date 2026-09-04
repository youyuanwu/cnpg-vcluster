from __future__ import annotations

import sys
import unittest

from scripts.lib.process import CommandError, run


class ProcessTests(unittest.TestCase):
    def test_captures_failure_without_leaking_token(self) -> None:
        with self.assertRaises(CommandError) as context:
            run(
                [sys.executable, "-c", "import sys; print('token=abcdef.0123456789abcdef'); sys.exit(7)"],
                timeout=10,
            )
        self.assertEqual(context.exception.returncode, 7)
        self.assertNotIn("abcdef.0123456789abcdef", str(context.exception))

    def test_times_out(self) -> None:
        with self.assertRaises(CommandError) as context:
            run([sys.executable, "-c", "import time; time.sleep(2)"], timeout=1)
        self.assertEqual(context.exception.returncode, 124)

    def test_missing_executable_is_structured(self) -> None:
        with self.assertRaises(CommandError) as context:
            run(["/definitely/missing/capi-command"], timeout=1)
        self.assertEqual(context.exception.returncode, 126)
