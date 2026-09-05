from __future__ import annotations

import unittest

from scripts.lib.conditions import condition_summary, condition_true


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
