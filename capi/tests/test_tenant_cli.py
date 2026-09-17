from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts.lib.files import has_owner_only_permissions, write_private_file
from scripts.lib.locking import e2e_lock, profile_lock, tools_lock
from scripts.lib.tenant_runtime import (
    TenantRuntime,
    TenantRuntimeError,
    foundation_sha256,
)
from scripts.lib.tenant_spec import TenantSpec, TenantSpecError
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
        self.assertEqual(identity.observed, {"cluster": "cluster-uid"})
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

    def test_invalid_spec_and_foundation_failure_emit_failed_timing(self) -> None:
        _, root, spec_path = self.make_root()
        adapter = FakeAdapter()
        invalid_path = root / "invalid.json"
        invalid_path.write_text('{"profile":"local"}', encoding="utf-8")
        output = io.StringIO()
        with redirect_stdout(output):
            with self.assertRaises(TenantSpecError):
                execute(
                    root,
                    ["create", "local", str(invalid_path)],
                    adapters={"local": adapter},
                )
        rejected = list(
            (root / ".runtime" / "lifecycle" / "rejected" / "local").glob(
                "create-*.json"
            )
        )
        self.assertEqual(len(rejected), 1)
        self.assertIn('"status":"failed"', output.getvalue())

        def fail_foundation(root: Path, spec: TenantSpec):
            raise RuntimeError(
                "GET /subscriptions/00000000-0000-0000-0000-000000000000 failed"
            )

        adapter.foundation_identity = fail_foundation
        output = io.StringIO()
        with redirect_stdout(output):
            with self.assertRaisesRegex(RuntimeError, "subscriptions"):
                execute(
                    root,
                    ["create", "local", str(spec_path)],
                    adapters={"local": adapter},
                )
        self.assertIn('"phase":"foundation"', output.getvalue())
        self.assertIn('"status":"failed"', output.getvalue())
        self.assertNotIn(
            "00000000-0000-0000-0000-000000000000",
            output.getvalue(),
        )

    def test_invalid_spec_preserves_primary_error_if_evidence_fails(self) -> None:
        _, root, _ = self.make_root()
        invalid_path = root / "invalid.json"
        invalid_path.write_text('{"profile":"local"}', encoding="utf-8")
        with patch(
            "scripts.tenant.record_rejected_create",
            side_effect=RuntimeError("evidence write failure"),
        ):
            with self.assertRaises(TenantSpecError) as raised:
                execute(
                    root,
                    ["create", "local", str(invalid_path)],
                    adapters={"local": FakeAdapter()},
                )
        self.assertTrue(
            any("evidence write failure" in note for note in raised.exception.__notes__)
        )

    def test_adapter_failure_outside_phase_gets_failure_record(self) -> None:
        _, root, spec_path = self.make_root()
        adapter = FakeAdapter()

        def fail_create(*args):
            time.sleep(0.12)
            raise RuntimeError("injected unphased failure")

        adapter.create = fail_create
        output = io.StringIO()
        with redirect_stdout(output):
            with self.assertRaisesRegex(RuntimeError, "unphased"):
                execute(
                    root,
                    ["create", "local", str(spec_path)],
                    adapters={"local": adapter},
                )
        self.assertIn('"phase":"operation"', output.getvalue())
        self.assertIn('"status":"failed"', output.getvalue())
        operation = next(
            json.loads(line.removeprefix("TENANT_TIMING "))
            for line in output.getvalue().splitlines()
            if '"phase":"operation"' in line
        )
        self.assertGreaterEqual(operation["seconds"], 0.1)

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
        self.assertEqual(adapter.calls, ["foundation", "delete"])

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
            components={
                "probe": {
                    "detail": json.dumps(
                        {
                            "password": "super-secret",
                            "subscriptionId": "00000000-0000-0000-0000-000000000000",
                        }
                    )
                }
            },
            blockers=(
                "token=abcdef.abcdefghijklmnop",
                "GET /subscriptions/00000000-0000-0000-0000-000000000000",
            ),
        )
        with e2e_lock(root, exclusive=False):
            pass
        with profile_lock(root, "local", exclusive=True, create=True):
            pass
        with tools_lock(root, exclusive=False):
            pass
        output = io.StringIO()
        with redirect_stdout(output):
            execute(
                root,
                ["status", "local", "tenant-c"],
                adapters={"local": adapter},
            )
        self.assertNotIn("super-secret", output.getvalue())
        self.assertNotIn("abcdef.abcdefghijklmnop", output.getvalue())
        self.assertNotIn("00000000-0000-0000-0000-000000000000", output.getvalue())
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
            "foundationSha256": foundation_sha256(
                {"management": "management-uid"}
            ),
        }
        recovered = runtime.recover_observed_identity(
            journal,
            resource="cluster",
            identifier="cluster-uid",
            markers=markers,
            phase="control-plane-ready",
        )
        self.assertEqual(recovered.observed, {"cluster": "cluster-uid"})
        with self.assertRaisesRegex(TenantRuntimeError, "observed identity changed"):
            runtime.update_operation(
                recovered,
                phase="control-plane-ready",
                observed={"cluster": "different-uid"},
            )
        with self.assertRaisesRegex(TenantRuntimeError, "does not match"):
            runtime.require_recoverable_markers(
                journal,
                markers | {"operationId": "foreign"},
            )

    def test_dispatcher_recovers_resource_created_before_uid_persistence(self) -> None:
        _, root, spec_path = self.make_root()

        class RecoveringAdapter(FakeAdapter):
            def __init__(self):
                super().__init__()
                self.resource: dict[str, object] | None = None

            def create(self, root, spec, runtime, journal, timings):
                if self.resource is None:
                    self.resource = {
                        "uid": "cluster-uid",
                        "markers": {
                            "tenant": journal.tenant,
                            "profile": journal.profile,
                            "specificationSha256": journal.specification_sha256,
                            "operationId": journal.operation_id,
                            "foundationSha256": foundation_sha256(
                                journal.foundation_identity
                            ),
                        },
                    }
                    raise RuntimeError("injected crash before UID persistence")
                runtime.recover_observed_identity(
                    journal,
                    resource="cluster",
                    identifier=str(self.resource["uid"]),
                    markers=self.resource["markers"],
                    phase="control-plane-ready",
                )
                return {"cluster": str(self.resource["uid"])}

        adapter = RecoveringAdapter()
        with self.assertRaisesRegex(RuntimeError, "before UID"):
            execute(
                root,
                ["create", "local", str(spec_path)],
                adapters={"local": adapter},
            )
        runtime = TenantRuntime(root, "local", "tenant-c")
        operation_id = runtime.load_operation().operation_id
        self.assertEqual(
            execute(
                root,
                ["create", "local", str(spec_path)],
                adapters={"local": adapter},
            ),
            0,
        )
        self.assertEqual(runtime.load_identity().observed["cluster"], "cluster-uid")
        evidence = list(runtime.paths.evidence.glob("create-*.json"))
        self.assertEqual(len(evidence), 1)
        self.assertIn(operation_id, evidence[0].name)

    def test_durable_observed_identity_cannot_change_in_new_operation(self) -> None:
        _, root, spec_path = self.make_root()
        adapter = FakeAdapter()
        execute(
            root,
            ["create", "local", str(spec_path)],
            adapters={"local": adapter},
        )

        def replace_cluster(root, spec, runtime, journal, timings):
            runtime.update_operation(
                journal,
                phase="control-plane-ready",
                observed={"cluster": "replacement-uid"},
            )
            return {"worker": "worker-uid"}

        adapter.create = replace_cluster
        with self.assertRaisesRegex(TenantRuntimeError, "observed identity changed"):
            execute(
                root,
                ["create", "local", str(spec_path)],
                adapters={"local": adapter},
            )
        self.assertEqual(
            TenantRuntime(root, "local", "tenant-c").load_identity().observed,
            {"cluster": "cluster-uid"},
        )

    def test_create_rejects_changed_or_malformed_existing_identity(self) -> None:
        _, root, spec_path = self.make_root()
        adapter = FakeAdapter()
        execute(
            root,
            ["create", "local", str(spec_path)],
            adapters={"local": adapter},
        )
        changed = dict(SPEC)
        changed["podCIDR"] = "10.73.0.0/16"
        changed_path = root / "changed.json"
        changed_path.write_text(json.dumps(changed), encoding="utf-8")
        adapter.calls.clear()
        with self.assertRaisesRegex(TenantRuntimeError, "specification changed"):
            execute(
                root,
                ["create", "local", str(changed_path)],
                adapters={"local": adapter},
            )
        self.assertEqual(adapter.calls, ["foundation"])
        runtime = TenantRuntime(root, "local", "tenant-c")
        write_private_file(runtime.paths.identity, "{}\n")
        adapter.calls.clear()
        with self.assertRaisesRegex(TenantRuntimeError, "identity schema"):
            execute(
                root,
                ["create", "local", str(spec_path)],
                adapters={"local": adapter},
            )
        self.assertEqual(adapter.calls, ["foundation"])

    def test_delete_rejects_malformed_identity_before_adapter_mutation(self) -> None:
        _, root, _ = self.make_root()
        runtime = TenantRuntime(root, "local", "tenant-c")
        write_private_file(runtime.paths.identity, "{}\n")
        adapter = FakeAdapter()
        with self.assertRaisesRegex(TenantRuntimeError, "identity schema"):
            execute(
                root,
                ["delete", "local", "tenant-c", "local/tenant-c"],
                adapters={"local": adapter},
            )
        self.assertEqual(adapter.calls, [])

    def test_delete_rejects_stale_checksum_and_observed_identity(self) -> None:
        _, root, spec_path = self.make_root()
        adapter = FakeAdapter()
        execute(
            root,
            ["create", "local", str(spec_path)],
            adapters={"local": adapter},
        )
        runtime = TenantRuntime(root, "local", "tenant-c")
        payload = json.loads(runtime.paths.identity.read_text(encoding="utf-8"))
        payload["specificationSha256"] = "0" * 64
        write_private_file(
            runtime.paths.identity,
            json.dumps(payload) + "\n",
        )
        adapter.calls.clear()
        with self.assertRaisesRegex(TenantRuntimeError, "checksum"):
            execute(
                root,
                ["delete", "local", "tenant-c", "local/tenant-c"],
                adapters={"local": adapter},
            )
        self.assertEqual(adapter.calls, [])
        payload["specificationSha256"] = TenantSpec.from_mapping(SPEC).sha256()
        payload["observed"] = {}
        write_private_file(
            runtime.paths.identity,
            json.dumps(payload) + "\n",
        )
        with self.assertRaisesRegex(TenantRuntimeError, "observed"):
            execute(
                root,
                ["delete", "local", "tenant-c", "local/tenant-c"],
                adapters={"local": adapter},
            )
        self.assertEqual(adapter.calls, [])
        payload["observed"] = {"cluster": 42}
        write_private_file(
            runtime.paths.identity,
            json.dumps(payload) + "\n",
        )
        with self.assertRaisesRegex(TenantRuntimeError, "observed"):
            execute(
                root,
                ["delete", "local", "tenant-c", "local/tenant-c"],
                adapters={"local": adapter},
            )
        self.assertEqual(adapter.calls, [])

    def test_delete_recovers_if_identity_removal_was_interrupted(self) -> None:
        _, root, spec_path = self.make_root()
        adapter = FakeAdapter()
        execute(
            root,
            ["create", "local", str(spec_path)],
            adapters={"local": adapter},
        )
        runtime = TenantRuntime(root, "local", "tenant-c")
        from scripts.lib import tenant_runtime

        original_unlink = tenant_runtime._unlink_private_file

        def fail_identity(path: Path) -> None:
            if path == runtime.paths.identity:
                raise RuntimeError("injected identity unlink failure")
            original_unlink(path)

        with patch(
            "scripts.lib.tenant_runtime._unlink_private_file",
            side_effect=fail_identity,
        ):
            with self.assertRaisesRegex(RuntimeError, "identity unlink"):
                execute(
                    root,
                    ["delete", "local", "tenant-c", "local/tenant-c"],
                    adapters={"local": adapter},
                )
        self.assertTrue(runtime.identity_exists())
        self.assertFalse(runtime.operation_exists())
        execute(
            root,
            ["delete", "local", "tenant-c", "local/tenant-c"],
            adapters={"local": adapter},
        )
        self.assertFalse(runtime.identity_exists())
        self.assertFalse(runtime.operation_exists())
        self.assertEqual(
            execute(
                root,
                ["delete", "local", "tenant-c", "local/tenant-c"],
                adapters={"local": adapter},
            ),
            0,
        )

    def test_status_rejects_runtime_residue_without_profile_lock(self) -> None:
        _, root, _ = self.make_root()
        runtime = TenantRuntime(root, "local", "tenant-c")
        write_private_file(runtime.paths.identity, "{}\n")
        output = io.StringIO()
        with redirect_stdout(output):
            code = execute(
                root,
                ["status", "local", "tenant-c"],
                adapters={"local": FakeAdapter()},
            )
        self.assertEqual(code, 1)
        self.assertEqual(
            json.loads(output.getvalue())["classification"],
            "ownership-invalid",
        )

    def test_status_wraps_inspection_failure_in_status_envelope(self) -> None:
        _, root, _ = self.make_root()
        adapter = FakeAdapter()

        def fail_status(root: Path, tenant: str):
            raise RuntimeError("injected inspection failure")

        adapter.status = fail_status
        output = io.StringIO()
        with redirect_stdout(output):
            code = execute(
                root,
                ["status", "local", "tenant-c"],
                adapters={"local": adapter},
            )
        payload = json.loads(output.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(payload["classification"], "ownership-invalid")
        self.assertIn("inspection failure", payload["blockers"][0])

    def test_authoritative_status_fixture_checks_all_sources(self) -> None:
        _, root, _ = self.make_root()

        class InspectingAdapter(FakeAdapter):
            def __init__(self, sources):
                super().__init__()
                self.sources = sources

            def status(self, root: Path, tenant: str):
                if any(self.sources.values()):
                    return TenantStatus(
                        profile="local",
                        tenant=tenant,
                        classification="ownership-invalid",
                        foundation_healthy=True,
                        blockers=("authoritative tenant residue remains",),
                    )
                return TenantStatus(
                    profile="local",
                    tenant=tenant,
                    classification="absent",
                    foundation_healthy=True,
                )

        for source in ("management", "provider", "artifacts", "infrastructure"):
            with self.subTest(source=source):
                sources = {
                    "management": False,
                    "provider": False,
                    "artifacts": False,
                    "infrastructure": False,
                }
                sources[source] = True
                output = io.StringIO()
                with redirect_stdout(output):
                    code = execute(
                        root,
                        ["status", "local", "tenant-c"],
                        adapters={"local": InspectingAdapter(sources)},
                    )
                self.assertEqual(code, 1)
                self.assertEqual(
                    json.loads(output.getvalue())["classification"],
                    "ownership-invalid",
                )

    def test_status_with_existing_profile_lock_creates_no_other_locks(self) -> None:
        _, root, _ = self.make_root()
        with profile_lock(root, "local", exclusive=True, create=True):
            pass
        output = io.StringIO()
        with redirect_stdout(output):
            code = execute(
                root,
                ["status", "local", "tenant-c"],
                adapters={"local": FakeAdapter()},
            )
        self.assertEqual(code, 1)
        self.assertFalse((root / ".tools").exists())
        self.assertEqual(
            json.loads(output.getvalue())["classification"],
            "ownership-invalid",
        )

    def test_status_does_not_chmod_existing_tools_lock(self) -> None:
        _, root, _ = self.make_root()
        with e2e_lock(root, exclusive=False):
            pass
        with profile_lock(root, "local", exclusive=True, create=True):
            pass
        lock_path = root / ".tools" / ".lock"
        lock_path.write_text("", encoding="utf-8")
        lock_path.chmod(0o644)
        output = io.StringIO()
        with redirect_stdout(output):
            code = execute(
                root,
                ["status", "local", "tenant-c"],
                adapters={"local": FakeAdapter()},
            )
        self.assertEqual(code, 1)
        self.assertEqual(lock_path.stat().st_mode & 0o777, 0o644)

    def test_profile_lock_blocks_another_process(self) -> None:
        _, root, _ = self.make_root()
        script = (
            "from pathlib import Path; "
            "from scripts.lib.locking import profile_lock; "
            f"root=Path({str(root)!r}); "
            "ctx=profile_lock(root,'local',exclusive=True,create=True); "
            "ctx.__enter__(); print('acquired', flush=True); ctx.__exit__(None,None,None)"
        )
        with profile_lock(root, "local", exclusive=True, create=True):
            process = subprocess.Popen(
                [sys.executable, "-c", script],
                cwd=Path(__file__).resolve().parents[1],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            time.sleep(0.2)
            self.assertIsNone(process.poll())
        stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertEqual(stdout.strip(), "acquired")

    def test_just_preserves_literal_specification_path(self) -> None:
        _, root, _ = self.make_root()
        repository_root = Path(__file__).resolve().parents[1]
        literal = root / '$PAW_REVIEW_LITERAL a"b.json'
        literal.write_text(json.dumps(SPEC), encoding="utf-8")
        result = subprocess.run(
            [
                "just",
                "--justfile",
                str(repository_root / "Justfile"),
                "tenant-create",
                "local",
                str(literal),
            ],
            cwd=repository_root,
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "PAW_REVIEW_LITERAL": "expanded"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "tenant profile adapter is not implemented: local",
            result.stderr,
        )
        self.assertNotIn("unable to read tenant specification", result.stderr)

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
