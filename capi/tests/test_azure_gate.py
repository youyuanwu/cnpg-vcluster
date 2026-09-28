from __future__ import annotations

import json
import unittest
from pathlib import Path

from scripts.lib.azure.gate import (
    WorkerSnapshot,
    build_worker_snapshot,
    require_owned_resource_delta,
    require_replacement,
)
from scripts.lib.files import write_private_file
from scripts.test_azure_tenant_lifecycle import _incomplete_gate


VMSS = (
    "/subscriptions/00000000-0000-0000-0000-000000000000/"
    "resourceGroups/rg/providers/Microsoft.Compute/"
    "virtualMachineScaleSets/tenant-c-worker"
)


def instance(identifier: int) -> str:
    return f"{VMSS}/virtualMachines/{identifier}"


def readiness(identifiers=(0, 1, 2)):
    nodes = [
        {
            "name": f"node-{identifier}",
            "uid": f"uid-{identifier}",
            "providerID": f"azure://{instance(identifier)}",
            "internalIP": f"10.220.16.{identifier + 4}",
        }
        for identifier in identifiers
    ]
    return {
        "requestedWorkers": 3,
        "readyReplicas": 3,
        "nodeRefs": [item["name"] for item in nodes],
        "nodes": nodes,
    }


class AzureGateTests(unittest.TestCase):
    def test_exact_mapping_checkpoint_and_non_primary_selection(self) -> None:
        snapshot = build_worker_snapshot(
            readiness(),
            VMSS,
            [instance(0), instance(1), instance(2)],
        )
        self.assertEqual("0", snapshot.primary.instance_id)
        self.assertEqual("2", snapshot.target.instance_id)
        self.assertEqual(snapshot, WorkerSnapshot.from_mapping(snapshot.to_mapping()))

    def test_mapping_fails_closed_on_ambiguous_identity(self) -> None:
        payload = readiness()
        payload["nodes"][2]["uid"] = payload["nodes"][1]["uid"]
        with self.assertRaisesRegex(RuntimeError, "duplicated"):
            build_worker_snapshot(
                payload,
                VMSS,
                [instance(0), instance(1), instance(2)],
            )
        with self.assertRaisesRegex(RuntimeError, "exactly three"):
            build_worker_snapshot(
                readiness(),
                VMSS,
                [instance(0), instance(1)],
            )

    def test_recovery_requires_unchanged_survivors_and_one_replacement(self) -> None:
        before = build_worker_snapshot(
            readiness(),
            VMSS,
            [instance(0), instance(1), instance(2)],
        )
        after = build_worker_snapshot(
            readiness((0, 1, 3)),
            VMSS,
            [instance(0), instance(1), instance(3)],
        )
        deleted, replacement = require_replacement(before, after)
        self.assertEqual("2", deleted.instance_id)
        self.assertEqual("3", replacement.instance_id)
        reused = readiness((0, 1, 2))
        reused["nodes"][2]["name"] = "replacement"
        reused["nodes"][2]["uid"] = "replacement-uid"
        reused["nodeRefs"][2] = "replacement"
        with self.assertRaisesRegex(RuntimeError, "reused"):
            require_replacement(
                before,
                build_worker_snapshot(
                    reused,
                    VMSS,
                    [instance(0), instance(1), instance(2)],
                ),
            )

    def test_owned_delta_allows_only_vm_and_associated_nic_replacement(self) -> None:
        before = build_worker_snapshot(
            readiness(),
            VMSS,
            [instance(0), instance(1), instance(2)],
        )
        after = build_worker_snapshot(
            readiness((0, 1, 3)),
            VMSS,
            [instance(0), instance(1), instance(3)],
        )
        deleted, replacement = require_replacement(before, after)
        stable = {
            "id": VMSS,
            "type": "microsoft.compute/virtualmachinescalesets",
        }
        recorded = {
            "azure": [
                stable,
                {
                    "id": instance(2),
                    "type": "microsoft.compute/virtualmachinescalesets/virtualmachines",
                },
                {
                    "id": f"{instance(2)}/networkInterfaces/old",
                    "type": "microsoft.network/networkinterfaces",
                    "virtualMachineId": instance(2),
                },
            ],
            "provider": [
                {
                    "apiVersion": "network.azure.com/v1",
                    "kind": "PublicIPAddress",
                    "name": "tenant-c",
                    "uid": "uid",
                    "resourceId": "/public-ip",
                }
            ],
        }
        discovered = {
            "azure": [
                stable,
                {
                    "id": instance(3),
                    "type": "microsoft.compute/virtualmachinescalesets/virtualmachines",
                },
                {
                    "id": f"{instance(3)}/networkInterfaces/new",
                    "type": "microsoft.network/networkinterfaces",
                    "virtualMachineId": instance(3),
                },
            ],
            "provider": list(recorded["provider"]),
        }
        require_owned_resource_delta(recorded, discovered, deleted, replacement)
        discovered["azure"].append(
            {
                "id": "/unrelated",
                "type": "microsoft.network/publicipaddresses",
            }
        )
        with self.assertRaisesRegex(RuntimeError, "unrelated"):
            require_owned_resource_delta(
                recorded, discovered, deleted, replacement
            )

    def test_incomplete_evidence_resumes_only_destructive_attempt(self) -> None:
        with self.subTest("started"):
            root = Path(self._testMethodName)
            self.addCleanup(
                lambda: (
                    (root / "lifecycle-operation-1.json").unlink(missing_ok=True),
                    root.rmdir() if root.exists() else None,
                )
            )
            payload = {
                "schema": 2,
                "operationId": "operation-1",
                "tenant": "tenant-c",
                "specificationSha256": "spec-sha",
                "sourceSha256": "source-sha",
                "records": [
                    {
                        "phase": "foundation-readiness",
                        "status": "passed",
                        "seconds": 1.0,
                    },
                    {
                        "phase": "worker-instance-deletion",
                        "status": "started",
                        "seconds": 0.0,
                    },
                ],
            }
            write_private_file(
                root / "lifecycle-operation-1.json",
                json.dumps(payload),
            )
            self.assertEqual(
                "operation-1",
                _incomplete_gate(
                    root,
                    "tenant-c",
                    "spec-sha",
                    "source-sha",
                )["operationId"],
            )
            payload["records"].append(
                {
                    "phase": "recreation",
                    "status": "passed",
                    "seconds": 1.0,
                }
            )
            write_private_file(
                root / "lifecycle-operation-1.json",
                json.dumps(payload),
            )
            self.assertIsNone(
                _incomplete_gate(
                    root,
                    "tenant-c",
                    "spec-sha",
                    "source-sha",
                )
            )

    def test_gate_source_contains_only_explicit_failure_injection(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source = (root / "scripts" / "test_azure_tenant_lifecycle.py").read_text()
        self.assertEqual(1, source.count('"delete-instances"'))
        self.assertIn("ordinary-tenant-deletion", source)
        self.assertIn("external-absence-proof", source)
