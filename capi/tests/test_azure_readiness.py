from __future__ import annotations

import base64
import json
import subprocess
import time
import unittest
from unittest.mock import patch

from scripts.azure import AzureTenantAdapter
from scripts.lib.azure.common import load_azure_configuration, tenant_names
from scripts.lib.azure.readiness import (
    _capture_tenant_kubeconfig,
    _collect_ready_observations,
    _tenant_spec_blockers,
    _wait_ready_observations,
)
from scripts.lib.tenant_runtime import TenantRuntime, foundation_sha256
from scripts.lib.tenant_timing import TenantTimings
from scripts.lib.tenants import LIFECYCLE_MARKERS, lifecycle_markers
from tests.azure_fixtures import AzureFixtureMixin, FOUNDATION


def completed(stdout: str = "", returncode: int = 0):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


class AzureReadinessTests(AzureFixtureMixin, unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
