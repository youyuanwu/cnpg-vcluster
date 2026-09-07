from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from scripts.lib.process import CommandError
from scripts.lib.tenants import inspect_storage_volume
from scripts.storage import _delete_storage, _render_storage


class StorageTests(unittest.TestCase):
    def test_static_pv_has_no_node_affinity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "manifests" / "storage"
            source.mkdir(parents=True)
            source.joinpath("hostpath-smoke.yaml.tpl").write_text(
                Path(__file__)
                .resolve()
                .parents[1]
                .joinpath("manifests/storage/hostpath-smoke.yaml.tpl")
                .read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            tenant = type("Tenant", (), {"name": "spike"})()
            config = {
                "SPIKE_STORAGE_CLASS": "test-hostpath",
                "SPIKE_STORAGE_CONTAINER_PATH": "/shared",
                "VERIFY_IMAGE": "busybox@sha256:" + "a" * 64,
            }
            rendered = _render_storage(root, config, tenant).read_text(
                encoding="utf-8"
            )
        self.assertNotIn("nodeAffinity", rendered)
        self.assertIn("path: /shared/volumes/smoke", rendered)

    def test_volume_inspection_failure_is_not_absence(self) -> None:
        with patch(
            "scripts.lib.tenants.run",
            return_value=CompletedProcess([], 1, stdout="", stderr="daemon unavailable"),
        ):
            with self.assertRaises(RuntimeError):
                inspect_storage_volume("test")

    def test_storage_api_delete_failure_stops_cleanup(self) -> None:
        tenant = type("Tenant", (), {"name": "spike"})()
        with patch(
            "scripts.storage._tenant_kubectl",
            side_effect=CommandError(("kubectl",), 1, "injected"),
        ):
            with self.assertRaises(CommandError):
                _delete_storage(
                    Path("."),
                    {
                        "DELETE_TIMEOUT": "1s",
                        "SPIKE_STORAGE_CLASS": "test",
                    },
                    tenant,
                )
