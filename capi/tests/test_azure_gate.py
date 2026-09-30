from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.lib.azure.gate import (
    WorkerSnapshot,
    build_worker_snapshot,
    require_owned_resource_delta,
    require_replacement,
)
from scripts.test_azure_tenant_lifecycle import (
    _admin_create_tenant,
    _admin_delete_tenant,
    _ensure_tenant_ready,
    _require_allocation_lease_absent,
    _require_recreated_identity,
)


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
    def test_admin_lifecycle_responses_are_exact_and_credential_free(self) -> None:
        spec = type(
            "Spec",
            (),
            {
                "name": "tenant-c",
                "workers": 3,
                "kubernetes_version": "1.32.13",
            },
        )()
        with (
            patch(
                "scripts.test_azure_tenant_lifecycle._admin_mutation",
                return_value={
                    "identity": {
                        "name": "tenant-c",
                        "uid": "uid",
                        "generation": True,
                    },
                    "provider": "azure",
                    "kubernetesVersion": "1.32.13",
                    "password": "forbidden",
                },
            ),
            self.assertRaisesRegex(RuntimeError, "create response"),
        ):
            _admin_create_tenant(spec)
        with (
            patch(
                "scripts.test_azure_tenant_lifecycle._admin_mutation",
                return_value={
                    "identity": {
                        "name": "tenant-c",
                        "uid": "uid",
                        "generation": "bad",
                    },
                    "state": "accepted",
                },
            ),
            self.assertRaisesRegex(RuntimeError, "delete response"),
        ):
            _admin_delete_tenant("tenant-c", "uid")

    def test_recreated_identity_requires_new_nonempty_tenant_and_lease_uids(self):
        valid = {
            "metadata": {"uid": "tenant-new"},
            "status": {
                "provider": {
                    "type": "azure",
                    "networkAllocation": {"leaseUID": "lease-new"},
                }
            },
        }
        _require_recreated_identity(valid, "tenant-old", "lease-old")
        for value in (None, "", "lease-old"):
            invalid = json.loads(json.dumps(valid))
            if value is None:
                invalid["status"]["provider"]["networkAllocation"].pop("leaseUID")
            else:
                invalid["status"]["provider"]["networkAllocation"]["leaseUID"] = value
            with self.subTest(value=value), self.assertRaisesRegex(
                RuntimeError, "retained old identity"
            ):
                _require_recreated_identity(
                    invalid,
                    "tenant-old",
                    "lease-old",
                )

    def test_allocation_release_proof_rejects_retained_lease(self):
        with (
            patch(
                "scripts.test_azure_tenant_lifecycle._kubectl",
                return_value=type(
                    "Result",
                    (),
                    {"stdout": "lease.coordination.k8s.io/tenant-azure-slot-old\n"},
                )(),
            ),
            self.assertRaisesRegex(RuntimeError, "Lease remained"),
        ):
            _require_allocation_lease_absent("tenant-azure-slot-old")

    def test_ready_gate_reuses_parsed_spec_after_terminating_tenant(self) -> None:
        spec = type("Spec", (), {"name": "tenant-c"})()
        events = []
        with (
            patch(
                "scripts.test_azure_tenant_lifecycle.read_tenant",
                return_value={
                    "metadata": {
                        "name": "tenant-c",
                        "deletionTimestamp": "2026-09-29T00:00:00Z",
                    }
                },
            ),
            patch(
                "scripts.test_azure_tenant_lifecycle.wait_tenant_absent",
                side_effect=lambda *_args: events.append("absent"),
            ),
            patch(
                "scripts.test_azure_tenant_lifecycle._admin_create_tenant",
                side_effect=lambda observed: events.append(observed),
            ),
            patch(
                "scripts.test_azure_tenant_lifecycle.wait_tenant_ready",
                side_effect=lambda *_args: events.append("ready"),
            ),
            patch(
                "scripts.test_azure_tenant_lifecycle._require_status",
                return_value={"classification": "ready"},
            ),
        ):
            self.assertEqual(
                {"classification": "ready"},
                _ensure_tenant_ready({}, spec),
            )
        self.assertEqual(["absent", spec, "ready"], events)

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

    def test_gate_source_contains_only_explicit_failure_injection(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source = (root / "scripts" / "test_azure_tenant_lifecycle.py").read_text()
        self.assertEqual(1, source.count('"delete-instances"'))
        self.assertIn("ordinary-tenant-deletion", source)
        self.assertIn("external-absence-proof", source)
        self.assertEqual(2, source.count("_source_sha256(spec_path)"))
        self.assertEqual(3, source.count("_admin_create_tenant"))
        self.assertNotIn('_tenant_command("create"', source)
        self.assertNotIn(".runtime", source)
        self.assertNotIn("checkpoint", source.lower())
        self.assertNotIn("persist_evidence", source)
        self.assertNotIn("_incomplete_gate", source)
        self.assertLess(
            source.index('"worker-identity-verification"'),
            source.index('"worker-instance-deletion"'),
        )
        self.assertLess(
            source.index("capture_operator_deletion_proof"),
            source.index('"ordinary-tenant-deletion"'),
        )
