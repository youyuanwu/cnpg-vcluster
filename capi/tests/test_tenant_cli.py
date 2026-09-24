from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.lib.tenant_spec import TenantSpecError
from scripts.tenant import execute


class TenantCLITests(unittest.TestCase):
    def test_default_dispatch_rejects_legacy_local_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cases = (
                (["create", "local", "local.json"], "local-tenant-apply"),
                (["status", "local", "tenant-c"], "local-tenant-status"),
                (
                    ["delete", "local", "tenant-c", "local/tenant-c"],
                    "local-tenant-delete",
                ),
            )
            for arguments, guidance in cases:
                with self.subTest(arguments=arguments):
                    with self.assertRaisesRegex(TenantSpecError, guidance):
                        execute(root, arguments)

    def test_default_dispatch_constructs_only_azure_adapter(self) -> None:
        adapter = object()
        with (
            patch("scripts.azure.AzureTenantAdapter", return_value=adapter),
            patch("scripts.tenant.status_tenant", return_value=0) as status,
        ):
            result = execute(Path("."), ["status", "azure", "tenant-a"])
        self.assertEqual(0, result)
        self.assertIs(status.call_args.args[3]["azure"], adapter)
