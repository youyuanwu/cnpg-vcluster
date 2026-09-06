from __future__ import annotations

import unittest
from pathlib import Path

from unittest.mock import patch

from scripts.machines import _bootstrap_secrets


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
