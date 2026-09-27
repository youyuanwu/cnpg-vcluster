from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.azure import (
    AzureTenantAdapter,
    classify_azure_owned_resources,
    discover_azure_owned_resources,
)
from scripts.lib.azure.common import (
    azure_tenant_runtime_path,
    load_azure_configuration,
)
from scripts.lib.azure.rendering import (
    _azure_tags,
    _reconcile_manifest,
    _render_addon_job,
    _render_tenant_control_plane,
    _render_worker_pool,
)
from scripts.lib.files import write_private_file
from scripts.lib.tenant_runtime import TenantRuntime, foundation_sha256
from scripts.lib.tenant_timing import TenantTimings
from scripts.lib.tenants import LIFECYCLE_MARKERS, lifecycle_markers
from tests.azure_fixtures import AzureFixtureMixin, FOUNDATION


class AzureRenderingTests(AzureFixtureMixin, unittest.TestCase):
    def test_rendered_resources_use_spec_and_all_have_markers(self):
        root = self.make_root()
        config = load_azure_configuration(root)
        spec = self.spec("blue", workers=3)
        runtime, journal = self.start_journal(root, spec)
        inventory = self.inventory(root, config)
        paths = (
            _render_tenant_control_plane(root, config, inventory, spec, journal),
            _render_worker_pool(root, config, inventory, spec, journal),
            _render_addon_job(root, config, spec, journal),
        )
        expected = lifecycle_markers(spec, journal)
        kinds = set()
        combined = ""
        for path in paths:
            payload = json.loads(path.read_text(encoding="utf-8"))
            for item in payload["items"]:
                kinds.add(item["kind"])
                annotations = item["metadata"]["annotations"]
                self.assertEqual(
                    {key: annotations[value] for key, value in LIFECYCLE_MARKERS.items()},
                    expected,
                )
                if item["kind"] == "AzureCluster":
                    self.assertEqual(
                        item["metadata"]["labels"][
                            "cnpg-vcluster-external-control-plane"
                        ],
                        "true",
                    )
                if item["kind"] == "Deployment":
                    container = item["spec"]["template"]["spec"]["containers"][0]
                    self.assertIn("--address=127.0.0.1", container["args"])
                    self.assertNotIn("--address=0.0.0.0", container["args"])
                    self.assertNotIn("ports", container)
                    self.assertEqual(
                        container["readinessProbe"]["exec"]["command"][-1],
                        "--raw=/readyz",
                    )
            combined += path.read_text(encoding="utf-8")
        self.assertIn('"replicas": 3', combined)
        self.assertIn("10.72.0.0/16", combined)
        self.assertIn("10.142.0.0/16", combined)
        self.assertIn("1.32.13", combined)
        self.assertNotIn("yy-cv-tenant", combined)
        self.assertEqual(
            kinds,
            {
                "Namespace",
                "AzureClusterIdentity",
                "Cluster",
                "AzureCluster",
                "KamajiControlPlane",
                "KubeadmConfig",
                "AzureMachinePool",
                "MachinePool",
                "ConfigMap",
                "Deployment",
                "Job",
            },
        )
        self.assertTrue(runtime.operation_exists())
    def test_reconcile_recovers_marked_uid_before_next_resource(self):
        root = self.make_root()
        spec = self.spec()
        runtime, journal = self.start_journal(root, spec)
        markers = lifecycle_markers(spec, journal)
        item = {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {
                "name": spec.name,
                "annotations": {
                    LIFECYCLE_MARKERS[key]: value
                    for key, value in markers.items()
                },
            },
        }
        path = azure_tenant_runtime_path(root, spec.name) / "one.json"
        write_private_file(
            path,
            json.dumps({"apiVersion": "v1", "kind": "List", "items": [item]}),
        )
        observed = {
            "metadata": {
                "uid": "namespace-uid",
                "annotations": item["metadata"]["annotations"],
            }
        }
        with (
            patch("scripts.lib.azure.rendering._get_management_resource", return_value=observed),
            patch("scripts.lib.azure.rendering._kubectl") as kubectl,
        ):
            updated = _reconcile_manifest(
                root,
                spec,
                runtime,
                journal,
                path,
                phase="control-plane-resources",
            )
        self.assertEqual(updated.observed["namespaceUid"], "namespace-uid")
        kubectl.assert_called_once()
    def test_reconcile_refuses_foreign_markers_before_apply(self):
        root = self.make_root()
        spec = self.spec()
        runtime, journal = self.start_journal(root, spec)
        item = {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {"name": spec.name},
        }
        path = azure_tenant_runtime_path(root, spec.name) / "one.json"
        write_private_file(
            path,
            json.dumps({"apiVersion": "v1", "kind": "List", "items": [item]}),
        )
        foreign = {
            "metadata": {
                "uid": "foreign",
                "annotations": {
                    value: "foreign" for value in LIFECYCLE_MARKERS.values()
                },
            }
        }
        with (
            patch("scripts.lib.azure.rendering._get_management_resource", return_value=foreign),
            patch("scripts.lib.azure.rendering._kubectl") as kubectl,
            self.assertRaisesRegex(RuntimeError, "foreign.*markers"),
        ):
            _reconcile_manifest(
                root,
                spec,
                runtime,
                journal,
                path,
                phase="control-plane-resources",
            )
        kubectl.assert_not_called()
    def test_reconcile_refuses_recorded_uid_mismatch_before_apply(self):
        root = self.make_root()
        spec = self.spec()
        runtime, journal = self.start_journal(root, spec)
        journal = runtime.update_operation(
            journal,
            phase="control-plane-resources",
            observed={"clusterUid": "recorded-uid"},
        )
        markers = lifecycle_markers(spec, journal)
        item = {
            "apiVersion": "cluster.x-k8s.io/v1beta1",
            "kind": "Cluster",
            "metadata": {
                "name": spec.name,
                "namespace": spec.namespace,
                "annotations": {
                    LIFECYCLE_MARKERS[key]: value
                    for key, value in markers.items()
                },
            },
        }
        path = azure_tenant_runtime_path(root, spec.name) / "cluster.json"
        write_private_file(
            path,
            json.dumps({"apiVersion": "v1", "kind": "List", "items": [item]}),
        )
        existing = {
            "metadata": {
                "uid": "replacement-uid",
                "annotations": item["metadata"]["annotations"],
            }
        }
        with (
            patch("scripts.lib.azure.rendering._get_management_resource", return_value=existing),
            patch("scripts.lib.azure.rendering._kubectl") as kubectl,
            self.assertRaisesRegex(RuntimeError, "identity changed"),
        ):
            _reconcile_manifest(
                root,
                spec,
                runtime,
                journal,
                path,
                phase="control-plane-resources",
            )
        kubectl.assert_not_called()
    def test_reconcile_recovers_real_stage_resources_after_uid_write_failure(self):
        cases = (
            ("Cluster", "tenant-c", "clusterUid", "control-plane-resources"),
            ("MachinePool", "tenant-c-worker", "machinePoolUid", "worker-resources"),
            (
                "ConfigMap",
                "tenant-c-azure-cloud-provider-values",
                "cloudValuesConfigMapUid",
                "addon-resources",
            ),
        )
        for kind, name, key, phase in cases:
            with self.subTest(kind=kind):
                root = self.make_root()
                spec = self.spec()
                runtime, journal = self.start_journal(root, spec)
                markers = lifecycle_markers(spec, journal)
                item = {
                    "apiVersion": "v1",
                    "kind": kind,
                    "metadata": {
                        "name": name,
                        "namespace": spec.namespace,
                        "annotations": {
                            LIFECYCLE_MARKERS[marker]: value
                            for marker, value in markers.items()
                        },
                    },
                }
                path = azure_tenant_runtime_path(root, spec.name) / f"{kind}.json"
                write_private_file(
                    path,
                    json.dumps(
                        {"apiVersion": "v1", "kind": "List", "items": [item]}
                    ),
                )
                observed = {
                    "metadata": {
                        "uid": f"{kind.lower()}-uid",
                        "annotations": item["metadata"]["annotations"],
                    }
                }
                original_update = runtime.update_operation
                calls = 0

                def fail_first_update(*args, **kwargs):
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        raise RuntimeError("injected UID persistence failure")
                    return original_update(*args, **kwargs)

                with (
                    patch(
                        "scripts.lib.azure.rendering._get_management_resource",
                        side_effect=[None, observed],
                    ),
                    patch("scripts.lib.azure.rendering._kubectl"),
                    patch.object(
                        runtime,
                        "update_operation",
                        side_effect=fail_first_update,
                    ),
                    self.assertRaisesRegex(RuntimeError, "UID persistence"),
                ):
                    _reconcile_manifest(
                        root,
                        spec,
                        runtime,
                        journal,
                        path,
                        phase=phase,
                    )
                self.assertNotIn(key, runtime.load_operation().observed)
                with (
                    patch(
                        "scripts.lib.azure.rendering._get_management_resource",
                        side_effect=[observed, observed],
                    ),
                    patch("scripts.lib.azure.rendering._kubectl"),
                ):
                    recovered = _reconcile_manifest(
                        root,
                        spec,
                        runtime,
                        runtime.load_operation(),
                        path,
                        phase=phase,
                    )
                self.assertEqual(recovered.observed[key], f"{kind.lower()}-uid")
    def test_interrupted_create_retains_journal_at_each_stage(self):
        for failing_phase in ("control-plane", "workers", "add-ons"):
            with self.subTest(phase=failing_phase):
                root = self.make_root()
                spec = self.spec()
                runtime, journal = self.start_journal(root, spec)
                adapter = AzureTenantAdapter()
                timings = TenantTimings(
                    root,
                    tenant=spec.name,
                    operation="create",
                    operation_id=journal.operation_id,
                )

                def reconcile(_root, _spec, actual_runtime, current, _path, *, phase):
                    stage = {
                        "control-plane-resources": "control-plane",
                        "worker-resources": "workers",
                        "addon-resources": "add-ons",
                    }[phase]
                    if stage == failing_phase:
                        raise RuntimeError(f"stop-{stage}")
                    return actual_runtime.update_operation(
                        current,
                        phase=phase,
                        observed={f"{stage}Uid": f"{stage}-uid"},
                    )

                endpoint = {
                    "spec": {
                        "controlPlaneEndpoint": {
                            "host": "10.220.0.6",
                            "port": 6443,
                        }
                    }
                }
                with (
                    patch.object(adapter, "_config", return_value=load_azure_configuration(root)),
                    patch("scripts.azure.load_inventory", return_value={}),
                    patch("scripts.azure._render_tenant_control_plane", return_value=Path("cp")),
                    patch("scripts.azure._render_worker_pool", return_value=Path("worker")),
                    patch("scripts.azure._render_addon_job", return_value=Path("addons")),
                    patch("scripts.azure._reconcile_manifest", side_effect=reconcile),
                    patch("scripts.azure._retain_external_control_plane_lb"),
                    patch("scripts.azure._wait_tenant_endpoint", return_value=endpoint),
                    patch(
                        "scripts.azure._capture_tenant_kubeconfig",
                        side_effect=lambda _r, _s, rt, current: rt.update_operation(
                            current,
                            phase="control-plane-ready",
                            observed={"credentialUid": "credential-uid"},
                        ),
                    ),
                    patch("scripts.azure._wait_worker_registered"),
                    patch(
                        "scripts.azure._capture_vmss_identities",
                        side_effect=lambda _r, _c, _s, rt, current: rt.update_operation(
                            current,
                            phase="workers-registered",
                            observed={"vmssId": "vmss-id"},
                        ),
                    ),
                    self.assertRaisesRegex(RuntimeError, f"stop-{failing_phase}"),
                ):
                    adapter.create(root, spec, runtime, journal, timings)
                self.assertTrue(runtime.operation_exists())
                self.assertFalse(runtime.identity_exists())
    def test_worker_pool_bounds_node_drain(self):
        root = self.make_root()
        config = load_azure_configuration(root)
        spec = self.spec()
        _, journal = self.start_journal(root, spec)
        manifest = _render_worker_pool(
            root,
            config,
            self.inventory(root, config),
            spec,
            journal,
        )
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        machine_pool = next(
            item for item in payload["items"] if item["kind"] == "MachinePool"
        )
        self.assertEqual(
            machine_pool["spec"]["template"]["spec"]["nodeDrainTimeout"],
            "2m",
        )
    def test_discovery_classifies_complete_known_resource_set(self):
        spec = self.spec()
        markers = {
            "tenant": spec.name,
            "profile": "azure",
            "specificationSha256": spec.sha256(),
            "foundationSha256": "foundation",
            "operationId": "operation",
        }
        tags = _azure_tags(markers)
        vmss = "/subscriptions/x/resourceGroups/rg/providers/Microsoft.Compute/virtualMachineScaleSets/tenant-c-worker"
        resources = [
            {"id": vmss, "type": "Microsoft.Compute/virtualMachineScaleSets", "tags": tags},
            {
                "id": vmss + "/virtualMachines/0",
                "type": "Microsoft.Compute/virtualMachineScaleSets/virtualMachines",
                "tags": {},
            },
            {
                "id": "/subscriptions/x/resourceGroups/rg/providers/Microsoft.Network/networkInterfaces/nic-0",
                "type": "Microsoft.Network/networkInterfaces",
                "tags": tags,
            },
            {
                "id": "/subscriptions/x/resourceGroups/rg/providers/Microsoft.Network/natGateways/nat",
                "type": "Microsoft.Network/natGateways",
                "tags": tags,
            },
            {
                "id": "/subscriptions/x/resourceGroups/rg/providers/Microsoft.Network/publicIPAddresses/pip",
                "type": "Microsoft.Network/publicIPAddresses",
                "tags": tags,
            },
        ]
        aso = {
            "kind": "NatGateway",
            "metadata": {
                "name": "nat",
                "uid": "aso-uid",
                "ownerReferences": [{"uid": "azure-cluster-uid"}],
            },
            "status": {
                "id": "/subscriptions/x/resourceGroups/rg/providers/Microsoft.Network/natGateways/nat"
            },
        }
        result = classify_azure_owned_resources(
            resources,
            markers,
            parent_ids=(vmss,),
            aso_objects=(aso,),
            parent_uids=("azure-cluster-uid",),
        )
        self.assertEqual(len(result["azure"]), 5)
        self.assertEqual(result["aso"][0]["uid"], "aso-uid")
    def test_discovery_fails_closed_for_unknown_or_foreign_ownership(self):
        markers = {
            "tenant": "tenant-c",
            "profile": "azure",
            "specificationSha256": "spec",
            "foundationSha256": "foundation",
            "operationId": "operation",
        }
        tags = _azure_tags(markers)
        resources = [
            {
                "id": "/subscriptions/x/resourceGroups/rg/providers/Microsoft.Storage/storageAccounts/unknown",
                "type": "Microsoft.Storage/storageAccounts",
                "tags": tags,
            }
        ]
        with self.assertRaisesRegex(RuntimeError, "ownership is unknown"):
            classify_azure_owned_resources(resources, markers)
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
            patch("scripts.azure._get_management_resource", side_effect=parent),
            patch("scripts.azure.load_inventory", return_value=inventory),
            patch("scripts.azure._json", return_value=[]),
            patch("scripts.azure._kubectl", side_effect=kubectl),
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
            patch("scripts.azure._get_management_resource", side_effect=parent),
            patch("scripts.azure.load_inventory", return_value=inventory),
            patch("scripts.azure._json", return_value=[]),
            patch("scripts.azure._kubectl", side_effect=kubectl),
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
