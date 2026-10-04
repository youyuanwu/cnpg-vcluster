from __future__ import annotations

import sys
import unittest
import os
from subprocess import CompletedProcess
from unittest.mock import patch

from scripts.lib.process import CommandError, run


class ProcessTests(unittest.TestCase):
    def test_offline_mode_blocks_acquisition_commands(self) -> None:
        with patch.dict(os.environ, {"CAPI_OFFLINE_ENFORCED": "1"}):
            for command in (
                ["git", "ls-remote", "https://example.invalid/repo"],
                ["docker", "pull", "example/image:v1"],
                ["docker", "buildx", "imagetools", "inspect", "example/image:v1"],
                ["/tmp/helm", "pull", "oci://example/chart"],
            ):
                with self.assertRaisesRegex(CommandError, "offline enforcement"):
                    run(command, timeout=1)

    def test_offline_mode_injects_docker_pull_never(self) -> None:
        result = CompletedProcess([], 0, stdout="", stderr="")
        with (
            patch.dict(os.environ, {"CAPI_OFFLINE_ENFORCED": "1"}),
            patch("scripts.lib.process.subprocess.run", return_value=result) as subprocess_run,
        ):
            run(["docker", "run", "--rm", "example/image:v1"], timeout=1)
        self.assertEqual(
            subprocess_run.call_args.args[0][:3],
            ["docker", "run", "--pull=never"],
        )
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

    def test_redacts_whitespace_containing_secret_argument(self) -> None:
        error = CommandError(
            ("example", "--token", "secret value with spaces", "--flag"),
            1,
            "",
        )
        rendered = str(error)
        self.assertNotIn("secret value with spaces", rendered)
        self.assertNotIn("with spaces", rendered)
