from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.lib.conditions import (
    condition_summary,
    condition_true,
    sanitized_condition_summary,
)
from scripts.test_endpoint_negative import _write_condition_evidence


class ConditionTests(unittest.TestCase):
    def test_current_true_condition(self) -> None:
        resource = {
            "metadata": {"generation": 2},
            "status": {
                "conditions": [
                    {
                        "type": "Available",
                        "status": "True",
                        "observedGeneration": 2,
                        "reason": "Available",
                    }
                ]
            },
        }
        self.assertTrue(condition_true(resource, "Available"))
        self.assertEqual(condition_summary(resource)[0]["reason"], "Available")

    def test_stale_condition_is_false(self) -> None:
        resource = {
            "metadata": {"generation": 2},
            "status": {
                "conditions": [
                    {
                        "type": "Available",
                        "status": "True",
                        "observedGeneration": 1,
                    }
                ]
            },
        }
        self.assertFalse(condition_true(resource, "Available"))

    def test_missing_observed_generation_is_false(self) -> None:
        resource = {
            "metadata": {"generation": 2},
            "status": {
                "conditions": [
                    {
                        "type": "Available",
                        "status": "True",
                        "reason": "Available",
                        "message": "ready",
                    }
                ]
            },
        }
        self.assertFalse(condition_true(resource, "Available"))

    def test_negative_condition_evidence_is_sanitized_and_private(self) -> None:
        resource = {
            "apiVersion": "example/v1",
            "kind": "Example",
            "metadata": {
                "name": "invalid",
                "namespace": "test",
                "uid": "uid",
                "generation": 2,
            },
            "status": {
                "conditions": [
                    {
                        "type": "Available",
                        "status": "False",
                        "observedGeneration": 2,
                        "reason": "password=condition-secret",
                        "message": "token=condition-token",
                    }
                ]
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_condition_evidence(root, resource)
            path = (
                root
                / ".runtime"
                / "evidence"
                / "negative-condition-example-invalid.json"
            )
            text = path.read_text(encoding="utf-8")
            payload = json.loads(text)
            self.assertEqual(path.stat().st_mode & 0o077, 0)
            self.assertNotIn("condition-secret", text)
            self.assertNotIn("condition-token", text)
            self.assertEqual(
                payload["conditions"][0]["observedGeneration"],
                2,
            )

    def test_sanitized_condition_summary_redacts_free_form_fields(self) -> None:
        resource = {
            "status": {
                "conditions": [
                    {
                        "type": "Ready",
                        "status": "False",
                        "reason": "password=reason-secret",
                        "message": "token=message-secret",
                        "observedGeneration": 1,
                    }
                ]
            }
        }
        text = json.dumps(sanitized_condition_summary(resource))
        self.assertNotIn("reason-secret", text)
        self.assertNotIn("message-secret", text)
