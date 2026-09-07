from __future__ import annotations

import tempfile
import unittest
import hashlib
from pathlib import Path
from unittest.mock import patch

from scripts.cnpg import _render_cluster, _verify_marker, delete_cnpg, run_cnpg_gate


class CnpgTests(unittest.TestCase):
    def test_marker_verification_is_read_only(self) -> None:
        tenant = type(
            "Tenant", (), {"name": "spike", "cnpg_cluster": "postgres"}
        )()
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
            tenant = type(
                "Tenant", (), {"name": "spike", "cnpg_cluster": "postgres"}
            )()
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

    def test_lower_layer_failure_clears_stale_success_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            success = root / ".runtime" / "evidence" / "cnpg-success.json"
            success.parent.mkdir(parents=True)
            success.write_text('{"stale":true}\n', encoding="utf-8")
            with patch(
                "scripts.cnpg.run_storage_gate",
                side_effect=RuntimeError("injected storage failure"),
            ):
                with self.assertRaises(RuntimeError):
                    run_cnpg_gate(root, {})
            self.assertFalse(success.exists())
            self.assertTrue(
                (root / ".runtime" / "evidence" / "cnpg-failure.txt").is_file()
            )

    def test_cnpg_delete_rejects_pvc_inspection_failure(self) -> None:
        tenant = type(
            "Tenant", (), {"name": "spike", "cnpg_cluster": "postgres"}
        )()
        responses = [
            type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
            type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
            type(
                "Result",
                (),
                {
                    "returncode": 1,
                    "stdout": "",
                    "stderr": "connection refused",
                },
            )(),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "manifests" / "cnpg"
            target.mkdir(parents=True)
            repository = Path(__file__).resolve().parents[1]
            for name in ("cluster.yaml.tpl", "static-pvs.yaml.tpl"):
                target.joinpath(name).write_text(
                    repository.joinpath("manifests/cnpg", name).read_text(
                        encoding="utf-8"
                    ),
                    encoding="utf-8",
                )
            inputs = root / ".tools" / "inputs"
            inputs.mkdir(parents=True)
            source = repository / ".tools" / "inputs" / "cnpg.yaml"
            inputs.joinpath("cnpg.yaml").write_bytes(source.read_bytes())
            config = {
                "SPIKE_CNPG_CLUSTER": "postgres",
                "POSTGRES_IMAGE": "postgres@sha256:" + "a" * 64,
                "SPIKE_STORAGE_CLASS": "hostpath",
                "SPIKE_STORAGE_CONTAINER_PATH": "/shared",
                "CNPG_MANIFEST_SHA256": hashlib.sha256(
                    source.read_bytes()
                ).hexdigest(),
                "CNPG_CONTROLLER_IMAGE_TAGGED": "ghcr.io/cloudnative-pg/cloudnative-pg:1.30.0",
                "CNPG_CONTROLLER_IMAGE": "ghcr.io/cloudnative-pg/cloudnative-pg:1.30.0@sha256:"
                + "a" * 64,
                "DELETE_TIMEOUT": "1s",
                "DATABASE_NAMESPACE": "database",
            }
            with patch("scripts.cnpg._tenant_kubectl", side_effect=responses):
                with self.assertRaises(RuntimeError):
                    delete_cnpg(root, config, tenant)
