from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.retained import (
    _delete_representative_tenant,
    dev_bootstrap,
    dev_clean,
    dev_test,
    dev_tenant,
    dev_up,
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
        with tempfile.TemporaryDirectory() as temporary:
            with (
                patch("scripts.retained.prepare_inotify", side_effect=lambda *_: calls.append("host")),
                patch("scripts.retained.create_management", side_effect=lambda *_: calls.append("management")),
                patch("scripts.retained.write_retained_state", side_effect=lambda *_: calls.append("state")),
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

    def test_dev_up_reconciles_without_deleting_tenant(self) -> None:
        calls: list[str] = []
        tenant = type("Tenant", (), {"name": "tenant-a"})()
        with (
            patch("scripts.retained.dev_bootstrap", side_effect=lambda *_: calls.append("bootstrap")),
            patch("scripts.retained.ManagementClient", return_value=object()),
            patch("scripts.retained.validate_create_inputs", return_value=[tenant]),
            patch("scripts.retained.reconcile_tenant", side_effect=lambda *_: calls.append("reconcile")),
            patch("scripts.retained.write_retained_state", side_effect=lambda *_: calls.append("state")),
        ):
            dev_up(Path("."), {})
        self.assertEqual(
            calls, ["bootstrap", "reconcile", "state"]
        )

    def test_dev_test_runs_selected_suite_without_reconciliation(self) -> None:
        tenant = type("Tenant", (), {"name": "tenant-a"})()
        with (
            patch("scripts.retained.validate_retained_state"),
            patch("scripts.retained.ManagementClient", return_value=object()),
            patch("scripts.retained.configured_tenants", return_value=[tenant]),
            patch("scripts.retained._dev_test_network") as network,
        ):
            dev_test(Path("."), {}, "network")
        network.assert_called_once()

    def test_dev_test_rejects_unknown_suite(self) -> None:
        tenant = type("Tenant", (), {"name": "tenant-a"})()
        with (
            patch("scripts.retained.validate_retained_state"),
            patch("scripts.retained.ManagementClient", return_value=object()),
            patch("scripts.retained.configured_tenants", return_value=[tenant]),
        ):
            with self.assertRaisesRegex(RuntimeError, "unknown retained test"):
                dev_test(Path("."), {}, "unknown")

    def test_partial_namespace_or_storage_record_blocks_recreation(self) -> None:
        tenant = type(
            "Tenant", (), {"name": "tenant-a", "namespace": "tenant-a"}
        )()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                patch(
                    "scripts.retained.verify_tenant_management_ownership",
                    return_value={"cluster": None, "namespace": {}},
                ),
                patch("scripts.retained.inspect_storage_volume", return_value=None),
            ):
                with self.assertRaisesRegex(RuntimeError, "partial"):
                    _delete_representative_tenant(root, {}, object(), tenant)
            record = root / ".runtime/storage/tenant-a/volume.json"
            record.parent.mkdir(parents=True)
            record.write_text("{}")
            with (
                patch(
                    "scripts.retained.verify_tenant_management_ownership",
                    return_value={"cluster": None, "namespace": None},
                ),
                patch("scripts.retained.inspect_storage_volume", return_value=None),
            ):
                with self.assertRaisesRegex(RuntimeError, "partial"):
                    _delete_representative_tenant(root, {}, object(), tenant)

    def test_cluster_absent_journal_is_finished_before_recreation(self) -> None:
        tenant = type(
            "Tenant", (), {"name": "tenant-a", "namespace": "tenant-a"}
        )()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            journal = root / ".runtime/deletions/tenant-a.json"
            journal.parent.mkdir(parents=True)
            journal.write_text("{}")
            with (
                patch(
                    "scripts.retained.verify_tenant_management_ownership",
                    return_value={"cluster": None},
                ),
                patch("scripts.retained.finish_journaled_tenant_deletion") as finish,
            ):
                _delete_representative_tenant(root, {}, object(), tenant)
            finish.assert_called_once()

    def test_invalid_or_dangling_journal_blocks_recreation(self) -> None:
        tenant = type(
            "Tenant", (), {"name": "tenant-a", "namespace": "tenant-a"}
        )()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            journal = root / ".runtime/deletions/tenant-a.json"
            journal.parent.mkdir(parents=True)
            journal.symlink_to(root / "missing")
            with (
                patch(
                    "scripts.retained.verify_tenant_management_ownership",
                    return_value={"cluster": None},
                ),
                patch(
                    "scripts.retained.finish_journaled_tenant_deletion",
                    side_effect=RuntimeError("invalid journal"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "invalid journal"):
                    _delete_representative_tenant(root, {}, object(), tenant)

    def test_other_owned_resource_blocks_recreation(self) -> None:
        tenant = type(
            "Tenant", (), {"name": "tenant-a", "namespace": "tenant-a"}
        )()
        with tempfile.TemporaryDirectory() as temporary:
            with patch(
                "scripts.retained.verify_tenant_management_ownership",
                return_value={"cluster": None, "devMachineTemplate": {}},
            ):
                with self.assertRaisesRegex(RuntimeError, "partial"):
                    _delete_representative_tenant(
                        Path(temporary), {}, object(), tenant
                    )

    def test_clean_routes_through_authoritative_destroy(self) -> None:
        with patch("scripts.retained.destroy") as destroy:
            dev_clean(Path("."), {})
        destroy.assert_called_once()


if __name__ == "__main__":
    unittest.main()
