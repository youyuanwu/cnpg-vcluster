from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts.azure import _run_profile_mutation
from scripts.lib.azure.deletion import _remove_private_tree
from scripts.lib.azure.common import (
    _foundation_defaults_checksum,
    azure_tenant_runtime_path,
    load_azure_configuration,
    tenant_names,
)
from scripts.lib.azure.foundation import create_foundation, load_inventory, preflight
from scripts.lib.config import ConfigError
from scripts.lib.files import write_private_file
from scripts.lib.locking import azure_lock
from scripts.lib.tenant_spec import TenantSpecError
from tests.azure_fixtures import AzureFixtureMixin, FOUNDATION, SUBSCRIPTION


def completed(stdout: str = "", returncode: int = 0):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


class AzureFoundationTests(AzureFixtureMixin, unittest.TestCase):
    def test_configuration_contains_only_foundation_and_profile_limits(self):
        root = self.make_root()
        config = load_azure_configuration(root)
        self.assertEqual(config["AZURE_PREFIX"], "yy-cv")
        for removed in (
            "AZURE_TENANT_NODE_COUNT",
            "AZURE_TENANT_POD_CIDR",
            "AZURE_TENANT_SERVICE_CIDR",
            "AZURE_TENANT_DNS_SERVICE_IP",
        ):
            self.assertNotIn(removed, config)
        self.assertEqual(
            config["AZURE_SUPPORTED_TENANT_KUBERNETES_VERSION"],
            "1.32.13",
        )
    def test_preflight_output_does_not_expose_subscription_id(self):
        root = self.make_root()
        config = load_azure_configuration(root)
        output = io.StringIO()
        with (
            patch(
                "scripts.lib.azure.foundation._active_subscription",
                return_value={"id": SUBSCRIPTION, "state": "Enabled"},
            ),
            patch("scripts.lib.azure.foundation._az", return_value=completed("Registered\n")),
            patch("scripts.lib.azure.foundation._sku_available"),
            patch("scripts.lib.azure.foundation._reference_image_available"),
            patch("scripts.lib.azure.foundation.run", return_value=completed()),
            redirect_stdout(output),
        ):
            result = preflight(root, config)
        self.assertEqual(result["subscriptionId"], SUBSCRIPTION)
        self.assertNotIn(SUBSCRIPTION, output.getvalue())
    def test_rejects_broad_local_parameter_permissions(self):
        root = self.make_root()
        (root / "config" / "azure.local.env").chmod(0o644)
        with self.assertRaisesRegex(ConfigError, "owner-only"):
            load_azure_configuration(root)
    def test_rejects_invalid_prefix(self):
        root = self.make_root()
        path = root / "config" / "azure.local.env"
        path.write_text(
            f"AZURE_SUBSCRIPTION_ID={SUBSCRIPTION}\n"
            "AZURE_LOCATION=westus2\nAZURE_PREFIX=YY_cv\n",
            encoding="utf-8",
        )
        path.chmod(0o600)
        with self.assertRaisesRegex(ConfigError, "AZURE_PREFIX"):
            load_azure_configuration(root)
    def test_rejects_pre_cutover_tenant_configuration(self):
        root = self.make_root()
        path = root / "config" / "azure.local.env"
        with path.open("a", encoding="utf-8") as output:
            output.write("AZURE_TENANT_NODE_COUNT=1\n")
        with self.assertRaisesRegex(ConfigError, "pre-cutover Azure tenant configuration"):
            load_azure_configuration(root)
    def test_azure_database_count_is_rejected(self):
        with self.assertRaisesRegex(TenantSpecError, "unknown.*databaseCount"):
            self.spec(databaseCount=1)
    def test_foundation_checksum_ignores_tenant_profile_limits(self):
        root = self.make_root()
        config = load_azure_configuration(root)
        baseline = _foundation_defaults_checksum(root, config)
        changed = dict(config)
        changed["AZURE_SUPPORTED_TENANT_KUBERNETES_VERSION"] = "9.9.9"
        changed["AZURE_TENANT_NODE_SKU"] = "different"
        changed["AZURE_TENANT_TIMEOUT"] = "1m"
        self.assertEqual(baseline, _foundation_defaults_checksum(root, changed))
        changed["AZURE_AKS_NODE_COUNT"] = "3"
        self.assertNotEqual(baseline, _foundation_defaults_checksum(root, changed))
    def test_old_foundation_inventory_requires_clean_redeploy(self):
        root = self.make_root()
        config = load_azure_configuration(root)
        old = self.inventory(root, config)
        old["schema"] = 1
        old["defaultsSha256"] = old.pop("foundationDefaultsSha256")
        self.write_inventory(root, old)
        with self.assertRaisesRegex(RuntimeError, "pre-cutover.*clean foundation redeploy"):
            load_inventory(root, config)
    def test_foundation_checksum_mismatch_requires_clean_redeploy(self):
        root = self.make_root()
        config = load_azure_configuration(root)
        payload = self.inventory(root, config)
        payload["foundationDefaultsSha256"] = "stale"
        self.write_inventory(root, payload)
        with self.assertRaisesRegex(RuntimeError, "checksum changed.*clean foundation"):
            load_inventory(root, config)
    def test_foundation_create_refuses_old_inventory_before_deployment(self):
        root = self.make_root()
        config = load_azure_configuration(root)
        old = self.inventory(root, config)
        old["schema"] = 1
        self.write_inventory(root, old)
        with (
            patch("scripts.lib.azure.foundation.preflight", return_value={}),
            patch("scripts.lib.azure.foundation._json") as deploy,
            self.assertRaisesRegex(RuntimeError, "pre-cutover"),
        ):
            create_foundation(root, config)
        deploy.assert_not_called()
    def test_tenant_names_and_artifacts_are_tenant_keyed(self):
        root = self.make_root()
        spec = self.spec("blue")
        selected = tenant_names(spec)
        self.assertEqual(selected["cluster"], "blue")
        self.assertEqual(selected["pool"], "blue-worker")
        path = azure_tenant_runtime_path(root, "blue")
        self.assertEqual(path, root / ".runtime" / "azure" / "tenants" / "blue")
        self.assertNotIn("yy-cv-tenant", json.dumps(selected))
    def test_tenant_runtime_removal_cannot_remove_foundation_files(self):
        root = self.make_root()
        foundation = root / ".runtime" / "azure"
        tenant = azure_tenant_runtime_path(root, "blue")
        write_private_file(foundation / "resources.json", "{}")
        write_private_file(foundation / "management.kubeconfig", "foundation")
        write_private_file(tenant / "endpoint.json", "{}")
        for child in tenant.iterdir():
            child.unlink()
        tenant.rmdir()
        self.assertTrue((foundation / "resources.json").is_file())
        self.assertTrue((foundation / "management.kubeconfig").is_file())
    def test_foundation_mutation_waits_for_azure_lock(self):
        root = self.make_root()
        marker = root / "acquired"
        script = (
            "from pathlib import Path; "
            "from scripts.azure import _run_profile_mutation; "
            f"root=Path({str(root)!r}); marker=Path({str(marker)!r}); "
            "_run_profile_mutation(root, {}, "
            "lambda _root, _config: marker.write_text('yes'))"
        )
        with azure_lock(root, exclusive=True, create=True):
            process = subprocess.Popen(
                [sys.executable, "-c", script],
                cwd=Path(__file__).resolve().parents[1],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            time.sleep(0.2)
            self.assertIsNone(process.poll())
            self.assertFalse(marker.exists())
        _, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertEqual(marker.read_text(encoding="utf-8"), "yes")


if __name__ == "__main__":
    unittest.main()
