from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch

from scripts.destroy_tenant import (
    _tenant_resource,
    prepare_tenant_deletion,
)
from scripts.lib.tenant_runtime import TenantIdentity, TenantRuntime
from scripts.lib.tenant_spec import TenantSpec
from scripts.lib.tenant_status import TenantStatus
from scripts.lib.tenants import (
    inspect_management_resource,
    verify_tenant_management_ownership,
)
from scripts.local_tenant import LocalTenantAdapter


SPEC = TenantSpec.from_mapping(
    {
        "schema": 1,
        "profile": "local",
        "name": "tenant-c",
        "kubernetesVersion": "1.36.4",
        "workers": 1,
        "podCIDR": "10.73.0.0/16",
        "serviceCIDR": "10.143.0.0/16",
        "databaseCount": 1,
    }
)


def result(returncode: int, stderr: str = "", stdout: str = ""):
    return type(
        "Result",
        (),
        {"returncode": returncode, "stderr": stderr, "stdout": stdout},
    )()


def identity(name: str = "tenant-c") -> TenantIdentity:
    spec = SPEC
    if name != SPEC.name:
        spec = TenantSpec.from_mapping(
            {**SPEC.to_mapping(), "name": name}
        )
    return TenantIdentity(
        profile="local",
        tenant=name,
        specification=spec,
        specification_sha256=spec.sha256(),
        foundation_identity={"management": "uid"},
        observed={
            "endpoint": "172.18.0.10",
            "markerOperationId": "create-operation",
        },
    )


