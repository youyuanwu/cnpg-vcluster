from __future__ import annotations

import tempfile
import unittest
import json
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from scripts.lib.tenant_spec import TenantSpecError
from scripts.lib.tenant_status import TenantStatus
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

    def test_default_status_dispatch_uses_only_operator_path(self) -> None:
        observed = TenantStatus(
            profile="azure",
            tenant="tenant-a",
            classification="ready",
            foundation_healthy=True,
        )
        with (
            patch(
                "scripts.lib.azure.operator.status_tenant",
                return_value=observed,
            ) as status,
        ):
            result = execute(Path("."), ["status", "azure", "tenant-a"])
        self.assertEqual(0, result)
        status.assert_called_once_with(Path("."), "tenant-a")

    def test_create_and_delete_dispatch_only_tenant_cr_operations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spec_path = root / "tenant.json"
            spec_path.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "profile": "azure",
                        "name": "tenant-a",
                        "kubernetesVersion": "1.32.13",
                        "workers": 3,
                    }
                ),
                encoding="utf-8",
            )
            contexts = patch.multiple(
                "scripts.tenant",
                _tenant_e2e_lock=lambda _root: nullcontext(),
                azure_lock=lambda *_args, **_kwargs: nullcontext(True),
                tools_lock=lambda *_args, **_kwargs: nullcontext(True),
                supported_versions=lambda _root: {"azure": "1.32.13"},
            )
            with (
                contexts,
                patch("scripts.lib.azure.operator.create_tenant") as create,
                patch("scripts.lib.azure.operator.delete_tenant") as delete,
            ):
                self.assertEqual(
                    0,
                    execute(root, ["create", "azure", str(spec_path)]),
                )
                self.assertEqual(
                    0,
                    execute(
                        root,
                        ["delete", "azure", "tenant-a", "azure/tenant-a"],
                    ),
                )
            self.assertEqual("tenant-a", create.call_args.args[1].name)
            delete.assert_called_once_with(root, "tenant-a")
