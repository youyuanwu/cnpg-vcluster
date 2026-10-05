from __future__ import annotations

import json
import time
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts.lib.azure.gate import (
    WorkerSnapshot,
    build_worker_snapshot,
    require_owned_resource_delta,
    require_replacement,
)
from scripts.test_azure_tenant_lifecycle import (
    _admin_create_tenant,
    _admin_delete_tenant,
    _admin_mutation,
    _ensure_tenant_ready,
    _install_database_runtime,
    _require_allocation_lease_absent,
    _require_disks_absent,
    _require_runtime_identity,
    _disk_records,
    _require_recreated_identity,
    _wait_databases_ready,
    _wait_database_capability,
    main,
)
from scripts.lib.azure.operator import tenant_document


VMSS = (
    "/subscriptions/00000000-0000-0000-0000-000000000000/"
    "resourceGroups/rg/providers/Microsoft.Compute/"
    "virtualMachineScaleSets/tenant-c-worker"
)


def instance(identifier: int) -> str:
    return f"{VMSS}/virtualMachines/{identifier}"


def readiness(identifiers=(0, 1, 2)):
    nodes = [
        {
            "name": f"node-{identifier}",
            "uid": f"uid-{identifier}",
            "providerID": f"azure://{instance(identifier)}",
            "internalIP": f"10.220.16.{identifier + 4}",
        }
        for identifier in identifiers
    ]
    return {
        "requestedWorkers": 3,
        "readyReplicas": 3,
        "nodeRefs": [item["name"] for item in nodes],
        "nodes": nodes,
    }


def runtime_tenant(spec):
    document = tenant_document(spec)
    document["metadata"].update({"uid": "tenant-uid", "generation": 1})
    document["status"] = {
        "phase": "Ready",
        "observedGeneration": 1,
        "conditions": [
            {"type": "Ready", "status": "True", "observedGeneration": 1},
        ],
        "provider": {
            "type": "azure",
            "binding": {"tenantUID": "tenant-uid", "operationId": "operation-1"},
            "management": {"namespaceUID": "namespace-uid", "clusterUID": "cluster-uid"},
            "kubeconfig": {"secretUID": "secret-uid", "contentSha256": "digest"},
        },
        "databaseCapability": {"available": False},
    }
    return document


