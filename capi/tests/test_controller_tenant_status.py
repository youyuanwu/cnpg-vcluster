from __future__ import annotations

import math
import unittest

from scripts.controller_tenant_status import (
    FUNCTIONAL_CATEGORIES,
    MAX_EVIDENCE_AGE_SECONDS,
    evaluate_tenant,
)


def tenant_document(*, now: float = 10_000.0) -> dict[str, object]:
    categories = {name: True for name in FUNCTIONAL_CATEGORIES}
    return {
        "metadata": {"name": "tenant-a", "generation": 2},
        "status": {
            "observedGeneration": 2,
            "phase": "Ready",
            "specHash": "spec",
            "foundationHash": "foundation",
            "observationsHash": "observations",
            "conditions": [{"type": "Ready", "status": "True"}],
            "functionalEvidence": {
                "verifiedAt": now,
                "expiresAt": now + MAX_EVIDENCE_AGE_SECONDS,
                "specHash": "spec",
                "foundationHash": "foundation",
                "observationsHash": "observations",
                "categories": categories,
            },
        },
    }


class ControllerTenantStatusTests(unittest.TestCase):
    def test_ready_evidence_is_accepted(self) -> None:
        result = evaluate_tenant(tenant_document(), now=10_001.0)
        self.assertEqual("ready", result["classification"])
        self.assertEqual([], result["blockers"])

    def test_stale_evidence_is_rejected_without_controller_mutation(self) -> None:
        result = evaluate_tenant(
            tenant_document(),
            now=10_000.0 + MAX_EVIDENCE_AGE_SECONDS + 1,
        )
        self.assertEqual("degraded", result["classification"])
        self.assertTrue(any("stale" in blocker for blocker in result["blockers"]))

    def test_future_non_finite_and_inconsistent_expiry_fail_closed(self) -> None:
        for verified_at, expires_at in (
            (10_001.0, 10_001.0 + MAX_EVIDENCE_AGE_SECONDS),
            (math.nan, 10_000.0 + MAX_EVIDENCE_AGE_SECONDS),
            (10_000.0, 10_001.0),
        ):
            with self.subTest(verified_at=verified_at, expires_at=expires_at):
                document = tenant_document()
                evidence = document["status"]["functionalEvidence"]
                evidence["verifiedAt"] = verified_at
                evidence["expiresAt"] = expires_at
                result = evaluate_tenant(document, now=10_000.0)
                self.assertNotEqual("ready", result["classification"])

    def test_generation_hashes_and_categories_are_independently_checked(self) -> None:
        document = tenant_document()
        document["status"]["observedGeneration"] = 1
        document["status"]["functionalEvidence"]["specHash"] = "other"
        document["status"]["functionalEvidence"]["categories"].pop("database")
        result = evaluate_tenant(document, now=10_001.0)
        self.assertGreaterEqual(len(result["blockers"]), 3)
