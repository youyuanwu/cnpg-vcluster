from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import time
import unittest
from unittest.mock import patch

from scripts.lib.azure.lifecycle import AzureTenantAdapter
from scripts.lib.azure.common import (
    azure_tenant_runtime_path,
    load_azure_configuration,
    tenant_names,
)
from scripts.lib.azure.readiness import (
    _capture_tenant_kubeconfig,
    _collect_ready_observations,
    _retain_external_control_plane_lb,
    _tenant_spec_blockers,
    _wait_addon_job,
    _wait_ready_observations,
    _wait_tenant_endpoint,
    _wait_worker_registered,
)
from scripts.lib.azure.rendering import (
    _render_addon_job,
    _render_tenant_control_plane,
    _render_worker_pool,
)
from scripts.lib.files import write_private_file
from scripts.lib.tenant_runtime import TenantRuntime, foundation_sha256
from scripts.lib.tenant_timing import TenantTimings
from scripts.lib.tenants import LIFECYCLE_MARKERS, lifecycle_markers
from tests.azure_fixtures import AzureFixtureMixin, FOUNDATION


def completed(stdout: str = "", returncode: int = 0):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


class AzureReadinessTests(AzureFixtureMixin, unittest.TestCase):
    def test_external_control_plane_retention_uses_canonical_markers(self):
        root = self.make_root()
        spec = self.spec()
        _, journal = self.start_journal(root, spec)
        with (
            patch(
                "scripts.lib.azure.readiness._get_management_resource",
                return_value=None,
            ),
            self.assertRaisesRegex(RuntimeError, "AzureCluster is absent"),
        ):
            _retain_external_control_plane_lb(root, spec, journal)

    def test_endpoint_readiness_retains_compatibility_patches(self):
        root = self.make_root()
        config = load_azure_configuration(root)
        spec = self.spec()
        endpoint = {"host": "10.220.0.6", "port": 6443}
        resources = (
            {"status": {"conditions": [{"type": "Ready", "status": "True"}]}},
            {"status": {"infrastructureReady": False}},
            {
                "spec": {"controlPlaneEndpoint": endpoint},
                "status": {"ready": True},
            },
        )
        with (
            patch(
                "scripts.lib.azure.readiness._get_management_resource",
                side_effect=resources,
            ),
            patch("scripts.lib.azure.readiness._kubectl") as kubectl,
            patch(
                "scripts.lib.azure.readiness.time.monotonic",
                side_effect=(0, 1),
            ),
        ):
            payload = _wait_tenant_endpoint(root, config, spec)
        self.assertEqual(payload["spec"]["controlPlaneEndpoint"], endpoint)
        self.assertEqual(kubectl.call_count, 2)

    def test_worker_registration_retries_to_exact_count(self):
        root = self.make_root()
        config = load_azure_configuration(root)
        spec = self.spec(workers=3)
        with (
            patch(
                "scripts.lib.azure.readiness._get_management_resource",
                return_value={"metadata": {"name": "tenant-c-worker"}},
            ),
            patch(
                "scripts.lib.azure.readiness._az",
                side_effect=(
                    completed('[{"id":"one","instanceId":"0"}]'),
                    completed(
                        '[{"id":"one","instanceId":"0"},'
                        '{"id":"two","instanceId":"1"},'
                        '{"id":"three","instanceId":"2"}]'
                    ),
                ),
            ) as az,
            patch(
                "scripts.lib.azure.readiness.time.monotonic",
                side_effect=(0, 1, 2),
            ),
            patch("scripts.lib.azure.readiness.time.sleep"),
        ):
            _, instances = _wait_worker_registered(root, config, spec)
        self.assertEqual(len(instances), 3)
        self.assertEqual(az.call_count, 2)

    def test_addon_failure_reports_job_logs(self):
        root = self.make_root()
        config = load_azure_configuration(root)
        spec = self.spec()
        with (
            patch(
                "scripts.lib.azure.readiness._kubectl",
                side_effect=(
                    completed(),
                    completed(returncode=1),
                    completed("redacted add-on failure"),
                ),
            ),
            self.assertRaisesRegex(RuntimeError, "redacted add-on failure"),
        ):
            _wait_addon_job(root, config, spec)

    def test_ready_wait_retries_until_cloud_and_network_converge(self):
        root = self.make_root()
        config = load_azure_configuration(root)
        spec = self.spec()
        ready = {"nodes": [{"name": "node-0"}]}
        with (
            patch(
                "scripts.lib.azure.readiness._collect_ready_observations",
                side_effect=[
                    ({}, ("Node is not Ready",)),
                    ({}, ("calicoNode is not Ready",)),
                    (ready, ()),
                ],
            ) as collect,
            patch("scripts.lib.azure.readiness.time.sleep"),
            patch(
                "scripts.lib.azure.readiness.time.monotonic",
                side_effect=[0, 1, 2, 3],
            ),
        ):
            self.assertEqual(
                _wait_ready_observations(root, config, spec),
                ready,
            )
        self.assertEqual(collect.call_count, 3)
    def test_tenant_kubeconfig_requires_verified_controller_owner(self):
        root = self.make_root()
        spec = self.spec()
        runtime, journal = self.start_journal(root, spec)
        journal = runtime.update_operation(
            journal,
            phase="control-plane-resources",
            observed={
                "clusterUid": "cluster-uid",
                "kamajiControlPlaneUid": "control-plane-uid",
            },
        )
        encoded = base64.b64encode(b"kubeconfig").decode()
        for owner_uid, accepted in (
            ("foreign-uid", False),
            ("control-plane-uid", True),
        ):
            with self.subTest(owner_uid=owner_uid):
                secret = {
                    "metadata": {
                        "uid": "secret-uid",
                        "ownerReferences": [
                            {
                                "controller": True,
                                "uid": owner_uid,
                                "kind": "KamajiControlPlane",
                            }
                        ],
                    },
                    "data": {"value": encoded},
                }
                with patch(
                    "scripts.lib.azure.readiness._get_management_resource",
                    return_value=secret,
                ):
                    if accepted:
                        updated = _capture_tenant_kubeconfig(
                            root,
                            spec,
                            runtime,
                            journal,
                        )
                        self.assertEqual(
                            updated.observed["tenantKubeconfigSecretUid"],
                            "secret-uid",
                        )
                    else:
                        with self.assertRaisesRegex(RuntimeError, "incomplete"):
                            _capture_tenant_kubeconfig(
                                root,
                                spec,
                                runtime,
                                journal,
                            )
    def test_ready_observations_enforce_node_cloud_identity_and_subnet(self):
        root = self.make_root()
        config = load_azure_configuration(root)
        spec = self.spec()
        cluster = {
            "status": {
                "conditions": [
                    {"type": "ControlPlaneAvailable", "status": "True"}
                ]
            }
        }
        control_plane = {"status": {"ready": True}}
        pool = {
            "status": {
                "readyReplicas": 1,
                "nodeRefs": [{"name": "node-0"}],
            }
        }

        def management(_root, _namespace, resource):
            if resource.startswith("cluster/"):
                return cluster
            if resource.startswith("kamajicontrolplane/"):
                return control_plane
            return pool

        node = {
            "metadata": {"name": "node-0", "uid": "node-uid"},
            "spec": {"providerID": "azure:///vmss/0"},
            "status": {
                "addresses": [{"type": "InternalIP", "address": "10.220.16.4"}],
                "conditions": [{"type": "Ready", "status": "True"}],
            },
        }
        workload = {
            "metadata": {"uid": "workload-uid"},
            "spec": {"replicas": 1},
            "status": {
                "availableReplicas": 1,
                "updatedReplicas": 1,
                "desiredNumberScheduled": 1,
                "numberReady": 1,
                "updatedNumberScheduled": 1,
            },
        }
        responses = [completed(json.dumps({"items": [node]}))] + [
            completed(json.dumps(workload)) for _ in range(4)
        ]
        with (
            patch("scripts.lib.azure.readiness._get_management_resource", side_effect=management),
            patch("scripts.lib.azure.readiness._tenant_kubectl", side_effect=responses),
        ):
            observations, blockers = _collect_ready_observations(root, config, spec)
        self.assertEqual(blockers, ())
        self.assertEqual(observations["readyReplicas"], 1)
        node["status"]["addresses"][0]["address"] = "10.99.0.4"
        responses = [completed(json.dumps({"items": [node]}))] + [
            completed(json.dumps(workload)) for _ in range(4)
        ]
        with (
            patch("scripts.lib.azure.readiness._get_management_resource", side_effect=management),
            patch("scripts.lib.azure.readiness._tenant_kubectl", side_effect=responses),
        ):
            _, blockers = _collect_ready_observations(root, config, spec)
        self.assertTrue(any("outside the tenant subnet" in item for item in blockers))
    def test_tenant_spec_accepts_capz_defaulted_network_interface(self):
        spec = self.spec()
        selected = tenant_names(spec)
        payloads = {
            "clusterUid": {
                "spec": {
                    "clusterNetwork": {
                        "pods": {"cidrBlocks": [str(spec.pod_network)]},
                        "services": {"cidrBlocks": [str(spec.service_network)]},
                        "serviceDomain": spec.cluster_domain,
                    }
                }
            },
            "kamajiControlPlaneUid": {
                "spec": {"version": spec.kubernetes_version}
            },
            "machinePoolUid": {
                "spec": {"replicas": spec.workers, "clusterName": spec.name}
            },
            "azureMachinePoolUid": {
                "spec": {
                    "template": {
                        "vmSize": "Standard_B2s",
                        "networkInterfaces": [
                            {
                                "subnetName": "tenant",
                                "privateIPConfigs": 1,
                            }
                        ],
                        "image": {
                            "computeGallery": {
                                "version": spec.kubernetes_version
                            }
                        },
                    }
                }
            },
        }
        self.assertEqual(
            _tenant_spec_blockers(
                spec,
                selected,
                payloads,
                {"AZURE_TENANT_NODE_SKU": "Standard_B2s"},
            ),
            (),
        )
    def test_ready_status_rejects_future_evidence_and_identity_changes(self):
        root = self.make_root()
        config = load_azure_configuration(root)
        spec = self.spec()
        runtime, journal = self.start_journal(root, spec)
        inventory = self.inventory(root, config)
        manifests = (
            _render_tenant_control_plane(root, config, inventory, spec, journal),
            _render_worker_pool(root, config, inventory, spec, journal),
            _render_addon_job(root, config, spec, journal),
        )
        payloads = {}
        observed = dict(journal.observed)
        kind_keys = {
            "Namespace": "namespaceUid",
            "AzureClusterIdentity": "azureClusterIdentityUid",
            "Cluster": "clusterUid",
            "AzureCluster": "azureClusterUid",
            "KamajiControlPlane": "kamajiControlPlaneUid",
            "KubeadmConfig": "kubeadmConfigUid",
            "AzureMachinePool": "azureMachinePoolUid",
            "MachinePool": "machinePoolUid",
            "Job": "addonJobUid",
            "Deployment": "statusProbeDeploymentUid",
        }
        configmap_keys = iter(("cloudValuesConfigMapUid", "networkValuesConfigMapUid"))
        for path in manifests:
            for item in json.loads(path.read_text(encoding="utf-8"))["items"]:
                key = (
                    next(configmap_keys)
                    if item["kind"] == "ConfigMap"
                    else kind_keys[item["kind"]]
                )
                uid = f"{key}-value"
                item["metadata"]["uid"] = uid
                payloads[
                    (
                        item["metadata"].get("namespace"),
                        f"{item['kind'].lower()}/{item['metadata']['name']}",
                    )
                ] = item
                observed[key] = uid
        endpoint = {"host": "10.220.0.6", "port": 6443}
        payloads[(spec.namespace, f"kamajicontrolplane/{spec.name}")]["spec"][
            "controlPlaneEndpoint"
        ] = endpoint
        kubeconfig = b"apiVersion: v1\n"
        write_private_file(
            azure_tenant_runtime_path(root, spec.name) / "kubeconfig",
            kubeconfig,
        )
        observed.update(
            {
                "endpoint": "10.220.0.6:6443",
                "tenantKubeconfigSecretUid": "secret-uid",
                "tenantKubeconfigSha256": hashlib.sha256(kubeconfig).hexdigest(),
            }
        )
        ready = {
            "controlPlaneAvailable": True,
            "kamajiReady": True,
            "requestedWorkers": 1,
            "readyReplicas": 1,
            "nodeRefs": ["node-0"],
            "nodes": [
                {
                    "name": "node-0",
                    "uid": "node-uid",
                    "providerID": "azure:///vmss/0",
                    "internalIP": "10.220.16.4",
                }
            ],
            "componentIdentities": {
                "cloudController": "cloud-controller-uid",
                "cloudNode": "cloud-node-uid",
                "calicoNode": "calico-node-uid",
                "calicoControllers": "calico-controllers-uid",
            },
            "cloudController": True,
            "cloudNode": True,
            "calicoNode": True,
            "calicoControllers": True,
        }
        discovery = {"azure": [], "aso": [], "unknown": []}
        observed["azureResources"] = json.dumps(
            discovery,
            sort_keys=True,
            separators=(",", ":"),
        )
        observed["nodeIdentities"] = json.dumps(
            ready["nodes"],
            sort_keys=True,
            separators=(",", ":"),
        )
        observed.update(
            {
                f"{key}Uid": value
                for key, value in ready["componentIdentities"].items()
            }
        )
        current = runtime.load_operation()
        runtime.complete_create(current, spec, observed)
        runtime.write_ready_evidence(
            {
                "schema": 1,
                "profile": "azure",
                "tenant": spec.name,
                "specificationSha256": spec.sha256(),
                "foundationIdentity": FOUNDATION,
                "observed": observed,
                "verifiedAt": 100,
                "ready": ready,
            }
        )
        secret = {
            "metadata": {"uid": "secret-uid"},
            "data": {"value": base64.b64encode(kubeconfig).decode()},
        }

        def resource(_root, namespace, name):
            if name == f"secret/{spec.name}-kubeconfig":
                return secret
            return payloads.get((namespace, name))

        adapter = AzureTenantAdapter(clock=lambda: 101)
        with (
            patch.object(adapter, "_config", return_value=config),
            patch(
                "scripts.lib.azure.lifecycle._inspect_foundation",
                return_value=(FOUNDATION, True, ()),
            ),
            patch("scripts.lib.azure.lifecycle._get_management_resource", side_effect=resource),
            patch(
                "scripts.lib.azure.lifecycle._collect_ready_observations",
                return_value=(ready, ()),
            ),
            patch(
                "scripts.lib.azure.lifecycle.discover_azure_owned_resources",
                return_value=discovery,
            ),
        ):
            status = adapter.status(root, spec.name)
        self.assertEqual(status.classification, "ready")
        self.assertTrue(status.components["tenantNetworkReady"])

        evidence = runtime.load_ready_evidence()
        evidence["verifiedAt"] = 102
        runtime.write_ready_evidence(evidence)
        with (
            patch.object(adapter, "_config", return_value=config),
            patch(
                "scripts.lib.azure.lifecycle._inspect_foundation",
                return_value=(FOUNDATION, True, ()),
            ),
            patch("scripts.lib.azure.lifecycle._get_management_resource", side_effect=resource),
            patch(
                "scripts.lib.azure.lifecycle._collect_ready_observations",
                return_value=(ready, ()),
            ),
            patch(
                "scripts.lib.azure.lifecycle.discover_azure_owned_resources",
                return_value=discovery,
            ),
        ):
            status = adapter.status(root, spec.name)
        self.assertEqual(status.classification, "degraded")
        self.assertIn(
            "Azure Ready evidence does not match current identities",
            status.blockers,
        )

        evidence["verifiedAt"] = float("nan")
        runtime.write_ready_evidence(evidence)
        with (
            patch.object(adapter, "_config", return_value=config),
            patch(
                "scripts.lib.azure.lifecycle._inspect_foundation",
                return_value=(FOUNDATION, True, ()),
            ),
            patch("scripts.lib.azure.lifecycle._get_management_resource", side_effect=resource),
            patch(
                "scripts.lib.azure.lifecycle._collect_ready_observations",
                return_value=(ready, ()),
            ),
            patch(
                "scripts.lib.azure.lifecycle.discover_azure_owned_resources",
                return_value=discovery,
            ),
        ):
            status = adapter.status(root, spec.name)
        self.assertEqual(status.classification, "degraded")
        self.assertIn(
            "Azure Ready evidence does not match current identities",
            status.blockers,
        )

        evidence["verifiedAt"] = 100
        evidence["observed"]["machinePoolUid"] = "foreign"
        runtime.write_ready_evidence(evidence)
        with (
            patch.object(adapter, "_config", return_value=config),
            patch(
                "scripts.lib.azure.lifecycle._inspect_foundation",
                return_value=(FOUNDATION, True, ()),
            ),
            patch("scripts.lib.azure.lifecycle._get_management_resource", side_effect=resource),
            patch(
                "scripts.lib.azure.lifecycle._collect_ready_observations",
                return_value=(ready, ()),
            ),
            patch(
                "scripts.lib.azure.lifecycle.discover_azure_owned_resources",
                return_value=discovery,
            ),
        ):
            status = adapter.status(root, spec.name)
        self.assertEqual(status.classification, "degraded")
        self.assertIn(
            "Azure Ready evidence does not match current identities",
            status.blockers,
        )


if __name__ == "__main__":
    unittest.main()
