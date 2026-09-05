from __future__ import annotations

import unittest
import json
from pathlib import Path

from scripts.lib.management import _observed_identity
from scripts.lib.providers import PROVIDERS


class ManagementTests(unittest.TestCase):
    def test_kind_identity_uses_exact_standard_label(self) -> None:
        config = {
            "KIND_CLUSTER_NAME": "management",
            "OWNERSHIP_LABEL": "example.owner",
        }
        payload = {
            "Id": "container-id",
            "Config": {
                "Labels": {
                    "io.x-k8s.kind.cluster": "management",
                    "io.x-k8s.kind.role": "control-plane",
                }
            },
        }
        identity = _observed_identity(config, payload)
        self.assertEqual(identity.identifier, "container-id")
        self.assertEqual(identity.labels["io.x-k8s.kind.cluster"], "management")
        self.assertEqual(identity.labels["example.owner"], "")

    def test_provider_order_and_endpoint_feature_gate(self) -> None:
        self.assertEqual(
            [provider.name for provider in PROVIDERS],
            ["capi-core", "cabpk", "capd", "kamaji-control-plane"],
        )
        settings = json.loads(
            (
                Path(__file__).resolve().parents[1]
                / "manifests"
                / "management"
                / "kamaji-provider-settings.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            settings["featureGates"]["SkipInfraClusterPatch"],
            True,
        )
        self.assertEqual(
            settings["featureGates"]["DynamicInfrastructureClusterPatch"],
            False,
        )
