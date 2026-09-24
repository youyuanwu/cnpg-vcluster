from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from scripts.endpoint import run_endpoint_gate
from scripts.lib.controller_scenarios import (
    manifest_tenant_name,
    tenant_from_document,
    tenant_snapshot,
)


def tenant_document() -> dict[str, object]:
    return {
        "metadata": {"name": "tenant-a", "uid": "tenant-uid"},
        "spec": {
            "kubernetesVersion": "1.36.4",
            "workers": 2,
            "databaseCount": 3,
            "podCIDR": "10.73.0.0/16",
            "serviceCIDR": "10.143.0.0/16",
        },
        "status": {
            "endpoint": "172.18.255.10:6443",
            "foundationHash": "foundation",
            "clusterUID": "cluster-uid",
            "tenantAPICreationAuthorized": True,
        },
    }


class ControllerScenarioTests(unittest.TestCase):
    def test_manifest_name_is_read_from_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "custom.yaml"
            path.write_text(
                "apiVersion: tenancy.cnpg-vcluster.io/v1alpha1\n"
                "kind: Tenant\n"
                "metadata:\n"
                "  labels:\n"
                "    example: value\n"
                "  name: tenant-a\n"
                "spec:\n"
                "  workers: 1\n",
                encoding="utf-8",
            )
            self.assertEqual("tenant-a", manifest_tenant_name(path))

    def test_tenant_is_derived_from_controller_status(self) -> None:
        with patch(
            "scripts.lib.controller_scenarios.run",
            return_value=CompletedProcess(
                [],
                0,
                stdout='[{"Mountpoint":"/var/lib/docker/volumes/tenant-a/_data"}]',
                stderr="",
            ),
        ):
            tenant = tenant_from_document(
                Path("."),
                {
                    "LAB_PREFIX": "lab",
                    "SPIKE_API_PORT": "6443",
                    "SPIKE_CLUSTER_DOMAIN": "spike.capi.local",
                    "SPIKE_CNPG_CLUSTER": "capi-postgres",
                },
                tenant_document(),
            )
        self.assertEqual("tenant-a", tenant.name)
        self.assertEqual("172.18.255.10", tenant.vip)
        self.assertEqual("10.143.0.10", tenant.dns_ip)
        self.assertEqual(2, tenant.workers)
        self.assertEqual(3, tenant.database_count)

    def test_snapshot_uses_live_management_and_host_identities(self) -> None:
        class Client:
            def __init__(self) -> None:
                self.index = 0

            def kubectl(self, *_arguments):
                self.index += 1
                return CompletedProcess(
                    [],
                    0,
                    stdout=(
                        '{"metadata":{"uid":"resource-'
                        + str(self.index)
                        + '"}}'
                    ),
                    stderr="",
                )

        with patch(
            "scripts.lib.controller_scenarios.run",
            side_effect=[
                CompletedProcess(
                    [],
                    0,
                    stdout='[{"Name":"volume","CreatedAt":"now","Mountpoint":"/volume","Labels":{"owned":"true"}}]',
                    stderr="",
                ),
                CompletedProcess(
                    [],
                    0,
                    stdout="worker-b bbbb\nworker-a aaaa\n",
                    stderr="",
                ),
            ],
        ):
            snapshot = tenant_snapshot(
                {"LAB_PREFIX": "lab"},
                Client(),
                tenant_document(),
            )
        self.assertEqual("tenant-uid", snapshot["uid"])
        self.assertEqual(
            [
                "worker-a aaaa",
                "worker-b bbbb",
            ],
            snapshot["workerContainers"],
        )
        self.assertEqual(8, len(snapshot["managementResources"]))

    def test_endpoint_gate_cleans_partially_applied_tenant(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "tenant.yaml"
            manifest.write_text(
                "apiVersion: tenancy.cnpg-vcluster.io/v1alpha1\n"
                "kind: Tenant\nmetadata:\n  name: tenant-a\n",
                encoding="utf-8",
            )
            with (
                patch(
                    "scripts.endpoint.management_status",
                    return_value={"apiReady": False},
                ),
                patch("scripts.endpoint.create_management"),
                patch(
                    "scripts.endpoint.apply_controller_tenant",
                    side_effect=RuntimeError("readiness failed"),
                ),
                patch("scripts.endpoint.delete_controller_tenant") as delete,
            ):
                with self.assertRaisesRegex(RuntimeError, "readiness failed"):
                    run_endpoint_gate(root, {}, manifest=manifest)
            delete.assert_called_once_with(root, {}, "tenant-a")
