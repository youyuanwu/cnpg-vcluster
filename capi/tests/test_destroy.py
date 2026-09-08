from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts.destroy import destroy, inspect_host_residue
from scripts.destroy_tenant import prepare_tenant_deletion


class DestroyTests(unittest.TestCase):
    def test_dangling_deletion_journal_blocks_live_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            journal = root / ".runtime/deletions/tenant-a.json"
            journal.parent.mkdir(parents=True)
            journal.symlink_to(root / "missing")
            tenant = type("Tenant", (), {"name": "tenant-a"})()
            with (
                patch("scripts.destroy_tenant.delete_cnpg") as delete_cnpg,
                patch("scripts.destroy_tenant.delete_addons") as delete_addons,
            ):
                with self.assertRaises(RuntimeError):
                    prepare_tenant_deletion(
                        root,
                        {},
                        object(),
                        tenant,
                        {"metadata": {"uid": "cluster-uid"}},
                    )
            delete_cnpg.assert_not_called()
            delete_addons.assert_not_called()

    def test_management_absent_residue_preserves_runtime_and_host_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / ".runtime"
            runtime.mkdir(mode=0o700)
            with (
                patch("scripts.destroy._validate_runtime_inventory"),
                patch("scripts.destroy.validate_inotify_state"),
                patch(
                    "scripts.destroy.management_status",
                    return_value={},
                ),
                patch(
                    "scripts.destroy.inspect_host_residue",
                    return_value={
                        "containers": ["worker"],
                        "probes": [],
                        "volumes": [],
                    },
                ),
                patch("scripts.destroy.restore_inotify") as restore,
            ):
                with self.assertRaisesRegex(RuntimeError, "residue remains"):
                    destroy(root, {})
            restore.assert_not_called()
            self.assertTrue(runtime.exists())

    def test_host_residue_reports_workers_probes_and_volumes(self) -> None:
        responses = [
            Mock(stdout="worker-a\n", returncode=0, stderr=""),
            Mock(stdout="", returncode=0, stderr=""),
            Mock(stdout="", returncode=0, stderr=""),
            Mock(stdout="probe-a\n", returncode=0, stderr=""),
            Mock(stdout="", returncode=1, stderr="No such volume"),
            Mock(stdout="[]", returncode=0, stderr=""),
            Mock(stdout="", returncode=1, stderr="No such volume"),
        ]
        with patch("scripts.destroy.run", side_effect=responses):
            residue = inspect_host_residue(
                {
                    "SPIKE_NAME": "spike",
                    "TENANT_NAMES": "tenant-a tenant-b",
                    "OWNERSHIP_LABEL": "example.owner",
                    "LAB_PREFIX": "lab",
                }
            )
        self.assertEqual(residue["containers"], ["worker-a"])
        self.assertEqual(residue["probes"], ["probe-a"])
        self.assertEqual(residue["volumes"], ["lab-tenant-a-storage"])

    def test_host_residue_rejects_volume_inspection_failure(self) -> None:
        responses = [
            Mock(stdout="", returncode=0, stderr=""),
            Mock(stdout="", returncode=0, stderr=""),
            Mock(stdout="", returncode=0, stderr=""),
            Mock(stdout="", returncode=0, stderr=""),
            Mock(stdout="", returncode=1, stderr="daemon unavailable"),
        ]
        with patch("scripts.destroy.run", side_effect=responses):
            with self.assertRaisesRegex(RuntimeError, "inspection failed"):
                inspect_host_residue(
                    {
                        "SPIKE_NAME": "spike",
                        "TENANT_NAMES": "tenant-a tenant-b",
                        "OWNERSHIP_LABEL": "example.owner",
                        "LAB_PREFIX": "lab",
                    }
                )

    def test_global_destroy_resumes_second_tenant_journal_without_survivor(self) -> None:
        spike = type("Tenant", (), {"name": "spike"})()
        tenant_a = type("Tenant", (), {"name": "tenant-a"})()
        tenant_b = type("Tenant", (), {"name": "tenant-b"})()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            journal = root / ".runtime" / "deletions" / "tenant-b.json"
            journal.parent.mkdir(parents=True)
            journal.write_text("{}\n", encoding="utf-8")
            client = Mock()
            client.kubectl.return_value = Mock(
                returncode=0, stdout="", stderr=""
            )
            with (
                patch("scripts.destroy._validate_runtime_inventory"),
                patch("scripts.destroy.validate_inotify_state"),
                patch(
                    "scripts.destroy.management_status",
                    return_value={
                        "apiReady": True,
                        "clusterReported": True,
                    },
                ),
                patch("scripts.destroy.require_management_ownership"),
                patch("scripts.destroy.validate_management_kubeconfig"),
                patch("scripts.destroy.ManagementClient", return_value=client),
                patch("scripts.destroy.spike_tenant", return_value=spike),
                patch(
                    "scripts.destroy.configured_tenants",
                    return_value=[tenant_a, tenant_b],
                ),
                patch(
                    "scripts.destroy.inspect_management_resource",
                    return_value=None,
                ),
                patch("scripts.destroy.delete_tenant") as delete_tenant,
                patch(
                    "scripts.destroy_tenant.finish_journaled_tenant_deletion"
                ) as finish_journaled,
                patch("scripts.destroy._delete_kubernetes_stack"),
                patch("scripts.destroy.delete_management"),
                patch("scripts.destroy.restore_inotify"),
            ):
                destroy(
                    root,
                    {"TENANT_NAMES": "tenant-a tenant-b"},
                )
            self.assertEqual(
                [call.args[3].name for call in delete_tenant.call_args_list],
                ["spike", "tenant-a"],
            )
            finish_journaled.assert_called_once()
            self.assertEqual(finish_journaled.call_args.args[3].name, "tenant-b")
