from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.lib.tenants import Tenant, _render_template, storage_volume_name


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
            )
            self.assertEqual(
                storage_volume_name({"LAB_PREFIX": "test"}, tenant),
                "test-spike-storage",
            )
