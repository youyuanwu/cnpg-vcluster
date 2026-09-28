from __future__ import annotations

import io
import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts.azure import (
    _run_profile_mutation,
)
from scripts.lib.azure.deletion import (
    _exact_delete_management_resource,
    _enable_capz_external_control_plane_delete,
    _exclude_tenant_machines_from_drain,
)
from scripts.lib.azure.lifecycle import AzureTenantAdapter
from scripts.lib.azure.contracts import _management_resource_specs
from scripts.lib.azure.ownership import (
    _classify_management_owned_resources,
    _discover_owned_repeatedly,
    classify_azure_owned_resources,
    discover_azure_owned_resources,
)
from scripts.lib.azure.rendering import (
    _azure_tags,
    _reconcile_manifest,
    _render_addon_job,
    _render_tenant_control_plane,
    _render_worker_pool,
)
from scripts.lib.azure.readiness import (
    _capture_tenant_kubeconfig,
    _collect_ready_observations,
    _tenant_spec_blockers,
    _wait_ready_observations,
)
from scripts.lib.azure.common import (
    FOUNDATION_INVENTORY_SCHEMA,
    _azure_id_equal,
    _foundation_defaults_checksum,
    _validate_networks,
    azure_tenant_runtime_path,
    load_azure_configuration,
    names,
    tenant_names,
)
from scripts.lib.azure.foundation import (
    _capz_external_control_plane_webhook_ready,
    create_foundation,
    load_inventory,
    preflight,
)
from scripts.lib.config import ConfigError
from scripts.lib.files import write_private_file
from scripts.lib.locking import azure_lock
from scripts.lib.tenant_runtime import TenantRuntime, foundation_sha256
from scripts.lib.tenant_spec import TenantSpec, TenantSpecError
from scripts.lib.tenant_status import TenantStatus
from scripts.lib.tenant_timing import TenantTimings, record_rejected_create
from scripts.lib.tenants import LIFECYCLE_MARKERS, lifecycle_markers


from tests.azure_fixtures import AzureFixtureMixin, DEFAULTS, FOUNDATION, SUBSCRIPTION

def completed(stdout: str = "", returncode: int = 0):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


