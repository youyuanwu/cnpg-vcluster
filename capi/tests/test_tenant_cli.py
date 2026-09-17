from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts.lib.files import has_owner_only_permissions
from scripts.lib.tenant_runtime import TenantRuntime, TenantRuntimeError
from scripts.lib.tenant_spec import TenantSpec
from scripts.lib.tenant_status import TenantStatus
from scripts.tenant import execute


SPEC = {
    "schema": 1,
    "profile": "local",
    "name": "tenant-c",
    "kubernetesVersion": "1.36.4",
    "workers": 1,
    "podCIDR": "10.72.0.0/16",
    "serviceCIDR": "10.142.0.0/16",
    "databaseCount": 1,
}


class FakeAdapter:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.status_value = TenantStatus(
            profile="local",
            tenant="tenant-c",
            classification="absent",
            foundation_healthy=True,
        )
        self.failure: RuntimeError | None = None

    def foundation_identity(self, root: Path, spec: TenantSpec):
        self.calls.append("foundation")
        return {"management": "management-uid"}

    def intended_resources(self, spec: TenantSpec):
        return (f"Cluster/{spec.name}",)

    def create(self, root, spec, runtime, journal, timings):
        self.calls.append("create")
        self.assert_operation_precedes_mutation(runtime, journal)
        with timings.phase("control-plane"):
            if self.failure is not None:
                raise self.failure
        runtime.update_operation(
            journal,
            phase="control-plane-ready",
            observed={"cluster": "cluster-uid"},
        )
        return {"cluster": "cluster-uid"}

    def status(self, root: Path, tenant: str):
        self.calls.append("status")
        return self.status_value

    def delete(self, root, spec, identity, runtime, journal, timings):
        self.calls.append("delete")
        self.assert_operation_precedes_mutation(runtime, journal)
        with timings.phase("deletion"):
            if self.failure is not None:
                raise self.failure

    @staticmethod
    def assert_operation_precedes_mutation(runtime, journal):
        observed = runtime.load_operation()
        if observed.operation_id != journal.operation_id:
            raise AssertionError("operation journal was not persisted")


