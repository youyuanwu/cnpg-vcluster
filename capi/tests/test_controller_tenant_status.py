from __future__ import annotations

import math
import unittest

from scripts.controller_tenant_status import (
    FUNCTIONAL_CATEGORIES,
    MAX_EVIDENCE_AGE_SECONDS,
    canonical_spec_hash,
    evaluate_tenant,
    observations_hash,
)


def tenant_document(*, now: float = 10_000.0) -> dict[str, object]:
    categories = {name: True for name in FUNCTIONAL_CATEGORIES}
    document = {
        "metadata": {"name": "tenant-a", "generation": 2},
        "spec": {
            "kubernetesVersion": "v1.36.4",
            "workers": 1,
            "databaseCount": 1,
            "podCIDR": "10.20.0.0/16",
            "serviceCIDR": "10.21.0.0/16",
        },
        "status": {
            "observedGeneration": 2,
            "phase": "Ready",
            "specHash": "",
            "foundationHash": "foundation",
            "observationsHash": "",
            "observedResources": [
                {
                    "apiVersion": "cluster.x-k8s.io/v1beta2",
                    "kind": "Cluster",
                    "namespace": "tenant-a",
                    "name": "tenant-a",
                    "uid": "cluster-uid",
                }
            ],
            "conditions": [
                {
                    "type": "Ready",
                    "status": "True",
                    "observedGeneration": 2,
                }
            ],
            "functionalEvidence": {
                "verifiedAt": now,
                "expiresAt": now + MAX_EVIDENCE_AGE_SECONDS,
                "specHash": "",
                "foundationHash": "foundation",
                "observationsHash": "",
                "categories": categories,
            },
        },
    }
    spec_hash = canonical_spec_hash(document["spec"])
    observed_hash = observations_hash(document["status"]["observedResources"])
    document["status"]["specHash"] = spec_hash
    document["status"]["observationsHash"] = observed_hash
    document["status"]["functionalEvidence"]["specHash"] = spec_hash
    document["status"]["functionalEvidence"]["observationsHash"] = observed_hash
    return document


class ControllerTenantStatusTests(unittest.TestCase):
    def test_canonical_hash_matches_go_contract(self) -> None:
        self.assertEqual(
            "e9afd0733e391c39ea140af4cfbca48afe00d997b350474e41ff118c38e50a15",
            canonical_spec_hash(tenant_document()["spec"]),
        )

    def test_ready_evidence_is_accepted(self) -> None:
        result = evaluate_tenant(
            tenant_document(),
            foundation_hash="foundation",
            now=10_001.0,
        )
        self.assertEqual("ready", result["classification"])
        self.assertEqual([], result["blockers"])

    def test_stale_evidence_is_rejected_without_controller_mutation(self) -> None:
        result = evaluate_tenant(
            tenant_document(),
            foundation_hash="foundation",
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
                result = evaluate_tenant(
                    document,
                    foundation_hash="foundation",
                    now=10_000.0,
                )
                self.assertNotEqual("ready", result["classification"])

    def test_generation_hashes_and_categories_are_independently_checked(self) -> None:
        document = tenant_document()
        document["status"]["observedGeneration"] = 1
        document["status"]["functionalEvidence"]["specHash"] = "other"
        document["status"]["functionalEvidence"]["categories"].pop("database")
        result = evaluate_tenant(
            document,
            foundation_hash="foundation",
            now=10_001.0,
        )
        self.assertGreaterEqual(len(result["blockers"]), 3)

    def test_live_spec_resource_foundation_and_deletion_state_are_checked(self) -> None:
        document = tenant_document()
        document["spec"]["workers"] = 2
        document["status"]["observedResources"][0]["uid"] = "replaced"
        document["metadata"]["deletionTimestamp"] = "2026-09-20T00:00:00Z"
        result = evaluate_tenant(
            document,
            foundation_hash="changed-foundation",
            now=10_001.0,
        )
        blockers = " ".join(result["blockers"])
        self.assertIn("specHash", blockers)
        self.assertIn("foundationHash", blockers)
        self.assertIn("observationsHash", blockers)
        self.assertIn("deleting", blockers)
