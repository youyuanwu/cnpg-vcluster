from __future__ import annotations

import unittest

from scripts.lib.redaction import redact


class RedactionTests(unittest.TestCase):
    def test_redacts_supported_secret_shapes(self) -> None:
        candidates = {
            "password": "hunter2",
            "bootstrap": "abcdef.0123456789abcdef",
            "header": "header-secret",
            "separated": "separated-secret",
            "assigned": "assigned-secret",
            "subscription": "00000000-0000-0000-0000-000000000000",
            "mapping": "python-mapping-secret",
            "key": "private-key-candidate",
        }
        source = "\n".join((
            f"password={candidates['password']}",
            f"token={candidates['bootstrap']}",
            f"Authorization: Bearer {candidates['header']}",
            f"--token {candidates['separated']}",
            f"--password={candidates['assigned']}",
            f"Subscription {candidates['subscription']} is not registered",
            f"provider error: {{'Authorization': '{candidates['mapping']}'}}",
            f"-----BEGIN PRIVATE KEY-----\n{candidates['key']}\n-----END PRIVATE KEY-----",
        ))
        result = redact(source)
        for secret in candidates.values():
            self.assertIn(secret, source)
            self.assertNotIn(secret, result)
        self.assertIn("REDACTED", result)