class AzurePhaseFourTests(AzureFixtureMixin, unittest.TestCase):

    def test_azure_resource_ids_are_case_insensitive(self):
        self.assertTrue(
            _azure_id_equal(
                "/subscriptions/x/resourcegroups/rg/providers/example/item",
                "/subscriptions/x/resourceGroups/rg/providers/example/item",
            )
        )
        self.assertFalse(
            _azure_id_equal(
                "/subscriptions/x/resourceGroups/a",
                "/subscriptions/x/resourceGroups/b",
            )
        )
    def test_tenant_timing_evidence_is_azure_only(self):
        root = self.make_root()
        timings = TenantTimings(
            root,
            tenant="tenant-c",
            operation="create",
            operation_id="operation-1",
        )
        timings.record_passed("validation", 0.25)
        evidence = timings.persist()
        self.assertEqual(
            evidence.relative_to(root).as_posix(),
            ".runtime/lifecycle/azure/tenant-c/evidence/create-operation-1.json",
        )

        record_rejected_create(
            root,
            operation_id="operation-2",
            seconds=0.5,
            error=RuntimeError("rejected"),
        )
        self.assertTrue(
            (
                root
                / ".runtime/lifecycle/rejected/azure/create-operation-2.json"
            ).is_file()
        )
        self.assertFalse((root / ".runtime/lifecycle/rejected/local").exists())
















    def test_network_validation_checks_every_foundation_and_recorded_range(self):
        root = self.make_root()
        config = load_azure_configuration(root)
        recorded = self.spec(
            "other",
            podCIDR="10.73.0.0/16",
            serviceCIDR="10.143.0.0/16",
        )
        cases = {
            "VNet": "10.220.32.0/24",
            "AKS Pod": "10.221.2.0/24",
            "AKS Service": "10.222.2.0/24",
            "recorded Pod": "10.73.2.0/24",
            "recorded Service": "10.143.2.0/24",
        }
        for label, pod in cases.items():
            with self.subTest(label=label):
                spec = self.spec(podCIDR=pod)
                with self.assertRaisesRegex(TenantSpecError, "overlaps"):
                    _validate_networks(config, spec, recorded_specs=(recorded,))

    def test_network_validation_supplies_all_shared_and_recorded_ranges(self):
        root = self.make_root()
        config = load_azure_configuration(root)
        spec = self.spec()
        recorded = self.spec(
            "other",
            podCIDR="10.73.0.0/16",
            serviceCIDR="10.143.0.0/16",
        )
        with patch(
            "scripts.lib.azure.common.require_non_overlapping_networks"
        ) as validate:
            _validate_networks(config, spec, recorded_specs=(recorded,))
        networks = validate.call_args.args[1]
        self.assertEqual(
            set(networks),
            {
                "Azure VNet",
                "AKS subnet",
                "tenant node subnet",
                "AKS Pod CIDR",
                "AKS Service CIDR",
                "tenant other Pod CIDR",
                "tenant other Service CIDR",
            },
        )















    def test_absent_status_is_read_only_and_distinguishes_foundation_health(self):
        root = self.make_root()
        adapter = AzureTenantAdapter()
        before = sorted(path.relative_to(root) for path in root.rglob("*"))
        with (
            patch(
                "scripts.lib.azure.lifecycle._inspect_foundation",
                return_value=(FOUNDATION, True, ()),
            ),
            patch("scripts.lib.azure.lifecycle._get_management_resource", return_value=None),
            patch("scripts.lib.azure.deletion._json", return_value=[]),
        ):
            status = adapter.status(root, "missing")
        after = sorted(path.relative_to(root) for path in root.rglob("*"))
        self.assertEqual(status.classification, "absent")
        self.assertTrue(status.foundation_healthy)
        self.assertEqual(before, after)
        with patch(
            "scripts.lib.azure.lifecycle._inspect_foundation",
            side_effect=RuntimeError("foundation unavailable"),
        ):
            status = adapter.status(root, "missing")
        self.assertEqual(status.classification, "degraded")
        self.assertFalse(status.foundation_healthy)

    def test_absent_status_rejects_tenant_runtime_residue(self):
        root = self.make_root()
        adapter = AzureTenantAdapter()
        residue = azure_tenant_runtime_path(root, "missing") / "endpoint.json"
        write_private_file(residue, "{}\n")
        with (
            patch(
                "scripts.lib.azure.lifecycle._inspect_foundation",
                return_value=(FOUNDATION, True, ()),
            ),
            patch("scripts.lib.azure.lifecycle._get_management_resource", return_value=None),
            patch("scripts.lib.azure.deletion._json", return_value=[]),
        ):
            status = adapter.status(root, "missing")
        self.assertEqual(status.classification, "ownership-invalid")
        self.assertIn("endpoint.json", status.components["runtimeResidue"])

    def test_absent_status_fails_closed_on_inspection_error_or_malformed_azure_list(
        self,
    ):
        root = self.make_root()
        adapter = AzureTenantAdapter()
        with (
            patch(
                "scripts.lib.azure.lifecycle._inspect_foundation",
                return_value=(FOUNDATION, True, ()),
            ),
            patch(
                "scripts.lib.azure.lifecycle._get_management_resource",
                side_effect=RuntimeError("Forbidden"),
            ),
            patch("scripts.lib.azure.deletion._json", return_value=[]),
        ):
            status = adapter.status(root, "missing")
        self.assertEqual(status.classification, "ownership-invalid")
        with (
            patch(
                "scripts.lib.azure.lifecycle._inspect_foundation",
                return_value=(FOUNDATION, True, ()),
            ),
            patch("scripts.lib.azure.lifecycle._get_management_resource", return_value=None),
            patch("scripts.lib.azure.deletion._json", return_value={"unexpected": True}),
        ):
            status = adapter.status(root, "missing")
        self.assertEqual(status.classification, "ownership-invalid")

    def test_management_get_only_accepts_kubernetes_object_not_found(self):
        from scripts.lib.azure.foundation import _get_management_resource

        failures = (
            "Unable to connect to the server: getting credentials: "
            "exec: executable kubelogin not found",
            "dial tcp: lookup host: no such host",
            "transport connection failed: host not found",
            "Error from server (Forbidden): forbidden",
        )
        for stderr in failures:
            with (
                self.subTest(stderr=stderr),
                patch(
                    "scripts.lib.azure.foundation._kubectl",
                    return_value=subprocess.CompletedProcess(
                        [], 1, stdout="", stderr=stderr
                    ),
                ),
                self.assertRaisesRegex(RuntimeError, "inspection failed"),
            ):
                _get_management_resource(
                    self.make_root(),
                    "tenant-c",
                    "cluster/tenant-c",
                )
        with patch(
            "scripts.lib.azure.foundation._kubectl",
            return_value=subprocess.CompletedProcess(
                [],
                1,
                stdout="",
                stderr=(
                    'Error from server (NotFound): clusters "tenant-c" not found'
                ),
            ),
        ):
            self.assertIsNone(
                _get_management_resource(
                    self.make_root(),
                    "tenant-c",
                    "cluster/tenant-c",
                )
            )



    def test_staged_commands_are_removed(self):
        root = Path(__file__).resolve().parents[1]
        justfile = (root / "Justfile").read_text(encoding="utf-8")
        source = (root / "scripts" / "azure.py").read_text(encoding="utf-8")
        for command in (
            "azure-create-tenant-control-plane",
            "azure-create-worker",
            "azure-install-addons",
            "azure-status:",
            '"create-tenant-control-plane"',
            '"create-worker"',
            '"install-addons"',
        ):
            self.assertNotIn(command, justfile + source)
        self.assertIn("azure-foundation-status:", justfile)

    def test_tracked_example_contains_no_subscription_or_secret(self):
        example = (
            Path(__file__).resolve().parents[1]
            / "config"
            / "tenants"
            / "examples"
            / "azure.json"
        ).read_text(encoding="utf-8")
        self.assertNotRegex(example, r"[0-9a-f]{8}-[0-9a-f-]{27,}")
        self.assertNotRegex(example.lower(), r"subscription|clientsecret|password")


