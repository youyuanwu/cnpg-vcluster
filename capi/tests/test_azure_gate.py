from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.lib.azure.gate import (
    build_worker_snapshot,
    refreshed_observed,
    require_owned_resource_delta,
    require_replacement,
)
from scripts.lib.azure.ownership import classify_azure_owned_resources
from scripts.lib.files import write_private_file
from scripts.lib.tenant_runtime import TenantRuntimeError
from scripts.test_azure_tenant_lifecycle import (
    _complete_prior_gate_attempt,
    _incompatible_existing_identity,
    _incomplete_gate_records,
    _require_authenticated_worker_deletion,
    _require_three_worker_pool,
)
from tests.azure_fixtures import AzureFixtureMixin


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


class AzureGateTests(AzureFixtureMixin, unittest.TestCase):
    def test_exact_mapping_and_non_primary_selection(self):
        snapshot = build_worker_snapshot(
            readiness(),
            VMSS,
            [instance(0), instance(1), instance(2)],
        )
        self.assertEqual(snapshot.primary.instance_id, "0")
        self.assertEqual(snapshot.target.instance_id, "2")

    def test_mapping_fails_closed_on_provider_or_identity_ambiguity(self):
        payload = readiness()
        payload["nodes"][2]["providerID"] = "azure:///foreign/virtualMachines/2"
        with self.assertRaisesRegex(RuntimeError, "expected VMSS"):
            build_worker_snapshot(
                payload,
                VMSS,
                [instance(0), instance(1), instance(2)],
            )
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
                [instance(0), instance(1), instance(2), instance(2)],
            )
        raw = readiness()
        raw["nodes"][0]["providerID"] = instance(0)
        with self.assertRaisesRegex(RuntimeError, "provider ID"):
            build_worker_snapshot(
                raw,
                VMSS,
                [instance(0), instance(1), instance(2)],
            )
        invalid_vmss = VMSS.replace("/subscriptions/00000000", "/subscriptions/ ")
        invalid_readiness = readiness()
        for node in invalid_readiness["nodes"]:
            node["providerID"] = node["providerID"].replace(VMSS, invalid_vmss)
        with self.assertRaisesRegex(RuntimeError, "VMSS resource ID"):
            build_worker_snapshot(
                invalid_readiness,
                invalid_vmss,
                [
                    value.replace(VMSS, invalid_vmss)
                    for value in (instance(0), instance(1), instance(2))
                ],
            )

    def test_recovery_requires_unchanged_survivors_and_new_pair(self):
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
        self.assertEqual(deleted.instance_id, "2")
        self.assertEqual(replacement.instance_id, "3")
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

    def test_identity_compare_and_replace_is_exact_and_operation_safe(self):
        root = self.make_root()
        spec = self.spec(workers=3)
        runtime, journal = self.start_journal(root, spec)
        runtime.complete_create(
            runtime.load_operation(),
            spec,
            {
                "markerOperationId": journal.operation_id,
                "vmssId": VMSS,
                "vmssInstanceIds": "[]",
                "nodeIdentities": "[]",
                "azureResources": json.dumps(
                    {"azure": [], "aso": [], "unknown": []}
                ),
            },
        )
        identity = runtime.load_identity()
        observed = refreshed_observed(
            identity.observed,
            readiness(),
            [instance(0), instance(1), instance(2)],
            {"azure": [], "aso": [], "unknown": []},
        )
        replacement = runtime.compare_and_replace_identity(identity, observed)
        self.assertEqual(replacement.observed, observed)
        with self.assertRaisesRegex(
            TenantRuntimeError,
            "changed before replacement",
        ):
            runtime.compare_and_replace_identity(identity, observed)
        unrelated = dict(replacement.observed)
        unrelated["vmssId"] = "changed"
        with self.assertRaisesRegex(TenantRuntimeError, "unrelated"):
            runtime.compare_and_replace_identity(replacement, unrelated)

    def test_owned_resource_delta_rejects_unrelated_changes(self):
        before = build_worker_snapshot(
            readiness(),
            VMSS,
            [instance(0), instance(1), instance(2)],
        )

    def test_real_classifier_preserves_nic_instance_association(self):
        markers = {
            "tenant": "tenant-c",
            "profile": "azure",
            "specificationSha256": "spec-sha",
            "foundationSha256": "foundation-sha",
            "operationId": "operation-1",
        }
        old_nic = {
        "id": f"{VMSS}/virtualMachines/2/networkInterfaces/old",
        "type": "Microsoft.Network/networkInterfaces",
        "virtualMachineId": instance(2),
        }
        new_nic = {
        "id": f"{VMSS}/virtualMachines/3/networkInterfaces/new",
        "type": "Microsoft.Network/networkInterfaces",
        "virtualMachineId": instance(3),
        }
        recorded = classify_azure_owned_resources(
        [
            {
                "id": instance(2),
                "type": "Microsoft.Compute/virtualMachineScaleSets/virtualMachines",
            },
            old_nic,
        ],
        markers,
        verified_ids=(instance(2), old_nic["id"]),
        )
        discovered = classify_azure_owned_resources(
        [
            {
                "id": instance(3),
                "type": "Microsoft.Compute/virtualMachineScaleSets/virtualMachines",
            },
            new_nic,
        ],
        markers,
        verified_ids=(instance(3), new_nic["id"]),
        )
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
        require_owned_resource_delta(
        json.dumps(recorded),
        discovered,
        deleted,
        replacement,
        )
        after = build_worker_snapshot(
            readiness((0, 1, 3)),
            VMSS,
            [instance(0), instance(1), instance(3)],
        )
        deleted, replacement = require_replacement(before, after)
        recorded = {
            "azure": [
                {
                    "id": VMSS,
                    "type": "Microsoft.Compute/virtualMachineScaleSets",
                    "tags": {"tenant": "tenant-c"},
                },
                {
                    "id": instance(2),
                    "type": "Microsoft.Compute/virtualMachineScaleSets/virtualMachines",
                }
            ],
            "aso": [],
            "unknown": [],
        }
        discovered = {
            "azure": [
                {
                    "id": VMSS,
                    "type": "Microsoft.Compute/virtualMachineScaleSets",
                    "tags": {"tenant": "tenant-c"},
                },
                {
                    "id": instance(3),
                    "type": "Microsoft.Compute/virtualMachineScaleSets/virtualMachines",
                }
            ],
            "aso": [],
            "unknown": [],
        }
        require_owned_resource_delta(
            json.dumps(recorded),
            discovered,
            deleted,
            replacement,
        )
        discovered["azure"].append(
            {
                "id": f"{VMSS}/unrelated",
                "type": "Microsoft.Network/publicIPAddresses",
            }
        )
        with self.assertRaisesRegex(RuntimeError, "unrelated"):
            require_owned_resource_delta(
                json.dumps(recorded),
                discovered,
                deleted,
                replacement,
            )
        discovered = {
            "azure": [
                {
                    "id": VMSS,
                    "type": "Microsoft.Compute/virtualMachineScaleSets",
                    "tags": {"tenant": "changed"},
                },
                {
                    "id": instance(3),
                    "type": "Microsoft.Compute/virtualMachineScaleSets/virtualMachines",
                },
            ],
            "aso": [],
            "unknown": [],
        }
        with self.assertRaisesRegex(RuntimeError, "retained"):
            require_owned_resource_delta(
                json.dumps(recorded),
                discovered,
                deleted,
                replacement,
            )
        recorded_with_nic = {
            **recorded,
            "azure": [
                *recorded["azure"],
                {
                    "id": f"{VMSS}/networkInterfaces/old",
                    "type": "Microsoft.Network/networkInterfaces",
                    "virtualMachineId": instance(0),
                },
            ],
        }
        discovered_with_nic = {
            **discovered,
            "azure": [
                {
                    "id": VMSS,
                    "type": "Microsoft.Compute/virtualMachineScaleSets",
                    "tags": {"tenant": "tenant-c"},
                },
                {
                    "id": instance(3),
                    "type": "Microsoft.Compute/virtualMachineScaleSets/virtualMachines",
                },
                {
                    "id": f"{VMSS}/networkInterfaces/new",
                    "type": "Microsoft.Network/networkInterfaces",
                    "virtualMachineId": instance(1),
                },
            ],
        }
        with self.assertRaisesRegex(RuntimeError, "NIC ownership"):
            require_owned_resource_delta(
                json.dumps(recorded_with_nic),
                discovered_with_nic,
                deleted,
                replacement,
            )

    def test_incomplete_evidence_classifier_is_exact_and_unambiguous(self):
        root = self.make_root()
        evidence = root / ".runtime" / "azure-gate" / "evidence"
        payload = {
            "schema": 1,
            "operationId": "operation-1",
            "tenant": "tenant-c",
            "specificationSha256": "spec-sha",
            "revision": "revision",
            "records": [
                {
                    "phase": "worker-instance-deletion",
                    "status": "passed",
                    "seconds": 1.0,
                }
            ],
        }
        write_private_file(
            evidence / "lifecycle-operation-1.json",
            json.dumps(payload),
        )
        self.assertEqual(
            _incomplete_gate_records(
                evidence,
                "tenant-c",
                "spec-sha",
                "revision",
            ),
            {
                "worker-instance-deletion",
                "worker-instance-deletion-started",
                "gate-operation:operation-1",
            },
        )
        payload["operationId"] = "operation-2"
        write_private_file(
            evidence / "lifecycle-operation-2.json",
            json.dumps(payload),
        )
        with self.assertRaisesRegex(RuntimeError, "multiple incomplete"):
            _incomplete_gate_records(
                evidence,
                "tenant-c",
                "spec-sha",
                "revision",
            )

    def test_continuation_retires_original_attempt(self):
        root = self.make_root()
        evidence = root / ".runtime" / "azure-gate" / "evidence"
        payload = {
            "schema": 1,
            "operationId": "operation-1",
            "tenant": "tenant-c",
            "specificationSha256": "spec-sha",
            "revision": "revision",
            "records": [
                {
                    "phase": "worker-instance-deletion",
                    "status": "passed",
                    "seconds": 1.0,
                }
            ],
        }
        write_private_file(
            evidence / "lifecycle-operation-1.json",
            json.dumps(payload),
        )
        phases = _incomplete_gate_records(
            evidence,
            "tenant-c",
            "spec-sha",
            "revision",
        )
        _complete_prior_gate_attempt(
            evidence,
            phases,
            [
                {"phase": "worker-recovery", "status": "passed", "seconds": 1.0},
                {
                    "phase": "worker-identity-refresh",
                    "status": "passed",
                    "seconds": 1.0,
                },
                {
                    "phase": "targeted-delete-absent",
                    "status": "passed",
                    "seconds": 1.0,
                },
                {
                    "phase": "foundation-verification",
                    "status": "passed",
                    "seconds": 1.0,
                },
                {"phase": "recreation", "status": "passed", "seconds": 1.0},
            ],
        )
        self.assertIsNone(
            _incomplete_gate_records(
                evidence,
                "tenant-c",
                "spec-sha",
                "revision",
            )
        )

    def test_pool_desired_replicas_survive_transient_readiness(self):
        with patch(
            "scripts.test_azure_tenant_lifecycle._get_management_resource",
            return_value={
                "spec": {"replicas": 3},
                "status": {"readyReplicas": 2},
            },
        ):
            _require_three_worker_pool(self.spec(workers=3), require_ready=False)
            with self.assertRaisesRegex(RuntimeError, "three-replica Ready"):
                _require_three_worker_pool(
                    self.spec(workers=3),
                    require_ready=True,
                )

    def test_evidence_classifier_rejects_malformed_record_shapes(self):
        root = self.make_root()
        evidence = root / ".runtime" / "azure-gate" / "evidence"
        payload = {
            "schema": 1,
            "operationId": "operation-1",
            "tenant": "tenant-c",
            "specificationSha256": "spec-sha",
            "revision": "revision",
            "records": [
                {
                    "phase": "worker-instance-deletion",
                    "status": "passed",
                    "seconds": -1,
                    "extra": True,
                }
            ],
        }
        write_private_file(
            evidence / "lifecycle-operation-1.json",
            json.dumps(payload),
        )
        with self.assertRaisesRegex(RuntimeError, "invalid Azure lifecycle"):
            _incomplete_gate_records(
                evidence,
                "tenant-c",
                "spec-sha",
                "revision",
            )

    def test_evidence_classifier_rejects_impossible_phase_order(self):
        root = self.make_root()
        evidence = root / ".runtime" / "azure-gate" / "evidence"
        phases = (
            "recreation",
            "targeted-delete-absent",
            "foundation-verification",
            "worker-identity-refresh",
            "worker-recovery",
            "worker-instance-deletion",
        )
        payload = {
            "schema": 1,
            "operationId": "operation-1",
            "tenant": "tenant-c",
            "specificationSha256": "spec-sha",
            "revision": "revision",
            "records": [
                {"phase": phase, "status": "passed", "seconds": 1.0}
                for phase in phases
            ],
        }
        write_private_file(
            evidence / "lifecycle-operation-1.json",
            json.dumps(payload),
        )
        with self.assertRaisesRegex(RuntimeError, "phase order"):
            _incomplete_gate_records(
                evidence,
                "tenant-c",
                "spec-sha",
                "revision",
            )

    def test_completed_gate_is_not_an_incomplete_candidate(self):
        root = self.make_root()
        evidence = root / ".runtime" / "azure-gate" / "evidence"
        payload = {
            "schema": 1,
            "operationId": "operation-1",
            "tenant": "tenant-c",
            "specificationSha256": "spec-sha",
            "revision": "revision",
            "records": [
                {
                    "phase": phase,
                    "status": "passed",
                    "seconds": 1.0,
                }
                for phase in (
                    "worker-instance-deletion",
                    "worker-recovery",
                    "worker-identity-refresh",
                    "targeted-delete-absent",
                    "foundation-verification",
                    "recreation",
                )
            ],
        }
        write_private_file(
            evidence / "lifecycle-operation-1.json",
            json.dumps(payload),
        )
        self.assertIsNone(
            _incomplete_gate_records(
                evidence,
                "tenant-c",
                "spec-sha",
                "revision",
            )
        )

    def test_started_but_unconfirmed_deletion_fails_closed(self):
        _require_authenticated_worker_deletion(
            {"worker-instance-deletion-started"}
        )
        _require_authenticated_worker_deletion(
            {
                "worker-instance-deletion-started",
                "worker-instance-deletion",
            }
        )
        with self.assertRaisesRegex(RuntimeError, "ambiguous"):
            _require_authenticated_worker_deletion(set())

    def test_unrelated_historical_evidence_is_ignored(self):
        root = self.make_root()
        evidence = root / ".runtime" / "azure-gate" / "evidence"
        write_private_file(
            evidence / "lifecycle-old.json",
            json.dumps({"legacy": True}),
        )
        self.assertIsNone(
            _incomplete_gate_records(
                evidence,
                "tenant-c",
                "spec-sha",
                "current-revision",
            )
        )

    def test_incompatible_existing_tenant_is_selected_for_cleanup(self):
        root = self.make_root()
        old_spec = self.spec(workers=1)
        runtime, journal = self.start_journal(root, old_spec)
        runtime.complete_create(
            runtime.load_operation(),
            old_spec,
            {"markerOperationId": journal.operation_id},
        )
        self.assertIsNotNone(
            _incompatible_existing_identity(runtime, self.spec(workers=3))
        )
        self.assertIsNone(
            _incompatible_existing_identity(runtime, old_spec)
        )

    def test_live_gate_recipe_exists_but_is_not_invoked_by_tests(self):
        root = Path(__file__).resolve().parents[1]
        justfile = (root / "Justfile").read_text(encoding="utf-8")
        script = (
            root / "scripts" / "test_azure_tenant_lifecycle.py"
        ).read_text(encoding="utf-8")
        self.assertIn("azure-test-tenant-lifecycle", justfile)
        self.assertIn("destroy-legacy-foundation", script)
        self.assertIn("targeted-delete-absent", script)
        self.assertIn("worker-instance-deletion", script)
        self.assertIn("worker-identity-refresh", script)
        self.assertIn('"recreation"', script)
        self.assertIn('"status", "--porcelain", "--untracked-files=no"', script)
        self.assertIn('"revision": revision', script)
        example = json.loads(
            (root / "config" / "tenants" / "examples" / "azure.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(example["workers"], 3)


if __name__ == "__main__":
    unittest.main()
