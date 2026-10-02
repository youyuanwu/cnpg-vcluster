from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import Mock, patch

from scripts.lib.controller_scenarios import LEASE, MANAGEMENT_CATALOG
from scripts.lib.timing import PHASES, PhaseTimings
from scripts.test_e2e import run_e2e, verify_no_local_runtime_residue, verify_tenant_deletion


class TimingTests(unittest.TestCase):
    def test_e2e_runtime_check_preserves_only_azure_foundation_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            azure = root / ".runtime/azure/resources.json"
            manager = (
                root
                / ".runtime/rendered/azure-controller/manager.yaml"
            )
            for path in (azure, manager):
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                path.write_text("{}\n", encoding="utf-8")
            verify_no_local_runtime_residue(root)
            tenant_state = (
                root
                / ".runtime/azure-gate/evidence/lifecycle-operation.json"
            )
            tenant_state.parent.mkdir(parents=True, exist_ok=True)
            tenant_state.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "azure-gate"):
                verify_no_local_runtime_residue(root)

    def test_records_passed_failed_and_skipped_without_error_text(self) -> None:
        values = iter((1.0, 2.25, 3.0, 3.5))
        timings = PhaseTimings()
        with patch(
            "scripts.lib.timing.time.monotonic",
            side_effect=lambda: next(values),
        ):
            with timings.phase("tools_cache"):
                pass
            with self.assertRaises(RuntimeError):
                with timings.phase("initial_cleanup"):
                    raise RuntimeError("timing-candidate-secret")
        records = timings.records()
        self.assertEqual([item["phase"] for item in records], list(PHASES))
        self.assertEqual(records[0]["status"], "passed")
        self.assertEqual(records[1]["status"], "failed")
        self.assertTrue(
            all(item["status"] == "skipped" for item in records[2:])
        )
        self.assertNotIn("timing-candidate-secret", json.dumps(records))

    def test_emit_produces_eight_structured_records(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            PhaseTimings().emit()
        payloads = [
            json.loads(line.removeprefix("CAPI_TIMING "))
            for line in output.getvalue().splitlines()
        ]
        self.assertEqual([item["phase"] for item in payloads], list(PHASES))

    @staticmethod
    def _root(temporary: str) -> Path:
        root = Path(temporary)
        spec = root / "config" / "tenants" / "examples" / "local.yaml"
        spec.parent.mkdir(parents=True)
        spec.write_text(
            "apiVersion: tenancy.cnpg-vcluster.io/v1alpha3\n"
            "kind: Tenant\nmetadata:\n  name: tenant-example\n",
            encoding="utf-8",
        )
        return root

    def test_e2e_tenant_create_failure_still_runs_teardown(self) -> None:
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            root = self._root(temporary)

            with (
                patch("scripts.test_e2e.ROOT", root),
                patch("scripts.test_e2e.load_configuration", return_value={}),
                patch("scripts.test_e2e.read_inotify", return_value=1),
                patch("scripts.test_e2e.run_just"),
                patch("scripts.test_e2e.verify_all_inputs"),
                patch("scripts.test_e2e.verify_no_lab_residue"),
                patch("scripts.test_e2e.wait_tenant_ready"),
                patch("scripts.test_e2e.ManagementClient", return_value=object()),
                patch("scripts.test_e2e.verify_admin_api"),
                patch(
                    "scripts.test_e2e.create_tenant_via_admin",
                    side_effect=RuntimeError("injected tenant setup failure"),
                ),
                redirect_stdout(output),
            ):
                with self.assertRaisesRegex(RuntimeError, "tenant setup failure"):
                    run_e2e()
        teardown = next(
            json.loads(line.removeprefix("CAPI_TIMING "))
            for line in output.getvalue().splitlines()
            if '"phase":"management_teardown_host_restoration"' in line
        )
        self.assertEqual(teardown["status"], "passed")

    def test_primary_and_teardown_failures_preserve_both_results(self) -> None:
        calls = 0
        cleanup_secret = "cleanup-note-candidate"
        with tempfile.TemporaryDirectory() as temporary:
            root = self._root(temporary)

            def run_just_failure(*args, **_kwargs):
                nonlocal calls
                if args[-1] == "destroy":
                    calls += 1
                    if calls == 2:
                        raise RuntimeError(f"injected teardown failure password={cleanup_secret}")

            with (
                patch("scripts.test_e2e.ROOT", root),
                patch("scripts.test_e2e.load_configuration", return_value={}),
                patch("scripts.test_e2e.read_inotify", return_value=1),
                patch(
                    "scripts.test_e2e.run_just",
                    side_effect=run_just_failure,
                ),
                patch("scripts.test_e2e.verify_all_inputs"),
                patch("scripts.test_e2e.wait_tenant_ready"),
                patch("scripts.test_e2e.ManagementClient", return_value=object()),
                patch("scripts.test_e2e.verify_admin_api"),
                patch(
                    "scripts.test_e2e.create_tenant_via_admin",
                    side_effect=RuntimeError("injected primary failure"),
                ),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "primary failure",
                ) as raised:
                    run_e2e()
        self.assertTrue(
            any(
                "teardown failure" in note
                for note in raised.exception.__notes__
            )
        )
        notes = " ".join(raised.exception.__notes__)
        self.assertNotIn(cleanup_secret, notes)
        self.assertIn("REDACTED", notes)

    def test_taxonomy_matches_the_controller_lifecycle(self) -> None:
        self.assertEqual(
            (
                "tools_cache",
                "initial_cleanup",
                "host_preparation",
                "management_bootstrap",
                "tenant_convergence",
                "tenant_sql_probe",
                "tenant_deletion_finalization",
                "management_teardown_host_restoration",
            ),
            PHASES,
        )

    def test_unknown_and_duplicate_phases_are_rejected(self) -> None:
        timings = PhaseTimings()
        with self.assertRaises(ValueError):
            with timings.phase("tenant_workers_network"):
                pass
        with timings.phase("tenant_convergence"):
            pass
        with self.assertRaises(RuntimeError):
            with timings.phase("tenant_convergence"):
                pass

    def test_catalog_query_and_explicit_finalization_precede_management_teardown(self) -> None:
        self._exercise_e2e()

    def test_catalog_query_failure_fails_gate_and_still_tears_down(self) -> None:
        self._exercise_e2e(sql_result="unexpected")

    def test_finalization_failure_fails_gate_and_still_tears_down(self) -> None:
        self._exercise_e2e(deletion_failure=True)

    def test_live_namespace_and_allocation_leaks_fail_before_management_teardown(self) -> None:
        for residue in ("namespace", "allocation", "inspection-error"):
            with self.subTest(residue=residue):
                self._exercise_e2e(residue=residue)

    def _exercise_e2e(
        self, *, sql_result: str = "1", deletion_failure: bool = False,
        residue: str = "",
    ) -> None:
        output = io.StringIO()
        calls: list[str] = []
        document = {"metadata": {"name": "tenant-example", "uid": "tenant-uid"}}
        tenant = Mock(name="tenant-example")
        delete = Mock()
        identity = {
            "name": "tenant-example",
            "uid": "tenant-uid",
            "allocationLease": {"name": "tenant-slot-test", "uid": "lease-uid"},
            "managementResources": [
                ("v1", "Namespace", "", "tenant-example", "namespace-uid"),
                (
                    "cluster.x-k8s.io/v1beta2",
                    "Cluster",
                    "tenant-example",
                    "tenant-example",
                    "cluster-uid",
                ),
            ],
            "workerContainers": ["tenant-worker " + "a" * 64],
            "providerContainers": ["tenant-worker " + "a" * 64],
            "dockerVolume": {"name": "lab-tenant-example-storage"},
        }

        def inspect(*arguments, **_kwargs):
            raw = next(
                (
                    argument.removeprefix("--raw=")
                    for argument in arguments
                    if argument.startswith("--raw=")
                ),
                None,
            )
            if raw == LEASE.inventory_path:
                items = (
                    [{"metadata": {
                        "name": "tenant-slot-test",
                        "uid": "lease-uid",
                        "annotations": {},
                    }}]
                    if residue == "allocation"
                    else []
                )
                return CompletedProcess(
                    [],
                    0,
                    stdout=json.dumps({
                        "apiVersion": LEASE.api_version,
                        "kind": f"{LEASE.kind}List",
                        "items": items,
                    }),
                    stderr="",
                )
            namespace = next(
                resource for resource in MANAGEMENT_CATALOG
                if resource.kind == "Namespace"
            )
            if raw == namespace.object_path(None, "tenant-example"):
                if residue == "namespace":
                    return CompletedProcess(
                        [],
                        0,
                        stdout=json.dumps({
                            "apiVersion": namespace.api_version,
                            "kind": namespace.kind,
                            "metadata": {
                                "name": "tenant-example",
                                "uid": "namespace-uid",
                            },
                        }),
                        stderr="",
                    )
                if residue == "inspection-error":
                    return CompletedProcess([], 1, stdout="", stderr="inspection unavailable")
            return CompletedProcess(
                [],
                1 if raw is not None else 0,
                stdout="",
                stderr="NotFound" if raw is not None else "",
            )

        client = Mock()
        client.kubectl.side_effect = inspect

        def capture(*args):
            self.assertEqual(({"CNPG_TIMEOUT": "60s"}, client, document), args)
            calls.append("identity")
            return identity

        def verify(*args):
            self.assertEqual((client, identity), args)
            calls.append("verify-absence")
            verify_tenant_deletion(*args)

        def run_just(_root, _config, command, *_args):
            calls.append(command)

        def query(*args, **_kwargs):
            calls.append("query")
            if sql_result != "1":
                raise RuntimeError("injected catalog query failure")

        def entry(name):
            return {
                "name": name, "logicalUid": f"{name}-uid", "instances": 3,
                "phase": "ready", "readyInstances": 3,
            }
        complete = {"catalogUid": "catalog-uid", "databases": [
            entry(name) for name in ("alpha", "beta", "gamma")
        ]}
        recreated = {"catalogUid": "catalog-uid", "databases": [
            entry("alpha"), {**entry("beta"), "logicalUid": "new-beta-uid"},
            entry("gamma"),
        ]}
        catalog = Mock()
        catalog.read.return_value = {"catalogUid": "catalog-uid", "databases": []}
        catalog.add.side_effect = ["alpha-uid", "beta-uid", "gamma-uid", "new-beta-uid"]
        catalog.wait.side_effect = [
            complete, {"databases": [entry("alpha"), entry("gamma")]},
            recreated,
        ]
        catalog.query.side_effect = query
        catalog.assert_fresh = Mock()
        catalog.path = "api/v1/tenants/tenant-example/databases"
        client.request_json.return_value = CompletedProcess(
            [], 1,
            json.dumps({
                "schemaVersion": 5,
                "error": {"code": "stale-identity", "retryable": False},
            }), "HTTP 409: Conflict",
        )
        client.json.return_value = {"metadata": {"uid": "catalog-uid"}}

        def delete_tenant(*args):
            delete(*args)
            calls.append("finalization")
            if deletion_failure:
                raise RuntimeError("injected finalization failure")

        with tempfile.TemporaryDirectory() as temporary:
            root = self._root(temporary)
            with (
                patch("scripts.test_e2e.ROOT", root),
                patch("scripts.test_e2e.load_configuration", return_value={"CNPG_TIMEOUT": "60s"}),
                patch("scripts.test_e2e.read_inotify", return_value=1),
                patch("scripts.test_e2e.run_just", side_effect=run_just),
                patch("scripts.test_e2e.wait_tenant_ready", return_value=document),
                patch("scripts.test_e2e.tenant_from_document", return_value=tenant),
                patch("scripts.test_e2e.ManagementClient", return_value=client),
                patch.multiple(
                    "scripts.test_e2e",
                    create_tenant_via_admin=Mock(
                        side_effect=lambda *_args, **_kwargs: (
                            calls.append("admin-create"),
                            {"identity": {"uid": "tenant-uid"}},
                        )[1]
                    ),
                    delete_tenant_via_admin=Mock(side_effect=delete_tenant),
                    wait_tenant_absent=Mock(),
                    verify_all_inputs=Mock(),
                    verify_no_lab_residue=Mock(),
                    _failover=Mock(),
                    _restart_database_controller=Mock(),
                    _verify_restarts_and_markers=Mock(
                        side_effect=lambda *_args: {
                            item["name"]: item for item in complete["databases"]
                        }
                    ),
                    _verify_entry_gone=Mock(),
                    _catalog_record=Mock(return_value={}),
                    wait_for=Mock(return_value=document),
                ),
                patch("scripts.test_e2e.export_tenant_kubeconfig") as export,
                patch("scripts.test_e2e.catalog_client", return_value=catalog),
                patch("scripts.test_e2e.ready_entries", side_effect=lambda result, *_:
                      {item["name"]: item for item in result["databases"]}),
                patch("scripts.test_e2e.capture_tenant_deletion_identity", side_effect=capture),
                patch("scripts.test_e2e.verify_tenant_deletion", side_effect=verify),
                patch("scripts.test_e2e.run", return_value=CompletedProcess([], 0, stdout="", stderr="")),
                patch(
                    "scripts.test_e2e.verify_admin_api",
                    side_effect=lambda *_args, **kwargs: calls.append(
                        "admin-api:"
                        + ",".join(kwargs["expected_tenant_names"])
                    ),
                ),
                redirect_stdout(output),
            ):
                if deletion_failure or residue or sql_result != "1":
                    with self.assertRaises(RuntimeError):
                        run_e2e()
                else:
                    self.assertEqual(0, run_e2e())
                export.assert_called_once()
        self.assertEqual("destroy", calls[-1])
        timings = {
            record["phase"]: record["status"]
            for line in output.getvalue().splitlines()
            if line.startswith("CAPI_TIMING ")
            for record in [json.loads(line.removeprefix("CAPI_TIMING "))]
        }
        self.assertEqual("passed", timings["management_teardown_host_restoration"])
        if sql_result == "1":
            delete.assert_called_once_with(client, "tenant-example", "tenant-uid")
            expected = ["query", "identity", "finalization"]
            if not deletion_failure:
                expected.append("verify-absence")
                if not residue:
                    expected.append("admin-api:")
            expected.append("destroy")
            self.assertEqual(expected, calls[-len(expected):])
            if not deletion_failure and not residue:
                self.assertEqual(
                    [
                        "admin-api:",
                        "admin-api:tenant-example",
                        "admin-api:",
                    ],
                    [
                        call
                        for call in calls
                        if call.startswith("admin-api:")
                    ],
                )
            self.assertEqual(
                "failed" if deletion_failure or residue else "passed",
                timings["tenant_deletion_finalization"],
            )
        else:
            delete.assert_not_called()
            self.assertEqual("failed", timings["tenant_sql_probe"])
            self.assertNotIn("three independent three-instance", output.getvalue())


if __name__ == "__main__":
    unittest.main()
