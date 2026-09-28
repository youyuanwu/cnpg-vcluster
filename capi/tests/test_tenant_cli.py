from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.lib.tenant_spec import TenantSpecError
from scripts.tenant import execute


class TenantCLITests(unittest.TestCase):
    def test_default_dispatch_rejects_non_azure_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cases = (
                ["create", "local", "local.json"],
                ["status", "local", "tenant-c"],
                ["delete", "local", "tenant-c", "local/tenant-c"],
            )
            for arguments in cases:
                with self.subTest(arguments=arguments):
                    with self.assertRaisesRegex(TenantSpecError, "unsupported tenant profile"):
                        execute(root, arguments)

    def test_default_dispatch_constructs_only_azure_adapter(self) -> None:
        adapter = object()
        with (
            patch("scripts.lib.azure.lifecycle.AzureTenantAdapter", return_value=adapter),
            patch("scripts.tenant.status_tenant", return_value=0) as status,
        ):
            result = execute(Path("."), ["status", "azure", "tenant-a"])
        self.assertEqual(0, result)
        self.assertIs(status.call_args.args[2], adapter)
