from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.retained import (
    dev_bootstrap,
    dev_clean,
    retained_path,
    validate_retained_state,
)


class RetainedTests(unittest.TestCase):
    def test_missing_state_refuses_retained_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(RuntimeError, "dev-bootstrap"):
                validate_retained_state(Path(temporary), {})

    def test_stale_state_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = retained_path(root)
            path.parent.mkdir(mode=0o700)
            path.write_text(json.dumps({"schema": 1, "revision": "old"}))
            path.chmod(0o600)
            with (
                patch("scripts.retained.require_management_ownership"),
                patch("scripts.retained.validate_management_kubeconfig"),
                patch(
                    "scripts.retained._retained_payload",
                    return_value={"schema": 1, "revision": "new"},
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "stale"):
                    validate_retained_state(root, {})

    def test_bootstrap_prepares_host_before_management_and_state(self) -> None:
        calls: list[str] = []
        with tempfile.TemporaryDirectory() as temporary:
            with (
                patch(
                    "scripts.retained.prepare_inotify",
                    side_effect=lambda *_: calls.append("host"),
                ),
                patch(
                    "scripts.retained.create_management",
                    side_effect=lambda *_: calls.append("management"),
                ),
                patch(
                    "scripts.retained.write_retained_state",
                    side_effect=lambda *_: calls.append("state"),
                ),
            ):
                dev_bootstrap(Path(temporary), {})
        self.assertEqual(calls, ["host", "management", "state"])

    def test_bootstrap_refuses_unbound_existing_management(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = root / ".runtime/management/identity.json"
            identity.parent.mkdir(parents=True)
            identity.write_text("{}")
            with self.assertRaisesRegex(RuntimeError, "dev-clean"):
                dev_bootstrap(root, {})

    def test_dev_clean_runs_full_cleanup(self) -> None:
        with patch("scripts.retained.destroy") as destroy:
            dev_clean(Path("."), {"value": "config"})
        destroy.assert_called_once_with(Path("."), {"value": "config"})


if __name__ == "__main__":
    unittest.main()
