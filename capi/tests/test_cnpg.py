from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.cnpg import _render_cluster, _verify_marker


class CnpgTests(unittest.TestCase):
    def test_marker_verification_is_read_only(self) -> None:
        tenant = type("Tenant", (), {"name": "spike"})()
        with patch("scripts.cnpg._sql", return_value="capi-marker") as sql:
            _verify_marker(Path("."), {}, tenant)
        statement = sql.call_args.args[3]
        self.assertTrue(statement.startswith("SELECT "))
        self.assertNotIn("CREATE", statement)
        self.assertNotIn("INSERT", statement)

    def test_static_cnpg_pvs_have_no_node_affinity(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "manifests" / "cnpg"
            target.mkdir(parents=True)
            for name in ("cluster.yaml.tpl", "static-pvs.yaml.tpl"):
                target.joinpath(name).write_text(
                    repository.joinpath("manifests/cnpg", name).read_text(
                        encoding="utf-8"
                    ),
                    encoding="utf-8",
                )
            tenant = type("Tenant", (), {"name": "spike"})()
            config = {
                "SPIKE_CNPG_CLUSTER": "postgres",
                "POSTGRES_IMAGE": "postgres@sha256:" + "a" * 64,
                "SPIKE_STORAGE_CLASS": "hostpath",
                "SPIKE_STORAGE_CONTAINER_PATH": "/shared",
            }
            pvs, _ = _render_cluster(root, config, tenant)
            rendered = pvs.read_text(encoding="utf-8")
        self.assertNotIn("nodeAffinity", rendered)
        for ordinal in (1, 2, 3):
            self.assertIn(f"name: postgres-pv-{ordinal}", rendered)
            self.assertIn(f"name: postgres-{ordinal}", rendered)
