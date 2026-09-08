from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.lib.files import has_owner_only_permissions
from scripts.lib.ownership import IdentityRecord, OwnershipError


class OwnershipTests(unittest.TestCase):
    def test_round_trip_and_exact_match(self) -> None:
        expected = IdentityRecord("container", "management", "abc", {"owner": "true"})
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "identity.json"
            expected.save(path)
            observed = IdentityRecord.load(path)
            expected.require_exact(observed)
            self.assertTrue(has_owner_only_permissions(path))

    def test_rejects_identity_mismatch(self) -> None:
        expected = IdentityRecord("container", "management", "abc", {"owner": "true"})
        observed = IdentityRecord("container", "management", "def", {"owner": "true"})
        with self.assertRaises(OwnershipError):
            expected.require_exact(observed)

    def test_rejects_malformed_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "identity.json"
            path.write_text("{}\n", encoding="utf-8")
            with self.assertRaises(OwnershipError):
                IdentityRecord.load(path)
