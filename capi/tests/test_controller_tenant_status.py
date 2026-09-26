from __future__ import annotations

import unittest

from scripts.controller_tenant_status import evaluate_tenant


def tenant_document() -> dict[str, object]:
    return {
        "metadata": {"name": "tenant-a", "generation": 2},
        "spec": {
            "kubernetesVersion": "v1.36.4",
            "workers": 1,
            "databases": 1,
        },
        "status": {
            "observedGeneration": 2,
            "phase": "Ready",
            "conditions": [
                {
                    "type": "Ready",
                    "status": "True",
                    "observedGeneration": 2,
                    "reason": "Ready",
                }
            ],
        },
    }


class ControllerTenantStatusTests(unittest.TestCase):
    def test_current_ready_condition_is_accepted(self) -> None:
        result = evaluate_tenant(tenant_document())
        self.assertEqual("ready", result["classification"])
        self.assertEqual([], result["blockers"])

    def test_stale_observed_generation_is_rejected(self) -> None:
        document = tenant_document()
        document["status"]["observedGeneration"] = 1
        result = evaluate_tenant(document)
        self.assertEqual("degraded", result["classification"])
        self.assertTrue(
            any("current generation" in blocker for blocker in result["blockers"])
        )

    def test_stale_ready_condition_is_rejected(self) -> None:
        document = tenant_document()
        document["status"]["conditions"][0]["observedGeneration"] = 1
        result = evaluate_tenant(document)
        self.assertEqual("degraded", result["classification"])
        self.assertTrue(
            any("Ready condition" in blocker for blocker in result["blockers"])
        )

    def test_deleting_tenant_is_not_ready(self) -> None:
        document = tenant_document()
        document["metadata"]["deletionTimestamp"] = "2026-09-22T00:00:00Z"
        result = evaluate_tenant(document)
        self.assertEqual("ready", result["phase"].lower())
        self.assertEqual("deleting", result["classification"])
        self.assertIn("Tenant is deleting", result["blockers"])

    def test_failure_phase_maps_to_failed(self) -> None:
        document = tenant_document()
        document["status"]["phase"] = "Failed"
        document["status"]["conditions"][0]["status"] = "False"
        result = evaluate_tenant(document)
        self.assertEqual("failed", result["classification"])
