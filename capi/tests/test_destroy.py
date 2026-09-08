from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts.destroy import destroy


class DestroyTests(unittest.TestCase):
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
