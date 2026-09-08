from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.create import create, reconcile_tenant
from scripts.lib.files import IntegrityError


class CreateTests(unittest.TestCase):
    def test_input_failure_precedes_tenant_reconciliation(self) -> None:
        config = {
            "TENANT_COMPATIBILITY_REVISION": "capi-kamaji-two-tenant-v1",
            "CNPG_COMPATIBILITY_REVISION": "docker-volume-hostpath-v1",
        }
        with (
            patch("scripts.create.verify_all_inputs"),
            patch("scripts.create.create_management"),
            patch(
                "scripts.create.validate_create_inputs",
                side_effect=IntegrityError("injected tenant-b input tamper"),
            ),
            patch("scripts.create.reconcile_tenant") as reconcile,
        ):
            with self.assertRaises(IntegrityError):
                create(Path("."), config)
        reconcile.assert_not_called()

    def test_verified_inputs_precede_management_mutation(self) -> None:
        with (
            patch(
                "scripts.create.verify_all_inputs",
                side_effect=IntegrityError("injected full-create tamper"),
            ),
            patch("scripts.create.create_management") as create_management,
        ):
            with self.assertRaises(IntegrityError):
                create(Path("."), {})
        create_management.assert_not_called()

    def test_reconcile_rejects_database_inspection_dns_failure_before_mutation(self) -> None:
        tenant = type(
            "Tenant",
            (),
            {"name": "tenant-a", "namespace": "tenant-a", "cnpg_cluster": "postgres"},
        )()
        result = type(
            "Result",
            (),
            {
                "returncode": 1,
                "stdout": "",
                "stderr": "lookup management API: host not found",
            },
        )()
        with (
            patch("scripts.create.tenant_kubeconfig_path") as kubeconfig,
            patch("scripts.create._tenant_kubectl", return_value=result),
            patch("scripts.create.apply_control_plane") as apply_control_plane,
        ):
            kubeconfig.return_value.is_file.return_value = True
            with self.assertRaisesRegex(RuntimeError, "inspection failed"):
                reconcile_tenant(
                    Path("."),
                    {"DATABASE_NAMESPACE": "database"},
                    object(),
                    tenant,
                )
        apply_control_plane.assert_not_called()

    def test_reconcile_accepts_explicit_database_not_found(self) -> None:
        tenant = type(
            "Tenant",
            (),
            {"name": "tenant-a", "namespace": "tenant-a", "cnpg_cluster": "postgres"},
        )()
        result = type(
            "Result",
            (),
            {
                "returncode": 1,
                "stdout": "",
                "stderr": 'Error from server (NotFound): clusters.postgresql.cnpg.io "postgres" not found',
            },
        )()
        expected = {"identity": "stable"}
        with (
            patch("scripts.create.tenant_kubeconfig_path") as kubeconfig,
            patch("scripts.create._tenant_kubectl", return_value=result),
            patch(
                "scripts.create.stable_tenant_snapshot",
                side_effect=[None, expected],
            ),
            patch("scripts.create.apply_control_plane") as apply_control_plane,
            patch("scripts.create.export_tenant_kubeconfig"),
            patch("scripts.create.apply_bootstrap_rbac"),
            patch("scripts.create.apply_workers"),
            patch("scripts.create.apply_addons"),
            patch("scripts.create.wait_network_ready"),
            patch("scripts.create.verify_network"),
            patch("scripts.create.worker_snapshot"),
            patch("scripts.create.install_cnpg"),
            patch("scripts.create._write_marker"),
            patch("scripts.create._verify_marker"),
        ):
            kubeconfig.return_value.is_file.return_value = True
            observed = reconcile_tenant(
                Path("."),
                {"DATABASE_NAMESPACE": "database"},
                object(),
                tenant,
            )
        self.assertEqual(observed, expected)
        apply_control_plane.assert_called_once()
