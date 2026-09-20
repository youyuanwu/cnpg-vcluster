from __future__ import annotations

import unittest
import json
from pathlib import Path
from subprocess import CompletedProcess

from unittest.mock import patch

from scripts.machines import _bootstrap_secrets, worker_snapshot


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
