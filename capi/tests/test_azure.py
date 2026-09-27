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
    AzureTenantAdapter,
    _classify_management_owned_resources,
    _discover_owned_repeatedly,
    _exact_delete_management_resource,
    _enable_capz_external_control_plane_delete,
    _exclude_tenant_machines_from_drain,
    _management_resource_specs,
    _run_profile_mutation,
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












    def test_discovery_fails_closed_on_aso_api_failure_and_unknown_kind(self):
        root = self.make_root()
        config = load_azure_configuration(root)
        spec = self.spec()
        runtime, journal = self.start_journal(root, spec)
        journal = runtime.update_operation(
            journal,
            phase="workers",
            observed={
                "markerOperationId": journal.operation_id,
                "azureClusterUid": "azure-cluster-uid",
                "azureMachinePoolUid": "azure-pool-uid",
            },
        )
        markers = lifecycle_markers(spec, journal)

        def parent(_root, _namespace, resource):
            uid = (
                "azure-cluster-uid"
                if resource.startswith("azurecluster/")
                else "azure-pool-uid"
            )
            return {
                "metadata": {
                    "uid": uid,
                    "annotations": {
                        LIFECYCLE_MARKERS[key]: value
                        for key, value in markers.items()
                    },
                }
            }

        with (
            patch(
                "scripts.azure._get_management_resource",
                side_effect=parent,
            ),
            patch("scripts.azure.load_inventory", return_value=self.inventory(root, config)),
            patch("scripts.azure._json", return_value=[]),
            patch(
                "scripts.azure._kubectl",
                return_value=subprocess.CompletedProcess(
                    [], 1, stdout="", stderr="forbidden"
                ),
            ),
            self.assertRaisesRegex(RuntimeError, "API discovery failed"),
        ):
            discover_azure_owned_resources(root, config, spec, journal)

        parentless = {
            "kind": "NatGateway",
            "metadata": {
                "name": "parentless",
                "uid": "parentless-uid",
            },
            "status": {
                "id": (
                    "/subscriptions/redacted/resourceGroups/yy-cv-rg/"
                    "providers/Microsoft.Network/natGateways/parentless"
                )
            },
        }
        responses = iter(
            (
                subprocess.CompletedProcess(
                    [], 0, stdout="natgateways.network.azure.com\n", stderr=""
                ),
                subprocess.CompletedProcess([], 0, stdout="", stderr=""),
                subprocess.CompletedProcess([], 0, stdout="", stderr=""),
                subprocess.CompletedProcess(
                    [],
                    0,
                    stdout=json.dumps({"items": [parentless]}),
                    stderr="",
                ),
            )
        )
        with (
            patch(
                "scripts.azure._get_management_resource",
                side_effect=parent,
            ),
            patch(
                "scripts.azure.load_inventory",
                return_value=self.inventory(root, config),
            ),
            patch("scripts.azure._json", return_value=[]),
            patch(
                "scripts.azure._kubectl",
                side_effect=lambda *_a, **_k: next(responses),
            ),
            self.assertRaisesRegex(RuntimeError, "foreign ASO ownership"),
        ):
            discover_azure_owned_resources(root, config, spec, journal)

        unknown = {
            "kind": "PrivateEndpoint",
            "metadata": {
                "name": "unknown",
                "uid": "unknown-uid",
                "ownerReferences": [{"uid": "azure-cluster-uid"}],
            },
            "status": {},
        }
        responses = iter(
            (
                subprocess.CompletedProcess(
                    [], 0, stdout="privateendpoints.network.azure.com\n", stderr=""
                ),
                subprocess.CompletedProcess([], 0, stdout="", stderr=""),
                subprocess.CompletedProcess([], 0, stdout="", stderr=""),
                subprocess.CompletedProcess(
                    [],
                    0,
                    stdout=json.dumps({"items": [unknown]}),
                    stderr="",
                ),
            )
        )
        with (
            patch(
                "scripts.azure._get_management_resource",
                side_effect=parent,
            ),
            patch("scripts.azure.load_inventory", return_value=self.inventory(root, config)),
            patch("scripts.azure._json", return_value=[]),
            patch("scripts.azure._kubectl", side_effect=lambda *_a, **_k: next(responses)),
            self.assertRaisesRegex(RuntimeError, "ownership is unknown"),
        ):
            discover_azure_owned_resources(root, config, spec, journal)


    def test_foreign_aso_id_cannot_promote_azure_resource(self):
        markers = {
            "tenant": "tenant-c",
            "profile": "azure",
            "specificationSha256": "spec",
            "foundationSha256": "foundation",
            "operationId": "operation",
        }
        resource_id = (
            "/subscriptions/x/resourceGroups/rg/providers/"
            "Microsoft.Network/publicIPAddresses/foreign"
        )
        resources = [
            {
                "id": resource_id,
                "type": "Microsoft.Network/publicIPAddresses",
                "tags": {},
            }
        ]
        foreign = {
            "kind": "PublicIPAddress",
            "metadata": {
                "name": "foreign",
                "uid": "foreign-uid",
                "ownerReferences": [],
            },
            "status": {"id": resource_id},
        }
        with self.assertRaisesRegex(RuntimeError, "foreign ASO ownership"):
            classify_azure_owned_resources(
                resources,
                markers,
                aso_objects=(foreign,),
                verified_ids=(),
            )

    def test_absent_status_is_read_only_and_distinguishes_foundation_health(self):
        root = self.make_root()
        adapter = AzureTenantAdapter()
        before = sorted(path.relative_to(root) for path in root.rglob("*"))
        with (
            patch(
                "scripts.azure._inspect_foundation",
                return_value=(FOUNDATION, True, ()),
            ),
            patch("scripts.azure._get_management_resource", return_value=None),
            patch("scripts.azure._json", return_value=[]),
        ):
            status = adapter.status(root, "missing")
        after = sorted(path.relative_to(root) for path in root.rglob("*"))
        self.assertEqual(status.classification, "absent")
        self.assertTrue(status.foundation_healthy)
        self.assertEqual(before, after)
        with patch(
            "scripts.azure._inspect_foundation",
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
                "scripts.azure._inspect_foundation",
                return_value=(FOUNDATION, True, ()),
            ),
            patch("scripts.azure._get_management_resource", return_value=None),
            patch("scripts.azure._json", return_value=[]),
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
                "scripts.azure._inspect_foundation",
                return_value=(FOUNDATION, True, ()),
            ),
            patch(
                "scripts.azure._kubectl",
                return_value=subprocess.CompletedProcess(
                    [], 1, stdout="", stderr="Forbidden"
                ),
            ),
            patch("scripts.azure._json", return_value=[]),
        ):
            status = adapter.status(root, "missing")
        self.assertEqual(status.classification, "ownership-invalid")
        with (
            patch(
                "scripts.azure._inspect_foundation",
                return_value=(FOUNDATION, True, ()),
            ),
            patch("scripts.azure._get_management_resource", return_value=None),
            patch("scripts.azure._json", return_value={"unexpected": True}),
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


class AzurePhaseFiveTests(unittest.TestCase):
    make_root = AzurePhaseFourTests.make_root
    spec = staticmethod(AzurePhaseFourTests.spec)
    start_journal = AzurePhaseFourTests.start_journal
    inventory = AzurePhaseFourTests.inventory

    def ready_identity(self, root: Path, spec: TenantSpec):
        runtime, journal = self.start_journal(root, spec)
        observed = {
            "markerOperationId": journal.operation_id,
            "tenantKubeconfigSecretUid": "tenant-kubeconfig-secret-uid",
            "tenantKubeconfigSha256": "kubeconfig-sha256",
            "vmssId": (
                "/subscriptions/redacted/resourceGroups/yy-cv-rg/providers/"
                "Microsoft.Compute/virtualMachineScaleSets/tenant-c-worker"
            ),
            "vmssInstanceIds": "[]",
            "azureResources": json.dumps(
                {"azure": [], "aso": [], "unknown": []},
                sort_keys=True,
                separators=(",", ":"),
            ),
        }
        for key, _, _, _ in _management_resource_specs(spec):
            observed[key] = f"{key}-value"
        runtime.complete_create(runtime.load_operation(), spec, observed)
        write_private_file(
            azure_tenant_runtime_path(root, spec.name) / "endpoint.json",
            "{}\n",
        )
        return runtime, runtime.load_identity()

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

    def management_payloads(self, spec, identity):
        markers = {
            "tenant": spec.name,
            "profile": "azure",
            "specificationSha256": spec.sha256(),
            "foundationSha256": foundation_sha256(identity.foundation_identity),
            "operationId": identity.observed["markerOperationId"],
        }
        payloads = []
        namespace = None
        for key, namespace_name, kind, name in _management_resource_specs(spec):
            payload = {
                "apiVersion": "v1",
                "kind": kind,
                "metadata": {
                    "name": name,
                    "uid": identity.observed[key],
                    "resourceVersion": f"{key}-rv",
                    "annotations": {
                        LIFECYCLE_MARKERS[marker]: value
                        for marker, value in markers.items()
                    },
                },
            }
            if namespace_name is not None:
                payload["metadata"]["namespace"] = namespace_name
            if kind == "Namespace":
                namespace = payload
            else:
                payloads.append(payload)
        payloads.append(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {
                    "name": f"{spec.name}-kubeconfig",
                    "uid": identity.observed["tenantKubeconfigSecretUid"],
                    "resourceVersion": "secret-rv",
                    "ownerReferences": [
                        {
                            "uid": identity.observed["kamajiControlPlaneUid"],
                            "controller": True,
                        }
                    ],
                },
            }
        )
        return namespace, payloads

    def test_management_inventory_refuses_unknown_and_foreign_state(self):
        root = self.make_root()
        spec = self.spec()
        _, identity = self.ready_identity(root, spec)
        namespace, payloads = self.management_payloads(spec, identity)
        payloads.append(
            {
                "apiVersion": "unknown.example/v1",
                "kind": "Mystery",
                "metadata": {
                    "name": "late",
                    "uid": "late-uid",
                    "resourceVersion": "1",
                },
            }
        )
        with self.assertRaisesRegex(RuntimeError, "ownership is unknown"):
            _classify_management_owned_resources(
                spec,
                identity,
                namespace,
                payloads,
                require_complete=True,
            )
        payloads.pop()
        payloads[0]["metadata"]["annotations"][
            LIFECYCLE_MARKERS["tenant"]
        ] = "foreign"
        with self.assertRaisesRegex(RuntimeError, "foreign.*markers"):
            _classify_management_owned_resources(
                spec,
                identity,
                namespace,
                payloads,
                require_complete=True,
            )

    def test_management_inventory_accepts_capz_generated_and_shared_references(self):
        root = self.make_root()
        spec = self.spec()
        _, identity = self.ready_identity(root, spec)
        namespace, payloads = self.management_payloads(spec, identity)
        markers = {
            LIFECYCLE_MARKERS["tenant"]: spec.name,
            LIFECYCLE_MARKERS["profile"]: "azure",
            LIFECYCLE_MARKERS["specificationSha256"]: spec.sha256(),
            LIFECYCLE_MARKERS["foundationSha256"]: foundation_sha256(
                identity.foundation_identity
            ),
            LIFECYCLE_MARKERS["operationId"]: identity.observed["markerOperationId"],
        }
        azure_cluster_uid = identity.observed["azureClusterUid"]
        payloads.extend(
            (
                {
                    "kind": "AzureMachinePoolMachine",
                    "metadata": {
                        "name": f"{spec.name}-worker-0",
                        "uid": "azure-pool-machine-uid",
                        "ownerReferences": [
                            {"uid": identity.observed["azureMachinePoolUid"]}
                        ],
                    },
                },
                {
                    "kind": "PodMetrics",
                    "metadata": {
                        "name": "status-probe",
                        "uid": "metrics-uid",
                    },
                },
                {
                    "kind": "NatGateway",
                    "metadata": {
                        "name": f"{spec.name}-node-natgw-1",
                        "uid": "orphaned-nat-gateway-uid",
                    },
                    "spec": {
                        "tags": {
                            "cnpg-vcluster-tenant": spec.name,
                            "cnpg-vcluster-profile": "azure",
                            "cnpg-vcluster-spec-sha256": spec.sha256(),
                            "cnpg-vcluster-foundation-sha256": foundation_sha256(
                                identity.foundation_identity
                            ),
                            "cnpg-vcluster-operation-id": identity.observed[
                                "markerOperationId"
                            ],
                        }
                    },
                },
                {
                    "kind": "ReplicaSet",
                    "metadata": {
                        "name": "status-probe",
                        "uid": "replica-set-uid",
                        "ownerReferences": [
                            {"uid": identity.observed["statusProbeDeploymentUid"]}
                        ],
                    },
                },
                {
                    "kind": "Pod",
                    "metadata": {
                        "name": "status-probe-pod",
                        "uid": "pod-uid",
                        "ownerReferences": [{"uid": "replica-set-uid"}],
                    },
                },
                *(
                    {
                        "kind": kind,
                        "metadata": {
                            "name": name,
                            "uid": f"{kind}-uid",
                            "ownerReferences": [{"uid": azure_cluster_uid}],
                            "annotations": markers,
                        },
                    }
                    for kind, name in (
                        ("ResourceGroup", "yy-cv-rg"),
                        ("VirtualNetwork", "yy-cv-vnet"),
                        ("VirtualNetworksSubnet", "yy-cv-vnet-tenant"),
                    )
                ),
            )
        )
        classified = _classify_management_owned_resources(
            spec,
            identity,
            namespace,
            payloads,
            require_complete=True,
        )
        controller_kinds = {item["kind"] for item in classified["controller"]}
        self.assertTrue(
            {
                "AzureMachinePoolMachine",
                "NatGateway",
                "ResourceGroup",
                "VirtualNetwork",
                "VirtualNetworksSubnet",
            }.issubset(controller_kinds)
        )
        self.assertIn(
            "PodMetrics",
            {item["kind"] for item in classified["namespaceChildren"]},
        )
        self.assertTrue(
            {"Pod", "ReplicaSet"}.issubset(
                {item["kind"] for item in classified["namespaceChildren"]}
            )
        )

    def test_management_cleanup_accepts_only_snapshotted_orphan_uid(self):
        root = self.make_root()
        spec = self.spec()
        _, identity = self.ready_identity(root, spec)
        namespace, payloads = self.management_payloads(spec, identity)
        orphan = {
            "kind": "Service",
            "metadata": {
                "name": spec.name,
                "uid": "snapshotted-service-uid",
                "deletionTimestamp": "2026-01-01T00:00:00Z",
                "ownerReferences": [{"uid": "deleted-owner-uid"}],
            },
        }
        payloads.append(orphan)
        with self.assertRaisesRegex(RuntimeError, "ownership is unknown"):
            _classify_management_owned_resources(
                spec,
                identity,
                namespace,
                payloads,
                require_complete=False,
            )
        classified = _classify_management_owned_resources(
            spec,
            identity,
            namespace,
            payloads,
            require_complete=False,
            verified_uids=("snapshotted-service-uid",),
        )
        self.assertIn(
            "snapshotted-service-uid",
            {item["uid"] for item in classified["controller"]},
        )

    def test_delete_resume_restores_recorded_discovery_identities(self):
        root = self.make_root()
        spec = self.spec()
        runtime, identity = self.ready_identity(root, spec)
        journal = runtime.start_operation(
            operation="delete",
            spec=spec,
            foundation_identity=identity.foundation_identity,
            intended_resources=AzureTenantAdapter.intended_resources(spec),
            operation_id="delete-resume",
        )
        management = {
            "controller": [
                {
                    "kind": "Service",
                    "name": spec.name,
                    "uid": "recorded-service-uid",
                }
            ],
            "orchestration": [],
            "namespaceChildren": [],
            "unknown": [],
        }
        azure = {
            "azure": [
                {
                    "id": (
                        "/subscriptions/redacted/resourceGroups/yy-cv-rg/"
                        "providers/Microsoft.Network/publicIPAddresses/recorded"
                    ),
                    "type": "microsoft.network/publicipaddresses",
                }
            ],
            "aso": [],
            "unknown": [],
        }
        runtime.update_operation(
            journal,
            phase="controller-cleanup",
            observed={
                "deleteManagementBefore": json.dumps(management),
                "deleteAzureBefore": json.dumps(azure),
            },
        )
        empty_management = {
            "controller": [],
            "orchestration": [],
            "namespaceChildren": [],
            "unknown": [],
        }
        empty_azure = {"azure": [], "aso": [], "unknown": []}
        adapter = AzureTenantAdapter()
        with (
            patch.object(
                adapter,
                "_config",
                return_value=load_azure_configuration(root),
            ),
            patch("scripts.azure._active_subscription"),
            patch(
                "scripts.azure._inspect_foundation",
                return_value=(FOUNDATION, True, ()),
            ),
            patch(
                "scripts.azure.discover_management_owned_resources",
                return_value=empty_management,
            ) as management_discovery,
            patch(
                "scripts.azure._discover_owned_repeatedly",
                return_value=empty_azure,
            ) as azure_discovery,
        ):
            adapter.validate_delete(root, spec, identity)
        self.assertIn(
            "recorded-service-uid",
            management_discovery.call_args.kwargs["verified_uids"],
        )
        self.assertIn(
            azure["azure"][0]["id"],
            azure_discovery.call_args.kwargs["verified_resource_ids"],
        )
        self.assertEqual(
            adapter._delete_snapshots[spec.name]["management"]["controller"][0][
                "uid"
            ],
            "recorded-service-uid",
        )

    def test_capz_external_control_plane_delete_workaround_is_delete_only(self):
        root = self.make_root()
        config = load_azure_configuration(root)
        spec = self.spec()
        _, identity = self.ready_identity(root, spec)
        markers = {
            LIFECYCLE_MARKERS["tenant"]: spec.name,
            LIFECYCLE_MARKERS["profile"]: "azure",
            LIFECYCLE_MARKERS["specificationSha256"]: spec.sha256(),
            LIFECYCLE_MARKERS["foundationSha256"]: foundation_sha256(
                identity.foundation_identity
            ),
            LIFECYCLE_MARKERS["operationId"]: identity.observed["markerOperationId"],
        }
        payload = {
            "metadata": {
                "uid": identity.observed["azureClusterUid"],
                "deletionTimestamp": "2026-01-01T00:00:00Z",
                "annotations": markers,
                "labels": {
                    "cnpg-vcluster-external-control-plane": "true",
                },
            },
            "spec": {
                "controlPlaneEnabled": False,
                "networkSpec": {"subnets": [{"name": "tenant", "role": "node"}]},
            },
        }
        updated = {
            **payload,
            "spec": {
                "controlPlaneEnabled": False,
                "networkSpec": {
                    "apiServerLB": {"type": "Public"},
                    "subnets": payload["spec"]["networkSpec"]["subnets"],
                },
            },
        }
        with (
            patch("scripts.azure._get_management_resource", side_effect=(payload, updated)),
            patch("scripts.azure._kubectl") as kubectl,
        ):
            _enable_capz_external_control_plane_delete(
                root,
                spec,
                identity,
            )
        patch_payload = json.loads(kubectl.call_args.args[-1])
        self.assertEqual(
            patch_payload,
            {"spec": {"networkSpec": {"apiServerLB": {"type": "Public"}}}},
        )

        not_deleting = {
            **payload,
            "metadata": {
                **payload["metadata"],
                "deletionTimestamp": None,
            },
        }
        with (
            patch(
                "scripts.azure._get_management_resource",
                return_value=not_deleting,
            ),
            patch("scripts.azure.time.monotonic", side_effect=(0, 121)),
            patch("scripts.azure.time.sleep"),
            patch("scripts.azure._kubectl") as kubectl,
            self.assertRaisesRegex(RuntimeError, "deletion did not start"),
        ):
            _enable_capz_external_control_plane_delete(
                root,
                spec,
                identity,
            )
        kubectl.assert_not_called()

    def test_delete_marks_only_owned_tenant_machines_to_skip_drain(self):
        root = self.make_root()
        spec = self.spec()
        _, identity = self.ready_identity(root, spec)
        markers = {
            LIFECYCLE_MARKERS["tenant"]: spec.name,
            LIFECYCLE_MARKERS["profile"]: "azure",
            LIFECYCLE_MARKERS["specificationSha256"]: spec.sha256(),
            LIFECYCLE_MARKERS["foundationSha256"]: foundation_sha256(
                identity.foundation_identity
            ),
            LIFECYCLE_MARKERS["operationId"]: identity.observed["markerOperationId"],
        }
        machine = {
            "kind": "Machine",
            "metadata": {
                "name": f"{spec.name}-worker-0",
                "uid": "machine-uid",
                "annotations": markers,
                "ownerReferences": [{"uid": identity.observed["machinePoolUid"]}],
            },
        }
        with patch(
            "scripts.azure._kubectl",
            side_effect=(
                subprocess.CompletedProcess(
                    [],
                    0,
                    stdout=json.dumps({"items": [machine]}),
                    stderr="",
                ),
                subprocess.CompletedProcess([], 0, stdout="", stderr=""),
            ),
        ) as kubectl:
            _exclude_tenant_machines_from_drain(root, spec, identity)
        patch_arguments = kubectl.call_args_list[1].args
        patch_payload = json.loads(patch_arguments[-1])
        self.assertEqual(patch_payload[0]["value"], "machine-uid")
        self.assertEqual(
            patch_payload[1]["path"],
            "/metadata/annotations/machine.cluster.x-k8s.io~1exclude-node-draining",
        )

    def test_worker_cleanup_waits_for_machines_and_vmss(self):
        root = self.make_root()
        config = load_azure_configuration(root)
        spec = self.spec()
        _, identity = self.ready_identity(root, spec)
        adapter = AzureTenantAdapter(
            monotonic=iter((0, 10_000)).__next__,
            sleep=lambda _seconds: None,
        )
        machine = {
            "metadata": {
                "name": f"{spec.name}-worker-0",
            }
        }
        with (
            patch(
                "scripts.azure.load_inventory",
                return_value={
                    "outputs": {"resourceGroupName": "yy-cv-rg"}
                },
            ),
            patch("scripts.azure._get_management_resource", return_value=None),
            patch(
                "scripts.azure._owned_tenant_machines",
                return_value=[machine],
            ),
            patch(
                "scripts.azure._az",
                return_value=subprocess.CompletedProcess(
                    [],
                    0,
                    stdout=json.dumps([identity.observed["vmssId"]]),
                    stderr="",
                ),
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "machine/tenant-c-worker-0.*vmss/tenant-c-worker",
            ),
        ):
            adapter._wait_for_worker_cleanup(
                root,
                config,
                spec,
                identity,
            )

    def test_delete_validation_refuses_foundation_and_uid_before_mutation(self):
        root = self.make_root()
        spec = self.spec()
        _, identity = self.ready_identity(root, spec)
        adapter = AzureTenantAdapter()
        with (
            patch.object(adapter, "_config", return_value=load_azure_configuration(root)),
            patch("scripts.azure._active_subscription"),
            patch(
                "scripts.azure._inspect_foundation",
                return_value=({**FOUNDATION, "aksId": "foreign"}, True, ()),
            ),
            patch("scripts.azure._exact_delete_management_resource") as mutate,
            self.assertRaisesRegex(RuntimeError, "foundation binding changed"),
        ):
            adapter.validate_delete(root, spec, identity)
        mutate.assert_not_called()

        namespace, payloads = self.management_payloads(spec, identity)
        payloads[0]["metadata"]["uid"] = "replacement"
        with self.assertRaisesRegex(RuntimeError, "UID changed"):
            _classify_management_owned_resources(
                spec,
                identity,
                namespace,
                payloads,
                require_complete=True,
            )

    def test_exact_delete_validates_uid_resource_version_and_markers(self):
        root = self.make_root()
        spec = self.spec()
        _, identity = self.ready_identity(root, spec)
        markers = {
            "tenant": spec.name,
            "profile": "azure",
            "specificationSha256": spec.sha256(),
            "foundationSha256": foundation_sha256(identity.foundation_identity),
            "operationId": identity.observed["markerOperationId"],
        }
        payload = {
            "apiVersion": "cluster.x-k8s.io/v1beta1",
            "kind": "Cluster",
            "metadata": {
                "name": spec.name,
                "uid": identity.observed["clusterUid"],
                "resourceVersion": "42",
                "annotations": {
                    LIFECYCLE_MARKERS[key]: value
                    for key, value in markers.items()
                },
            }
        }
        with (
            patch("scripts.azure._get_management_resource", return_value=payload),
            patch("scripts.azure._kubectl") as kubectl,
        ):
            self.assertTrue(
                _exact_delete_management_resource(
                    root,
                    spec,
                    identity,
                    namespace=spec.namespace,
                    resource=f"cluster/{spec.name}",
                    uid_key="clusterUid",
                    cascade="foreground",
                )
            )
        arguments = kubectl.call_args.args
        self.assertIn(
            (
                "--raw=/apis/cluster.x-k8s.io/v1beta1/namespaces/"
                f"{spec.namespace}/clusters/{spec.name}"
            ),
            arguments,
        )
        options = json.loads(kubectl.call_args.kwargs["input_text"])
        self.assertEqual(
            options["preconditions"],
            {
                "uid": identity.observed["clusterUid"],
                "resourceVersion": "42",
            },
        )
        self.assertEqual(options["propagationPolicy"], "Foreground")
        payload["metadata"]["uid"] = "foreign"
        with (
            patch("scripts.azure._get_management_resource", return_value=payload),
            patch("scripts.azure._kubectl") as kubectl,
            self.assertRaisesRegex(RuntimeError, "UID changed"),
        ):
            _exact_delete_management_resource(
                root,
                spec,
                identity,
                namespace=spec.namespace,
                resource=f"cluster/{spec.name}",
                uid_key="clusterUid",
                cascade="foreground",
            )
        kubectl.assert_not_called()
        namespace, _ = self.management_payloads(spec, identity)
        with (
            patch("scripts.azure._get_management_resource", return_value=namespace),
            patch("scripts.azure._kubectl") as kubectl,
        ):
            _exact_delete_management_resource(
                root,
                spec,
                identity,
                namespace=None,
                resource=f"namespace/{spec.name}",
                uid_key="namespaceUid",
                cascade="foreground",
            )
        self.assertIn(
            f"--raw=/api/v1/namespaces/{spec.name}",
            kubectl.call_args.args,
        )

    def test_repeated_discovery_captures_late_resources(self):
        root = self.make_root()
        spec = self.spec()
        _, identity = self.ready_identity(root, spec)
        first = {
            "azure": [
                {
                    "id": "/subscriptions/redacted/resourceGroups/rg/providers/"
                    "Microsoft.Compute/virtualMachineScaleSets/pool",
                    "type": "microsoft.compute/virtualmachinescalesets",
                }
            ],
            "aso": [],
            "unknown": [],
        }
        late = {
            "azure": [
                {
                    "id": "/subscriptions/redacted/resourceGroups/rg/providers/"
                    "Microsoft.Network/publicIPAddresses/late",
                    "type": "microsoft.network/publicipaddresses",
                }
            ],
            "aso": [],
            "unknown": [],
        }
        with patch(
            "scripts.azure.discover_azure_owned_resources",
            side_effect=(first, late),
        ) as discover:
            result = _discover_owned_repeatedly(
                root,
                load_azure_configuration(root),
                spec,
                identity,
                passes=2,
                require_parents=False,
                require_azure_resources=False,
            )
        self.assertEqual(discover.call_count, 2)
        self.assertEqual(len(result["azure"]), 2)
        self.assertIn(
            first["azure"][0]["id"],
            discover.call_args_list[1].kwargs["verified_resource_ids"],
        )

    def test_delete_orders_controllers_before_orchestration_and_runtime(self):
        root = self.make_root()
        spec = self.spec()
        runtime, identity = self.ready_identity(root, spec)
        journal = runtime.start_operation(
            operation="delete",
            spec=spec,
            foundation_identity=identity.foundation_identity,
            intended_resources=AzureTenantAdapter.intended_resources(spec),
            operation_id="delete-operation",
        )
        adapter = AzureTenantAdapter()
        adapter._delete_snapshots[spec.name] = {
            "foundation": FOUNDATION,
            "management": {
                "controller": [],
                "orchestration": [],
                "namespaceChildren": [],
                "unknown": [],
            },
            "azure": {"azure": [], "aso": [], "unknown": []},
        }
        calls = []

        def delete_resource(*_args, resource, **_kwargs):
            calls.append(f"delete:{resource}")
            return True

        timings = TenantTimings(
            root,
            tenant=spec.name,
            operation="delete",
            operation_id=journal.operation_id,
        )
        with (
            patch.object(adapter, "_config", return_value=load_azure_configuration(root)),
            patch(
                "scripts.azure._exact_delete_management_resource",
                side_effect=delete_resource,
            ),
            patch("scripts.azure._exclude_tenant_machines_from_drain"),
            patch("scripts.azure._enable_capz_external_control_plane_delete"),
            patch.object(
                adapter,
                "_wait_for_worker_cleanup",
                side_effect=lambda *_a: calls.append("workers-absent"),
            ),
            patch.object(
                adapter,
                "_wait_for_controller_cleanup",
                side_effect=lambda *_a: (
                    calls.append("controllers-absent")
                    or (
                        {
                            "controller": [],
                            "orchestration": [],
                            "namespaceChildren": [],
                            "unknown": [],
                        },
                        {"azure": [], "aso": [], "unknown": []},
                    )
                ),
            ),
            patch.object(
                adapter,
                "_wait_for_tenant_absence",
                side_effect=lambda *_a: (
                    calls.append("tenant-absent")
                    or {"azure": [], "aso": [], "unknown": []}
                ),
            ),
            patch(
                "scripts.azure._inspect_foundation",
                side_effect=lambda *_a, **_k: (
                    calls.append("foundation-verified")
                    or (FOUNDATION, True, ())
                ),
            ),
            patch(
                "scripts.azure._remove_private_tree",
                side_effect=lambda *_a: calls.append("runtime-removed"),
            ),
            patch.object(
                adapter,
                "_inspect_absence",
                return_value=TenantStatus(
                    profile="azure",
                    tenant=spec.name,
                    classification="absent",
                    foundation_healthy=True,
                ),
            ),
        ):
            adapter.delete(root, spec, identity, runtime, journal, timings)
        self.assertEqual(calls[0], f"delete:machinepool/{spec.name}-worker")
        self.assertEqual(calls[1], "workers-absent")
        self.assertEqual(calls[2], f"delete:cluster/{spec.name}")
        controller_index = calls.index("controllers-absent")
        namespace_index = calls.index(f"delete:namespace/{spec.name}")
        self.assertLess(controller_index, namespace_index)
        self.assertLess(namespace_index, calls.index("tenant-absent"))
        self.assertLess(
            calls.index("foundation-verified"),
            calls.index("runtime-removed"),
        )
        phases = [record["phase"] for record in timings.records()]
        self.assertEqual(
            phases,
            [
                "worker-deletion",
                "deletion",
                "controller-cleanup",
                "orchestration-cleanup",
                "azure-absence",
                "foundation-verification",
                "runtime-cleanup",
            ],
        )

    def test_delete_timeout_retains_state_and_sanitized_diagnostics(self):
        root = self.make_root()
        spec = self.spec()
        runtime, identity = self.ready_identity(root, spec)
        journal = runtime.start_operation(
            operation="delete",
            spec=spec,
            foundation_identity=identity.foundation_identity,
            intended_resources=AzureTenantAdapter.intended_resources(spec),
            operation_id="delete-timeout",
        )
        adapter = AzureTenantAdapter()
        adapter._delete_snapshots[spec.name] = {
            "foundation": FOUNDATION,
            "management": {
                "controller": [],
                "orchestration": [],
                "namespaceChildren": [],
                "unknown": [],
            },
            "azure": {"azure": [], "aso": [], "unknown": []},
        }
        timings = TenantTimings(
            root,
            tenant=spec.name,
            operation="delete",
            operation_id=journal.operation_id,
        )
        with (
            patch.object(adapter, "_config", return_value=load_azure_configuration(root)),
            patch("scripts.azure._exact_delete_management_resource", return_value=True),
            patch.object(adapter, "_wait_for_worker_cleanup"),
            patch.object(
                adapter,
                "_wait_for_controller_cleanup",
                side_effect=RuntimeError(
                    "CAPZ panic: finalizer blocked "
                    "/subscriptions/00000000-0000-0000-0000-000000000000/"
                    "resourceGroups/rg/providers/Microsoft.Compute/"
                    "virtualMachineScaleSets/pool"
                ),
            ),
            patch("scripts.azure._get_management_resource", return_value=None),
            patch(
                "scripts.azure._kubectl",
                return_value=completed(json.dumps({"items": []})),
            ),
            self.assertRaisesRegex(RuntimeError, "CAPZ panic"),
        ):
            adapter.delete(root, spec, identity, runtime, journal, timings)
        self.assertTrue(runtime.identity_exists())
        self.assertTrue(runtime.operation_exists())
        diagnostic = (
            runtime.paths.evidence
            / f"delete-diagnostics-{journal.operation_id}.json"
        )
        self.assertTrue(diagnostic.is_file())
        self.assertEqual(diagnostic.stat().st_mode & 0o777, 0o600)
        text = diagnostic.read_text(encoding="utf-8")
        self.assertIn("CAPZ panic", text)
        self.assertNotIn(SUBSCRIPTION, text)
        self.assertTrue(
            any(
                record["phase"] == "controller-cleanup"
                and record["status"] == "failed"
                for record in timings.records()
            )
        )

    def test_controller_wait_is_bounded_and_reports_exact_residue(self):
        root = self.make_root()
        spec = self.spec()
        _, identity = self.ready_identity(root, spec)
        adapter = AzureTenantAdapter(
            monotonic=iter((0.0, 1.0)).__next__,
            sleep=lambda _seconds: None,
        )
        management = {
            "controller": [
                {"kind": "AzureMachinePool", "name": "tenant-c-worker"}
            ],
            "orchestration": [],
            "namespaceChildren": [],
            "unknown": [],
        }
        azure = {
            "azure": [
                {
                    "id": "/subscriptions/redacted/resourceGroups/rg/providers/"
                    "Microsoft.Compute/virtualMachineScaleSets/tenant-c-worker",
                    "type": "microsoft.compute/virtualmachinescalesets",
                }
            ],
            "aso": [
                {
                    "kind": "NatGateway",
                    "name": "tenant-c-nat",
                    "uid": "nat-uid",
                    "azureResourceId": "/subscriptions/redacted/nat",
                }
            ],
            "unknown": [],
        }
        with (
            patch("scripts.azure.parse_duration", return_value=0),
            patch(
                "scripts.azure.discover_management_owned_resources",
                return_value=management,
            ),
            patch(
                "scripts.azure._discover_owned_repeatedly",
                return_value=azure,
            ),
            self.assertRaises(RuntimeError) as raised,
        ):
            adapter._wait_for_controller_cleanup(
                root,
                load_azure_configuration(root),
                spec,
                identity,
                {
                    "controller": [],
                    "orchestration": [],
                    "namespaceChildren": [],
                    "unknown": [],
                },
                {"azure": [], "aso": [], "unknown": []},
            )
        message = str(raised.exception)
        self.assertIn("AzureMachinePool/tenant-c-worker", message)
        self.assertIn("virtualMachineScaleSets/tenant-c-worker", message)
        self.assertIn("NatGateway/tenant-c-nat", message)

    def test_foundation_change_fails_before_runtime_cleanup(self):
        root = self.make_root()
        spec = self.spec()
        runtime, identity = self.ready_identity(root, spec)
        journal = runtime.start_operation(
            operation="delete",
            spec=spec,
            foundation_identity=identity.foundation_identity,
            intended_resources=AzureTenantAdapter.intended_resources(spec),
            operation_id="foundation-change",
        )
        adapter = AzureTenantAdapter()
        adapter._delete_snapshots[spec.name] = {
            "foundation": FOUNDATION,
            "management": {
                "controller": [],
                "orchestration": [],
                "namespaceChildren": [],
                "unknown": [],
            },
            "azure": {"azure": [], "aso": [], "unknown": []},
        }
        timings = TenantTimings(
            root,
            tenant=spec.name,
            operation="delete",
            operation_id=journal.operation_id,
        )
        with (
            patch.object(adapter, "_config", return_value=load_azure_configuration(root)),
            patch("scripts.azure._exact_delete_management_resource", return_value=True),
            patch.object(adapter, "_wait_for_worker_cleanup"),
            patch.object(
                adapter,
                "_wait_for_controller_cleanup",
                return_value=(
                    {
                        "controller": [],
                        "orchestration": [],
                        "namespaceChildren": [],
                        "unknown": [],
                    },
                    {"azure": [], "aso": [], "unknown": []},
                ),
            ),
            patch.object(
                adapter,
                "_wait_for_tenant_absence",
                return_value={"azure": [], "aso": [], "unknown": []},
            ),
            patch(
                "scripts.azure._inspect_foundation",
                return_value=({**FOUNDATION, "vnetId": "changed"}, True, ()),
            ),
            patch("scripts.azure._remove_private_tree") as remove_runtime,
            patch("scripts.azure._get_management_resource", return_value=None),
            patch(
                "scripts.azure._kubectl",
                return_value=completed(json.dumps({"items": []})),
            ),
            self.assertRaisesRegex(RuntimeError, "foundation identity changed"),
        ):
            adapter.delete(root, spec, identity, runtime, journal, timings)
        remove_runtime.assert_not_called()
        self.assertTrue(runtime.identity_exists())
        self.assertTrue(runtime.operation_exists())

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
                "scripts.azure._inspect_foundation",
                return_value=(FOUNDATION, True, ()),
            ),
            patch("scripts.azure._get_management_resource", return_value=None),
            patch("scripts.azure._json", return_value=[]),
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

    def test_live_gate_recipe_exists_but_is_not_invoked_by_tests(self):
        root = Path(__file__).resolve().parents[1]
        justfile = (root / "Justfile").read_text(encoding="utf-8")
        script = (
            root / "scripts" / "test_azure_tenant_lifecycle.py"
        ).read_text(encoding="utf-8")
        self.assertIn("azure-test-tenant-lifecycle", justfile)
        self.assertIn("destroy-legacy-foundation", script)
        self.assertIn("targeted-delete-absent", script)
        self.assertIn('"recreation"', script)
        self.assertIn('"status", "--porcelain", "--untracked-files=no"', script)
        self.assertIn('"revision": revision', script)


if __name__ == "__main__":
    unittest.main()
