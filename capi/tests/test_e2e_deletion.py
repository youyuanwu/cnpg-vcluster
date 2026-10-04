from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import Mock, patch

from scripts.test_e2e import (
    _restart_worker_and_verify_markers,
    _wait_databases_ready,
    _verify_restarts_and_markers,
    capture_tenant_deletion_identity,
    verify_tenant_deletion,
)
from scripts.lib.catalog_lifecycle import CatalogClient
from scripts.lib.controller_scenarios import LEASE, MANAGEMENT_CATALOG
from scripts.lib.kube import wait_for as actual_wait_for
from tests.test_controller_scenarios import allocation_lease, lease_inventory


def response(payload=None, *, error: str = "") -> CompletedProcess:
    return CompletedProcess(
        [], 1 if error else 0,
        stdout="" if payload is None else json.dumps(payload),
        stderr=error,
    )


class TenantDeletionProofTests(unittest.TestCase):
    def setUp(self) -> None:
        self.identity = {
            "name": "tenant-a",
            "uid": "tenant-uid",
            "clusterUID": "cluster-uid",
            "foundationHash": "foundation",
            "allocationLease": {
                "apiVersion": LEASE.api_version,
                "kind": LEASE.kind,
                **allocation_lease()["metadata"],
            },
            "managementResources": [
                ("v1", "Namespace", "", "tenant-a", "namespace-uid"),
                ("cluster.x-k8s.io/v1beta2", "Cluster", "tenant-a", "tenant-a", "cluster-uid"),
                ("infrastructure.cluster.x-k8s.io/v1beta2", "DevCluster", "tenant-a", "tenant-a", "devcluster-uid"),
                ("controlplane.cluster.x-k8s.io/v1alpha2", "KamajiControlPlane", "tenant-a", "tenant-a", "controlplane-uid"),
            ],
            "workerContainers": ["worker-a " + "a" * 64],
            "providerContainers": ["worker-a " + "a" * 64, "tenant-a-lb " + "b" * 64],
            "dockerVolume": {"name": "lab-tenant-a-storage"},
        }
        self.client = Mock()
        self.client.kubectl.side_effect = self.empty_inventory
        self.document = {"metadata": {"name": "tenant-a", "uid": "tenant-uid"}}

    @staticmethod
    def empty_inventory(*args, **_kwargs):
        raw = next(
            (arg.removeprefix("--raw=") for arg in args if arg.startswith("--raw=")),
            None,
        )
        if raw == LEASE.inventory_path:
            return response(lease_inventory([]))
        if raw is not None:
            return response(error="NotFound")
        return response()

    def inventory_with_tenant(self, payload):
        def handler(*args, **kwargs):
            if any(arg.startswith("--raw=") for arg in args):
                return self.empty_inventory(*args, **kwargs)
            return response(payload)
        return handler

    def test_capture_rechecks_tenant_uid_and_records_live_snapshot_and_allocation(self) -> None:
        current = copy.deepcopy(self.document)
        current["metadata"]["resourceVersion"] = "latest"
        self.client.kubectl.side_effect = self.inventory_with_tenant(current)
        with patch("scripts.test_e2e.tenant_snapshot", return_value=self.identity) as snapshot:
            captured = capture_tenant_deletion_identity(
                {"LAB_PREFIX": "lab"}, self.client, self.document,
            )
        snapshot.assert_called_once_with({"LAB_PREFIX": "lab"}, self.client, current)
        self.assertEqual("tenant-uid", captured["uid"])
        self.assertEqual(self.identity["allocationLease"], captured["allocationLease"])

    def test_unknown_create_outcome_restarts_database_controller_once(self) -> None:
        blocked = {
            "databases": [
                {
                    "name": "alpha",
                    "phase": "progressing",
                    "blockers": [{"code": "UnknownCreateOutcome"}],
                },
                {"name": "beta", "phase": "ready", "blockers": []},
                {"name": "gamma", "phase": "ready", "blockers": []},
            ],
        }
        ready = {
            "databases": [
                {"name": name, "phase": "ready", "blockers": []}
                for name in ("alpha", "beta", "gamma")
            ],
        }
        catalog = Mock()
        catalog.read.side_effect = [blocked, ready]
        with (
            patch("scripts.test_e2e._restart_database_controller") as restart,
            patch("scripts.test_e2e.time.monotonic", side_effect=[0, 1]),
            patch("scripts.test_e2e.time.sleep"),
        ):
            result = _wait_databases_ready(
                self.client,
                catalog,
                "catalog-uid",
                {"alpha", "beta", "gamma"},
                30,
            )
        restart.assert_called_once_with(self.client)
        self.assertEqual(ready, result)

    def test_capture_rejects_same_name_replacement_or_missing_tenant(self) -> None:
        for payload in (None, {"metadata": {"name": "tenant-a", "uid": "replacement"}}):
            with self.subTest(payload=payload):
                self.client.kubectl.side_effect = self.inventory_with_tenant(payload)
                with patch("scripts.test_e2e.tenant_snapshot") as snapshot:
                    with self.assertRaisesRegex(RuntimeError, "identity changed"):
                        capture_tenant_deletion_identity({}, self.client, self.document)
                    snapshot.assert_not_called()

    def test_capture_propagates_mismatched_lease_identity(self) -> None:
        self.client.kubectl.side_effect = self.inventory_with_tenant(self.document)
        with patch("scripts.test_e2e.tenant_snapshot", side_effect=RuntimeError("Lease identity changed")):
            with self.assertRaisesRegex(RuntimeError, "Lease identity changed"):
                capture_tenant_deletion_identity({"LAB_PREFIX": "lab"}, self.client, self.document)

    def test_capture_rejects_replaced_provider_root_or_missing_host_identity(self) -> None:
        for field, value in (
            ("clusterUID", "replaced-cluster"),
            ("workerContainers", []),
            ("dockerVolume", {"name": "foreign-volume"}),
        ):
            with self.subTest(field=field):
                identity = {**self.identity, field: value}
                self.client.kubectl.side_effect = self.inventory_with_tenant(self.document)
                with patch("scripts.test_e2e.tenant_snapshot", return_value=identity):
                    with self.assertRaisesRegex(RuntimeError, "provider deletion identity is incomplete"):
                        capture_tenant_deletion_identity({"LAB_PREFIX": "lab"}, self.client, self.document)

    def test_absence_is_checked_for_exact_management_names_and_docker_identities(self) -> None:
        with patch("scripts.test_e2e.run", return_value=response()) as docker:
            verify_tenant_deletion(self.client, self.identity)
        calls = [call.args for call in self.client.kubectl.call_args_list]
        cluster = next(
            resource for resource in MANAGEMENT_CATALOG
            if resource.kind == "Cluster"
        )
        self.assertIn(
            ("get", f"--raw={cluster.object_path('tenant-a', 'tenant-a')}"),
            calls,
        )
        self.assertEqual(["docker", "ps", "-aq", "--no-trunc"], docker.call_args_list[0].args[0])
        self.assertIn(
            "label=io.x-k8s.kind.cluster=tenant-a",
            docker.call_args_list[1].args[0],
        )
        self.assertEqual(
            ["docker", "volume", "ls", "--format", "{{.Name}}"],
            docker.call_args_list[2].args[0],
        )

    def test_namespace_and_each_provider_root_must_be_absent(self) -> None:
        for api_version, kind, namespace, name, uid in self.identity["managementResources"]:
            with self.subTest(kind=kind):
                resource = next(
                    resource for resource in MANAGEMENT_CATALOG
                    if resource.api_version == api_version and resource.kind == kind
                )
                target = resource.object_path(namespace or None, name)

                def inspect(*args, **_kwargs):
                    raw = next(
                        (arg.removeprefix("--raw=") for arg in args if arg.startswith("--raw=")),
                        None,
                    )
                    if raw == target:
                        return response({
                            "apiVersion": api_version,
                            "kind": kind,
                            "metadata": {"name": name, "uid": uid},
                        })
                    return self.empty_inventory(*args, **_kwargs)

                self.client.kubectl.side_effect = inspect
                with patch("scripts.test_e2e.run") as docker:
                    with self.assertRaisesRegex(RuntimeError, "management resource remained"):
                        verify_tenant_deletion(self.client, self.identity)
                    docker.assert_not_called()

    def test_leases_are_checked_by_exact_name_uid_and_tenant_markers(self) -> None:
        for field in ("name", "uid", "tenant", "tenant-uid"):
            with self.subTest(field=field):
                lease = allocation_lease()
                lease["metadata"].update(name="foreign-name", uid="foreign-uid")
                lease["metadata"]["annotations"].update({
                    "tenancy.cnpg-vcluster.io/tenant": "other",
                    "tenancy.cnpg-vcluster.io/tenant-uid": "other",
                })
                if field in ("name", "uid"):
                    lease["metadata"][field] = self.identity["allocationLease"][field]
                else:
                    lease["metadata"]["annotations"]["tenancy.cnpg-vcluster.io/" + field] = (
                        "tenant-a" if field == "tenant" else "tenant-uid"
                    )
                self.client.kubectl.side_effect = lambda *args, **kwargs: (
                    response(lease_inventory([lease]))
                    if f"--raw={LEASE.inventory_path}" in args
                    else self.empty_inventory(*args, **kwargs)
                )
                with self.assertRaisesRegex(RuntimeError, "Lease remained"):
                    verify_tenant_deletion(self.client, self.identity)

    def test_malformed_allocation_state_is_not_treated_as_absence(self) -> None:
        for payload in ({}, {"items": None}, {"items": [{}]}):
            with self.subTest(payload=payload):
                self.client.kubectl.side_effect = lambda *args, **kwargs: (
                    response(payload)
                    if f"--raw={LEASE.inventory_path}" in args
                    else self.empty_inventory(*args, **kwargs)
                )
                with self.assertRaisesRegex(RuntimeError, "invalid allocation Lease"):
                    verify_tenant_deletion(self.client, self.identity)

    def test_inspection_failure_is_redacted_and_not_treated_as_absence(self) -> None:
        candidate = "inspection-candidate-secret"
        self.client.kubectl.side_effect = None
        self.client.kubectl.return_value = response(error=f"forbidden password={candidate}")
        with self.assertRaisesRegex(RuntimeError, "failed to inspect") as failure:
            verify_tenant_deletion(self.client, self.identity)
        self.assertNotIn(candidate, str(failure.exception))
        self.assertIn("REDACTED", str(failure.exception))

    def test_docker_failures_at_each_inspection_fail_closed(self) -> None:
        for index in range(3):
            with self.subTest(index=index):
                with patch(
                    "scripts.lib.process.subprocess.run",
                    side_effect=[response()] * index + [response(error="Docker daemon unavailable")],
                ):
                    with self.assertRaisesRegex(RuntimeError, "Docker daemon unavailable"):
                        verify_tenant_deletion(self.client, self.identity)

    def test_original_relabelled_worker_new_scoped_worker_or_exact_volume_is_a_leak(self) -> None:
        for outputs in (
            ["a" * 64, "", ""],
            ["b" * 64, "", ""],
            ["", "new-scoped-worker", ""],
            ["", "", "lab-tenant-a-storage"],
        ):
            with self.subTest(outputs=outputs):
                with patch("scripts.test_e2e.run", side_effect=[
                    CompletedProcess([], 0, stdout=value + "\n", stderr="") for value in outputs
                ]):
                    with self.assertRaisesRegex(RuntimeError, "remained after deletion"):
                        verify_tenant_deletion(self.client, self.identity)


class WorkerRestartPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(".")
        self.config = {
            "OWNERSHIP_LABEL": "lab-owner",
            "LAB_PREFIX": "lab",
            "COMMAND_TIMEOUT": "30s",
            "WORKER_REGISTRATION_TIMEOUT": "60s",
            "WAIT_POLL_INTERVAL": "1s",
            "CNPG_TIMEOUT": "60s",
        }
        self.tenant = type("Tenant", (), {
            "name": "tenant-example", "workers": 3,
            "storage_host_path": Path("/owned/storage"),
        })()
        self.uid = "tenant-uid"
        self.container_id = "a" * 64
        self.worker = "tenant-example-worker-a"
        self.workers = {
            self.worker: {
                "machineUID": "machine-uid",
                "devMachineUID": "devmachine-uid",
                "nodeUID": "node-uid",
                "containerID": self.container_id,
            },
            "tenant-example-worker-b": {
                "machineUID": "machine-b", "devMachineUID": "dev-b",
                "nodeUID": "node-b", "containerID": "b" * 64,
            },
            "tenant-example-worker-c": {
                "machineUID": "machine-c", "devMachineUID": "dev-c",
                "nodeUID": "node-c", "containerID": "c" * 64,
            },
        }
        self.entries = {
            name: {
                "logicalUid": f"{name}-uid",
                "clusterUid": f"{name}-cluster-uid",
                "instanceTopology": [
                    {"name": f"{name}-primary", "uid": f"{name}-pod-uid",
                     "role": "primary", "ready": True},
                ],
            }
            for name in ("alpha", "beta", "gamma")
        }
        self.events: list[str] = []
        self.marker_results: dict[str, list[str]] = {
            name: [f"marker-{name}"] for name in self.entries
        }
        self.transient_unavailable: dict[str, int] = {}
        self.query_error: dict[str, str] = {}
        self.started_at = "2026-10-01T12:00:00Z"
        self.restart_changes_started_at = True

        def mutate(_method, path, payload):
            name = next(
                name for name, entry in self.entries.items()
                if entry["logicalUid"] == payload["logicalUid"]
            )
            self.assertTrue(path.endswith(f"/{payload['logicalUid']}/query"))
            self.events.append(
                f"{'write' if payload['sql'].startswith('CREATE TABLE') else 'read'}:{name}"
            )
            if self.transient_unavailable.get(name, 0):
                self.transient_unavailable[name] -= 1
                return response(error="HTTP 503: Service Unavailable")
            if name in self.query_error:
                return response(error=self.query_error[name])
            values = self.marker_results[name]
            return response({
                "schemaVersion": 5,
                "data": {
                    "catalogUid": "catalog-uid", "logicalUid": payload["logicalUid"],
                    "instance": payload["instance"], "instanceUid": payload["instanceUid"],
                    "executedAt": "now", "durationMs": 1, "truncated": False,
                    "results": [{
                        "columns": ["value"], "rows": [[value] for value in values],
                        "affectedRows": len(values), "truncated": False,
                    }],
                },
            })

        self.catalog = CatalogClient(lambda _path: "", mutate, self.tenant.name, self.uid)
        def read(expected_uid):
            self.assertEqual(expected_uid, "catalog-uid")
            self.events.append("catalog-read")
            return {
                "catalogUid": expected_uid, "closed": False,
                "capabilityAvailable": True,
                "databases": [
                    {"name": name, "logicalUid": entry["logicalUid"],
                     "phase": "ready", "readyInstances": 3}
                    for name, entry in self.entries.items()
                ],
            }
        self.catalog.read = read

        def wait(predicate, _timeout, _uid):
            self.events.append("catalog-ready")
            ready = {
                "databases": [
                    {"phase": "ready", "readyInstances": 3}
                    for _ in self.entries
                ],
            }
            if not predicate(ready):
                raise AssertionError("all three databases must be Ready")
            return ready

        self.catalog.wait = wait
        self.client = Mock()

    def docker(self, command, **_kwargs):
        if command[:3] == ["docker", "volume", "inspect"]:
            self.events.append("volume")
            return response([{
                "Name": "lab-tenant-example-storage",
                "Labels": {
                    "lab-owner": "lab",
                    "cnpg-vcluster.capi/role": "tenant-storage",
                    "cnpg-vcluster.capi/tenant": "tenant-example",
                },
                "Mountpoint": "/owned/storage",
            }])
        if command[:2] == ["docker", "inspect"]:
            self.events.append("inspect")
            self.assertEqual(command[2], self.container_id)
            return response([{
                "Id": self.container_id, "Name": f"/{self.worker}",
                "Config": {"Labels": {
                    "io.x-k8s.kind.cluster": "tenant-example",
                    "io.x-k8s.kind.role": "worker",
                }},
                "State": {"Running": True, "StartedAt": self.started_at},
            }])
        if command[:2] == ["docker", "restart"]:
            self.events.append("restart")
            self.assertEqual(command[2], self.container_id)
            if self.restart_changes_started_at:
                self.started_at = "2026-10-01T12:01:00Z"
            return response()
        raise AssertionError(f"unexpected Docker command: {command}")

    def scenario(self, *, current_uid="tenant-uid", snapshots=None, recovered=None,
                 renewals=None, lease_uids=None, ready_states=None, actual_wait=False,
                 project_from_payload=False):
        renewal_times = iter(
            ["2026-10-01T12:00:30Z", "2026-10-01T12:01:01Z"]
            if renewals is None else renewals
        )
        lease_identities = iter(["lease-uid"] * 4 if lease_uids is None else lease_uids)
        ready_sequence = iter(ready_states) if ready_states is not None else None

        def tenant(_client, resource):
            self.events.append("tenant")
            self.assertEqual(resource, "tenant/tenant-example")
            return {"metadata": {"name": "tenant-example", "uid": current_uid},
                    "spec": {"workers": 3, "provider": {"type": "local"}}}

        def network(*_args):
            self.events.append("network-ready")

        def snapshot(*_args):
            self.events.append("snapshot")
            return next(snapshots) if snapshots is not None else self.workers

        def node(*args, **_kwargs):
            if f"leases.coordination.k8s.io/{self.worker}" in args:
                self.events.append("lease")
                return response({
                    "metadata": {
                        "name": self.worker, "namespace": "kube-node-lease",
                        "uid": next(lease_identities),
                    },
                    "spec": {
                        "holderIdentity": self.worker,
                        "renewTime": next(renewal_times),
                    },
                })
            self.events.append("node-ready")
            self.assertEqual(args[3:6], ("get", f"node/{self.worker}", "-o"))
            return response({"metadata": {"name": self.worker, "uid": "node-uid"},
                             "status": {"conditions": [{"type": "Ready", "status": "True"}]}})

        def bounded_wait(description, _timeout, _interval, predicate):
            for _ in range(3):
                result = predicate()
                if result:
                    return result
            raise RuntimeError(f"timed out waiting for {description}")

        def project(current, _names, _provider):
            if ready_sequence is not None:
                return next(ready_sequence)
            if project_from_payload and all("name" in entry for entry in current["databases"]):
                return {
                    entry["name"]: {
                        **self.entries[entry["name"]],
                        "logicalUid": entry["logicalUid"],
                    }
                    for entry in current["databases"]
                }
            return recovered if recovered is not None else self.entries

        with (
            patch("scripts.test_e2e._inspect_management_object", side_effect=tenant),
            patch("scripts.test_e2e.verify_tenant_management_ownership"),
            patch("scripts.test_e2e.run", side_effect=self.docker),
            patch("scripts.test_e2e.worker_snapshot", side_effect=snapshot),
            patch("scripts.test_e2e._tenant_kubectl", side_effect=node),
            patch("scripts.test_e2e.wait_network_ready", side_effect=network),
            patch("scripts.test_e2e.wait_for",
                  side_effect=actual_wait_for if actual_wait else bounded_wait),
            patch("scripts.test_e2e.ready_entries", side_effect=project),
        ):
            return _restart_worker_and_verify_markers(
                self.root, self.config, self.client, self.tenant, self.uid,
                self.catalog, "catalog-uid", self.entries,
            )

    def test_exact_worker_restart_precedes_three_cluster_readiness_and_marker_rereads(self) -> None:
        self.assertEqual(self.scenario(), self.entries)
        self.assertEqual(
            self.events,
            ["tenant", "volume", "snapshot", "lease", "inspect", "tenant", "restart",
             "inspect", "node-ready", "lease", "network-ready", "tenant", "snapshot",
             "catalog-ready", "catalog-read", "read:alpha",
             "catalog-read", "read:beta", "catalog-read", "read:gamma"],
        )

    def test_transient_query_unavailability_requires_fresh_catalog_and_marker(self) -> None:
        self.transient_unavailable["alpha"] = 1
        self.assertEqual(self.scenario(), self.entries)
        self.assertEqual(self.events.count("catalog-read"), 4)
        self.assertEqual(self.events.count("read:alpha"), 2)
        self.assertEqual(self.transient_unavailable["alpha"], 0)

    def test_persistent_unavailable_query_fails_with_bounded_timeout(self) -> None:
        self.transient_unavailable["alpha"] = 5
        with self.assertRaisesRegex(RuntimeError, "timed out waiting for exact marker for alpha"):
            self.scenario()
        self.assertEqual(self.events.count("read:alpha"), 3)
        self.assertNotIn("read:beta", self.events)

    def test_persistent_unavailable_expires_real_wait_deadline(self) -> None:
        self.transient_unavailable["alpha"] = 500
        with (
            patch("scripts.lib.kube.time.monotonic",
                  side_effect=iter(range(500))),
            patch("scripts.lib.kube.time.sleep"),
            self.assertRaisesRegex(RuntimeError, "timed out waiting for exact marker for alpha"),
        ):
            self.scenario(actual_wait=True)
        self.assertNotIn("read:beta", self.events)
        self.assertLessEqual(self.events.count("read:alpha"), 60)

    def test_non_503_query_failure_and_catalog_replacement_are_not_retried(self) -> None:
        self.query_error["alpha"] = "HTTP 409: Conflict"
        with self.assertRaisesRegex(RuntimeError, "HTTP 409"):
            self.scenario()
        self.assertEqual(self.events.count("read:alpha"), 1)
        self.events.clear()
        self.started_at = "2026-10-01T12:00:00Z"
        self.query_error.clear()
        self.transient_unavailable["alpha"] = 1
        original_read = self.catalog.read
        calls = 0
        def replaced(expected_uid):
            nonlocal calls
            calls += 1
            current = original_read(expected_uid)
            if calls == 2:
                current["catalogUid"] = "replacement"
            return current
        self.catalog.read = replaced
        with self.assertRaisesRegex(RuntimeError, "catalog identity changed"):
            self.scenario()
        self.assertEqual(self.events.count("read:alpha"), 1)

    def test_replacement_entry_on_retry_fails_without_second_query(self) -> None:
        self.transient_unavailable["alpha"] = 1
        original_read = self.catalog.read
        calls = 0
        def replaced(expected_uid):
            nonlocal calls
            calls += 1
            current = original_read(expected_uid)
            if calls == 2:
                current["databases"][0]["logicalUid"] = "replacement"
            return current
        self.catalog.read = replaced
        with self.assertRaisesRegex(RuntimeError, "catalog entry identity changed"):
            self.scenario(project_from_payload=True)
        self.assertEqual(self.events.count("read:alpha"), 1)

    def test_query_failure_other_than_unavailable_is_not_retried(self) -> None:
        self.marker_results["alpha"] = ["replaced"]
        with self.assertRaisesRegex(RuntimeError, "marker differs"):
            self.scenario()
        self.assertEqual(self.events.count("read:alpha"), 1)
        self.assertNotIn("read:beta", self.events)

    def test_stale_ready_node_and_catalog_cannot_prove_worker_rejoin(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "timed out waiting for recorded Tenant worker Node"):
            self.scenario(renewals=[
                "2026-10-01T12:00:30Z",
                "2026-10-01T12:00:30Z",
                "2026-10-01T12:00:30Z",
                "2026-10-01T12:00:30Z",
            ])
        self.assertEqual(self.events.count("node-ready"), 3)
        self.assertNotIn("catalog-ready", self.events)

    def test_renewal_before_restart_is_not_sufficient_and_new_lease_is_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "timed out waiting for recorded Tenant worker Node"):
            self.scenario(renewals=[
                "2026-10-01T12:00:30Z",
                "2026-10-01T12:00:59Z",
                "2026-10-01T12:00:59Z",
                "2026-10-01T12:00:59Z",
            ])
        self.events.clear()
        self.started_at = "2026-10-01T12:00:00Z"
        with self.assertRaisesRegex(RuntimeError, "Lease identity changed"):
            self.scenario(lease_uids=["lease-uid", "replacement-uid"])
        self.assertNotIn("catalog-ready", self.events)
        self.events.clear()
        self.started_at = "2026-10-01T12:00:00Z"
        self.assertEqual(self.scenario(renewals=[
            "2026-10-01T12:00:30Z",
            "2026-10-01T12:00:59Z",
            "2026-10-01T12:01:01Z",
        ]), self.entries)
        self.assertEqual(self.events.count("node-ready"), 2)

    def test_all_markers_are_written_before_failover_controller_and_worker_restart(self) -> None:
        with (
             patch("scripts.test_e2e._failover", side_effect=lambda *_: self.events.append("failover")),
             patch(
                 "scripts.test_e2e._restart_database_controller",
                 side_effect=lambda *_: self.events.append("controller-restart"),
             ),
             patch("scripts.test_e2e.ready_entries", return_value=self.entries),
             patch(
                 "scripts.test_e2e._restart_worker_and_verify_markers",
                 side_effect=lambda *_: self.events.append("worker-restart") or self.entries,
             ),
        ):
             self.assertEqual(
                 _verify_restarts_and_markers(
                     self.root, self.config, self.client, self.tenant, self.uid,
                     self.catalog, "catalog-uid", self.entries,
                 ),
                 self.entries,
             )
        self.assertEqual(self.events, [
             "write:alpha", "read:alpha", "write:beta", "read:beta",
             "write:gamma", "read:gamma", "failover", "controller-restart",
             "catalog-ready", "read:alpha", "read:beta", "read:gamma",
             "worker-restart",
        ])

    def test_unchanged_worker_or_replaced_identity_fails(self) -> None:
        for kind in ("not-restarted", "replaced-node", "foreign-tenant"):
            with self.subTest(kind=kind):
                self.events.clear()
                self.started_at = "2026-10-01T12:00:00Z"
                if kind == "not-restarted":
                    self.restart_changes_started_at = False
                    with self.assertRaisesRegex(RuntimeError, "did not restart"):
                        self.scenario()
                    self.restart_changes_started_at = True
                elif kind == "replaced-node":
                    after = copy.deepcopy(self.workers)
                    after[self.worker]["nodeUID"] = "replacement"
                    with self.assertRaisesRegex(RuntimeError, "worker/node identity changed"):
                        self.scenario(snapshots=iter([self.workers, after]))
                elif kind == "foreign-tenant":
                    with self.assertRaisesRegex(RuntimeError, "Tenant identity changed"):
                        self.scenario(current_uid="replacement")
                    self.assertNotIn("restart", self.events)

    def test_foreign_volume_or_incomplete_worker_identity_blocks_restart(self) -> None:
        for kind in ("foreign-volume", "missing-node-uid"):
            with self.subTest(kind=kind):
                self.events.clear()
                if kind == "foreign-volume":
                    original_docker = self.docker

                    def foreign_volume(command, **kwargs):
                        result = original_docker(command, **kwargs)
                        if command[:3] == ["docker", "volume", "inspect"]:
                            payload = json.loads(result.stdout)
                            payload[0]["Labels"]["lab-owner"] = "foreign"
                            return response(payload)
                        return result

                    with patch.object(self, "docker", side_effect=foreign_volume):
                        with self.assertRaisesRegex(RuntimeError, "storage ownership changed"):
                            self.scenario()
                else:
                    workers = copy.deepcopy(self.workers)
                    workers[self.worker]["nodeUID"] = ""
                    with self.assertRaisesRegex(RuntimeError, "identity is incomplete"):
                        self.scenario(snapshots=iter([workers]))
                self.assertNotIn("restart", self.events)

    def test_missing_or_changed_marker_fails_after_worker_restart(self) -> None:
        for name in self.entries:
            for values in ([], ["replaced"]):
                with self.subTest(name=name, values=values):
                    self.events.clear()
                    self.started_at = "2026-10-01T12:00:00Z"
                    self.marker_results[name] = values
                    with self.assertRaisesRegex(RuntimeError, "marker differs"):
                        self.scenario()
                    self.assertIn("restart", self.events)
                    self.marker_results[name] = [f"marker-{name}"]

    def test_replaced_database_identity_fails_before_marker_read(self) -> None:
        recovered = copy.deepcopy(self.entries)
        recovered["beta"]["logicalUid"] = "replacement"
        with self.assertRaisesRegex(RuntimeError, "catalog entry identity changed"):
            self.scenario(recovered=recovered)
        self.assertNotIn("read:beta", self.events)
