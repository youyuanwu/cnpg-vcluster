from __future__ import annotations

import json
import subprocess
import unittest
from unittest.mock import patch

from scripts.lib.azure.lifecycle import AzureTenantAdapter
from scripts.lib.azure.common import load_azure_configuration
from scripts.lib.azure.contracts import (
    MANAGEMENT_RESOURCE_DESCRIPTORS,
    RESOURCE_IDENTITY_KEYS,
    _expected_tenant_markers,
    _management_resource_specs,
)
from scripts.lib.azure.ownership import (
    _classify_management_owned_resources,
    _discover_owned_repeatedly,
    classify_azure_owned_resources,
    discover_azure_owned_resources,
)
from scripts.lib.azure.rendering import _azure_tags
from scripts.lib.tenant_runtime import foundation_sha256
from scripts.lib.tenants import LIFECYCLE_MARKERS, lifecycle_markers
from tests.azure_fixtures import AzureFixtureMixin, FOUNDATION


def completed(stdout: str = "", returncode: int = 0):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


class AzureOwnershipTests(AzureFixtureMixin, unittest.TestCase):
    def test_contracts_preserve_markers_and_management_inventory(self):
        root = self.make_root()
        spec = self.spec()
        _, journal = self.start_journal(root, spec)
        self.assertEqual(
            _expected_tenant_markers(spec, journal),
            lifecycle_markers(spec, journal),
        )
        specs = _management_resource_specs(spec)
        self.assertEqual(
            tuple((key, kind) for key, _, kind, _ in specs),
            tuple(
                (key, kind)
                for key, _, kind, _ in MANAGEMENT_RESOURCE_DESCRIPTORS
            ),
        )
        management = tuple(
            (
                f"{kind}/{namespace}/{name}"
                if namespace is not None
                else f"{kind}/{name}"
            )
            for _, namespace, kind, name in specs
        )
        self.assertEqual(
            AzureTenantAdapter.intended_resources(spec),
            (
                *management[:8],
                "VirtualMachineScaleSet/tenant-c-worker",
                *management[8:],
                "Credential/tenant-c",
            ),
        )
        identity_keys = {
            value
            for value in RESOURCE_IDENTITY_KEYS.values()
            if isinstance(value, str)
        } | set(RESOURCE_IDENTITY_KEYS["ConfigMap"].values())
        self.assertEqual(identity_keys, {key for key, _, _, _ in specs})
        with self.assertRaises(TypeError):
            RESOURCE_IDENTITY_KEYS["Cluster"] = "changed"
        with self.assertRaises(TypeError):
            RESOURCE_IDENTITY_KEYS["ConfigMap"]["cloud"] = "changed"

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
                "scripts.lib.azure.ownership._get_management_resource",
                side_effect=parent,
            ),
            patch("scripts.lib.azure.ownership.load_inventory", return_value=self.inventory(root, config)),
            patch("scripts.lib.azure.ownership._json", return_value=[]),
            patch(
                "scripts.lib.azure.ownership._kubectl",
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
                "scripts.lib.azure.ownership._get_management_resource",
                side_effect=parent,
            ),
            patch(
                "scripts.lib.azure.ownership.load_inventory",
                return_value=self.inventory(root, config),
            ),
            patch("scripts.lib.azure.ownership._json", return_value=[]),
            patch(
                "scripts.lib.azure.ownership._kubectl",
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
                "scripts.lib.azure.ownership._get_management_resource",
                side_effect=parent,
            ),
            patch("scripts.lib.azure.ownership.load_inventory", return_value=self.inventory(root, config)),
            patch("scripts.lib.azure.ownership._json", return_value=[]),
            patch("scripts.lib.azure.ownership._kubectl", side_effect=lambda *_a, **_k: next(responses)),
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
            patch("scripts.lib.azure.lifecycle._active_subscription"),
            patch(
                "scripts.lib.azure.lifecycle._inspect_foundation",
                return_value=(FOUNDATION, True, ()),
            ),
            patch(
                "scripts.lib.azure.lifecycle.discover_management_owned_resources",
                return_value=empty_management,
            ) as management_discovery,
            patch(
                "scripts.lib.azure.lifecycle._discover_owned_repeatedly",
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
    def test_delete_validation_refuses_foundation_and_uid_before_mutation(self):
        root = self.make_root()
        spec = self.spec()
        _, identity = self.ready_identity(root, spec)
        adapter = AzureTenantAdapter()
        with (
            patch.object(adapter, "_config", return_value=load_azure_configuration(root)),
            patch("scripts.lib.azure.lifecycle._active_subscription"),
            patch(
                "scripts.lib.azure.lifecycle._inspect_foundation",
                return_value=({**FOUNDATION, "aksId": "foreign"}, True, ()),
            ),
            patch("scripts.lib.azure.lifecycle._exact_delete_management_resource") as mutate,
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
            "scripts.lib.azure.ownership.discover_azure_owned_resources",
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
            patch("scripts.lib.azure.lifecycle.parse_duration", return_value=0),
            patch(
                "scripts.lib.azure.lifecycle.discover_management_owned_resources",
                return_value=management,
            ),
            patch(
                "scripts.lib.azure.lifecycle._discover_owned_repeatedly",
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
    def test_discovery_accepts_exact_skipped_foundation_references(self):
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
        inventory = self.inventory(root, config)

        def parent(_root, _namespace, resource):
            return {
                "metadata": {
                    "uid": (
                        "azure-cluster-uid"
                        if resource.startswith("azurecluster/")
                        else "azure-pool-uid"
                    ),
                    "annotations": {
                        LIFECYCLE_MARKERS[key]: value
                        for key, value in markers.items()
                    },
                }
            }

        references = {
            "resourcegroups.resources.azure.com": {
                "kind": "ResourceGroup",
                "metadata": {
                    "name": "resource-group",
                    "annotations": {
                        "serviceoperator.azure.com/reconcile-policy": "skip"
                    },
                },
                "status": {"id": inventory["outputs"]["resourceGroupId"]},
            },
            "virtualnetworks.network.azure.com": {
                "kind": "VirtualNetwork",
                "metadata": {
                    "name": "vnet",
                    "ownerReferences": [{"uid": "azure-cluster-uid"}],
                    "annotations": {
                        "serviceoperator.azure.com/reconcile-policy": "skip"
                    },
                },
                "status": {"id": inventory["outputs"]["vnetId"]},
            },
            "virtualnetworkssubnets.network.azure.com": {
                "kind": "VirtualNetworksSubnet",
                "metadata": {
                    "name": "subnet",
                    "ownerReferences": [{"uid": "azure-cluster-uid"}],
                    "annotations": {
                        "serviceoperator.azure.com/reconcile-policy": "skip"
                    },
                },
                "status": {"id": inventory["outputs"]["tenantSubnetId"]},
            },
        }

        def kubectl(_root, *arguments, **_kwargs):
            if arguments[0] == "api-resources":
                group = arguments[2]
                output = "\n".join(
                    name
                    for name in references
                    if name.endswith(group)
                )
                if output:
                    output += "\n"
                return subprocess.CompletedProcess([], 0, stdout=output, stderr="")
            resource = arguments[3]
            return subprocess.CompletedProcess(
                [],
                0,
                stdout=json.dumps({"items": [references[resource]]}),
                stderr="",
            )

        with (
            patch("scripts.lib.azure.ownership._get_management_resource", side_effect=parent),
            patch("scripts.lib.azure.ownership.load_inventory", return_value=inventory),
            patch("scripts.lib.azure.ownership._json", return_value=[]),
            patch("scripts.lib.azure.ownership._kubectl", side_effect=kubectl),
        ):
            discovery = discover_azure_owned_resources(
                root,
                config,
                spec,
                journal,
            )
        self.assertEqual(discovery, {"azure": [], "aso": [], "unknown": []})
        references["resourcegroups.resources.azure.com"]["status"]["id"] = (
            "/subscriptions/redacted/resourceGroups/foreign"
        )
        with (
            patch("scripts.lib.azure.ownership._get_management_resource", side_effect=parent),
            patch("scripts.lib.azure.ownership.load_inventory", return_value=inventory),
            patch("scripts.lib.azure.ownership._json", return_value=[]),
            patch("scripts.lib.azure.ownership._kubectl", side_effect=kubectl),
            self.assertRaisesRegex(RuntimeError, "foundation reference changed"),
        ):
            discover_azure_owned_resources(
                root,
                config,
                spec,
                journal,
            )
        tags = _azure_tags(markers)
        foreign = dict(tags)
        foreign["cnpg-vcluster-operation-id"] = "other"
        resources = [
            {
                "id": "/subscriptions/x/resourceGroups/rg/providers/Microsoft.Network/publicIPAddresses/pip",
                "type": "Microsoft.Network/publicIPAddresses",
                "tags": foreign,
            }
        ]
        with self.assertRaisesRegex(RuntimeError, "ownership is unknown"):
            classify_azure_owned_resources(resources, markers)


if __name__ == "__main__":
    unittest.main()
