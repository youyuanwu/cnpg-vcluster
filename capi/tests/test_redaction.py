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
            "private",
        ):
            self.assertNotIn(secret, result)
