from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts.destroy import _remove_local_runtime, destroy, inspect_host_residue
from scripts.destroy_tenant import prepare_tenant_deletion


class DestroyTests(unittest.TestCase):
    def test_runtime_inventory_accepts_selected_dynamic_tenant(self) -> None:
        from scripts.destroy import _validate_runtime_inventory

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = (
                root
                / ".runtime"
                / "lifecycle"
                / "local"
                / "tenant-c"
                / "evidence"
                / "create-operation.json"
            )
            evidence.parent.mkdir(parents=True, mode=0o700)
            for parent in (
                root / ".runtime",
                root / ".runtime" / "lifecycle",
                root / ".runtime" / "lifecycle" / "local",
                root / ".runtime" / "lifecycle" / "local" / "tenant-c",
                evidence.parent,
            ):
                parent.chmod(0o700)
            evidence.write_text("{}\n", encoding="utf-8")
            evidence.chmod(0o600)
            _validate_runtime_inventory(root)
            unexpected = root / ".runtime" / "lifecycle" / "local" / "_invalid"
            unexpected.mkdir(mode=0o700)
            invalid_identity = unexpected / "identity.json"
            invalid_identity.write_text("{}\n")
            invalid_identity.chmod(0o600)
            with self.assertRaisesRegex(RuntimeError, "unexpected runtime"):
                _validate_runtime_inventory(root)

    def test_runtime_inventory_rejects_retired_dev_up_evidence(self) -> None:
        from scripts.destroy import _validate_runtime_inventory

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = root / ".runtime/evidence/dev-up-success.json"
            evidence.parent.mkdir(parents=True, mode=0o700)
            evidence.write_text("{}")
            evidence.chmod(0o600)
            with self.assertRaisesRegex(RuntimeError, "unexpected runtime"):
                _validate_runtime_inventory(root)

    def test_local_runtime_cleanup_preserves_azure_state(self) -> None:
        from scripts.destroy import _validate_runtime_inventory

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            azure = root / ".runtime" / "azure" / "resources.json"
            azure_ready = (
                root
                / ".runtime"
                / "lifecycle"
                / "azure"
                / "tenant-example"
                / "ready.json"
            )
            azure_gate = (
                root
                / ".runtime"
                / "azure-gate"
                / "evidence"
                / "lifecycle-operation.json"
            )
            local = root / ".runtime" / "management" / "identity.json"
            azure.parent.mkdir(parents=True)
            azure_ready.parent.mkdir(parents=True)
            azure_gate.parent.mkdir(parents=True)
            local.parent.mkdir(parents=True)
            azure.write_text('{"azure":true}\n')
            azure_ready.write_text('{"ready":true}\n')
            azure_gate.write_text('{"gate":true}\n')
            local.write_text('{"local":true}\n')
            for path in (
                root / ".runtime",
                root / ".runtime" / "azure",
                root / ".runtime" / "lifecycle",
                root / ".runtime" / "lifecycle" / "azure",
                azure_ready.parent,
                root / ".runtime" / "azure-gate",
                azure_gate.parent,
                local.parent,
            ):
                path.chmod(0o700)
            azure.chmod(0o600)
            azure_ready.chmod(0o600)
            azure_gate.chmod(0o600)
            local.chmod(0o600)
            _validate_runtime_inventory(root)
            _remove_local_runtime(root)
            self.assertTrue(azure.is_file())
            self.assertTrue(azure_ready.is_file())
            self.assertTrue(azure_gate.is_file())
            self.assertFalse(local.exists())

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
                patch(
                    "scripts.destroy.recorded_tenant_names",
                    return_value=("tenant-a", "tenant-b"),
                ),
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
                        "registries": [],
                        "volumes": [],
                    },
                ),
                patch("scripts.destroy.delete_offline_registry"),
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
            Mock(stdout="probe-a\n", returncode=0, stderr=""),
            Mock(stdout="registry-a\n", returncode=0, stderr=""),
            Mock(stdout="lab-tenant-a-storage\n", returncode=0, stderr=""),
        ]
        with patch("scripts.destroy.run", side_effect=responses):
            residue = inspect_host_residue(
                {
                    "SPIKE_NAME": "spike",
                    "OWNERSHIP_LABEL": "example.owner",
                    "LAB_PREFIX": "lab",
                },
                ("tenant-a",),
            )
        self.assertEqual(residue["containers"], ["worker-a"])
        self.assertEqual(residue["probes"], ["probe-a"])
        self.assertEqual(residue["registries"], ["registry-a"])
        self.assertEqual(residue["volumes"], ["lab-tenant-a-storage"])

    def test_host_residue_rejects_volume_inspection_failure(self) -> None:
        responses = [
            Mock(stdout="", returncode=0, stderr=""),
            Mock(stdout="", returncode=0, stderr=""),
            Mock(stdout="", returncode=0, stderr=""),
            Mock(stdout="", returncode=0, stderr=""),
            RuntimeError("Docker volume inspection failed"),
        ]
        with patch("scripts.destroy.run", side_effect=responses):
            with self.assertRaisesRegex(RuntimeError, "inspection failed"):
                inspect_host_residue(
                    {
                        "SPIKE_NAME": "spike",
                        "OWNERSHIP_LABEL": "example.owner",
                        "LAB_PREFIX": "lab",
                    },
                    ("tenant-a",),
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
                patch(
                    "scripts.destroy.recorded_tenant_names",
                    return_value=("tenant-a", "tenant-b"),
                ),
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
                    "scripts.destroy.recorded_local_tenants",
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
                patch("scripts.destroy.delete_offline_registry"),
                patch("scripts.destroy.delete_management"),
                patch("scripts.destroy.restore_inotify"),
            ):
                destroy(
                    root,
                    {},
                )
            self.assertEqual(
                [call.args[3].name for call in delete_tenant.call_args_list],
                ["spike", "tenant-a"],
            )
            finish_journaled.assert_called_once()
            self.assertEqual(finish_journaled.call_args.args[3].name, "tenant-b")
