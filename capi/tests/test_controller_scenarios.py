from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.lib.controller_scenarios import (
    manifest_tenant_name,
    tenant_from_document,
    tenant_snapshot,
)


def tenant_document() -> dict[str, object]:
    return {
        "metadata": {"name": "tenant-a", "uid": "tenant-uid"},
        "spec": {
            "workers": 2,
            "databaseCount": 3,
            "podCIDR": "10.73.0.0/16",
            "serviceCIDR": "10.143.0.0/16",
        },
        "status": {
            "endpoint": "172.18.255.10:6443",
            "specHash": "spec",
            "foundationHash": "foundation",
            "dockerVolume": {
                "name": "tenant-a-storage",
                "mountpoint": "/var/lib/docker/volumes/tenant-a/_data",
            },
            "observedResources": [
                {
                    "apiVersion": "v1",
                    "kind": "Namespace",
                    "name": "tenant-a",
                    "uid": "namespace-uid",
                }
            ],
            "tenantResources": [],
            "workerContainers": [
                {"name": "worker-b", "id": "container-b"},
                {"name": "worker-a", "id": "container-a"},
            ],
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
        tenant = tenant_from_document(
            Path("."),
            {
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

    def test_snapshot_sorts_identity_inventories(self) -> None:
        snapshot = tenant_snapshot(tenant_document())
        self.assertEqual("tenant-uid", snapshot["uid"])
        self.assertEqual(
            [
                ("worker-a", "container-a"),
                ("worker-b", "container-b"),
            ],
            snapshot["workerContainers"],
        )
