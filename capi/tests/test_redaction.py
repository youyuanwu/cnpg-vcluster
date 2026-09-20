from __future__ import annotations

import unittest

from scripts.lib.redaction import redact


class RedactionTests(unittest.TestCase):
    def test_redacts_supported_secret_shapes(self) -> None:
        source = "\n".join(
            (
                "password=hunter2",
                "token=abcdef.0123456789abcdef",
                "Authorization: Bearer secret",
                "--token separated-secret",
                "--password=assigned-secret",
                "Subscription 00000000-0000-0000-0000-000000000000 is not registered",
                "provider error: {'Authorization': 'python-mapping-secret'}",
                "-----BEGIN PRIVATE KEY-----\nprivate\n-----END PRIVATE KEY-----",
            )
        )
        result = redact(source)
        for secret in (
            "hunter2",
            "abcdef.0123456789abcdef",
            "Bearer secret",
            "separated-secret",
            "assigned-secret",
            "00000000-0000-0000-0000-000000000000",
            "python-mapping-secret",
            "private",
        ):
            self.assertNotIn(secret, result)
