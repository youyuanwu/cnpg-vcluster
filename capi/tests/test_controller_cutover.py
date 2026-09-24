from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from scripts.lib.controller_cutover import (
    controller_lifecycle_epoch,
    controller_mutation_enabled,
    require_clean_controller_cutover,
)


class FakeManagementClient:
    def __init__(self, responses: list[CompletedProcess[str]]) -> None:
        self.responses = iter(responses)
        self.calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def kubectl(self, *arguments: str, **kwargs: object) -> CompletedProcess[str]:
        self.calls.append((arguments, kwargs))
        return next(self.responses)

    def json(self, *arguments: str) -> dict[str, object]:
        response = self.kubectl(*arguments, "-o", "json")
        return json.loads(response.stdout)


class ControllerCutoverTests(unittest.TestCase):
    def test_mutation_mode_reads_manager_arguments(self) -> None:
        deployment = {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "manager",
                                "args": ["--leader-elect=true", "--mutation-enabled=true"],
                            }
                        ]
                    }
                }
            }
        }
        client = FakeManagementClient(
            [
                CompletedProcess(
                    [], 0, stdout=json.dumps(deployment), stderr=""
                )
            ]
        )
        self.assertTrue(controller_mutation_enabled(client))

    def test_lifecycle_epoch_reads_manager_arguments(self) -> None:
        deployment = {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "manager",
                                "args": [
                                    "--mutation-enabled=true",
                                    "--lifecycle-epoch=desired-state-v2",
                                ],
                            }
                        ]
                    }
                }
            }
        }
        client = FakeManagementClient(
            [CompletedProcess([], 0, stdout=json.dumps(deployment), stderr="")]
        )
        self.assertEqual(
            "desired-state-v2",
            controller_lifecycle_epoch(client),
        )

    def test_legacy_controller_has_no_lifecycle_epoch(self) -> None:
        deployment = {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "manager",
                                "args": ["--mutation-enabled=true"],
                            }
                        ]
                    }
                }
            }
        }
        client = FakeManagementClient(
            [CompletedProcess([], 0, stdout=json.dumps(deployment), stderr="")]
        )
        self.assertIsNone(controller_lifecycle_epoch(client))

    def test_legacy_runtime_residue_blocks_before_cluster_inspection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            residue = root / ".runtime" / "lifecycle" / "local"
            residue.mkdir(parents=True)
            (residue / "tenant.json").write_text("{}", encoding="utf-8")
            client = FakeManagementClient([])
            with self.assertRaisesRegex(RuntimeError, "legacy local lifecycle"):
                require_clean_controller_cutover(
                    root,
                    {"OWNERSHIP_LABEL": "example.io/owned", "LAB_PREFIX": "lab"},
                    client,
                )
            self.assertEqual([], client.calls)

    def test_legacy_credentials_and_endpoint_allocations_block(self) -> None:
        for relative, content, expected in (
            (
                ".runtime/tenants/tenant-a/kubeconfig",
                "credentials",
                "legacy local lifecycle",
            ),
            (
                ".runtime/management/tenant-endpoints.json",
                json.dumps(
                    {
                        "schema": 1,
                        "networkId": "network-id",
                        "allocations": {"tenant-a": "172.18.255.1"},
                    }
                ),
                "endpoint allocations",
            ),
        ):
            with self.subTest(relative=relative):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    path = root / relative
                    path.parent.mkdir(parents=True)
                    path.write_text(content, encoding="utf-8")
                    with self.assertRaisesRegex(RuntimeError, expected):
                        require_clean_controller_cutover(
                            root,
                            {
                                "OWNERSHIP_LABEL": "example.io/owned",
                                "LAB_PREFIX": "lab",
                                "KIND_CLUSTER_NAME": "management",
                            },
                            FakeManagementClient([]),
                        )

    def test_clean_cutover_accepts_empty_kubernetes_and_docker_state(self) -> None:
        responses = [
            CompletedProcess([], 0, stdout="", stderr=""),
            CompletedProcess([], 0, stdout='{"items":[]}', stderr=""),
            *[
                CompletedProcess([], 0, stdout="", stderr="")
                for _ in range(6)
            ],
            CompletedProcess(
                [],
                1,
                stdout="",
                stderr="Error from server (NotFound)",
            ),
        ]
        client = FakeManagementClient(responses)
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch(
                "scripts.lib.controller_cutover.run",
                return_value=CompletedProcess([], 0, stdout="", stderr=""),
            ),
        ):
            root = Path(temporary)
            ledger = root / ".runtime" / "management" / "tenant-endpoints.json"
            ledger.parent.mkdir(parents=True)
            ledger.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "networkId": "network-id",
                        "allocations": {},
                    }
                ),
                encoding="utf-8",
            )
            require_clean_controller_cutover(
                root,
                {
                    "OWNERSHIP_LABEL": "example.io/owned",
                    "LAB_PREFIX": "lab",
                    "KIND_CLUSTER_NAME": "management",
                },
                client,
            )

    def test_orphan_capd_container_blocks_activation(self) -> None:
        responses = [
            CompletedProcess([], 0, stdout="", stderr=""),
            CompletedProcess([], 0, stdout='{"items":[]}', stderr=""),
            *[
                CompletedProcess([], 0, stdout="", stderr="")
                for _ in range(6)
            ],
            CompletedProcess(
                [],
                1,
                stdout="",
                stderr="Error from server (NotFound)",
            ),
        ]
        client = FakeManagementClient(responses)
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch(
                "scripts.lib.controller_cutover.run",
                side_effect=[
                    CompletedProcess([], 0, stdout="", stderr=""),
                    CompletedProcess([], 0, stdout="container-id\n", stderr=""),
                    CompletedProcess([], 0, stdout="", stderr=""),
                ],
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "CAPD tenant containers"):
                require_clean_controller_cutover(
                    Path(temporary),
                    {
                        "OWNERSHIP_LABEL": "example.io/owned",
                        "LAB_PREFIX": "lab",
                        "KIND_CLUSTER_NAME": "management",
                    },
                    client,
                )


if __name__ == "__main__":
    unittest.main()
