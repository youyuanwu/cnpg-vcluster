from __future__ import annotations

import copy
import json
import unittest
from subprocess import CompletedProcess
from unittest.mock import Mock, patch

from scripts.test_e2e import capture_tenant_deletion_identity, verify_tenant_deletion
from tests.test_controller_scenarios import allocation_lease


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
            "allocationLease": allocation_lease()["metadata"],
            "managementResources": [
                ("namespace", "", "tenant-a", "namespace-uid"),
                ("clusters.cluster.x-k8s.io", "tenant-a", "tenant-a", "cluster-uid"),
                ("devclusters.infrastructure.cluster.x-k8s.io", "tenant-a", "tenant-a", "devcluster-uid"),
                ("kamajicontrolplanes.controlplane.cluster.x-k8s.io", "tenant-a", "tenant-a", "controlplane-uid"),
            ],
            "workerContainers": ["worker-a " + "a" * 64],
            "providerContainers": ["worker-a " + "a" * 64, "tenant-a-lb " + "b" * 64],
            "dockerVolume": {"name": "lab-tenant-a-storage"},
        }
        self.client = Mock()
        self.client.kubectl.return_value = response()
        self.client.json.return_value = {"items": []}
        self.document = {"metadata": {"name": "tenant-a", "uid": "tenant-uid"}}

    def test_capture_rechecks_tenant_uid_and_records_live_snapshot_and_allocation(self) -> None:
        current = copy.deepcopy(self.document)
        current["metadata"]["resourceVersion"] = "latest"
        self.client.kubectl.return_value = response(current)
        with patch("scripts.test_e2e.tenant_snapshot", return_value=self.identity) as snapshot:
            captured = capture_tenant_deletion_identity(
                {"LAB_PREFIX": "lab"}, self.client, self.document,
            )
        snapshot.assert_called_once_with({"LAB_PREFIX": "lab"}, self.client, current)
        self.assertEqual("tenant-uid", captured["uid"])
        self.assertEqual(allocation_lease()["metadata"], captured["allocationLease"])

    def test_capture_rejects_same_name_replacement_or_missing_tenant(self) -> None:
        for payload in (None, {"metadata": {"name": "tenant-a", "uid": "replacement"}}):
            with self.subTest(payload=payload):
                self.client.kubectl.return_value = response(payload)
                with patch("scripts.test_e2e.tenant_snapshot") as snapshot:
                    with self.assertRaisesRegex(RuntimeError, "identity changed"):
                        capture_tenant_deletion_identity({}, self.client, self.document)
                    snapshot.assert_not_called()

    def test_capture_propagates_mismatched_lease_identity(self) -> None:
        self.client.kubectl.return_value = response(self.document)
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
                self.client.kubectl.return_value = response(self.document)
                with patch("scripts.test_e2e.tenant_snapshot", return_value=identity):
                    with self.assertRaisesRegex(RuntimeError, "provider deletion identity is incomplete"):
                        capture_tenant_deletion_identity({"LAB_PREFIX": "lab"}, self.client, self.document)

    def test_absence_is_checked_for_exact_management_names_and_docker_identities(self) -> None:
        with patch("scripts.test_e2e.run", return_value=response()) as docker:
            verify_tenant_deletion(self.client, self.identity)
        calls = [call.args for call in self.client.kubectl.call_args_list]
        self.assertIn(
            ("-n", "tenant-a", "get", "clusters.cluster.x-k8s.io/tenant-a",
             "-o", "json", "--ignore-not-found=true"),
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
        for kind, _, name, uid in self.identity["managementResources"]:
            with self.subTest(kind=kind):
                def inspect(*args, **_kwargs):
                    resource = args[args.index("get") + 1]
                    return response({"metadata": {"uid": uid}}) if resource == f"{kind}/{name}" else response()

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
                self.client.json.return_value = {"items": [lease]}
                with self.assertRaisesRegex(RuntimeError, "Lease remained"):
                    verify_tenant_deletion(self.client, self.identity)

    def test_malformed_allocation_state_is_not_treated_as_absence(self) -> None:
        for payload in ({}, {"items": None}, {"items": [{}]}):
            with self.subTest(payload=payload):
                self.client.json.return_value = payload
                with self.assertRaisesRegex(RuntimeError, "invalid allocation Lease"):
                    verify_tenant_deletion(self.client, self.identity)

    def test_inspection_failure_is_redacted_and_not_treated_as_absence(self) -> None:
        candidate = "inspection-candidate-secret"
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
