from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.lib.tenant_spec import (
    TenantSpec,
    TenantSpecError,
    load_tenant_spec,
)


AZURE = {
    "schema": 1,
    "profile": "azure",
    "name": "tenant-c",
    "kubernetesVersion": "1.32.13",
    "workers": 1,
}


class TenantSpecTests(unittest.TestCase):
    def test_parses_azure_spec_and_derives_fields(self) -> None:
        spec = TenantSpec.from_mapping(
            AZURE,
            supported_versions={"azure": "v1.32.13"},
        )
        self.assertEqual(spec.namespace, "tenant-c")
        self.assertEqual(spec.cluster_domain, "cluster.local")
        self.assertEqual(spec, TenantSpec.from_mapping(spec.to_mapping()))
        self.assertEqual(len(spec.sha256()), 64)

    def test_rejects_unknown_missing_and_duplicate_fields(self) -> None:
        with self.assertRaisesRegex(TenantSpecError, "unknown"):
            TenantSpec.from_mapping(AZURE | {"namespace": "tenant-c"})
        missing = dict(AZURE)
        missing.pop("workers")
        with self.assertRaisesRegex(TenantSpecError, "missing"):
            TenantSpec.from_mapping(missing)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "tenant.json"
            path.write_text(
                '{"schema":1,"schema":1,"profile":"azure"}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(TenantSpecError, "duplicate"):
                load_tenant_spec(path)

    def test_rejects_invalid_names_versions_counts_and_profiles(self) -> None:
        for name in ("Upper", "-tenant", "tenant-", "a" * 31):
            with self.subTest(name=name):
                with self.assertRaises(TenantSpecError):
                    TenantSpec.from_mapping(AZURE | {"name": name})
        with self.assertRaisesRegex(TenantSpecError, "Kubernetes version"):
            TenantSpec.from_mapping(AZURE | {"kubernetesVersion": "1.32"})
        with self.assertRaisesRegex(TenantSpecError, "integer"):
            TenantSpec.from_mapping(AZURE | {"workers": 0})
        with self.assertRaisesRegex(TenantSpecError, "unsupported"):
            TenantSpec.from_mapping(AZURE | {"profile": "other"})
        with self.assertRaisesRegex(TenantSpecError, "does not match"):
            TenantSpec.from_mapping(AZURE, expected_profile="local")

    def test_rejects_local_profile_database_count_and_wrong_supported_version(self) -> None:
        with self.assertRaisesRegex(TenantSpecError, "unsupported"):
            TenantSpec.from_mapping(AZURE | {"profile": "local"})
        with self.assertRaisesRegex(TenantSpecError, "unknown"):
            TenantSpec.from_mapping(AZURE | {"databaseCount": 3})
        with self.assertRaisesRegex(TenantSpecError, "unsupported azure"):
            TenantSpec.from_mapping(
                AZURE,
                supported_versions={"azure": "1.35.0"},
            )

    def test_rejects_removed_network_fields(self) -> None:
        with self.assertRaisesRegex(TenantSpecError, "unknown"):
            TenantSpec.from_mapping(AZURE | {"podCIDR": "10.72.0.0/16"})

    def test_canonical_digest_ignores_json_formatting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first.json"
            second = root / "second.json"
            first.write_text(json.dumps(AZURE), encoding="utf-8")
            second.write_text(
                json.dumps(AZURE, indent=4, sort_keys=True),
                encoding="utf-8",
            )
            self.assertEqual(
                load_tenant_spec(first).sha256(),
                load_tenant_spec(second).sha256(),
            )


if __name__ == "__main__":
    unittest.main()
