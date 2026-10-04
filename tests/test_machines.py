from __future__ import annotations

import unittest
import json
from pathlib import Path
from subprocess import CompletedProcess

from unittest.mock import patch

from scripts.machines import _bootstrap_secrets, _replace_machine, worker_snapshot
from scripts.lib.addons import wait_network_ready


class FakeClient:
    def kubectl(self, *arguments: str):
        class Result:
            stdout = """{
              "items": [
                {"metadata": {"name": "worker-a"}},
                {"metadata": {"name": "worker-b"}},
                {"metadata": {"name": "worker-c"}}
              ]
            }"""

        if "secrets" in arguments:
            Result.stdout = """{
              "items": [
                {
                  "metadata": {
                    "name": "cluster-kubeconfig",
                    "ownerReferences": [{"kind": "KamajiControlPlane"}]
                  },
                  "type": "cluster.x-k8s.io/secret"
                },
                {
                  "metadata": {
                    "name": "worker-a",
                    "ownerReferences": [{"kind": "KubeadmConfig"}]
                  },
                  "type": "cluster.x-k8s.io/secret"
                },
                {
                  "metadata": {
                    "name": "worker-b",
                    "ownerReferences": [{"kind": "KubeadmConfig"}]
                  },
                  "type": "cluster.x-k8s.io/secret"
                },
                {
                  "metadata": {
                    "name": "worker-c",
                    "ownerReferences": [{"kind": "KubeadmConfig"}]
                  },
                  "type": "cluster.x-k8s.io/secret"
                }
              ]
            }"""
        return Result()


class MachineTests(unittest.TestCase):
    def test_network_ready_after_worker_restart_reads_management_deployment(self) -> None:
        tenant = type("Tenant", (), {"namespace": "tenant-a", "name": "tenant-a"})()
        calls = []
        machine = {
            "metadata": {"generation": 1},
            "status": {"conditions": [
                {"type": "Ready", "status": "True", "observedGeneration": 1}
            ]},
        }

        def management(*arguments):
            calls.append(arguments)
            payload = (
                {"spec": {"replicas": 1}}
                if any("machinedeployments" in item for item in arguments)
                else {"items": [machine]}
            )
            return CompletedProcess([], 0, stdout=json.dumps(payload))

        def workload(*arguments, **_kwargs):
            payload = (
                {"items": [{"status": {"conditions": [
                    {"type": "Ready", "status": "True"}
                ]}}]}
                if "nodes" in arguments
                else {"spec": {"replicas": 1}, "status": {
                    "availableReplicas": 1, "desiredNumberScheduled": 1,
                    "numberAvailable": 1,
                }}
            )
            return CompletedProcess([], 0, stdout=json.dumps(payload))

        client = type("Client", (), {"kubectl": lambda _self, *args: management(*args)})()
        with (
            patch("scripts.lib.addons.ManagementClient", return_value=client),
            patch("scripts.lib.addons._tenant_kubectl", side_effect=workload),
            patch("scripts.lib.addons.wait_for",
                  side_effect=lambda _description, _timeout, _interval, predicate:
                  self.assertTrue(predicate())),
        ):
            wait_network_ready(Path("."), {
                "TENANT_CONTROL_PLANE_TIMEOUT": "1s",
                "WAIT_POLL_INTERVAL": "1s",
            }, tenant)
        self.assertTrue(any(
            "machinedeployments" in item for arguments in calls for item in arguments
        ))

    def test_worker_snapshot_uses_requested_worker_count(self) -> None:
        machine = {
            "metadata": {
                "name": "worker-a",
                "uid": "machine-uid",
                "generation": 1,
            },
            "status": {
                "conditions": [
                    {
                        "type": "Ready",
                        "status": "True",
                        "observedGeneration": 1,
                    }
                ]
            },
        }
        devmachine = {
            "metadata": {
                "name": "worker-a",
                "uid": "devmachine-uid",
                "generation": 1,
            },
            "status": {
                "conditions": [
                    {
                        "type": "Ready",
                        "status": "True",
                        "observedGeneration": 1,
                    }
                ]
            },
        }
        node = {"metadata": {"name": "worker-a", "uid": "node-uid"}}
        tenant = type(
            "Tenant",
            (),
            {"namespace": "tenant-c", "name": "tenant-c", "workers": 1},
        )()
        client = type("Client", (), {})()
        client.kubectl = lambda *_args, **_kwargs: CompletedProcess(
            [],
            0,
            stdout=json.dumps({"items": [devmachine]}),
        )

        def run(command, **_kwargs):
            if command[:3] == ["docker", "ps", "-a"]:
                return CompletedProcess(command, 0, stdout="worker-a\n")
            return CompletedProcess(command, 0, stdout="container-id\n")

        with (
            patch("scripts.machines._machine_items", return_value=[machine]),
            patch(
                "scripts.machines._registered",
                return_value={
                    "machine": machine,
                    "devmachine": devmachine,
                    "node": node,
                },
            ),
            patch("scripts.machines.verify_worker_runtime"),
            patch("scripts.machines.run", side_effect=run),
            patch(
                "scripts.machines._tenant_kubectl",
                return_value=CompletedProcess(
                    [],
                    0,
                    stdout=json.dumps({"items": [node]}),
                ),
            ),
        ):
            snapshot = worker_snapshot(Path("."), {}, client, tenant)
        self.assertEqual(
            snapshot,
            {
                "worker-a": {
                    "machineUID": "machine-uid",
                    "devMachineUID": "devmachine-uid",
                    "nodeUID": "node-uid",
                    "containerID": "container-id",
                }
            },
        )

    def test_bootstrap_secret_inventory_excludes_cluster_kubeconfig(self) -> None:
        tenant = type("Tenant", (), {"namespace": "tenant", "name": "cluster"})()
        with patch("scripts.machines._verify_bootstrap_secret"):
            self.assertEqual(
                _bootstrap_secrets(
                    Path("."),
                    {},
                    FakeClient(),
                    tenant,
                ),
                {"worker-a", "worker-b", "worker-c"},
            )

    def test_machine_replacement_uses_catalog_resource_identity(self) -> None:
        calls = []
        client = type("Client", (), {})()
        client.kubectl = lambda *args, **kwargs: calls.append(args) or CompletedProcess(
            [], 0, stdout=""
        )
        tenant = type(
            "Tenant",
            (),
            {"namespace": "tenant-a", "name": "tenant-a", "workers": 1},
        )()
        before = {"worker-a": {"machineUID": "old-uid"}}
        after = {"worker-b": {"machineUID": "new-uid"}}
        with (
            patch("scripts.machines.wait_network_ready"),
            patch("scripts.machines.worker_snapshot", return_value=after),
            patch(
                "scripts.machines.wait_for",
                side_effect=lambda _description, _timeout, _interval, predicate: predicate(),
            ),
            patch(
                "scripts.machines.run",
                return_value=CompletedProcess([], 1, stdout=""),
            ),
            patch(
                "scripts.machines.read_storage_marker",
                return_value="phase3-marker\n",
            ),
        ):
            self.assertEqual(
                _replace_machine(
                    Path("."),
                    {
                        "DELETE_TIMEOUT": "1s",
                        "WORKER_REGISTRATION_TIMEOUT": "1s",
                        "WAIT_POLL_INTERVAL": "1s",
                    },
                    client,
                    tenant,
                    before,
                ),
                after,
            )
        self.assertIn(
            "machines.cluster.x-k8s.io/worker-a",
            calls[0],
        )