class AzurePhaseFiveTests(AzureFixtureMixin, unittest.TestCase):
    make_root = AzurePhaseFourTests.make_root
    spec = staticmethod(AzurePhaseFourTests.spec)
    start_journal = AzurePhaseFourTests.start_journal
    inventory = AzurePhaseFourTests.inventory


    def test_capz_webhook_selector_must_be_exact(self):
        expected = {
            "matchExpressions": [
                {
                    "key": "cnpg-vcluster-external-control-plane",
                    "operator": "NotIn",
                    "values": ["true"],
                }
            ]
        }
        payload = {
            "webhooks": [
                {
                    "name": "default.azurecluster.infrastructure.cluster.x-k8s.io",
                    "objectSelector": expected,
                }
            ]
        }
        self.assertTrue(_capz_external_control_plane_webhook_ready(payload))
        payload["webhooks"][0]["objectSelector"] = {
            **expected,
            "matchLabels": {"other": "value"},
        }
        self.assertFalse(_capz_external_control_plane_webhook_ready(payload))
        payload["webhooks"][0]["objectSelector"] = {
            "matchExpressions": [
                *expected["matchExpressions"],
                {
                    "key": "cnpg-vcluster-external-control-plane",
                    "operator": "Exists",
                },
            ]
        }
        self.assertFalse(_capz_external_control_plane_webhook_ready(payload))
















    def test_authoritative_absence_ignores_pending_delete_journal(self):
        root = self.make_root()
        spec = self.spec()
        runtime = TenantRuntime(root, spec.name)
        runtime.start_operation(
            operation="delete",
            spec=spec,
            foundation_identity=FOUNDATION,
            intended_resources=AzureTenantAdapter.intended_resources(spec),
            operation_id="pending-delete",
        )
        adapter = AzureTenantAdapter()
        with (
            patch.object(adapter, "_config", return_value=load_azure_configuration(root)),
            patch(
                "scripts.lib.azure.lifecycle._inspect_foundation",
                return_value=(FOUNDATION, True, ()),
            ),
            patch("scripts.lib.azure.lifecycle._get_management_resource", return_value=None),
            patch("scripts.lib.azure.deletion._json", return_value=[]),
        ):
            status = adapter.authoritative_absence(root, spec.name)
        self.assertEqual(status.classification, "absent")
        self.assertTrue(runtime.operation_exists())

    def test_source_forbids_direct_vmss_delete_and_provider_finalizer_removal(self):
        source = (
            Path(__file__).resolve().parents[1] / "scripts" / "azure.py"
        ).read_text(encoding="utf-8")
        self.assertNotRegex(
            source,
            r"[\"']vmss[\"']\s*,\s*[\"']delete[\"']",
        )
        self.assertNotIn("/metadata/finalizers", source)
        self.assertNotRegex(
            source.lower(),
            r"(azurecluster|azuremachinepool|natgateway).{0,120}finalizers.{0,120}"
            r"(patch|replace)",
        )



if __name__ == "__main__":
    unittest.main()
