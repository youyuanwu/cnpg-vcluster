from __future__ import annotations

import base64
import tempfile
import unittest
import hashlib
from pathlib import Path
from unittest.mock import patch

from scripts.cnpg import (
    SQLProbeCleanupError,
    _render_cluster,
    _sql,
    _verify_marker,
    verify_retained_marker,
    cnpg_artifacts_present,
    delete_cnpg,
    run_cnpg_gate,
)
from scripts.lib.files import ensure_private_dir


class CnpgTests(unittest.TestCase):
    def test_retained_marker_uses_existing_primary_without_creating_pod(
        self,
    ) -> None:
        tenant = type(
            "Tenant", (), {"name": "tenant-a", "cnpg_cluster": "postgres"}
        )()
        responses = [
            type(
                "Result",
                (),
                {
                    "stdout": '{"status":{"currentPrimary":"postgres-1"}}',
                    "returncode": 0,
                    "stderr": "",
                },
            )(),
            type(
                "Result",
                (),
                {
                    "stdout": (
                        '{"data":{"password":"'
                        + base64.b64encode(b"secret-value").decode()
                        + '"}}'
                    ),
                    "returncode": 0,
                    "stderr": "",
                },
            )(),
            type(
                "Result",
                (),
                {
                    "stdout": "capi-marker\n",
                    "returncode": 0,
                    "stderr": "",
                },
            )(),
        ]
        with patch(
            "scripts.cnpg._tenant_kubectl", side_effect=responses
        ) as kubectl:
            verify_retained_marker(
                Path("."),
                {"DATABASE_NAMESPACE": "database"},
                tenant,
            )
        commands = [
            " ".join(str(argument) for argument in call.args)
            for call in kubectl.call_args_list
        ]
        self.assertTrue(any("exec -i pod/postgres-1" in item for item in commands))
        self.assertTrue(any("-h postgres-rw -U app" in item for item in commands))
        self.assertFalse(any(" apply " in f" {item} " for item in commands))
        self.assertFalse(any(" run " in f" {item} " for item in commands))
        self.assertFalse(any("secret-value" in item for item in commands))
        self.assertEqual(
            kubectl.call_args_list[-1].kwargs["input_text"],
            "secret-value\n",
        )

    def test_sql_probe_is_removed_after_wait_failure(self) -> None:
        calls: list[tuple[str, ...]] = []
        tenant = type(
            "Tenant", (), {"name": "spike", "cnpg_cluster": "postgres"}
        )()

        def kubectl(*args, **_kwargs):
            calls.append(tuple(str(item) for item in args))
            if "wait" in args:
                raise RuntimeError("pod did not become ready")
            if "get" in args:
                return type(
                    "Result",
                    (),
                    {
                        "returncode": 1,
                        "stdout": "",
                        "stderr": "Error from server (NotFound): pod not found",
                    },
                )()
            return type(
                "Result",
                (),
                {"returncode": 0, "stdout": "", "stderr": ""},
            )()

        config = {
            "DATABASE_NAMESPACE": "database",
            "POSTGRES_IMAGE": "postgres@sha256:" + "a" * 64,
            "SQL_TIMEOUT": "10s",
        }
        with tempfile.TemporaryDirectory() as temporary:
            with (
                patch("scripts.cnpg._tenant_kubectl", side_effect=kubectl),
                patch("scripts.cnpg.time.time_ns", return_value=123),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "pod did not become ready"
                ):
                    _sql(Path(temporary), config, tenant, "SELECT 1;")
        self.assertTrue(
            any(
                "delete" in call and "pod/cnpg-sql-123" in call
                for call in calls
            )
        )

    def test_sql_probe_cleanup_inspection_failure_is_fatal(self) -> None:
        tenant = type(
            "Tenant", (), {"name": "spike", "cnpg_cluster": "postgres"}
        )()

        def kubectl(*args, **_kwargs):
            if "get" in args:
                return type(
                    "Result",
                    (),
                    {
                        "returncode": 1,
                        "stdout": "",
                        "stderr": "tenant API connection refused",
                    },
                )()
            stdout = "1\n" if "exec" in args else ""
            return type(
                "Result",
                (),
                {"returncode": 0, "stdout": stdout, "stderr": ""},
            )()

        config = {
            "DATABASE_NAMESPACE": "database",
            "POSTGRES_IMAGE": "postgres@sha256:" + "a" * 64,
            "SQL_TIMEOUT": "10s",
        }
        with tempfile.TemporaryDirectory() as temporary:
            with (
                patch("scripts.cnpg._tenant_kubectl", side_effect=kubectl),
                patch("scripts.cnpg.time.time_ns", return_value=123),
            ):
                with self.assertRaises(SQLProbeCleanupError):
                    _sql(Path(temporary), config, tenant, "SELECT 1;")

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
            ensure_private_dir(success.parent)
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
            tagged_image = "ghcr.io/cloudnative-pg/cloudnative-pg:1.30.0"
            operator_manifest = (
                f"image: {tagged_image}\n"
                f"operatorImage: {tagged_image}\n"
            ).encode()
            inputs.joinpath("cnpg.yaml").write_bytes(operator_manifest)
            config = {
                "SPIKE_CNPG_CLUSTER": "postgres",
                "POSTGRES_IMAGE": "postgres@sha256:" + "a" * 64,
                "SPIKE_STORAGE_CLASS": "hostpath",
                "SPIKE_STORAGE_CONTAINER_PATH": "/shared",
                "CNPG_MANIFEST_SHA256": hashlib.sha256(
                    operator_manifest
                ).hexdigest(),
                "CNPG_CONTROLLER_IMAGE_TAGGED": tagged_image,
                "CNPG_CONTROLLER_IMAGE": "ghcr.io/cloudnative-pg/cloudnative-pg:1.30.0@sha256:"
                + "a" * 64,
                "DELETE_TIMEOUT": "1s",
                "DATABASE_NAMESPACE": "database",
            }
            with patch("scripts.cnpg._tenant_kubectl", side_effect=responses):
                with self.assertRaises(RuntimeError):
                    delete_cnpg(root, config, tenant)

    def test_cnpg_presence_does_not_query_absent_custom_resource_type(self) -> None:
        tenant = type(
            "Tenant", (), {"name": "spike", "cnpg_cluster": "postgres"}
        )()
        not_found = type(
            "Result",
            (),
            {
                "returncode": 1,
                "stdout": "",
                "stderr": "Error from server (NotFound): item not found",
            },
        )()
        with patch(
            "scripts.cnpg._tenant_kubectl",
            return_value=not_found,
        ) as kubectl:
            self.assertFalse(
                cnpg_artifacts_present(
                    Path("."),
                    {
                        "DATABASE_NAMESPACE": "database",
                        "CNPG_NAMESPACE": "cnpg-system",
                    },
                    tenant,
                )
            )
        queried = " ".join(
            str(argument)
            for call in kubectl.call_args_list
            for argument in call.args
        )
        self.assertNotIn("cluster/postgres", queried)
