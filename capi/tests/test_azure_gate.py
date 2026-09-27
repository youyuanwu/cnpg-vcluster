from __future__ import annotations

import json
import unittest
from pathlib import Path

from scripts.lib.azure.gate import (
    build_worker_snapshot,
    refreshed_observed,
    require_replacement,
)
from scripts.lib.tenant_runtime import TenantRuntimeError
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
            {"markerOperationId": journal.operation_id, "vmssId": VMSS},
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
