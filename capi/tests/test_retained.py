from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.retained import (
    dev_bootstrap,
    dev_clean,
    dev_tenant,
    retained_path,
    validate_retained_state,
)


class RetainedTests(unittest.TestCase):
    def test_missing_state_refuses_tenant_loop(self) -> None:
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
                patch("scripts.retained._retained_payload", return_value={"schema": 1, "revision": "new"}),
            ):
                with self.assertRaisesRegex(RuntimeError, "stale"):
                    validate_retained_state(root, {})

    def test_broad_or_symlinked_state_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = retained_path(root)
            path.parent.mkdir(mode=0o700)
            path.write_text("{}")
            path.chmod(0o644)
            with self.assertRaises(RuntimeError):
                validate_retained_state(root, {})
            path.unlink()
            target = root / "target"
            target.write_text("{}")
            target.chmod(0o600)
            path.symlink_to(target)
            with self.assertRaises(RuntimeError):
                validate_retained_state(root, {})

    def test_bootstrap_prepares_host_before_management_and_state(self) -> None:
        calls: list[str] = []
        with (
            patch("scripts.retained.prepare_inotify", side_effect=lambda *_: calls.append("host")),
            patch("scripts.retained.create_management", side_effect=lambda *_: calls.append("management")),
            patch("scripts.retained.write_retained_state", side_effect=lambda *_: calls.append("state")),
        ):
            dev_bootstrap(Path("."), {})
        self.assertEqual(calls, ["host", "management", "state"])

    def test_bootstrap_refuses_unbound_existing_management(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = root / ".runtime/management/identity.json"
            identity.parent.mkdir(parents=True)
            identity.write_text("{}")
            with self.assertRaisesRegex(RuntimeError, "dev-clean"):
                dev_bootstrap(root, {})

    def test_tenant_loop_validates_deletes_recreates_and_rebinds(self) -> None:
        calls: list[str] = []
        tenant = type("Tenant", (), {"name": "tenant-a"})()
        with (
            patch("scripts.retained.validate_retained_state", side_effect=lambda *_: calls.append("validate")),
            patch("scripts.retained.verify_all_inputs", side_effect=lambda *_: calls.append("inputs")),
            patch("scripts.retained.ManagementClient", return_value=object()),
            patch("scripts.retained.validate_create_inputs", return_value=[tenant]),
            patch("scripts.retained._delete_representative_tenant", side_effect=lambda *_: calls.append("delete")),
            patch("scripts.retained.reconcile_tenant", side_effect=lambda *_: calls.append("recreate")),
            patch("scripts.retained.write_retained_state", side_effect=lambda *_: calls.append("state")),
        ):
            dev_tenant(Path("."), {})
        self.assertEqual(calls, ["validate", "inputs", "delete", "recreate", "state"])

    def test_clean_routes_through_authoritative_destroy(self) -> None:
        with patch("scripts.retained.destroy") as destroy:
            dev_clean(Path("."), {})
        destroy.assert_called_once()


if __name__ == "__main__":
    unittest.main()
