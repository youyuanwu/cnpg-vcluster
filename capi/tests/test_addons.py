from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.lib.addons import (
    REFERENCE_LIMIT,
    SOURCE_LIMIT,
    _source_object,
    package_source,
)
from scripts.lib.files import IntegrityError
from scripts.lib.tenants import Tenant


class AddonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tenant = Tenant(
            name="spike",
            namespace="spike",
            vip="172.18.0.20",
            pod_cidr="10.1.0.0/16",
            service_cidr="10.2.0.0/16",
            dns_ip="10.2.0.10",
            domain="spike.local",
            storage_host_path=Path("/tmp/spike"),
        )
        self.config = {
            "OWNERSHIP_LABEL": "example.owner",
            "LAB_PREFIX": "example",
        }

    def test_source_object_is_below_selected_limit(self) -> None:
        payload = _source_object(self.config, self.tenant, "source", "kind: List\n")
        serialized = json.dumps(payload, separators=(",", ":")).encode()
        self.assertLess(len(serialized), SOURCE_LIMIT)

    def test_limits_are_explicit(self) -> None:
        self.assertEqual(SOURCE_LIMIT, 900 * 1024)
        self.assertEqual(REFERENCE_LIMIT, 100)

    def test_deterministic_document_splitting(self) -> None:
        content = "\n---\n".join(
            f"apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: item-{index}\n"
            f"data:\n  value: {'x' * 120}\n"
            for index in range(4)
        )
        first = package_source(
            self.config, self.tenant, "source", content, limit=500
        )
        second = package_source(
            self.config, self.tenant, "source", content, limit=500
        )
        self.assertEqual(first, second)
        self.assertGreater(len(first), 1)
        self.assertTrue(all(name.startswith("source-") for name, _, _ in first))

    def test_rejects_oversized_single_document(self) -> None:
        content = "apiVersion: v1\nkind: ConfigMap\ndata:\n  value: " + "x" * 500
        with self.assertRaises(IntegrityError):
            package_source(
                self.config, self.tenant, "source", content, limit=200
            )
