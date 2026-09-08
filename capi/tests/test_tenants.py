from __future__ import annotations

import tempfile
import unittest
import json
import shutil
import base64
from pathlib import Path
from unittest.mock import patch

from scripts.lib.tenants import (
    Tenant,
    _render_template,
    configured_tenants,
    remove_tenant_storage_volume,
    storage_volume_name,
    validate_tenant_kubeconfig_view,
)
from scripts.lib.files import IntegrityError


class TenantTests(unittest.TestCase):
    def test_template_rejects_unresolved_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.yaml"
            destination = Path(temporary) / "output.yaml"
            source.write_text("name: ${NAME}\nother: ${MISSING}\n", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                _render_template(source, destination, {"NAME": "value"})

    def test_storage_volume_name_is_tenant_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tenant = Tenant(
                name="spike",
                namespace="spike",
                vip="172.18.0.10",
                pod_cidr="10.1.0.0/16",
                service_cidr="10.2.0.0/16",
                dns_ip="10.2.0.10",
                domain="spike.local",
                storage_host_path=root / ".runtime" / "storage" / "spike",
                cnpg_cluster="spike-postgres",
                workers=1,
            )
            self.assertEqual(
                storage_volume_name({"LAB_PREFIX": "test"}, tenant),
                "test-spike-storage",
            )

    def test_configured_tenant_overlays_are_distinct(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copytree(
                repository / "manifests" / "tenants" / "overlays",
                root / "manifests" / "tenants" / "overlays",
            )
            network = root / ".runtime" / "management" / "network.json"
            network.parent.mkdir(parents=True)
            network.write_text(
                json.dumps(
                    {
                        "slots": {
                            "tenant-a": "172.18.0.10",
                            "tenant-b": "172.18.0.11",
                        }
                    }
                ),
                encoding="utf-8",
            )
            with patch(
                "scripts.lib.tenants.inspect_storage_volume",
                return_value=None,
            ):
                tenants = configured_tenants(
                    root,
                    {
                        "LAB_PREFIX": "test",
                        "OWNERSHIP_LABEL": "example.owner",
                        "TENANT_NAMES": "tenant-a tenant-b",
                        "TENANT_A_POD_CIDR": "10.70.0.0/16",
                        "TENANT_A_SERVICE_CIDR": "10.140.0.0/16",
                        "TENANT_B_POD_CIDR": "10.71.0.0/16",
                        "TENANT_B_SERVICE_CIDR": "10.141.0.0/16",
                        "WORKERS_PER_TENANT": "3",
                    },
                )
        self.assertEqual([tenant.name for tenant in tenants], ["tenant-a", "tenant-b"])
        self.assertEqual({tenant.workers for tenant in tenants}, {3})
        for attribute in (
            "namespace",
            "vip",
            "pod_cidr",
            "service_cidr",
            "dns_ip",
            "domain",
            "cnpg_cluster",
        ):
            values = [getattr(tenant, attribute) for tenant in tenants]
            self.assertEqual(len(values), len(set(values)))

    def test_configured_tenants_require_exact_overlay_set(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            network = root / ".runtime" / "management" / "network.json"
            network.parent.mkdir(parents=True)
            network.write_text('{"slots":{}}\n', encoding="utf-8")
            with self.assertRaises(IntegrityError):
                configured_tenants(root, {"TENANT_NAMES": "tenant-a"})

    def test_absent_volume_removes_valid_orphaned_identity_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tenant = Tenant(
                name="tenant-a",
                namespace="tenant-a",
                vip="172.18.0.10",
                pod_cidr="10.70.0.0/16",
                service_cidr="10.140.0.0/16",
                dns_ip="10.140.0.10",
                domain="tenant-a.local",
                storage_host_path=root / "storage",
                cnpg_cluster="tenant-a-postgres",
                workers=3,
            )
            record = root / ".runtime" / "storage" / tenant.name / "volume.json"
            record.parent.mkdir(parents=True)
            record.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "tenant": tenant.name,
                        "volumeName": "lab-tenant-a-storage",
                        "createdAt": "2026-01-01T00:00:00Z",
                        "mountpoint": "/var/lib/docker/volumes/test/_data",
                    }
                ),
                encoding="utf-8",
            )
            record.chmod(0o600)
            with patch(
                "scripts.lib.tenants.inspect_storage_volume",
                return_value=None,
            ):
                remove_tenant_storage_volume(
                    root, {"LAB_PREFIX": "lab"}, tenant
                )
            self.assertFalse(record.exists())
            self.assertFalse(record.parent.exists())

    def test_kubeconfig_validation_uses_active_context_only(self) -> None:
        tenant = type("Tenant", (), {"vip": "172.18.0.10"})()
        expected_ca = b"tenant-ca"
        view = {
            "current-context": "wrong",
            "contexts": [
                {"name": "expected", "context": {"cluster": "expected"}},
                {"name": "wrong", "context": {"cluster": "wrong"}},
            ],
            "clusters": [
                {
                    "name": "expected",
                    "cluster": {
                        "server": "https://172.18.0.10:6443",
                        "certificate-authority-data": base64.b64encode(
                            expected_ca
                        ).decode(),
                    },
                },
                {
                    "name": "wrong",
                    "cluster": {
                        "server": "https://172.18.0.11:6443",
                        "certificate-authority-data": base64.b64encode(
                            expected_ca
                        ).decode(),
                    },
                },
            ],
        }
        with self.assertRaisesRegex(RuntimeError, "active context"):
            validate_tenant_kubeconfig_view(
                {"SPIKE_API_PORT": "6443"},
                tenant,
                view,
                expected_ca,
            )
