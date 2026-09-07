from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.storage import _render_storage


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
