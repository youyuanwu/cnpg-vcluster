from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.create import create
from scripts.lib.files import IntegrityError


class CreateTests(unittest.TestCase):
    def test_input_failure_precedes_tenant_reconciliation(self) -> None:
        config = {
            "TENANT_COMPATIBILITY_REVISION": "capi-kamaji-two-tenant-v1",
            "CNPG_COMPATIBILITY_REVISION": "docker-volume-hostpath-v1",
        }
        with (
            patch("scripts.create.create_management"),
            patch(
                "scripts.create.validate_create_inputs",
                side_effect=IntegrityError("injected tenant-b input tamper"),
            ),
            patch("scripts.create.reconcile_tenant") as reconcile,
        ):
            with self.assertRaises(IntegrityError):
                create(Path("."), config)
        reconcile.assert_not_called()
