from __future__ import annotations

import unittest
import json
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from scripts.lib.management import _observed_identity, metallb_pool_apply_result
from scripts.lib.providers import PROVIDERS, _feature_gates
from scripts.create_management import create_management


class ManagementTests(unittest.TestCase):
    def test_management_images_are_restored_and_imported_before_workloads(self) -> None:
        calls: list[str] = []
        config = {"KIND_CLUSTER_NAME": "management"}
        with (
            patch("scripts.create_management.run_preflight", side_effect=lambda *_: calls.append("preflight")),
            patch("scripts.create_management.restore_host_images", side_effect=lambda *_: calls.append("restore")),
            patch("scripts.create_management.reconcile_kind", side_effect=lambda *_: calls.append("kind") or object()),
            patch("scripts.create_management.import_container_images", side_effect=lambda *_: calls.append("import")),
            patch("scripts.create_management.reconcile_network", side_effect=lambda *_: calls.append("network") or {}),
            patch("scripts.create_management.reconcile_offline_registry", side_effect=lambda *_: calls.append("registry")),
            patch("scripts.create_management.enforce_offline_node_egress", side_effect=lambda *_: calls.append("egress")),
            patch("scripts.create_management.verify_offline_registry_pulls", side_effect=lambda *_: calls.append("mirror-pulls")),
            patch("scripts.create_management.reconcile_cert_manager", side_effect=lambda *_: calls.append("cert-manager")),
            patch("scripts.create_management.reconcile_metallb"),
            patch("scripts.create_management.reconcile_kamaji"),
            patch("scripts.create_management.reconcile_providers"),
        ):
            create_management(Path("."), config)
        self.assertEqual(
            calls,
            [
                "preflight",
                "restore",
                "kind",
                "import",
                "network",
                "registry",
                "egress",
                "mirror-pulls",
                "cert-manager",
            ],
        )

    def test_metallb_webhook_connection_refusal_is_retryable(self) -> None:
        response = CompletedProcess(
            [],
            1,
            stdout="",
            stderr="failed calling webhook: connect: connection refused",
        )
        self.assertIsNone(metallb_pool_apply_result(response))

    def test_metallb_nontransient_admission_failure_is_fatal(self) -> None:
        response = CompletedProcess(
            [],
            1,
            stdout="",
            stderr="Error from server (Forbidden): denied",
        )
        with self.assertRaisesRegex(RuntimeError, "admission failed"):
            metallb_pool_apply_result(response)

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

    def test_feature_gate_parser_reports_all_observed_values(self) -> None:
        observed = _feature_gates(
            [
                "--leader-elect",
                "--feature-gates=DynamicInfrastructureClusterPatch=false,"
                "ExternalClusterReference=true,"
                "ExternalClusterReferenceCrossNamespace=false,"
                "SkipInfraClusterPatch=true",
            ]
        )
        self.assertEqual(
            observed,
            {
                "DynamicInfrastructureClusterPatch": False,
                "ExternalClusterReference": True,
                "ExternalClusterReferenceCrossNamespace": False,
                "SkipInfraClusterPatch": True,
            },
        )