class TenantCliTests(unittest.TestCase):
    def make_root(self) -> tuple[tempfile.TemporaryDirectory[str], Path, Path]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "config" / "azure").mkdir(parents=True)
        (root / "config" / "versions.env").write_text(
            "KUBERNETES_VERSION=v1.36.4\n",
            encoding="utf-8",
        )
        (root / "config" / "azure" / "defaults.env").write_text(
            "AZURE_TENANT_KUBERNETES_VERSION=1.32.13\n",
            encoding="utf-8",
        )
        spec_path = root / "tenant.json"
        spec_path.write_text(json.dumps(SPEC), encoding="utf-8")
        return temporary, root, spec_path

    def test_create_persists_identity_and_sanitized_timings(self) -> None:
        _, root, spec_path = self.make_root()
        adapter = FakeAdapter()
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(
                execute(
                    root,
                    ["create", "local", str(spec_path)],
                    adapters={"local": adapter},
                ),
                0,
            )
        runtime = TenantRuntime(root, "local", "tenant-c")
        identity = runtime.load_identity()
        self.assertEqual(identity["observed"], {"cluster": "cluster-uid"})
        self.assertFalse(runtime.operation_exists())
        evidence = list(runtime.paths.evidence.glob("create-*.json"))
        self.assertEqual(len(evidence), 1)
        self.assertTrue(has_owner_only_permissions(evidence[0]))
        self.assertIn("TENANT_TIMING ", output.getvalue())
        self.assertEqual(adapter.calls, ["foundation", "create"])

    def test_failed_create_leaves_journal_and_redacts_evidence(self) -> None:
        _, root, spec_path = self.make_root()
        adapter = FakeAdapter()
        adapter.failure = RuntimeError("password=super-secret")
        output = io.StringIO()
        with redirect_stdout(output):
            with self.assertRaisesRegex(RuntimeError, "password"):
                execute(
                    root,
                    ["create", "local", str(spec_path)],
                    adapters={"local": adapter},
                )
        runtime = TenantRuntime(root, "local", "tenant-c")
        self.assertTrue(runtime.operation_exists())
        evidence = next(runtime.paths.evidence.glob("create-*.json"))
        text = evidence.read_text(encoding="utf-8")
        self.assertNotIn("super-secret", text)
        self.assertIn("REDACTED", text)
        self.assertNotIn("super-secret", output.getvalue())

    def test_evidence_failure_does_not_mask_provider_failure(self) -> None:
        _, root, spec_path = self.make_root()
        adapter = FakeAdapter()
        adapter.failure = RuntimeError("primary provider failure")
        with patch(
            "scripts.tenant.TenantTimings.persist",
            side_effect=RuntimeError("evidence write failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "primary provider failure") as raised:
                execute(
                    root,
                    ["create", "local", str(spec_path)],
                    adapters={"local": adapter},
                )
        self.assertTrue(
            any("evidence write failure" in note for note in raised.exception.__notes__)
        )

    def test_confirmation_is_checked_before_adapter_or_runtime_mutation(self) -> None:
        _, root, _ = self.make_root()
        adapter = FakeAdapter()
        with self.assertRaisesRegex(RuntimeError, "confirmation token"):
            execute(
                root,
                ["delete", "local", "tenant-c", "wrong"],
                adapters={"local": adapter},
            )
        self.assertEqual(adapter.calls, [])
        self.assertFalse((root / ".runtime").exists())

    def test_delete_removes_identity_and_operation_but_keeps_evidence(self) -> None:
        _, root, spec_path = self.make_root()
        adapter = FakeAdapter()
        execute(
            root,
            ["create", "local", str(spec_path)],
            adapters={"local": adapter},
        )
        adapter.calls.clear()
        execute(
            root,
            ["delete", "local", "tenant-c", "local/tenant-c"],
            adapters={"local": adapter},
        )
        runtime = TenantRuntime(root, "local", "tenant-c")
        self.assertFalse(runtime.identity_exists())
        self.assertFalse(runtime.operation_exists())
        self.assertEqual(len(list(runtime.paths.evidence.glob("delete-*.json"))), 1)
        self.assertEqual(adapter.calls, ["delete"])

    def test_status_without_lock_is_read_only(self) -> None:
        _, root, _ = self.make_root()
        adapter = FakeAdapter()
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(
                execute(
                    root,
                    ["status", "local", "tenant-c"],
                    adapters={"local": adapter},
                ),
                0,
            )
        self.assertEqual(json.loads(output.getvalue())["classification"], "absent")
        self.assertFalse((root / ".runtime").exists())
        self.assertEqual(adapter.calls, ["status"])

    def test_status_exit_code_reflects_non_healthy_state(self) -> None:
        _, root, _ = self.make_root()
        adapter = FakeAdapter()
        adapter.status_value = TenantStatus(
            profile="local",
            tenant="tenant-c",
            classification="degraded",
            foundation_healthy=True,
            blockers=("functional evidence is stale",),
        )
        with redirect_stdout(io.StringIO()):
            self.assertEqual(
                execute(
                    root,
                    ["status", "local", "tenant-c"],
                    adapters={"local": adapter},
                ),
                1,
            )

    def test_status_redacts_nested_sensitive_values(self) -> None:
        _, root, _ = self.make_root()
        adapter = FakeAdapter()
        adapter.status_value = TenantStatus(
            profile="local",
            tenant="tenant-c",
            classification="failed",
            foundation_healthy=True,
            components={"probe": {"detail": "password=super-secret"}},
            blockers=("token=abcdef.abcdefghijklmnop",),
        )
        output = io.StringIO()
        with redirect_stdout(output):
            execute(
                root,
                ["status", "local", "tenant-c"],
                adapters={"local": adapter},
            )
        self.assertNotIn("super-secret", output.getvalue())
        self.assertNotIn("abcdef.abcdefghijklmnop", output.getvalue())
        self.assertIn("REDACTED", output.getvalue())

    def test_operation_marker_recovery_requires_exact_identity(self) -> None:
        _, root, _ = self.make_root()
        spec = TenantSpec.from_mapping(SPEC)
        runtime = TenantRuntime(root, "local", "tenant-c")
        journal = runtime.start_operation(
            operation="create",
            spec=spec,
            foundation_identity={"management": "management-uid"},
            intended_resources=("Cluster/tenant-c",),
            operation_id="operation-1",
        )
        markers = {
            "tenant": "tenant-c",
            "profile": "local",
            "specificationSha256": spec.sha256(),
            "operationId": "operation-1",
        }
        runtime.require_recoverable_markers(journal, markers)
        with self.assertRaisesRegex(TenantRuntimeError, "does not match"):
            runtime.require_recoverable_markers(
                journal,
                markers | {"operationId": "foreign"},
            )

    def test_lock_order_is_e2e_then_profile_then_tools(self) -> None:
        _, root, spec_path = self.make_root()
        adapter = FakeAdapter()
        order: list[str] = []

        class Lock:
            def __init__(self, name: str, result=None):
                self.name = name
                self.result = result

            def __enter__(self):
                order.append(f"enter-{self.name}")
                return self.result

            def __exit__(self, *args):
                order.append(f"exit-{self.name}")

        with (
            patch("scripts.tenant.e2e_lock", return_value=Lock("e2e")),
            patch(
                "scripts.tenant.profile_lock",
                return_value=Lock("profile", True),
            ),
            patch("scripts.tenant.tools_lock", return_value=Lock("tools")),
        ):
            execute(
                root,
                ["create", "local", str(spec_path)],
                adapters={"local": adapter},
            )
        self.assertEqual(
            order[:3],
            ["enter-e2e", "enter-profile", "enter-tools"],
        )
        self.assertEqual(
            order[-3:],
            ["exit-tools", "exit-profile", "exit-e2e"],
        )


if __name__ == "__main__":
    unittest.main()
