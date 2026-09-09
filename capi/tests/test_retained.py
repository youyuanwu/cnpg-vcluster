from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.retained import (
    _delete_representative_tenant,
    _tenant_is_healthy,
    dev_bootstrap,
    dev_clean,
    dev_up_evidence_path,
    dev_test,
    dev_tenant,
    dev_up,
    load_dev_up_evidence,
    retained_path,
    validate_retained_state,
    write_dev_up_evidence,
)


class RetainedTests(unittest.TestCase):
    def test_missing_state_refuses_tenant_loop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(RuntimeError, "dev-bootstrap"):
                validate_retained_state(Path(temporary), {})

    def test_stale_state_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = retained_path(root)
            path.parent.mkdir(mode=0o700)
            path.write_text(json.dumps({"schema": 1, "revision": "old"}))
            path.chmod(0o600)
            with (
                patch("scripts.retained.require_management_ownership"),
                patch("scripts.retained.validate_management_kubeconfig"),
                patch("scripts.retained._retained_payload", return_value={"schema": 1, "revision": "new"}),
            ):
                with self.assertRaisesRegex(RuntimeError, "stale"):
                    validate_retained_state(root, {})

    def test_broad_or_symlinked_state_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = retained_path(root)
            path.parent.mkdir(mode=0o700)
            path.write_text("{}")
            path.chmod(0o644)
            with self.assertRaises(RuntimeError):
                validate_retained_state(root, {})
            path.unlink()
            target = root / "target"
            target.write_text("{}")
            target.chmod(0o600)
            path.symlink_to(target)
            with self.assertRaises(RuntimeError):
                validate_retained_state(root, {})

    def test_bootstrap_prepares_host_before_management_and_state(self) -> None:
        calls: list[str] = []
        with tempfile.TemporaryDirectory() as temporary:
            with (
                patch("scripts.retained.prepare_inotify", side_effect=lambda *_: calls.append("host")),
                patch("scripts.retained.create_management", side_effect=lambda *_: calls.append("management")),
                patch("scripts.retained.write_retained_state", side_effect=lambda *_: calls.append("state")),
            ):
                dev_bootstrap(Path(temporary), {})
        self.assertEqual(calls, ["host", "management", "state"])

    def test_bootstrap_refuses_unbound_existing_management(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = root / ".runtime/management/identity.json"
            identity.parent.mkdir(parents=True)
            identity.write_text("{}")
            with self.assertRaisesRegex(RuntimeError, "dev-clean"):
                dev_bootstrap(root, {})

    def test_tenant_loop_validates_deletes_recreates_and_rebinds(self) -> None:
        calls: list[str] = []
        tenant = type("Tenant", (), {"name": "tenant-a"})()
        with (
            patch("scripts.retained.validate_retained_state", side_effect=lambda *_: calls.append("validate")),
            patch("scripts.retained.verify_all_inputs", side_effect=lambda *_: calls.append("inputs")),
            patch("scripts.retained.ManagementClient", return_value=object()),
            patch("scripts.retained.validate_create_inputs", return_value=[tenant]),
            patch("scripts.retained._delete_representative_tenant", side_effect=lambda *_: calls.append("delete")),
            patch("scripts.retained.reconcile_tenant", side_effect=lambda *_: calls.append("recreate")),
            patch("scripts.retained.write_retained_state", side_effect=lambda *_: calls.append("state")),
        ):
            dev_tenant(Path("."), {})
        self.assertEqual(calls, ["validate", "inputs", "delete", "recreate", "state"])

    def test_dev_up_bootstraps_when_retained_state_is_missing(self) -> None:
        calls: list[str] = []
        tenant = SimpleNamespace(name="tenant-a")
        with tempfile.TemporaryDirectory() as temporary:
            with (
                patch(
                    "scripts.retained.dev_bootstrap",
                    side_effect=lambda *_: calls.append("bootstrap"),
                ),
                patch("scripts.retained.ManagementClient", return_value=object()),
                patch(
                    "scripts.retained.validate_create_inputs",
                    return_value=[tenant],
                ),
                patch(
                    "scripts.retained.reconcile_tenant",
                    side_effect=lambda *_: calls.append("reconcile") or {"uid": "a"},
                ),
                patch(
                    "scripts.retained.write_dev_up_evidence",
                    side_effect=lambda *_: calls.append("evidence"),
                ),
                patch(
                    "scripts.retained.write_retained_state",
                    side_effect=lambda *_: calls.append("state"),
                ),
            ):
                dev_up(Path(temporary), {})
        self.assertEqual(
            calls, ["bootstrap", "reconcile", "evidence", "state"]
        )

    def test_dev_up_exits_without_reconciliation_when_fully_healthy(self) -> None:
        tenant = SimpleNamespace(name="tenant-a")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = retained_path(root)
            state.parent.mkdir(parents=True)
            state.write_text("{}")
            evidence = dev_up_evidence_path(root)
            evidence.parent.mkdir(parents=True)
            evidence.write_text("{}")
            with (
                patch("scripts.retained.validate_retained_state"),
                patch("scripts.retained.run_retained_preflight"),
                patch("scripts.retained.validate_inotify_state"),
                patch(
                    "scripts.retained.validate_create_inputs",
                    return_value=[tenant],
                ),
                patch("scripts.retained.ManagementClient", return_value=object()),
                patch(
                    "scripts.retained.load_dev_up_evidence",
                    return_value={"uid": "a"},
                ),
                patch("scripts.retained._management_is_healthy", return_value=True),
                patch(
                    "scripts.retained.verify_tenant_management_ownership",
                    return_value={"cluster": {"metadata": {"uid": "cluster"}}},
                ),
                patch("scripts.retained._tenant_is_healthy", return_value=True),
                patch("scripts.retained._reconcile_dev_up") as reconcile,
                patch("scripts.retained._delete_representative_tenant") as delete,
            ):
                dev_up(root, {})
            reconcile.assert_not_called()
            delete.assert_not_called()

    def test_dev_up_runs_full_reconcile_when_evidence_is_missing(self) -> None:
        tenant = SimpleNamespace(name="tenant-a")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = retained_path(root)
            state.parent.mkdir(parents=True)
            state.write_text("{}")
            with (
                patch("scripts.retained.validate_retained_state"),
                patch("scripts.retained.run_retained_preflight"),
                patch("scripts.retained.validate_inotify_state"),
                patch(
                    "scripts.retained.validate_create_inputs",
                    return_value=[tenant],
                ),
                patch("scripts.retained.ManagementClient", return_value=object()),
                patch("scripts.retained._reconcile_dev_up") as reconcile,
                patch("scripts.retained._management_is_healthy") as healthy,
            ):
                dev_up(root, {})
            reconcile.assert_called_once()
            self.assertTrue(reconcile.call_args.kwargs["include_management"])
            healthy.assert_not_called()

    def test_dev_up_reconciles_only_tenant_when_management_is_healthy(self) -> None:
        tenant = SimpleNamespace(name="tenant-a")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for path in (retained_path(root), dev_up_evidence_path(root)):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}")
            with (
                patch("scripts.retained.validate_retained_state"),
                patch("scripts.retained.run_retained_preflight"),
                patch("scripts.retained.validate_inotify_state"),
                patch(
                    "scripts.retained.validate_create_inputs",
                    return_value=[tenant],
                ),
                patch("scripts.retained.ManagementClient", return_value=object()),
                patch(
                    "scripts.retained.load_dev_up_evidence",
                    return_value={"uid": "a"},
                ),
                patch("scripts.retained._management_is_healthy", return_value=True),
                patch(
                    "scripts.retained.verify_tenant_management_ownership",
                    return_value={"cluster": {"metadata": {"uid": "cluster"}}},
                ),
                patch("scripts.retained._tenant_is_healthy", return_value=False),
                patch("scripts.retained._reconcile_dev_up") as reconcile,
            ):
                dev_up(root, {})
            reconcile.assert_called_once()
            self.assertFalse(reconcile.call_args.kwargs["include_management"])

    def test_dev_up_reconciles_management_when_health_is_incomplete(self) -> None:
        tenant = SimpleNamespace(name="tenant-a")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for path in (retained_path(root), dev_up_evidence_path(root)):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}")
            with (
                patch("scripts.retained.validate_retained_state"),
                patch("scripts.retained.run_retained_preflight"),
                patch("scripts.retained.validate_inotify_state"),
                patch(
                    "scripts.retained.validate_create_inputs",
                    return_value=[tenant],
                ),
                patch("scripts.retained.ManagementClient", return_value=object()),
                patch(
                    "scripts.retained.load_dev_up_evidence",
                    return_value={"uid": "a"},
                ),
                patch("scripts.retained._management_is_healthy", return_value=False),
                patch("scripts.retained._reconcile_dev_up") as reconcile,
            ):
                dev_up(root, {})
            reconcile.assert_called_once()
            self.assertTrue(reconcile.call_args.kwargs["include_management"])

    def test_dev_up_canonical_absence_uses_tenant_only_reconciliation(self) -> None:
        tenant = SimpleNamespace(name="tenant-a")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for path in (retained_path(root), dev_up_evidence_path(root)):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}")
            with (
                patch("scripts.retained.validate_retained_state"),
                patch("scripts.retained.run_retained_preflight"),
                patch("scripts.retained.validate_inotify_state"),
                patch(
                    "scripts.retained.validate_create_inputs",
                    return_value=[tenant],
                ),
                patch("scripts.retained.ManagementClient", return_value=object()),
                patch(
                    "scripts.retained.load_dev_up_evidence",
                    return_value={"uid": "a"},
                ),
                patch("scripts.retained._management_is_healthy", return_value=True),
                patch(
                    "scripts.retained.verify_tenant_management_ownership",
                    return_value={"cluster": None},
                ),
                patch(
                    "scripts.retained._delete_representative_tenant"
                ) as delete,
                patch("scripts.retained._reconcile_dev_up") as reconcile,
            ):
                dev_up(root, {})
            delete.assert_called_once()
            reconcile.assert_called_once()
            self.assertFalse(reconcile.call_args.kwargs["include_management"])

    def test_dev_up_resumes_valid_live_cluster_deletion_journal(self) -> None:
        tenant = SimpleNamespace(name="tenant-a")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for path in (
                retained_path(root),
                dev_up_evidence_path(root),
                root / ".runtime/deletions/tenant-a.json",
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}")
            with (
                patch("scripts.retained.validate_retained_state"),
                patch("scripts.retained.run_retained_preflight"),
                patch("scripts.retained.validate_inotify_state"),
                patch(
                    "scripts.retained.validate_create_inputs",
                    return_value=[tenant],
                ),
                patch("scripts.retained.ManagementClient", return_value=object()),
                patch(
                    "scripts.retained.load_dev_up_evidence",
                    return_value={"uid": "a"},
                ),
                patch("scripts.retained._management_is_healthy", return_value=True),
                patch(
                    "scripts.retained.verify_tenant_management_ownership",
                    return_value={"cluster": {"metadata": {"uid": "cluster"}}},
                ),
                patch("scripts.retained._tenant_is_healthy") as healthy,
                patch(
                    "scripts.retained._delete_representative_tenant"
                ) as delete,
                patch("scripts.retained._reconcile_dev_up") as reconcile,
            ):
                dev_up(root, {})
            healthy.assert_not_called()
            delete.assert_called_once()
            reconcile.assert_called_once()
            self.assertFalse(reconcile.call_args.kwargs["include_management"])

    def test_dev_up_propagates_authoritative_inspection_failure(self) -> None:
        tenant = SimpleNamespace(name="tenant-a")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for path in (retained_path(root), dev_up_evidence_path(root)):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}")
            with (
                patch("scripts.retained.validate_retained_state"),
                patch("scripts.retained.run_retained_preflight"),
                patch("scripts.retained.validate_inotify_state"),
                patch(
                    "scripts.retained.validate_create_inputs",
                    return_value=[tenant],
                ),
                patch("scripts.retained.ManagementClient", return_value=object()),
                patch(
                    "scripts.retained.load_dev_up_evidence",
                    return_value={"uid": "a"},
                ),
                patch("scripts.retained._management_is_healthy", return_value=True),
                patch(
                    "scripts.retained.verify_tenant_management_ownership",
                    side_effect=RuntimeError("management API connection refused"),
                ),
                patch("scripts.retained._reconcile_dev_up") as reconcile,
            ):
                with self.assertRaisesRegex(RuntimeError, "connection refused"):
                    dev_up(root, {})
            reconcile.assert_not_called()

    def test_dev_up_emits_machine_readable_healthy_timing(self) -> None:
        tenant = SimpleNamespace(name="tenant-a")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for path in (retained_path(root), dev_up_evidence_path(root)):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}")
            output = StringIO()
            with (
                patch("scripts.retained.validate_retained_state"),
                patch("scripts.retained.run_retained_preflight"),
                patch("scripts.retained.validate_inotify_state"),
                patch(
                    "scripts.retained.validate_create_inputs",
                    return_value=[tenant],
                ),
                patch("scripts.retained.ManagementClient", return_value=object()),
                patch(
                    "scripts.retained.load_dev_up_evidence",
                    return_value={"uid": "a"},
                ),
                patch("scripts.retained._management_is_healthy", return_value=True),
                patch(
                    "scripts.retained.verify_tenant_management_ownership",
                    return_value={"cluster": {"metadata": {"uid": "cluster"}}},
                ),
                patch("scripts.retained._tenant_is_healthy", return_value=True),
                redirect_stdout(output),
            ):
                dev_up(root, {})
        record = next(
            json.loads(line.removeprefix("CAPI_DEV_UP "))
            for line in output.getvalue().splitlines()
            if line.startswith("CAPI_DEV_UP ")
        )
        self.assertEqual(record["path"], "healthy")
        self.assertEqual(record["schema"], 1)
        self.assertEqual(record["status"], "passed")

    def test_dev_up_evidence_is_owner_only_and_exact(self) -> None:
        tenant = SimpleNamespace(
            name="tenant-a",
            namespace="tenant-a",
            vip="172.18.0.10",
            pod_cidr="10.70.0.0/16",
            service_cidr="10.140.0.0/16",
            domain="tenant-a.test",
            cnpg_cluster="tenant-a-db",
        )
        config = {
            "TENANT_COMPATIBILITY_REVISION": "tenant-v1",
            "CNPG_COMPATIBILITY_REVISION": "cnpg-v1",
            "SPIKE_API_PORT": "6443",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_dev_up_evidence(root, config, tenant, {"uid": "a"})
            path = dev_up_evidence_path(root)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                load_dev_up_evidence(root, config, tenant), {"uid": "a"}
            )
            payload = json.loads(path.read_text())
            payload["tenant"]["domain"] = "foreign.test"
            path.write_text(json.dumps(payload))
            path.chmod(0o600)
            with self.assertRaisesRegex(RuntimeError, "stale"):
                load_dev_up_evidence(root, config, tenant)

    def test_tenant_health_preserves_identity_around_all_suites(self) -> None:
        tenant = SimpleNamespace(name="tenant-a")
        identity = {"resources": {"cluster": "uid"}}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            kubeconfig = root / "kubeconfig"
            kubeconfig.write_text("config")
            with (
                patch(
                    "scripts.retained.tenant_kubeconfig_path",
                    return_value=kubeconfig,
                ),
                patch("scripts.retained.validate_tenant_kubeconfig_file"),
                patch("scripts.retained.verify_addon_source_ownership"),
                patch(
                    "scripts.retained.stable_tenant_snapshot",
                    side_effect=[identity, identity],
                ),
                patch(
                    "scripts.retained.collect_tenant_status",
                    return_value={"ready": True},
                ),
                patch("scripts.retained._dev_test_endpoint") as endpoint,
                patch("scripts.retained._dev_test_network") as network,
                patch("scripts.retained._dev_test_machines") as machines,
                patch("scripts.retained._dev_test_storage") as storage,
                patch("scripts.retained._dev_test_database") as database,
            ):
                self.assertTrue(
                    _tenant_is_healthy(
                        root,
                        {},
                        object(),
                        tenant,
                        {"metadata": {"uid": "cluster"}},
                        identity,
                    )
                )
            for suite in (endpoint, network, machines, storage, database):
                suite.assert_called_once()

    def test_tenant_health_rejects_complete_identity_mismatch(self) -> None:
        tenant = SimpleNamespace(name="tenant-a")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            kubeconfig = root / "kubeconfig"
            kubeconfig.write_text("config")
            with (
                patch(
                    "scripts.retained.tenant_kubeconfig_path",
                    return_value=kubeconfig,
                ),
                patch("scripts.retained.validate_tenant_kubeconfig_file"),
                patch("scripts.retained.verify_addon_source_ownership"),
                patch(
                    "scripts.retained.stable_tenant_snapshot",
                    return_value={"uid": "foreign"},
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "incompatible"):
                    _tenant_is_healthy(
                        root,
                        {},
                        object(),
                        tenant,
                        {"metadata": {"uid": "cluster"}},
                        {"uid": "expected"},
                    )

    def test_tenant_health_treats_missing_owned_addon_sources_as_repairable(
        self,
    ) -> None:
        tenant = SimpleNamespace(name="tenant-a")
        identity = {"uid": "expected"}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            kubeconfig = root / "kubeconfig"
            kubeconfig.write_text("config")
            with (
                patch(
                    "scripts.retained.tenant_kubeconfig_path",
                    return_value=kubeconfig,
                ),
                patch("scripts.retained.validate_tenant_kubeconfig_file"),
                patch(
                    "scripts.retained.verify_addon_source_ownership",
                    side_effect=[
                        None,
                        RuntimeError(
                            "tenant add-on sources are missing: configmap/source"
                        ),
                    ],
                ),
                patch(
                    "scripts.retained.stable_tenant_snapshot",
                    return_value=identity,
                ),
                patch(
                    "scripts.retained.collect_tenant_status",
                    return_value={"ready": True},
                ),
                patch("scripts.retained._dev_test_endpoint"),
                patch("scripts.retained._dev_test_machines"),
                patch("scripts.retained._dev_test_storage"),
            ):
                self.assertFalse(
                    _tenant_is_healthy(
                        root,
                        {},
                        object(),
                        tenant,
                        {"metadata": {"uid": "cluster"}},
                        identity,
                    )
                )

    def test_dev_test_runs_selected_suite_without_reconciliation(self) -> None:
        tenant = type("Tenant", (), {"name": "tenant-a"})()
        with (
            patch("scripts.retained.validate_retained_state"),
            patch("scripts.retained.ManagementClient", return_value=object()),
            patch("scripts.retained.configured_tenants", return_value=[tenant]),
            patch("scripts.retained._dev_test_network") as network,
        ):
            dev_test(Path("."), {}, "network")
        network.assert_called_once()

    def test_dev_test_rejects_unknown_suite(self) -> None:
        tenant = type("Tenant", (), {"name": "tenant-a"})()
        with (
            patch("scripts.retained.validate_retained_state"),
            patch("scripts.retained.ManagementClient", return_value=object()),
            patch("scripts.retained.configured_tenants", return_value=[tenant]),
        ):
            with self.assertRaisesRegex(RuntimeError, "unknown retained test"):
                dev_test(Path("."), {}, "unknown")

    def test_partial_namespace_or_storage_record_blocks_recreation(self) -> None:
        tenant = type(
            "Tenant", (), {"name": "tenant-a", "namespace": "tenant-a"}
        )()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                patch(
                    "scripts.retained.verify_tenant_management_ownership",
                    return_value={"cluster": None, "namespace": {}},
                ),
                patch("scripts.retained.inspect_storage_volume", return_value=None),
            ):
                with self.assertRaisesRegex(RuntimeError, "partial"):
                    _delete_representative_tenant(root, {}, object(), tenant)
            record = root / ".runtime/storage/tenant-a/volume.json"
            record.parent.mkdir(parents=True)
            record.write_text("{}")
            with (
                patch(
                    "scripts.retained.verify_tenant_management_ownership",
                    return_value={"cluster": None, "namespace": None},
                ),
                patch("scripts.retained.inspect_storage_volume", return_value=None),
            ):
                with self.assertRaisesRegex(RuntimeError, "partial"):
                    _delete_representative_tenant(root, {}, object(), tenant)

    def test_cluster_absent_journal_is_finished_before_recreation(self) -> None:
        tenant = type(
            "Tenant", (), {"name": "tenant-a", "namespace": "tenant-a"}
        )()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            journal = root / ".runtime/deletions/tenant-a.json"
            journal.parent.mkdir(parents=True)
            journal.write_text("{}")
            with (
                patch(
                    "scripts.retained.verify_tenant_management_ownership",
                    return_value={"cluster": None},
                ),
                patch("scripts.retained.finish_journaled_tenant_deletion") as finish,
            ):
                _delete_representative_tenant(root, {}, object(), tenant)
            finish.assert_called_once()

    def test_invalid_or_dangling_journal_blocks_recreation(self) -> None:
        tenant = type(
            "Tenant", (), {"name": "tenant-a", "namespace": "tenant-a"}
        )()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            journal = root / ".runtime/deletions/tenant-a.json"
            journal.parent.mkdir(parents=True)
            journal.symlink_to(root / "missing")
            with (
                patch(
                    "scripts.retained.verify_tenant_management_ownership",
                    return_value={"cluster": None},
                ),
                patch(
                    "scripts.retained.finish_journaled_tenant_deletion",
                    side_effect=RuntimeError("invalid journal"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "invalid journal"):
                    _delete_representative_tenant(root, {}, object(), tenant)

    def test_other_owned_resource_blocks_recreation(self) -> None:
        tenant = type(
            "Tenant", (), {"name": "tenant-a", "namespace": "tenant-a"}
        )()
        with tempfile.TemporaryDirectory() as temporary:
            with patch(
                "scripts.retained.verify_tenant_management_ownership",
                return_value={"cluster": None, "devMachineTemplate": {}},
            ):
                with self.assertRaisesRegex(RuntimeError, "partial"):
                    _delete_representative_tenant(
                        Path(temporary), {}, object(), tenant
                    )

    def test_clean_routes_through_authoritative_destroy(self) -> None:
        with patch("scripts.retained.destroy") as destroy:
            dev_clean(Path("."), {})
        destroy.assert_called_once()


if __name__ == "__main__":
    unittest.main()
