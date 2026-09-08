from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts.break_glass import FINALIZERS, break_glass


def result(returncode: int, stdout: str = "", stderr: str = ""):
    return type(
        "Result",
        (),
        {"returncode": returncode, "stdout": stdout, "stderr": stderr},
    )()


class BreakGlassTests(unittest.TestCase):
    def test_supported_finalizers_match_pinned_providers(self) -> None:
        self.assertEqual(
            FINALIZERS,
            {
                "cluster": "cluster.cluster.x-k8s.io",
                "machine": "machine.cluster.x-k8s.io",
                "machinedeployment": "cluster.x-k8s.io/machinedeployment",
                "devcluster": "dockercluster.infrastructure.cluster.x-k8s.io",
                "devmachine": "dockermachine.infrastructure.cluster.x-k8s.io",
                "kamajicontrolplane": "ecr.kamaji.clastix.io/finalizer",
                "configmap": "cnpg-vcluster.capi/break-glass",
            },
        )

    def resource(self):
        return {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": "fixture",
                "namespace": "tenant-a",
                "uid": "uid-1",
                "resourceVersion": "10",
                "deletionTimestamp": "2026-01-01T00:00:00Z",
                "labels": {"example.owner": "lab"},
                "finalizers": [
                    "keep.example/finalizer",
                    "cnpg-vcluster.capi/break-glass",
                ],
            },
            "status": {
                "conditions": [
                    {
                        "type": "Blocked",
                        "status": "True",
                        "reason": "token=condition-token",
                        "message": "password=condition-password",
                    }
                ]
            },
        }

    def test_wrong_uid_refuses_before_patch(self) -> None:
        client = Mock()
        client.kubectl.return_value = result(
            0, stdout=json.dumps(self.resource())
        )
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch("scripts.break_glass.require_management_ownership"),
            patch("scripts.break_glass.validate_management_kubeconfig"),
            patch("scripts.break_glass.ManagementClient", return_value=client),
        ):
            with self.assertRaisesRegex(RuntimeError, "exact owned"):
                break_glass(
                    Path(temporary),
                    {
                        "OWNERSHIP_LABEL": "example.owner",
                        "LAB_PREFIX": "lab",
                    },
                    "configmap",
                    "tenant-a",
                    "fixture",
                    "wrong",
                )
        self.assertEqual(client.kubectl.call_count, 1)

    def test_removes_only_exact_finalizer_and_preserves_docker(self) -> None:
        client = Mock()
        client.kubectl.side_effect = [
            result(0, stdout=json.dumps(self.resource())),
            result(0),
            result(
                1,
                stderr='Error from server (NotFound): configmaps "fixture" not found',
            ),
        ]
        inventory = {"containers": ["a"], "volumes": ["b"]}
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch("scripts.break_glass.require_management_ownership"),
            patch("scripts.break_glass.validate_management_kubeconfig"),
            patch("scripts.break_glass.ManagementClient", return_value=client),
            patch(
                "scripts.break_glass._docker_inventory",
                return_value=inventory,
            ),
        ):
            evidence = break_glass(
                Path(temporary),
                {
                    "OWNERSHIP_LABEL": "example.owner",
                    "LAB_PREFIX": "lab",
                },
                "configmap",
                "tenant-a",
                "fixture",
                "uid-1",
            )
            payload = json.loads(evidence.read_text(encoding="utf-8"))
        patch_payload = json.loads(client.kubectl.call_args_list[1].args[-1])
        self.assertEqual(
            patch_payload,
            [
                {"op": "test", "path": "/metadata/uid", "value": "uid-1"},
                {
                    "op": "test",
                    "path": "/metadata/resourceVersion",
                    "value": "10",
                },
                {
                    "op": "test",
                    "path": "/metadata/finalizers/1",
                    "value": "cnpg-vcluster.capi/break-glass",
                },
                {"op": "remove", "path": "/metadata/finalizers/1"},
            ],
        )
        self.assertEqual(
            payload["selectedFinalizer"],
            "cnpg-vcluster.capi/break-glass",
        )
        serialized = json.dumps(payload)
        self.assertNotIn("condition-token", serialized)
        self.assertNotIn("condition-password", serialized)
