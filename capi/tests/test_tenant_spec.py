from __future__ import annotations

import ipaddress
import json
import tempfile
import unittest
from pathlib import Path

from scripts.lib.tenant_spec import (
    TenantSpec,
    TenantSpecError,
    load_tenant_spec,
    require_non_overlapping_networks,
)


LOCAL = {
    "schema": 1,
    "profile": "local",
    "name": "tenant-c",
    "kubernetesVersion": "1.36.4",
    "workers": 1,
    "podCIDR": "10.72.0.0/16",
    "serviceCIDR": "10.142.0.0/16",
    "databaseCount": 3,
}


class TenantSpecTests(unittest.TestCase):
    def test_parses_local_spec_and_derives_fields(self) -> None:
        spec = TenantSpec.from_mapping(
            LOCAL,
            supported_versions={"local": "v1.36.4"},
        )
        self.assertEqual(spec.namespace, "tenant-c")
        self.assertEqual(spec.dns_service_ip, "10.142.0.10")
        self.assertEqual(spec.cluster_domain, "tenant-c.capi.local")
        self.assertEqual(spec.database_name, "tenant-c-postgres")
        self.assertEqual(spec.database_count, 3)
        self.assertEqual(spec, TenantSpec.from_mapping(spec.to_mapping()))
        self.assertEqual(len(spec.sha256()), 64)

    def test_parses_azure_spec_without_database_fields(self) -> None:
        payload = LOCAL | {
            "profile": "azure",
            "kubernetesVersion": "1.32.13",
        }
        payload.pop("databaseCount")
        spec = TenantSpec.from_mapping(
            payload,
            expected_profile="azure",
            supported_versions={"azure": "1.32.13"},
        )
        self.assertEqual(spec.cluster_domain, "cluster.local")
        self.assertIsNone(spec.database_count)
        self.assertIsNone(spec.database_name)

    def test_rejects_unknown_missing_and_duplicate_fields(self) -> None:
        with self.assertRaisesRegex(TenantSpecError, "unknown"):
            TenantSpec.from_mapping(LOCAL | {"namespace": "tenant-c"})
        missing = dict(LOCAL)
        missing.pop("workers")
        with self.assertRaisesRegex(TenantSpecError, "missing"):
            TenantSpec.from_mapping(missing)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "tenant.json"
            path.write_text(
                '{"schema":1,"schema":1,"profile":"local"}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(TenantSpecError, "duplicate"):
                load_tenant_spec(path)

    def test_rejects_invalid_names_versions_counts_and_profiles(self) -> None:
        for name in ("Upper", "-tenant", "tenant-", "a" * 31):
            with self.subTest(name=name):
                with self.assertRaises(TenantSpecError):
                    TenantSpec.from_mapping(LOCAL | {"name": name})
        with self.assertRaisesRegex(TenantSpecError, "Kubernetes version"):
            TenantSpec.from_mapping(LOCAL | {"kubernetesVersion": "1.36"})
        with self.assertRaisesRegex(TenantSpecError, "integer"):
            TenantSpec.from_mapping(LOCAL | {"workers": 0})
        with self.assertRaisesRegex(TenantSpecError, "unsupported"):
            TenantSpec.from_mapping(LOCAL | {"profile": "other"})
        with self.assertRaisesRegex(TenantSpecError, "does not match"):
            TenantSpec.from_mapping(LOCAL, expected_profile="azure")

    def test_rejects_azure_database_count_and_wrong_supported_version(self) -> None:
        with self.assertRaisesRegex(TenantSpecError, "unknown"):
            TenantSpec.from_mapping(LOCAL | {"profile": "azure"})
        with self.assertRaisesRegex(TenantSpecError, "unsupported local"):
            TenantSpec.from_mapping(
                LOCAL,
                supported_versions={"local": "1.35.0"},
            )

    def test_rejects_invalid_or_overlapping_networks(self) -> None:
        with self.assertRaisesRegex(TenantSpecError, "overlap"):
            TenantSpec.from_mapping(
                LOCAL | {"serviceCIDR": LOCAL["podCIDR"]}
            )
        with self.assertRaisesRegex(TenantSpecError, "IPv4"):
            TenantSpec.from_mapping(
                LOCAL | {"podCIDR": "2001:db8::/64"}
            )
        spec = TenantSpec.from_mapping(LOCAL)
        with self.assertRaisesRegex(TenantSpecError, "shared-pods"):
            require_non_overlapping_networks(
                spec,
                {"shared-pods": ipaddress.ip_network("10.72.1.0/24")},
            )

    def test_canonical_digest_ignores_json_formatting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first.json"
            second = root / "second.json"
            first.write_text(json.dumps(LOCAL), encoding="utf-8")
            second.write_text(
                json.dumps(LOCAL, indent=4, sort_keys=True),
                encoding="utf-8",
            )
            self.assertEqual(
                load_tenant_spec(first).sha256(),
                load_tenant_spec(second).sha256(),
            )


if __name__ == "__main__":
    unittest.main()
