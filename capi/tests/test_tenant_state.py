from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from scripts.cnpg import _sql, run_cnpg_gate
from scripts.lib.controller_scenarios import delete_controller_tenant
from scripts.lib.files import IntegrityError, write_private_file
from scripts.lib.tenants import (
    clear_all_tenant_kubeconfigs,
    clear_tenant_kubeconfig,
    ensure_tenant_kubeconfig,
    export_tenant_kubeconfig,
)
from scripts.machines import _foreign_node_rejected


class TenantStateTests(unittest.TestCase):
    def test_export_rejects_secret_type_without_creating_cache(self) -> None:
        tenant = type(
            "Tenant",
            (),
            {"name": "tenant-a", "namespace": "tenant-a-system"},
        )()
        client = type(
            "Client",
            (),
            {
                "kubectl": staticmethod(
                    lambda *_args, **_kwargs: CompletedProcess(
                        [],
                        0,
                        stdout=json.dumps(
                            {
                                "type": "Opaque",
                                "data": {"value": "a3ViZWNvbmZpZw=="},
                            }
                        ),
                        stderr="",
                    )
                )
            },
        )()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(RuntimeError, "type is unexpected"):
                export_tenant_kubeconfig(root, {}, client, tenant)
            self.assertFalse((root / ".runtime").exists())

    def test_failed_candidate_preserves_existing_cache(self) -> None:
        tenant = type(
            "Tenant",
            (),
            {"name": "tenant-a", "namespace": "tenant-a-system"},
        )()
        client = type(
            "Client",
            (),
            {
                "kubectl": staticmethod(
                    lambda *_args, **_kwargs: CompletedProcess(
                        [],
                        0,
                        stdout=json.dumps(
                            {
                                "type": "cluster.x-k8s.io/secret",
                                "data": {"value": "bmV3LWNvbmZpZw=="},
                            }
                        ),
                        stderr="",
                    )
                )
            },
        )()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = (
                root / ".runtime" / "tenants" / "tenant-a" / "kubeconfig"
            )
            write_private_file(cache, "old-config")
            with (
                patch(
                    "scripts.lib.tenants.validate_tenant_kubeconfig_file",
                    side_effect=RuntimeError("candidate invalid"),
                ),
                self.assertRaisesRegex(RuntimeError, "candidate invalid"),
            ):
                export_tenant_kubeconfig(root, {}, client, tenant)
            self.assertEqual(b"old-config", cache.read_bytes())
            self.assertEqual(
                ["kubeconfig"],
                sorted(path.name for path in cache.parent.iterdir()),
            )

    def test_valid_cache_recovery_removes_stale_candidate(self) -> None:
        tenant = type("Tenant", (), {"name": "tenant-a"})()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = (
                root / ".runtime" / "tenants" / "tenant-a" / "kubeconfig"
            )
            candidate = cache.with_name(
                ".kubeconfig-candidate-123-" + "a" * 32
            )
            write_private_file(cache, "valid-config")
            write_private_file(candidate, "stale-config")
            with (
                patch(
                    "scripts.lib.tenants.validate_tenant_kubeconfig_file",
                    return_value=cache,
                ) as validate,
                patch("scripts.lib.tenants.export_tenant_kubeconfig") as export,
            ):
                self.assertEqual(
                    cache,
                    ensure_tenant_kubeconfig(root, {}, object(), tenant),
                )
            self.assertFalse(candidate.exists())
            self.assertEqual(b"valid-config", cache.read_bytes())
            self.assertEqual(2, validate.call_count)
            export.assert_not_called()

    def test_export_failure_removes_stale_candidate_without_cache(self) -> None:
        tenant = type(
            "Tenant",
            (),
            {"name": "tenant-a", "namespace": "tenant-a-system"},
        )()
        client = type(
            "Client",
            (),
            {
                "kubectl": staticmethod(
                    lambda *_args, **_kwargs: CompletedProcess(
                        [],
                        0,
                        stdout=json.dumps(
                            {
                                "type": "Opaque",
                                "data": {"value": "a3ViZWNvbmZpZw=="},
                            }
                        ),
                        stderr="",
                    )
                )
            },
        )()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tenant_directory = root / ".runtime/tenants/tenant-a"
            write_private_file(
                tenant_directory
                / (".kubeconfig-candidate-123-" + "a" * 32),
                "stale-config",
            )
            with self.assertRaisesRegex(RuntimeError, "type is unexpected"):
                export_tenant_kubeconfig(root, {}, client, tenant)
            self.assertFalse(tenant_directory.exists())

    def test_clear_one_removes_only_exact_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selected = (
                root / ".runtime" / "tenants" / "tenant-a" / "kubeconfig"
            )
            retained = (
                root / ".runtime" / "tenants" / "tenant-b" / "kubeconfig"
            )
            write_private_file(selected, "selected\n")
            write_private_file(retained, "retained\n")

            self.assertTrue(clear_tenant_kubeconfig(root, "tenant-a"))
            self.assertFalse(selected.exists())
            self.assertEqual(retained.read_text(encoding="utf-8"), "retained\n")
            self.assertFalse(clear_tenant_kubeconfig(root, "tenant-a"))

    def test_clear_all_removes_only_tenant_kubeconfig_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("tenant-a", "tenant-b"):
                write_private_file(
                    root / ".runtime" / "tenants" / name / "kubeconfig",
                    name,
                )
            write_private_file(
                root
                / ".runtime"
                / "tenants"
                / "tenant-a"
                / (".kubeconfig-candidate-123-" + "a" * 32),
                "candidate",
            )
            unrelated = root / ".runtime" / "tenants" / "not-a-tenant!"
            unrelated.mkdir(mode=0o700)
            unrelated.joinpath("keep").write_text("keep\n", encoding="utf-8")

            self.assertEqual(
                ["tenant-a", "tenant-b"],
                clear_all_tenant_kubeconfigs(root),
            )
            self.assertEqual(
                "keep\n",
                unrelated.joinpath("keep").read_text(encoding="utf-8"),
            )

    def test_cache_clear_rejects_traversal_and_symlinked_tenant(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root / "outside"
            outside.mkdir(mode=0o700)
            outside_cache = outside / "kubeconfig"
            outside_cache.write_text("outside\n", encoding="utf-8")
            outside_cache.chmod(0o600)
            tenants = root / ".runtime" / "tenants"
            tenants.mkdir(parents=True, mode=0o700)
            tenants.parent.chmod(0o700)
            (tenants / "tenant-a").symlink_to(
                outside,
                target_is_directory=True,
            )

            with self.assertRaises(RuntimeError):
                clear_tenant_kubeconfig(root, "../outside")
            with self.assertRaises(IntegrityError):
                clear_tenant_kubeconfig(root, "tenant-a")
            self.assertEqual(
                "outside\n",
                outside_cache.read_text(encoding="utf-8"),
            )

    def test_public_delete_clears_cache_after_confirmed_absence(self) -> None:
        events = []
        with (
            patch(
                "scripts.lib.controller_scenarios.delete_tenant",
                side_effect=lambda *_args, **_kwargs: events.append("delete"),
            ),
            patch(
                "scripts.lib.controller_scenarios.wait_tenant_absent",
                side_effect=lambda *_args: events.append("absent"),
            ),
            patch(
                "scripts.lib.controller_scenarios.clear_tenant_kubeconfig",
                side_effect=lambda *_args: events.append("clear"),
            ),
        ):
            delete_controller_tenant(Path("."), {}, "tenant-a")
        self.assertEqual(["delete", "absent", "clear"], events)

    def test_cnpg_sql_manifest_uses_stdin(self) -> None:
        calls = []

        def kubectl(*arguments, **kwargs):
            calls.append((arguments, kwargs))
            if "exec" in arguments:
                return CompletedProcess([], 0, stdout="1\n", stderr="")
            if "get" in arguments:
                return CompletedProcess(
                    [],
                    1,
                    stdout="",
                    stderr='Error from server (NotFound): pods "probe" not found',
                )
            return CompletedProcess([], 0, stdout="", stderr="")

        tenant = type("Tenant", (), {"name": "tenant-a", "cnpg_cluster": "pg"})()
        with patch("scripts.cnpg._tenant_kubectl", side_effect=kubectl):
            self.assertEqual(
                "1",
                _sql(
                    Path("."),
                    {
                        "DATABASE_NAMESPACE": "database",
                        "POSTGRES_IMAGE": "postgres:test",
                        "SQL_TIMEOUT": "1s",
                    },
                    tenant,
                    "SELECT 1",
                ),
            )
        apply_arguments, apply_options = calls[0]
        self.assertEqual(("apply", "-f", "-"), apply_arguments[3:])
        manifest = json.loads(apply_options["input_text"])
        self.assertEqual("Pod", manifest["kind"])
        self.assertEqual("postgres:test", manifest["spec"]["containers"][0]["image"])

    def test_foreign_node_manifest_uses_stdin(self) -> None:
        calls = []

        def kubectl(*arguments, **kwargs):
            calls.append((arguments, kwargs))
            return CompletedProcess([], 0, stdout="", stderr="")

        with (
            patch("scripts.machines._tenant_kubectl", side_effect=kubectl),
            patch(
                "scripts.machines.worker_snapshot",
                side_effect=RuntimeError("foreign node rejected"),
            ),
        ):
            _foreign_node_rejected(Path("."), {}, object(), object())
        self.assertEqual(("apply", "-f", "-"), calls[0][0][3:])
        self.assertEqual(
            "foreign-capi-node",
            json.loads(calls[0][1]["input_text"])["metadata"]["name"],
        )

    def test_cnpg_gate_prints_evidence_without_persisting_it(self) -> None:
        tenant = type("Tenant", (), {"name": "tenant-a"})()
        evidence = {"cluster": "pg", "nodes": {"worker": "uid"}}
        output = io.StringIO()
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch(
                "scripts.cnpg.run_storage_gate",
                return_value=(object(), tenant, {}),
            ),
            patch("scripts.cnpg._cnpg_ready", return_value=True),
            patch(
                "scripts.cnpg._storage_identity",
                side_effect=[{"pv": "uid"}, {"pv": "uid"}],
            ),
            patch("scripts.cnpg._write_marker"),
            patch("scripts.cnpg._verify_filesystem"),
            patch("scripts.cnpg._replace_machine"),
            patch("scripts.cnpg.wait_tenant_ready"),
            patch("scripts.cnpg._verify_marker"),
            patch("scripts.cnpg._replica_restart"),
            patch("scripts.cnpg._primary_failover"),
            patch("scripts.cnpg._evidence_payload", return_value=evidence),
            patch("scripts.cnpg._cleanup_storage_and_tenant"),
            redirect_stdout(output),
        ):
            root = Path(temporary)
            run_cnpg_gate(root, {})
            self.assertFalse((root / ".runtime").exists())
        self.assertEqual(evidence, json.loads(output.getvalue()))


if __name__ == "__main__":
    unittest.main()
