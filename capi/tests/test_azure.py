from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.azure import (
    _render_addon_job,
    _render_worker_pool,
    _validate_networks,
    load_azure_configuration,
    names,
)
from scripts.lib.config import ConfigError


DEFAULTS = """\
AZURE_AKS_KUBERNETES_VERSION=1.35.7
AZURE_TENANT_KUBERNETES_VERSION=1.35.6
AZURE_AKS_NODE_SKU=Standard_D4as_v5
AZURE_TENANT_NODE_SKU=Standard_B2s
AZURE_AKS_NODE_COUNT=2
AZURE_TENANT_NODE_COUNT=1
AZURE_VNET_CIDR=10.220.0.0/16
AZURE_AKS_SUBNET_CIDR=10.220.0.0/20
AZURE_TENANT_SUBNET_CIDR=10.220.16.0/20
AZURE_AKS_POD_CIDR=10.221.0.0/16
AZURE_AKS_SERVICE_CIDR=10.222.0.0/16
AZURE_AKS_DNS_SERVICE_IP=10.222.0.10
AZURE_TENANT_POD_CIDR=10.70.0.0/16
AZURE_TENANT_SERVICE_CIDR=10.140.0.0/16
AZURE_CAPI_VERSION=v1.13.4
AZURE_CAPZ_VERSION=v1.26.1
AZURE_KAMAJI_CAPI_VERSION=v0.20.0
AZURE_KAMAJI_CHART_VERSION=1.0.0
AZURE_CLOUD_PROVIDER_VERSION=v1.32.3
AZURE_CALICO_VERSION=v3.32.2
"""


class AzureConfigurationTests(unittest.TestCase):
    def make_root(self, local: str) -> Path:
        temporary = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (temporary / "config" / "azure").mkdir(parents=True)
        (temporary / "config" / "azure" / "defaults.env").write_text(DEFAULTS)
        path = temporary / "config" / "azure.local.env"
        path.write_text(local)
        path.chmod(0o600)
        return temporary

    def test_loads_owner_only_local_parameters(self):
        root = self.make_root(
            "AZURE_SUBSCRIPTION_ID=00000000-0000-0000-0000-000000000000\n"
            "AZURE_LOCATION=westus2\nAZURE_PREFIX=yy-cv\n"
        )
        config = load_azure_configuration(root)
        self.assertEqual(config["AZURE_PREFIX"], "yy-cv")
        self.assertEqual(names(config)["resourceGroup"], "yy-cv-rg")

    def test_rejects_broad_local_parameter_permissions(self):
        root = self.make_root(
            "AZURE_SUBSCRIPTION_ID=x\nAZURE_LOCATION=westus2\nAZURE_PREFIX=yy-cv\n"
        )
        (root / "config" / "azure.local.env").chmod(0o644)
        with self.assertRaisesRegex(ConfigError, "owner-only"):
            load_azure_configuration(root)

    def test_rejects_invalid_prefix(self):
        root = self.make_root(
            "AZURE_SUBSCRIPTION_ID=x\nAZURE_LOCATION=westus2\nAZURE_PREFIX=YY_cv\n"
        )
        with self.assertRaisesRegex(ConfigError, "AZURE_PREFIX"):
            load_azure_configuration(root)

    def test_rejects_overlapping_networks(self):
        root = self.make_root(
            "AZURE_SUBSCRIPTION_ID=x\nAZURE_LOCATION=westus2\nAZURE_PREFIX=yy-cv\n"
        )
        config = load_azure_configuration(root)
        config["AZURE_TENANT_POD_CIDR"] = config["AZURE_AKS_POD_CIDR"]
        with self.assertRaisesRegex(ConfigError, "overlap"):
            _validate_networks(config)

    def test_worker_manifest_contains_bootstrap_compatibility(self):
        root = self.make_root(
            "AZURE_SUBSCRIPTION_ID=00000000-0000-0000-0000-000000000000\n"
            "AZURE_LOCATION=westus2\nAZURE_PREFIX=yy-cv\n"
        )
        (root / ".runtime").mkdir(mode=0o700)
        (root / ".runtime" / "azure").mkdir(mode=0o700)
        config = load_azure_configuration(root)
        inventory = {
            "outputs": {
                "identityName": "yy-cv-identity",
                "resourceGroupName": "yy-cv-rg",
                "tenantSubnetName": "tenant",
            }
        }
        text = _render_worker_pool(root, config, inventory).read_text()
        self.assertIn("kind: KubeadmConfig", text)
        self.assertIn("feature-gates: KubeletCrashLoopBackOffMax=true", text)
        self.assertIn("kind: AzureMachinePool", text)
        self.assertIn("kind: MachinePool", text)

    def test_addon_job_installs_cloud_provider_before_calico(self):
        root = self.make_root(
            "AZURE_SUBSCRIPTION_ID=00000000-0000-0000-0000-000000000000\n"
            "AZURE_LOCATION=westus2\nAZURE_PREFIX=yy-cv\n"
        )
        (root / ".runtime").mkdir(mode=0o700)
        (root / ".runtime" / "azure").mkdir(mode=0o700)
        config = load_azure_configuration(root)
        text = _render_addon_job(root, config).read_text()
        self.assertLess(
            text.index("upgrade --install cloud-provider-azure"),
            text.index("upgrade --install calico"),
        )
        self.assertIn("nodeSelector: null", text)
        self.assertIn("crd.projectcalico.org.v1", text)


if __name__ == "__main__":
    unittest.main()