class AzureGateTests(unittest.TestCase):
    def test_unknown_create_outcome_restarts_controller_once(self):
        blocked = {
            "databases": [
                {
                    "phase": "progressing",
                    "readyInstances": 0,
                    "blockers": [{"code": "UnknownCreateOutcome"}],
                },
                {"phase": "ready", "readyInstances": 3, "blockers": []},
                {"phase": "ready", "readyInstances": 3, "blockers": []},
            ],
        }
        ready = {
            "databases": [
                {"phase": "ready", "readyInstances": 3, "blockers": []},
                {"phase": "ready", "readyInstances": 3, "blockers": []},
                {"phase": "ready", "readyInstances": 3, "blockers": []},
            ],
        }
        catalog = SimpleNamespace(read=Mock(side_effect=[blocked, ready]))
        with (
            patch(
                "scripts.test_azure_tenant_lifecycle._restart_database_controller"
            ) as restart,
            patch(
                "scripts.test_azure_tenant_lifecycle.ready_entries",
                return_value={"alpha": {}, "beta": {}, "gamma": {}},
            ),
            patch(
                "scripts.test_azure_tenant_lifecycle.time.monotonic",
                side_effect=[0, 1],
            ),
            patch("scripts.test_azure_tenant_lifecycle.time.sleep"),
        ):
            result = _wait_databases_ready(catalog, "catalog-uid", 30)
        restart.assert_called_once_with()
        self.assertEqual(set(result), {"alpha", "beta", "gamma"})

    def test_runtime_install_is_bounded_and_precedes_capability_and_catalog(self):
        spec = SimpleNamespace(name="tenant-c", workers=3, kubernetes_version="1.32.13")
        tenant = runtime_tenant(spec)
        config = {"AZURE_TENANT_TIMEOUT": "20m"}
        events = []
        installed = False

        def inventory(*_args, **_kwargs):
            events.append("inventory")
            return CompletedProcess(
                [], 0,
                json.dumps({"kind": "TenantList", "metadata": {}, "items": [tenant]}),
                "",
            )

        def installer(command, **kwargs):
            nonlocal installed
            events.append("install")
            self.assertEqual(
                ["timeout", "--kill-after=5s", "420s",
                 "just", "azure-database-runtime-once"],
                command,
            )
            self.assertLessEqual(kwargs["timeout"], 435)
            self.assertGreater(kwargs["timeout"], 0)
            installed = True
            return CompletedProcess(command, 0, "", "")

        def read(*_args):
            events.append("capability")
            current = json.loads(json.dumps(tenant))
            current["status"]["databaseCapability"]["available"] = installed
            return current

        def catalog(*_args):
            events.append("catalog")
            raise RuntimeError("stop before database mutations")

        def snapshot(*_args):
            events.append("worker")
            return tenant, None, None

        with (
            patch("scripts.test_azure_tenant_lifecycle.os.umask"),
            patch("scripts.test_azure_tenant_lifecycle.load_azure_configuration",
                  return_value=config),
            patch("scripts.test_azure_tenant_lifecycle.supported_versions",
                  return_value=("1.32.13",)),
            patch("scripts.test_azure_tenant_lifecycle.load_tenant_spec",
                  return_value=spec),
            patch("scripts.test_azure_tenant_lifecycle._source_sha256",
                  return_value="source"),
            patch("scripts.test_azure_tenant_lifecycle._inspect_foundation",
                  return_value=({"resourceGroupId": "rg"}, None)),
            patch("scripts.test_azure_tenant_lifecycle._require_clean_tagged_foundation"),
            patch("scripts.test_azure_tenant_lifecycle._ensure_tenant_ready",
                  side_effect=lambda *_args: events.append("ready")),
            patch("scripts.test_azure_tenant_lifecycle._ready_snapshot",
                  side_effect=snapshot),
            patch("scripts.test_azure_tenant_lifecycle._kubectl", side_effect=inventory),
            patch("scripts.test_azure_tenant_lifecycle.run", side_effect=installer),
            patch("scripts.test_azure_tenant_lifecycle.read_tenant", side_effect=read),
            patch("scripts.test_azure_tenant_lifecycle.wait_for",
                  side_effect=lambda _description, _timeout, _interval, predicate:
                  self.assertIsNotNone(predicate())),
            patch("scripts.test_azure_tenant_lifecycle._catalog_client",
                  side_effect=catalog),
            self.assertRaisesRegex(RuntimeError, "stop before database mutations"),
        ):
            main([])
        self.assertLess(events.index("ready"), events.index("worker"))
        self.assertLess(events.index("worker"), events.index("install"))
        self.assertLess(events.index("install"), events.index("capability"))
        self.assertLess(events.index("capability"), events.index("catalog"))
        self.assertEqual(2, events.count("inventory"))

    def test_runtime_failure_blocks_capability_and_catalog(self):
        spec = SimpleNamespace(name="tenant-c", workers=3, kubernetes_version="1.32.13")
        tenant = runtime_tenant(spec)
        inventory = CompletedProcess(
            [], 0,
            json.dumps({"kind": "TenantList", "metadata": {}, "items": [tenant]}),
            "",
        )
        with (
            patch("scripts.test_azure_tenant_lifecycle._kubectl",
                  return_value=inventory),
            patch("scripts.test_azure_tenant_lifecycle.run",
                  side_effect=RuntimeError("CNPG/CSI install failed")) as installer,
            patch("scripts.test_azure_tenant_lifecycle.wait_for") as capability,
            patch("scripts.test_azure_tenant_lifecycle._catalog_client") as catalog,
            self.assertRaisesRegex(RuntimeError, "CNPG/CSI install failed"),
        ):
            _install_database_runtime(spec, tenant, time.monotonic() + 60)
            _wait_database_capability(spec, tenant, time.monotonic() + 60)
            catalog(spec.name, "tenant-uid")
        installer.assert_called_once()
        capability.assert_not_called()
        catalog.assert_not_called()

    def test_runtime_rejects_replaced_tenant_before_and_after_install(self):
        spec = SimpleNamespace(name="tenant-c", workers=3, kubernetes_version="1.32.13")
        tenant = runtime_tenant(spec)
        replacement = json.loads(json.dumps(tenant))
        replacement["metadata"]["uid"] = "replacement-uid"
        with self.assertRaisesRegex(RuntimeError, "identity"):
            _require_runtime_identity(spec, tenant, replacement)
        inventory = lambda item: CompletedProcess(
            [], 0,
            json.dumps({"kind": "TenantList", "metadata": {}, "items": [item]}),
            "",
        )
        with (
            patch("scripts.test_azure_tenant_lifecycle._kubectl",
                  return_value=inventory(replacement)),
            patch("scripts.test_azure_tenant_lifecycle.run") as installer,
            self.assertRaisesRegex(RuntimeError, "identity"),
        ):
            _install_database_runtime(spec, tenant, time.monotonic() + 60)
        installer.assert_not_called()
        with (
            patch("scripts.test_azure_tenant_lifecycle._kubectl",
                  side_effect=[inventory(tenant), inventory(replacement)]),
            patch("scripts.test_azure_tenant_lifecycle.run",
                  return_value=CompletedProcess([], 0, "", "")) as installer,
            self.assertRaisesRegex(RuntimeError, "identity"),
        ):
            _install_database_runtime(spec, tenant, time.monotonic() + 60)
        installer.assert_called_once()

    def test_runtime_capability_wait_fails_on_identity_drift_or_timeout(self):
        spec = SimpleNamespace(name="tenant-c", workers=3, kubernetes_version="1.32.13")
        tenant = runtime_tenant(spec)
        replacement = json.loads(json.dumps(tenant))
        replacement["status"]["provider"]["kubeconfig"]["secretUID"] = "other-secret"
        with (
            patch("scripts.test_azure_tenant_lifecycle.read_tenant",
                  return_value=replacement),
            patch("scripts.test_azure_tenant_lifecycle.wait_for",
                  side_effect=lambda _name, _timeout, _interval, predicate: predicate()),
            self.assertRaisesRegex(RuntimeError, "binding changed"),
        ):
            _wait_database_capability(spec, tenant, time.monotonic() + 60)
        with (
            patch("scripts.test_azure_tenant_lifecycle._kubectl") as inventory,
            patch("scripts.test_azure_tenant_lifecycle.run") as installer,
            self.assertRaisesRegex(RuntimeError, "timed out"),
        ):
            _install_database_runtime(spec, tenant, time.monotonic() - 1)
        inventory.assert_not_called()
        installer.assert_not_called()

    def test_disk_proof_requires_three_distinct_disks_per_entry_and_arm_404(self):
        uids = ("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                "cccccccc-cccc-4ccc-8ccc-cccccccccccc")
        entries = {
            uid: {"storage": [
                {
                    "ordinal": ordinal,
                    "disk": {"name": f"disk-{index}-{ordinal}",
                             "uid": f"disk-uid-{index}-{ordinal}"},
                    "armID": f"/subscriptions/sub/resourceGroups/rg/providers/"
                             f"Microsoft.Compute/disks/disk-{index}-{ordinal}",
                } for ordinal in (1, 2, 3)
            ]} for index, uid in enumerate(uids)
        }
        with patch("scripts.test_azure_tenant_lifecycle._catalog_record",
                   return_value={"status": {"entries": entries}}):
            disks = _disk_records(
                "tenant-a", set(uids), "/subscriptions/sub/resourceGroups/rg",
            )
        self.assertEqual(9, len(disks))
        with (
            patch("scripts.test_azure_tenant_lifecycle._kubectl",
                  return_value=CompletedProcess([], 0, "", "")) as kubectl,
            patch("scripts.test_azure_tenant_lifecycle._az",
                  return_value=CompletedProcess([], 1, "", "(404) ResourceNotFound")) as arm,
        ):
            _require_disks_absent({}, "tenant-a", disks)
            self.assertEqual(9, kubectl.call_count)
            self.assertEqual(9, arm.call_count)
        with (
            patch("scripts.test_azure_tenant_lifecycle._kubectl",
                  return_value=CompletedProcess([], 0, "", "")),
            patch("scripts.test_azure_tenant_lifecycle._az",
                  return_value=CompletedProcess([], 1, "", "Forbidden")),
            self.assertRaisesRegex(RuntimeError, "absence is unproven"),
        ):
            _require_disks_absent({}, "tenant-a", disks)

    def test_admin_mutation_uses_authenticated_json_transport(self) -> None:
        config = CompletedProcess([], 0, "{}", "")
        response = CompletedProcess(
            [],
            0,
            json.dumps({"schemaVersion": 7, "data": {"state": "accepted"}}),
            "",
        )
        with (
            patch(
                "scripts.test_azure_tenant_lifecycle._kubectl",
                return_value=config,
            ) as kubectl,
            patch(
                "scripts.test_azure_tenant_lifecycle.kubeconfig_json_request",
                return_value=response,
            ) as request,
        ):
            self.assertEqual(
                {"state": "accepted"},
                _admin_mutation("DELETE", "api/v1/tenants/tenant-c", {
                    "uid": "uid-c",
                    "confirmation": "tenant-c",
                }),
            )
        self.assertIn("--flatten", kubectl.call_args.args)
        request.assert_called_once()
        self.assertEqual("DELETE", request.call_args.args[1])
        self.assertEqual(
            {"uid": "uid-c", "confirmation": "tenant-c"},
            request.call_args.args[3],
        )

    def test_admin_lifecycle_responses_are_exact_and_credential_free(self) -> None:
        spec = type(
            "Spec",
            (),
            {
                "name": "tenant-c",
                "workers": 3,
                "kubernetes_version": "1.32.13",
            },
        )()
        with (
            patch(
                "scripts.test_azure_tenant_lifecycle._admin_mutation",
                return_value={
                    "identity": {
                        "name": "tenant-c",
                        "uid": "uid",
                        "generation": True,
                    },
                    "provider": "azure",
                    "kubernetesVersion": "1.32.13",
                    "password": "forbidden",
                },
            ),
            self.assertRaisesRegex(RuntimeError, "create response"),
        ):
            _admin_create_tenant(spec)
        with (
            patch(
                "scripts.test_azure_tenant_lifecycle._admin_mutation",
                return_value={
                    "identity": {
                        "name": "tenant-c",
                        "uid": "uid",
                        "generation": "bad",
                    },
                    "state": "accepted",
                },
            ),
            self.assertRaisesRegex(RuntimeError, "delete response"),
        ):
            _admin_delete_tenant("tenant-c", "uid")

    def test_recreated_identity_requires_new_nonempty_tenant_and_lease_uids(self):
        valid = {
            "metadata": {"uid": "tenant-new"},
            "status": {
                "provider": {
                    "type": "azure",
                    "networkAllocation": {"leaseUID": "lease-new"},
                }
            },
        }
        _require_recreated_identity(valid, "tenant-old", "lease-old")
        for value in (None, "", "lease-old"):
            invalid = json.loads(json.dumps(valid))
            if value is None:
                invalid["status"]["provider"]["networkAllocation"].pop("leaseUID")
            else:
                invalid["status"]["provider"]["networkAllocation"]["leaseUID"] = value
            with self.subTest(value=value), self.assertRaisesRegex(
                RuntimeError, "retained old identity"
            ):
                _require_recreated_identity(
                    invalid,
                    "tenant-old",
                    "lease-old",
                )

    def test_allocation_release_proof_rejects_retained_lease(self):
        with (
            patch(
                "scripts.test_azure_tenant_lifecycle._kubectl",
                return_value=type(
                    "Result",
                    (),
                    {"stdout": "lease.coordination.k8s.io/tenant-azure-slot-old\n"},
                )(),
            ),
            self.assertRaisesRegex(RuntimeError, "Lease remained"),
        ):
            _require_allocation_lease_absent("tenant-azure-slot-old")

    def test_ready_gate_requires_a_clean_tenant_identity(self) -> None:
        spec = type("Spec", (), {"name": "tenant-c"})()
        events = []
        with (
            patch(
                "scripts.test_azure_tenant_lifecycle.read_tenant",
                return_value={
                    "metadata": {
                        "name": "tenant-c",
                        "deletionTimestamp": "2026-09-29T00:00:00Z",
                    }
                },
            ),
            patch(
                "scripts.test_azure_tenant_lifecycle._admin_create_tenant",
                side_effect=lambda observed: events.append(observed),
            ),
            patch(
                "scripts.test_azure_tenant_lifecycle.wait_tenant_ready",
                side_effect=lambda *_args: events.append("ready"),
            ),
            patch(
                "scripts.test_azure_tenant_lifecycle._require_status",
                return_value={"classification": "ready"},
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "absent Tenant"):
                _ensure_tenant_ready({}, spec)
        self.assertEqual([], events)
        with (
            patch("scripts.test_azure_tenant_lifecycle.read_tenant", return_value=None),
            patch("scripts.test_azure_tenant_lifecycle._admin_create_tenant",
                  side_effect=lambda observed: events.append(observed)),
            patch("scripts.test_azure_tenant_lifecycle.wait_tenant_ready",
                  side_effect=lambda *_args: events.append("ready")),
            patch("scripts.test_azure_tenant_lifecycle._require_status",
                  return_value={"classification": "ready"}),
        ):
            self.assertEqual({"classification": "ready"}, _ensure_tenant_ready({}, spec))
        self.assertEqual([spec, "ready"], events)

    def test_exact_mapping_checkpoint_and_non_primary_selection(self) -> None:
        snapshot = build_worker_snapshot(
            readiness(),
            VMSS,
            [instance(0), instance(1), instance(2)],
        )
        self.assertEqual("0", snapshot.primary.instance_id)
        self.assertEqual("2", snapshot.target.instance_id)
        self.assertEqual(snapshot, WorkerSnapshot.from_mapping(snapshot.to_mapping()))

    def test_mapping_fails_closed_on_ambiguous_identity(self) -> None:
        payload = readiness()
        payload["nodes"][2]["uid"] = payload["nodes"][1]["uid"]
        with self.assertRaisesRegex(RuntimeError, "duplicated"):
            build_worker_snapshot(
                payload,
                VMSS,
                [instance(0), instance(1), instance(2)],
            )
        with self.assertRaisesRegex(RuntimeError, "exactly three"):
            build_worker_snapshot(
                readiness(),
                VMSS,
                [instance(0), instance(1)],
            )

    def test_recovery_requires_unchanged_survivors_and_one_replacement(self) -> None:
        before = build_worker_snapshot(
            readiness(),
            VMSS,
            [instance(0), instance(1), instance(2)],
        )
        after = build_worker_snapshot(
            readiness((0, 1, 3)),
            VMSS,
            [instance(0), instance(1), instance(3)],
        )
        deleted, replacement = require_replacement(before, after)
        self.assertEqual("2", deleted.instance_id)
        self.assertEqual("3", replacement.instance_id)
        reused = readiness((0, 1, 2))
        reused["nodes"][2]["name"] = "replacement"
        reused["nodes"][2]["uid"] = "replacement-uid"
        reused["nodeRefs"][2] = "replacement"
        with self.assertRaisesRegex(RuntimeError, "reused"):
            require_replacement(
                before,
                build_worker_snapshot(
                    reused,
                    VMSS,
                    [instance(0), instance(1), instance(2)],
                ),
            )

    def test_owned_delta_allows_only_vm_and_associated_nic_replacement(self) -> None:
        before = build_worker_snapshot(
            readiness(),
            VMSS,
            [instance(0), instance(1), instance(2)],
        )
        after = build_worker_snapshot(
            readiness((0, 1, 3)),
            VMSS,
            [instance(0), instance(1), instance(3)],
        )
        deleted, replacement = require_replacement(before, after)
        stable = {
            "id": VMSS,
            "type": "microsoft.compute/virtualmachinescalesets",
        }
        recorded = {
            "azure": [
                stable,
                {
                    "id": instance(2),
                    "type": "microsoft.compute/virtualmachinescalesets/virtualmachines",
                },
                {
                    "id": f"{instance(2)}/networkInterfaces/old",
                    "type": "microsoft.network/networkinterfaces",
                    "virtualMachineId": instance(2),
                },
            ],
            "provider": [
                {
                    "apiVersion": "network.azure.com/v1",
                    "kind": "PublicIPAddress",
                    "name": "tenant-c",
                    "uid": "uid",
                    "resourceId": "/public-ip",
                }
            ],
        }
        discovered = {
            "azure": [
                stable,
                {
                    "id": instance(3),
                    "type": "microsoft.compute/virtualmachinescalesets/virtualmachines",
                },
                {
                    "id": f"{instance(3)}/networkInterfaces/new",
                    "type": "microsoft.network/networkinterfaces",
                    "virtualMachineId": instance(3),
                },
            ],
            "provider": list(recorded["provider"]),
        }
        require_owned_resource_delta(recorded, discovered, deleted, replacement)
        discovered["azure"].append(
            {
                "id": "/unrelated",
                "type": "microsoft.network/publicipaddresses",
            }
        )
        with self.assertRaisesRegex(RuntimeError, "unrelated"):
            require_owned_resource_delta(
                recorded, discovered, deleted, replacement
            )

    def test_gate_source_contains_only_explicit_failure_injection(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source = (root / "scripts" / "test_azure_tenant_lifecycle.py").read_text()
        self.assertEqual(1, source.count('"delete-instances"'))
        self.assertIn("ordinary-tenant-deletion", source)
        self.assertIn("external-absence-proof", source)
        self.assertEqual(2, source.count("_source_sha256(spec_path)"))
        self.assertEqual(3, source.count("_admin_create_tenant"))
        self.assertNotIn('_tenant_command("create"', source)
        self.assertIn('ROOT / ".runtime" / "azure-tenant-lifecycle"', source)
        self.assertIn("tempfile.TemporaryDirectory(dir=scratch)", source)
        self.assertNotIn("checkpoint", source.lower())
        self.assertNotIn("persist_evidence", source)
        self.assertNotIn("_incomplete_gate", source)
        self.assertLess(
            source.index('"worker-identity-verification"'),
            source.index('"worker-instance-deletion"'),
        )
        self.assertLess(
            source.index("capture_operator_deletion_proof"),
            source.index('"ordinary-tenant-deletion"'),
        )
