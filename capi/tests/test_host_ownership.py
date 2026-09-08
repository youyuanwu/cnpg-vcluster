from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

from scripts.destroy import _validate_runtime_inventory
from scripts.lib.ownership import IdentityRecord, OwnershipError


class HostOwnershipTests(unittest.TestCase):
    def test_kind_identity_requires_exact_labels_and_identifier(self) -> None:
        expected = IdentityRecord(
            "container",
            "management-control-plane",
            "expected-id",
            {"io.x-k8s.kind.cluster": "management", "owner": ""},
        )
        mismatches = (
            IdentityRecord(
                "container",
                "management-control-plane",
                "other-id",
                expected.labels,
            ),
            IdentityRecord(
                "container",
                "management-control-plane",
                "expected-id",
                {"io.x-k8s.kind.cluster": "other", "owner": ""},
            ),
        )
        for observed in mismatches:
            with self.assertRaises(OwnershipError):
                expected.require_exact(observed)

    def test_runtime_inventory_rejects_unknown_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / ".runtime"
            runtime.mkdir(mode=0o700)
            unknown = runtime / "foreign"
            unknown.write_text("foreign\n", encoding="utf-8")
            unknown.chmod(0o600)
            with self.assertRaises(RuntimeError):
                _validate_runtime_inventory(root)
