from __future__ import annotations

import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from scripts.create import (
    _incomplete_snapshot_error,
    reconcile_tenant,
    validate_selected_tenant_inputs,
)
from scripts.lib.files import IntegrityError


class CreateTests(unittest.TestCase):
    def test_snapshot_only_suppresses_canonical_incomplete_errors(self) -> None:
        self.assertTrue(
            _incomplete_snapshot_error(
                RuntimeError(
                    'Error from server (NotFound): machines "worker" not found'
                )
            )
        )
        self.assertTrue(
            _incomplete_snapshot_error(
                RuntimeError("three-worker topology is not exact")
            )
        )
        self.assertFalse(
            _incomplete_snapshot_error(
                RuntimeError("management API connection refused")
            )
        )

    def test_selected_input_validation_renders_only_selected_tenant(self) -> None:
        tenant = type("Tenant", (), {"name": "tenant-c"})()
        config = {"CNPG_COMPATIBILITY_REVISION": "docker-volume-hostpath-v1"}
        with (
            patch("scripts.create.render_tenant_manifests") as tenants,
            patch("scripts.create.render_resource_set") as addons,
            patch("scripts.create._render_operator") as operator,
            patch("scripts.create._render_cluster") as cluster,
        ):
            validate_selected_tenant_inputs(Path("."), config, tenant)
        for rendered in (tenants, addons, operator, cluster):
            rendered.assert_called_once()
            self.assertIs(rendered.call_args.args[-1], tenant)

    def test_selected_input_validation_rejects_incompatible_cnpg(self) -> None:
        with self.assertRaises(IntegrityError):
            validate_selected_tenant_inputs(
                Path("."),
                {"CNPG_COMPATIBILITY_REVISION": "changed"},
                object(),
            )

    def test_reconcile_accepts_explicit_database_not_found(self) -> None:
        tenant = type(
            "Tenant",
            (),
            {
                "name": "tenant-c",
                "namespace": "tenant-c",
                "cnpg_cluster": "tenant-c-postgres",
                "vip": "172.18.0.10",
                "lifecycle_markers": {},
            },
        )()
        result = type(
            "Result",
            (),
            {
                "returncode": 1,
                "stdout": "",
                "stderr": 'Error from server (NotFound): clusters.postgresql.cnpg.io "tenant-c-postgres" not found',
            },
        )()
        final = {
            "resources": {
                "namespace": "namespace",
                "cluster": "cluster",
                "devcluster": "devcluster",
                "kamajicontrolplane": "control-plane",
                "machinedeployment": "deployment",
                "kubeadmconfigtemplate": "bootstrap-template",
                "devmachinetemplate": "machine-template",
            },
            "loadBalancerID": "load-balancer",
            "workers": {"worker": {"machineUID": "machine"}},
            "volume": {
                "name": "volume",
                "createdAt": "created",
                "mountpoint": "/volume",
            },
            "databaseUID": "database",
            "operatorUID": "operator",
            "storage": {"pvc": {"pvcUID": "pvc"}},
            "storageSmoke": {"ready": True},
            "kubeconfigSHA256": "kubeconfig",
        }
        volume = {
            "Name": "volume",
            "CreatedAt": "created",
            "Mountpoint": "/volume",
        }
        with ExitStack() as stack:
            stack.enter_context(
                patch("scripts.create.validate_selected_tenant_inputs")
            )
            kubeconfig = stack.enter_context(
                patch("scripts.create.tenant_kubeconfig_path")
            )
            stack.enter_context(
                patch("scripts.create._tenant_kubectl", return_value=result)
            )
            stack.enter_context(
                patch(
                    "scripts.create.stable_tenant_snapshot",
                    side_effect=[None, final],
                )
            )
            for name in (
                "apply_control_plane",
                "export_tenant_kubeconfig",
                "ensure_tenant_kubeconfig",
                "apply_bootstrap_rbac",
                "restore_host_images",
                "apply_workers",
                "preload_worker_images",
                "apply_addons",
                "_repair_addons",
                "wait_network_ready",
                "verify_network",
                "ensure_storage_ready",
                "install_cnpg",
                "_write_marker",
                "_verify_marker",
            ):
                stack.enter_context(patch(f"scripts.create.{name}"))
            stack.enter_context(
                patch(
                    "scripts.create.verify_tenant_management_ownership",
                    return_value={},
                )
            )
            stack.enter_context(
                patch(
                    "scripts.create.worker_snapshot",
                    return_value=final["workers"],
                )
            )
            stack.enter_context(
                patch(
                    "scripts.create.inspect_storage_volume",
                    return_value=volume,
                )
            )
            run = stack.enter_context(patch("scripts.create.run"))
            stack.enter_context(patch("scripts.cnpg._verify_filesystem"))
            kubeconfig.return_value.is_file.return_value = True
            kubeconfig.return_value.read_bytes.return_value = b"kubeconfig"
            run.return_value.stdout = '[{"Id":"load-balancer"}]'
            observed = reconcile_tenant(
                Path("."),
                {
                    "DATABASE_NAMESPACE": "database",
                    "LAB_PREFIX": "lab",
                },
                object(),
                tenant,
            )
        self.assertEqual(observed["clusterUID"], "cluster")


if __name__ == "__main__":
    unittest.main()
