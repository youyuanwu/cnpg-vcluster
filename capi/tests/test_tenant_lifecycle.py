from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts.destroy_tenant import (
    _tenant_resource,
    destroy_tenant_stack,
    prepare_tenant_deletion,
)
from scripts.create import require_no_pending_deletions
from scripts.lib.files import IntegrityError
from scripts.lib.tenants import (
    inspect_management_resource,
    verify_tenant_management_ownership,
)
from scripts.repair import repair


def result(returncode: int, stderr: str = "", stdout: str = ""):
    return type(
        "Result",
        (),
        {"returncode": returncode, "stderr": stderr, "stdout": stdout},
    )()


class TenantLifecycleTests(unittest.TestCase):
    def test_repair_input_failure_precedes_tenant_mutation(self) -> None:
        with (
            patch(
                "scripts.repair.verify_all_inputs",
                side_effect=IntegrityError("injected repair input tamper"),
            ),
            patch("scripts.repair.select_tenant") as select_tenant,
            patch("scripts.repair.reconcile_tenant") as reconcile_tenant,
        ):
            with self.assertRaises(IntegrityError):
                repair(Path("."), {}, "tenant-a")
        select_tenant.assert_not_called()
        reconcile_tenant.assert_not_called()

    def test_management_inspection_distinguishes_not_found_and_failure(self) -> None:
        tenant = type(
            "Tenant", (), {"namespace": "tenant-a", "name": "tenant-a"}
        )()
        client = Mock()
        client.kubectl.return_value = result(
            1,
            'Error from server (NotFound): clusters.cluster.x-k8s.io "tenant-a" not found',
        )
        self.assertIsNone(
            inspect_management_resource(client, tenant, "cluster/tenant-a")
        )
        client.kubectl.return_value = result(
            1, "lookup management API: host not found"
        )
        with self.assertRaisesRegex(RuntimeError, "inspection failed"):
            inspect_management_resource(client, tenant, "cluster/tenant-a")

    def test_tenant_api_inspection_distinguishes_not_found_and_failure(self) -> None:
        tenant = type("Tenant", (), {"name": "tenant-a"})()
        with patch(
            "scripts.destroy_tenant._tenant_kubectl",
            return_value=result(
                1,
                'Error from server (NotFound): deployments.apps "item" not found',
            ),
        ):
            self.assertIsNone(
                _tenant_resource(
                    Path("."), {}, tenant, "get", "deployment/item"
                )
            )
        with patch(
            "scripts.destroy_tenant._tenant_kubectl",
            return_value=result(1, "connection refused"),
        ):
            with self.assertRaisesRegex(RuntimeError, "inspection failed"):
                _tenant_resource(
                    Path("."), {}, tenant, "get", "deployment/item"
                )

    def test_management_ownership_rejects_foreign_cluster(self) -> None:
        tenant = type(
            "Tenant", (), {"namespace": "tenant-a", "name": "tenant-a"}
        )()
        client = Mock()
        namespace = {"metadata": {"labels": {"example.owner": "lab"}}}
        foreign = {"metadata": {"labels": {}}}
        not_found = result(
            1,
            'Error from server (NotFound): resource "item" not found',
        )
        client.kubectl.side_effect = [
            result(0, stdout=json.dumps(namespace)),
            result(0, stdout=json.dumps(foreign)),
            *([not_found] * 5),
        ]
        with self.assertRaisesRegex(RuntimeError, "ownership mismatch"):
            verify_tenant_management_ownership(
                {"OWNERSHIP_LABEL": "example.owner", "LAB_PREFIX": "lab"},
                client,
                tenant,
            )

    def test_existing_journal_must_match_live_cluster_uid(self) -> None:
        tenant = type("Tenant", (), {"name": "tenant-a"})()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            journal = root / ".runtime" / "deletions" / "tenant-a.json"
            journal.parent.mkdir(parents=True)
            journal.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "tenant": "tenant-a",
                        "clusterUID": "old",
                        "phase": "api-cleanup-complete",
                    }
                ),
                encoding="utf-8",
            )
            journal.chmod(0o600)
            with self.assertRaisesRegex(RuntimeError, "UID mismatch"):
                prepare_tenant_deletion(
                    root,
                    {},
                    object(),
                    tenant,
                    {"metadata": {"uid": "new"}},
                )

    def test_matching_journal_skips_repeated_live_api_cleanup(self) -> None:
        tenant = type("Tenant", (), {"name": "tenant-a"})()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            journal = root / ".runtime" / "deletions" / "tenant-a.json"
            journal.parent.mkdir(parents=True)
            journal.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "tenant": "tenant-a",
                        "clusterUID": "same",
                        "phase": "api-cleanup-complete",
                    }
                ),
                encoding="utf-8",
            )
            journal.chmod(0o600)
            with patch(
                "scripts.destroy_tenant.cnpg_artifacts_present"
            ) as cnpg_present:
                prepare_tenant_deletion(
                    root,
                    {},
                    object(),
                    tenant,
                    {"metadata": {"uid": "same"}},
                )
            cnpg_present.assert_not_called()

    def test_pending_deletion_blocks_reconciliation(self) -> None:
        tenant = type("Tenant", (), {"name": "tenant-a"})()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            journal = root / ".runtime" / "deletions" / "tenant-a.json"
            journal.parent.mkdir(parents=True)
            journal.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "pending tenant deletion"):
                require_no_pending_deletions(root, [tenant])

    def test_repeated_targeted_deletion_needs_no_journal(self) -> None:
        tenant = type(
            "Tenant",
            (),
            {"name": "tenant-a", "namespace": "tenant-a"},
        )()
        survivor = type("Tenant", (), {"name": "tenant-b"})()
        snapshot = {"survivor": "stable"}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                patch("scripts.destroy_tenant.verify_all_inputs"),
                patch(
                    "scripts.destroy_tenant._selected_tenants",
                    return_value=(tenant, survivor),
                ),
                patch("scripts.destroy_tenant.require_management_ownership"),
                patch("scripts.destroy_tenant.validate_management_kubeconfig"),
                patch("scripts.destroy_tenant.ManagementClient"),
                patch(
                    "scripts.destroy_tenant.verified_tenant_snapshot",
                    return_value=snapshot,
                ),
                patch(
                    "scripts.destroy_tenant.verify_tenant_management_ownership",
                    return_value={},
                ),
                patch(
                    "scripts.destroy_tenant.inspect_storage_volume",
                    return_value=None,
                ),
                patch("scripts.destroy_tenant._verify_deleted") as verify_deleted,
                patch(
                    "scripts.destroy_tenant.finish_prepared_tenant_deletion"
                ) as finish,
            ):
                destroy_tenant_stack(root, {"LAB_PREFIX": "lab"}, tenant.name)
            verify_deleted.assert_called_once()
            finish.assert_not_called()
