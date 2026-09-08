from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.lib.addons import (
    REFERENCE_LIMIT,
    SOURCE_LIMIT,
    _source_object,
    package_source,
    validate_inventory,
    validate_manifest_hashes,
    validate_resource_set_references,
    render_resource_set,
    verify_addon_source_ownership,
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
            cnpg_cluster="spike-postgres",
            workers=1,
        )
        self.config = {
            "OWNERSHIP_LABEL": "example.owner",
            "LAB_PREFIX": "example",
        }

    def test_source_object_is_below_selected_limit(self) -> None:
        payload = _source_object(self.config, self.tenant, "source", "kind: List\n")
        serialized = json.dumps(payload, separators=(",", ":")).encode()
        self.assertLess(len(serialized), SOURCE_LIMIT)

    def test_addon_source_ownership_rejects_foreign_configmap(self) -> None:
        client = type(
            "Client",
            (),
            {
                "kubectl": lambda self, *args, **kwargs: type(
                    "Result",
                    (),
                    {
                        "returncode": 0,
                        "stdout": json.dumps(
                            {"metadata": {"labels": {}}}
                        ),
                        "stderr": "",
                    },
                )()
            },
        )()
        with patch(
            "scripts.lib.addons.render_resource_set",
            return_value=(Path("resource-set.json"), {"source": "a" * 64}),
        ):
            with self.assertRaisesRegex(RuntimeError, "ownership mismatch"):
                verify_addon_source_ownership(
                    Path("."), self.config, client, self.tenant
                )

    def test_addon_source_presence_is_required_for_survivor(self) -> None:
        client = type(
            "Client",
            (),
            {
                "kubectl": lambda self, *args, **kwargs: type(
                    "Result",
                    (),
                    {
                        "returncode": 1,
                        "stdout": "",
                        "stderr": 'Error from server (NotFound): item not found',
                    },
                )()
            },
        )()
        with patch(
            "scripts.lib.addons.render_resource_set",
            return_value=(Path("resource-set.json"), {"source": "a" * 64}),
        ):
            with self.assertRaisesRegex(RuntimeError, "sources are missing"):
                verify_addon_source_ownership(
                    Path("."),
                    self.config,
                    client,
                    self.tenant,
                    require_present=True,
                )

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

    def test_rejects_101_references(self) -> None:
        with self.assertRaises(IntegrityError):
            validate_inventory({f"source-{index}": "a" * 64 for index in range(101)})

    def test_rejects_missing_or_extra_hash_coverage(self) -> None:
        resource_set = {
            "spec": {
                "resources": [
                    {"kind": "ConfigMap", "name": "source-a"},
                    {"kind": "ConfigMap", "name": "source-extra"},
                ]
            }
        }
        with self.assertRaises(IntegrityError):
            validate_resource_set_references(
                resource_set,
                {"source-a": "a" * 64, "source-b": "b" * 64},
            )

    def test_rejects_non_configmap_reference_kind(self) -> None:
        resource_set = {
            "spec": {
                "resources": [
                    {"kind": "Secret", "name": "source-a"},
                ]
            }
        }
        with self.assertRaises(IntegrityError):
            validate_resource_set_references(
                resource_set,
                {"source-a": "a" * 64},
            )

    def test_rendered_resource_set_references_every_chunk(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            calico = root / "calico.yaml"
            proxy = root / "proxy.yaml"
            calico.write_text(
                "\n---\n".join(
                    f"apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: item-{i}\n"
                    f"data:\n  value: {'x' * 500000}\n"
                    for i in range(3)
                ),
                encoding="utf-8",
            )
            proxy.write_text(
                "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: proxy\n",
                encoding="utf-8",
            )
            with patch("scripts.lib.addons.render_calico", return_value=calico), patch(
                "scripts.lib.addons.render_kube_proxy", return_value=proxy
            ):
                manifest, inventory = render_resource_set(
                    root, self.config, self.tenant
                )
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            resource_set = next(
                item
                for item in payload["items"]
                if item["kind"] == "ClusterResourceSet"
            )
            self.assertEqual(
                [item["name"] for item in resource_set["spec"]["resources"]],
                sorted(inventory),
            )

    def test_rejects_tampered_manifest_chunk_before_apply(self) -> None:
        manifest = {
            "items": [
                {
                    "kind": "ConfigMap",
                    "metadata": {"name": "source"},
                    "data": {"addons.yaml": "tampered"},
                }
            ]
        }
        with self.assertRaises(IntegrityError):
            validate_manifest_hashes(manifest, {"source": "a" * 64})

    def test_rejects_duplicate_manifest_source_names(self) -> None:
        content = "kind: ConfigMap\n"
        digest = hashlib.sha256(content.encode()).hexdigest()
        manifest = {
            "items": [
                {
                    "kind": "ConfigMap",
                    "metadata": {"name": "source"},
                    "data": {"addons.yaml": "tampered"},
                },
                {
                    "kind": "ConfigMap",
                    "metadata": {"name": "source"},
                    "data": {"addons.yaml": content},
                },
            ]
        }
        with self.assertRaises(IntegrityError):
            validate_manifest_hashes(manifest, {"source": digest})
