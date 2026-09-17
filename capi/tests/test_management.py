from __future__ import annotations

import unittest
import json
import tempfile
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from scripts.lib.management import (
    _observed_identity,
    allocate_tenant_endpoint,
    metallb_pool_apply_result,
    release_tenant_endpoint,
    validate_management_network,
    validate_management_server_version,
)
from scripts.lib.providers import PROVIDERS, _feature_gates
from scripts.lib.files import IntegrityError
from scripts.create_management import create_management


class ManagementTests(unittest.TestCase):
    def test_tenant_endpoint_allocation_is_stable_and_reusable_after_absence(
        self,
    ) -> None:
        network = {
            "schema": 1,
            "network": "kind",
            "network_id": "network-id",
            "subnet": "172.18.0.0/16",
            "pool_start": "172.18.255.220",
            "pool_end": "172.18.255.225",
            "pool_cidrs": [],
            "slots": {
                "tenant-a": "172.18.255.220",
                "tenant-b": "172.18.255.221",
                "spike": "172.18.255.222",
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch(
                "scripts.lib.management.validate_management_network",
                return_value=network,
            ):
                first = allocate_tenant_endpoint(root, {}, "tenant-c")
                self.assertEqual(first, "172.18.255.223")
                self.assertEqual(
                    allocate_tenant_endpoint(root, {}, "tenant-c"),
                    first,
                )
                self.assertEqual(
                    allocate_tenant_endpoint(root, {}, "tenant-d"),
                    "172.18.255.224",
                )
                with self.assertRaisesRegex(RuntimeError, "canonical absence"):
                    release_tenant_endpoint(
                        root,
                        {},
                        "tenant-c",
                        canonical_absent=False,
                    )
                release_tenant_endpoint(
                    root,
                    {},
                    "tenant-c",
                    canonical_absent=True,
                )
                self.assertEqual(
                    allocate_tenant_endpoint(root, {}, "tenant-e"),
                    "172.18.255.223",
                )
            record = root / ".runtime" / "management" / "tenant-endpoints.json"
            self.assertEqual(record.stat().st_mode & 0o077, 0)

    def test_tenant_endpoint_allocation_rejects_changed_network_identity(self) -> None:
        network = {
            "network_id": "network-id",
            "pool_start": "172.18.255.220",
            "pool_end": "172.18.255.223",
            "slots": {"spike": "172.18.255.220"},
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch(
                "scripts.lib.management.validate_management_network",
                return_value=network,
            ):
                allocate_tenant_endpoint(root, {}, "tenant-c")
            changed = dict(network, network_id="changed")
            with patch(
                "scripts.lib.management.validate_management_network",
                return_value=changed,
            ):
                with self.assertRaisesRegex(IntegrityError, "invalid"):
                    allocate_tenant_endpoint(root, {}, "tenant-c")

    def test_tenant_endpoint_allocation_rejects_exhaustion(self) -> None:
        network = {
            "network_id": "network-id",
            "pool_start": "172.18.255.220",
            "pool_end": "172.18.255.221",
            "slots": {
                "spike": "172.18.255.220",
                "tenant-a": "172.18.255.221",
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch(
                "scripts.lib.management.validate_management_network",
                return_value=network,
            ):
                with self.assertRaisesRegex(RuntimeError, "exhausted"):
                    allocate_tenant_endpoint(root, {}, "tenant-c")

    def test_tenant_endpoint_record_rejects_duplicates_and_ipv6_unchanged(
        self,
    ) -> None:
        network = {
            "network_id": "network-id",
            "pool_start": "172.18.255.220",
            "pool_end": "172.18.255.225",
            "slots": {"spike": "172.18.255.220"},
        }
        records = (
            (
                '{"schema":1,"networkId":"network-id",'
                '"allocations":{"tenant-c":"172.18.255.221",'
                '"tenant-c":"172.18.255.222"}}\n',
                "duplicate",
            ),
            (
                '{"schema":1,"networkId":"network-id",'
                '"allocations":{"tenant-c":"2001:db8::1"}}\n',
                "IPv4",
            ),
            (
                '{"schema":true,"networkId":"network-id",'
                '"allocations":{}}\n',
                "invalid",
            ),
        )
        for content, error in records:
            with self.subTest(error=error):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    record = (
                        root
                        / ".runtime"
                        / "management"
                        / "tenant-endpoints.json"
                    )
                    record.parent.mkdir(parents=True)
                    for parent in (root / ".runtime", record.parent):
                        parent.chmod(0o700)
                    record.write_text(content, encoding="utf-8")
                    record.chmod(0o600)
                    before = record.read_bytes()
                    with patch(
                        "scripts.lib.management.validate_management_network",
                        return_value=network,
                    ):
                        with self.assertRaisesRegex(IntegrityError, error):
                            allocate_tenant_endpoint(root, {}, "tenant-d")
                    self.assertEqual(record.read_bytes(), before)

    def test_management_server_version_must_match_exactly(self) -> None:
        client = type(
            "Client",
            (),
            {
                "kubectl": lambda *_args, **_kwargs: CompletedProcess(
                    [], 0, stdout='{"serverVersion":{"gitVersion":"v1.34.1"}}'
                )
            },
        )()
        validate_management_server_version(
            {"KUBERNETES_VERSION": "v1.34.1"}, client
        )
        with self.assertRaisesRegex(RuntimeError, "version mismatch"):
            validate_management_server_version(
                {"KUBERNETES_VERSION": "v1.34.2"}, client
            )

    def test_management_network_record_must_match_live_identity(self) -> None:
        record = {
            "schema": 1,
            "network": "kind",
            "network_id": "network-id",
            "subnet": "172.18.0.0/16",
            "pool_start": "172.18.255.250",
            "pool_end": "172.18.255.240",
            "pool_cidrs": [],
            "slots": {},
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / ".runtime/management/network.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(record))
            path.chmod(0o600)
            with patch(
                "scripts.lib.management._observed_management_network",
                return_value=record,
            ):
                self.assertEqual(
                    validate_management_network(root, {}), record
                )
            changed = dict(record, network_id="foreign")
            with patch(
                "scripts.lib.management._observed_management_network",
                return_value=changed,
            ):
                with self.assertRaisesRegex(RuntimeError, "identity changed"):
                    validate_management_network(root, {})

    def test_management_images_are_restored_and_imported_before_workloads(self) -> None:
        calls: list[str] = []
        config = {"KIND_CLUSTER_NAME": "management"}
        with (
            patch("scripts.create_management.run_preflight", side_effect=lambda *_: calls.append("preflight")),
            patch(
                "scripts.create_management.restore_host_images",
                side_effect=lambda *_, **__: calls.append("restore"),
            ),
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
