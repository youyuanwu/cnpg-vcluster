from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from scripts.lib.files import (
    IntegrityError,
    has_owner_only_permissions,
    verify_sha256,
    write_private_file,
)


class FileTests(unittest.TestCase):
    def test_private_atomic_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "nested" / "secret"
            write_private_file(path, "value\n")
            self.assertEqual(path.read_text(encoding="utf-8"), "value\n")
            self.assertTrue(has_owner_only_permissions(path))
            self.assertEqual(path.parent.stat().st_mode & 0o077, 0)

    def test_checksum_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "input"
            path.write_bytes(b"payload")
            expected = hashlib.sha256(b"payload").hexdigest()
            verify_sha256(path, expected)
            with self.assertRaises(IntegrityError):
                verify_sha256(path, "0" * 64)

    def test_checksum_rejects_missing_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(IntegrityError):
                verify_sha256(Path(temporary) / "missing", "0" * 64)