class TenantLifecycleTests(unittest.TestCase):
    def test_interrupted_delete_keeps_journal_until_ready_evidence_is_removed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = TenantRuntime(root, "local", SPEC.name)
            journal = runtime.start_operation(
                operation="create",
                spec=SPEC,
                foundation_identity={"management": "uid"},
                intended_resources=("Cluster/tenant-c",),
                operation_id="create-operation",
            )
            runtime.complete_create(
                journal,
                SPEC,
                {"cluster": "cluster-uid"},
            )
            runtime.write_ready_evidence(
                {
                    "schema": 1,
                    "profile": "local",
                    "tenant": SPEC.name,
                    "specificationSha256": SPEC.sha256(),
                    "foundationIdentity": {"management": "uid"},
                    "observed": {"cluster": "cluster-uid"},
                    "verifiedAt": 1000.0,
                    "functional": {
                        "controlPlane": True,
                        "workers": True,
                        "network": True,
                        "storage": True,
                        "database": True,
                    },
                }
            )
            delete = runtime.start_operation(
                operation="delete",
                spec=SPEC,
                foundation_identity={"management": "uid"},
                intended_resources=("Cluster/tenant-c",),
                operation_id="delete-operation",
            )
            from scripts.lib import tenant_runtime

            original = tenant_runtime._unlink_private_file

            def fail_ready(path: Path) -> None:
                if path == runtime.paths.ready:
                    raise RuntimeError("injected ready unlink failure")
                original(path)

            with patch(
                "scripts.lib.tenant_runtime._unlink_private_file",
                side_effect=fail_ready,
            ):
                with self.assertRaisesRegex(RuntimeError, "ready unlink"):
                    runtime.complete_delete(delete)
            self.assertTrue(runtime.operation_exists())
            self.assertTrue(runtime.identity_exists())
            self.assertTrue(runtime.paths.ready.exists())

    def test_orphaned_ready_evidence_is_ownership_invalid(self) -> None:
        adapter = LocalTenantAdapter()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = TenantRuntime(root, "local", SPEC.name)
            runtime.write_ready_evidence(
                {
                    "schema": 1,
                    "profile": "local",
                    "tenant": SPEC.name,
                    "specificationSha256": SPEC.sha256(),
                    "foundationIdentity": {"management": "uid"},
                    "observed": {"cluster": "cluster-uid"},
                    "verifiedAt": 1000.0,
                    "functional": {},
                }
            )
            with (
                patch.object(adapter, "_config", return_value={"LAB_PREFIX": "lab"}),
                patch.object(adapter, "_foundation", return_value=({}, False)),
                patch("scripts.local_tenant.management_status", return_value={}),
                patch("scripts.local_tenant.run", return_value=result(0, stdout="")),
                patch(
                    "scripts.local_tenant.inspect_storage_volume",
                    return_value=None,
                ),
            ):
                status = adapter.status(root, SPEC.name)
            self.assertEqual(status.classification, "ownership-invalid")

    def test_management_inspection_distinguishes_not_found_and_failure(self) -> None:
        tenant = type(
            "Tenant", (), {"namespace": "tenant-c", "name": "tenant-c"}
        )()
        client = Mock()
        client.kubectl.return_value = result(
            1,
            'Error from server (NotFound): clusters.cluster.x-k8s.io "tenant-c" not found',
        )
        self.assertIsNone(
            inspect_management_resource(client, tenant, "cluster/tenant-c")
        )
        client.kubectl.return_value = result(
            1,
            "lookup management API: host not found",
        )
        with self.assertRaisesRegex(RuntimeError, "inspection failed"):
            inspect_management_resource(client, tenant, "cluster/tenant-c")

    def test_tenant_api_inspection_distinguishes_not_found_and_failure(self) -> None:
        tenant = type("Tenant", (), {"name": "tenant-c"})()
        with patch(
            "scripts.destroy_tenant._tenant_kubectl",
            return_value=result(
                1,
                'Error from server (NotFound): deployments.apps "item" not found',
            ),
        ):
            self.assertIsNone(
                _tenant_resource(
                    Path("."),
                    {},
                    tenant,
                    "get",
                    "deployment/item",
                )
            )
        with patch(
            "scripts.destroy_tenant._tenant_kubectl",
            return_value=result(1, "connection refused"),
        ):
            with self.assertRaisesRegex(RuntimeError, "inspection failed"):
                _tenant_resource(
                    Path("."),
                    {},
                    tenant,
                    "get",
                    "deployment/item",
                )

    def test_management_ownership_rejects_foreign_marker(self) -> None:
        tenant = type(
            "Tenant", (), {"namespace": "tenant-c", "name": "tenant-c"}
        )()
        client = Mock()
        namespace = {
            "metadata": {
                "labels": {"example.owner": "lab"},
                "annotations": {},
            }
        }
        not_found = result(
            1,
            'Error from server (NotFound): resource "item" not found',
        )
        client.kubectl.side_effect = [
            result(0, stdout=json.dumps(namespace)),
            *([not_found] * 7),
        ]
        with self.assertRaisesRegex(RuntimeError, "marker mismatch"):
            verify_tenant_management_ownership(
                {"OWNERSHIP_LABEL": "example.owner", "LAB_PREFIX": "lab"},
                client,
                tenant,
                expected_markers={
                    "tenant": "tenant-c",
                    "profile": "local",
                    "specificationSha256": "spec",
                    "foundationSha256": "foundation",
                    "operationId": "operation",
                },
            )

    def test_matching_deletion_journal_skips_repeated_live_cleanup(self) -> None:
        tenant = type("Tenant", (), {"name": "tenant-c"})()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            journal = root / ".runtime" / "deletions" / "tenant-c.json"
            journal.parent.mkdir(parents=True)
            journal.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "tenant": "tenant-c",
                        "clusterUID": "same",
                        "phase": "api-cleanup-complete",
                    }
                )
            )
            journal.chmod(0o600)
            with patch(
                "scripts.destroy_tenant.cnpg_artifacts_present"
            ) as cnpg_present:
                prepare_tenant_deletion(
                    root,
                    {},
                    object(),
                    tenant,
                    {"metadata": {"uid": "same"}},
                )
            cnpg_present.assert_not_called()

    def test_create_allocates_endpoint_only_after_journal_and_writes_ready_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = TenantRuntime(root, "local", SPEC.name)
            journal = runtime.start_operation(
                operation="create",
                spec=SPEC,
                foundation_identity={"management": "uid"},
                intended_resources=("Cluster/tenant-c",),
                operation_id="operation",
            )
            observed = {
                "endpoint": "172.18.0.10",
                "markerOperationId": "operation",
            }
            adapter = LocalTenantAdapter(clock=lambda: 1000.0)
            calls = []

            def allocate(*_args):
                self.assertTrue(runtime.operation_exists())
                calls.append("allocate")
                return "172.18.0.10"

            with (
                patch.object(adapter, "_config", return_value={}),
                patch(
                    "scripts.local_tenant.allocate_tenant_endpoint",
                    side_effect=allocate,
                ),
                patch("scripts.local_tenant.resolve_tenant_storage"),
                patch(
                    "scripts.local_tenant.reconcile_tenant",
                    return_value=observed,
                ),
                patch(
                    "scripts.local_tenant.verify_tenant_functional",
                    return_value={
                        "network": True,
                        "database": True,
                        "workers": {"worker": {}},
                        "storage": {"pvc": "pv"},
                    },
                ),
                patch("scripts.local_tenant.ManagementClient"),
            ):
                returned = adapter.create(
                    root,
                    SPEC,
                    runtime,
                    journal,
                    object(),
                )
            self.assertEqual(returned, observed)
            self.assertEqual(calls, ["allocate"])
            evidence = runtime.load_ready_evidence()
            self.assertEqual(evidence["verifiedAt"], 1000.0)
            self.assertEqual(evidence["observed"], observed)
            self.assertEqual(runtime.paths.ready.stat().st_mode & 0o777, 0o600)

    def test_local_adapter_resumes_interrupted_identity_capture_stages(self) -> None:
        stages = (
            ("controlPlane", "control-plane-ready"),
            ("workers", "workers-ready"),
            ("database", "data-services-ready"),
        )
        for resource, phase in stages:
            with self.subTest(resource=resource):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    runtime = TenantRuntime(root, "local", SPEC.name)
                    journal = runtime.start_operation(
                        operation="create",
                        spec=SPEC,
                        foundation_identity={"management": "uid"},
                        intended_resources=("Cluster/tenant-c",),
                        operation_id=f"{resource}-operation",
                    )
                    adapter = LocalTenantAdapter(clock=lambda: 1000.0)
                    observed = {
                        "endpoint": "172.18.0.10",
                        "markerOperationId": journal.operation_id,
                        resource: f"{resource}-uid",
                    }
                    attempts = 0

                    def reconcile(*_args, **_kwargs):
                        nonlocal attempts
                        attempts += 1
                        current = runtime.load_operation()
                        if attempts == 1:
                            runtime.update_operation(
                                current,
                                phase=phase,
                                observed={resource: f"{resource}-uid"},
                            )
                            raise RuntimeError(f"injected {resource} interruption")
                        self.assertEqual(
                            runtime.load_operation().observed[resource],
                            f"{resource}-uid",
                        )
                        return observed

                    patches = (
                        patch.object(adapter, "_config", return_value={}),
                        patch(
                            "scripts.local_tenant.allocate_tenant_endpoint",
                            return_value="172.18.0.10",
                        ),
                        patch("scripts.local_tenant.resolve_tenant_storage"),
                        patch(
                            "scripts.local_tenant.reconcile_tenant",
                            side_effect=reconcile,
                        ),
                        patch(
                            "scripts.local_tenant.verify_tenant_functional",
                            return_value={
                                "network": True,
                                "database": True,
                                "workers": {"worker": {}},
                                "storage": {"pvc": "pv"},
                            },
                        ),
                        patch("scripts.local_tenant.ManagementClient"),
                    )
                    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                        with self.assertRaisesRegex(RuntimeError, "interruption"):
                            adapter.create(
                                root,
                                SPEC,
                                runtime,
                                journal,
                                object(),
                            )
                        returned = adapter.create(
                            root,
                            SPEC,
                            runtime,
                            runtime.load_operation(),
                            object(),
                        )
                    self.assertEqual(returned, observed)

    def test_unhealthy_survivor_refuses_before_target_mutation(self) -> None:
        adapter = LocalTenantAdapter()
        survivor_specs = {
            "tenant-a": TenantSpec.from_mapping(
                {**SPEC.to_mapping(), "name": "tenant-a"}
            ),
            "tenant-b": TenantSpec.from_mapping(
                {**SPEC.to_mapping(), "name": "tenant-b"}
            ),
        }
        with (
            patch.object(adapter, "_config", return_value={}),
            patch("scripts.local_tenant.ManagementClient"),
            patch(
                "scripts.local_tenant.recorded_local_specs",
                return_value=survivor_specs,
            ),
            patch(
                "scripts.local_tenant.tenant_endpoint_allocation",
                side_effect=["172.18.0.10", "172.18.0.11"],
            ),
            patch("scripts.local_tenant.tenant_from_spec"),
            patch("scripts.local_tenant.resolve_tenant_storage"),
            patch.object(
                adapter,
                "status",
                side_effect=[
                    TenantStatus(
                        "local",
                        "tenant-a",
                        "ready",
                        True,
                    ),
                    TenantStatus(
                        "local",
                        "tenant-b",
                        "degraded",
                        True,
                        blockers=("stale evidence",),
                    ),
                ],
            ),
            patch(
                "scripts.local_tenant.stable_tenant_snapshot",
                return_value={"identity": "stable"},
            ),
            patch("scripts.local_tenant.delete_selected_tenant") as mutate,
        ):
            with self.assertRaisesRegex(RuntimeError, "tenant-b"):
                adapter.validate_delete(Path("."), SPEC, identity())
        mutate.assert_not_called()

    def test_sole_tenant_delete_releases_endpoint_after_absence(self) -> None:
        adapter = LocalTenantAdapter()
        runtime = Mock()
        journal = Mock()
        timings = Mock()
        timings.phase.side_effect = lambda _name: nullcontext()
        calls = []
        with (
            patch.object(adapter, "_config", return_value={}),
            patch("scripts.local_tenant.resolve_tenant_storage"),
            patch(
                "scripts.local_tenant.delete_selected_tenant",
                side_effect=lambda *_args, **_kwargs: calls.append("delete"),
            ),
            patch(
                "scripts.local_tenant.release_tenant_endpoint",
                side_effect=lambda *_args, **_kwargs: calls.append("release"),
            ),
            patch(
                "scripts.local_tenant.tenant_endpoint_allocation",
                return_value=None,
            ),
            patch.object(
                adapter,
                "_foundation",
                return_value=({"management": "uid"}, True),
            ),
            patch(
                "scripts.local_tenant.recorded_local_tenants",
                return_value=[],
            ),
            patch("scripts.local_tenant.ManagementClient"),
        ):
            adapter.delete(
                Path("."),
                SPEC,
                identity(),
                runtime,
                journal,
                timings,
            )
        self.assertEqual(calls, ["delete", "release"])

    def test_local_status_reports_absent_without_writing_state(self) -> None:
        adapter = LocalTenantAdapter()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                patch.object(adapter, "_config", return_value={"LAB_PREFIX": "lab"}),
                patch.object(adapter, "_foundation", return_value=({}, False)),
                patch(
                    "scripts.local_tenant.management_status",
                    return_value={},
                ),
                patch(
                    "scripts.local_tenant.run",
                    return_value=result(0, stdout=""),
                ),
                patch(
                    "scripts.local_tenant.inspect_storage_volume",
                    return_value=None,
                ),
            ):
                status = adapter.status(root, "tenant-c")
            self.assertEqual(status.classification, "absent")
            self.assertFalse((root / ".runtime").exists())

    def test_local_status_reports_progressing_deleting_and_failed_operations(
        self,
    ) -> None:
        for operation, failed, expected in (
            ("create", False, "progressing"),
            ("delete", False, "deleting"),
            ("create", True, "failed"),
        ):
            with self.subTest(operation=operation, failed=failed):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    runtime = TenantRuntime(root, "local", SPEC.name)
                    journal = runtime.start_operation(
                        operation=operation,
                        spec=SPEC,
                        foundation_identity={"management": "uid"},
                        intended_resources=("Cluster/tenant-c",),
                        operation_id=f"{operation}-operation",
                    )
                    if failed:
                        runtime.paths.evidence.mkdir(parents=True, exist_ok=True)
                        runtime.paths.evidence.chmod(0o700)
                        timing = (
                            runtime.paths.evidence
                            / f"{operation}-{journal.operation_id}.json"
                        )
                        timing.write_text(
                            json.dumps(
                                {
                                    "records": [
                                        {"phase": "operation", "status": "failed"}
                                    ]
                                }
                            )
                        )
                        timing.chmod(0o600)
                    adapter = LocalTenantAdapter()
                    with (
                        patch.object(adapter, "_config", return_value={}),
                        patch.object(
                            adapter,
                            "_foundation",
                            return_value=({"management": "uid"}, True),
                        ),
                    ):
                        status = adapter.status(root, SPEC.name)
                    self.assertEqual(status.classification, expected)


if __name__ == "__main__":
    unittest.main()
