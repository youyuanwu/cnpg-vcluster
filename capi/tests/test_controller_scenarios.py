from __future__ import annotations

import io
import copy
import hashlib
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import Mock, patch

from scripts.endpoint import run_endpoint_gate
from scripts.lib import tenants as tenant_lib
from scripts.lib.controller_scenarios import (
    LEASE,
    MANAGEMENT_CATALOG,
    manifest_tenant_name,
    tenant_from_document,
    tenant_snapshot,
    allocation_lease_manifest,
    allocation_lease_name,
    tenant_allocation,
    tenant_spec_hash,
    verify_allocation_lease,
    wait_tenant_ready,
)
from scripts.lib.tenants import Tenant, verify_tenant_management_ownership
from scripts.lib.files import write_private_file


def tenant_document() -> dict[str, object]:
    return {
        "metadata": {"name": "tenant-a", "uid": "tenant-uid"},
        "spec": {
            "kubernetesVersion": "1.36.4",
            "workers": 2,
            "provider": {"type": "local", "databases": 3},
        },
        "status": {
            "provider": {
                "type": "local",
                "allocation": {
                    "slotId": "slot-0",
                    "endpoint": "172.18.255.10",
                    "podCIDR": "10.73.0.0/16",
                    "serviceCIDR": "10.143.0.0/16",
                },
                "foundationHash": "foundation",
                "clusterUID": "cluster-uid",
            },
        },
    }


CONFIG = {"OWNERSHIP_LABEL": "lab-owner", "LAB_PREFIX": "lab"}


def allocation_lease():
    lease = allocation_lease_manifest(CONFIG, tenant_document())
    lease["metadata"].update(uid="lease-uid", resourceVersion="7")
    lease["spec"] = {}
    return lease


def lease_inventory(leases):
    return {
        "apiVersion": LEASE.api_version,
        "kind": f"{LEASE.kind}List",
        "items": leases,
    }


class ControllerScenarioTests(unittest.TestCase):
    def test_management_ownership_uses_declared_machine_version(self) -> None:
        catalog = tuple(
            replace(resource, api_version="cluster.x-k8s.io/unavailable")
            if resource.kind == "Machine"
            else resource
            for resource in tenant_lib.MANAGEMENT_CATALOG
        )
        calls = []

        class Client:
            def kubectl(self, *arguments, **_kwargs):
                calls.append(arguments)
                error = (
                    "NotFound"
                    if any("unavailable" in argument for argument in arguments)
                    else "Error from server (NotFound): absent"
                )
                return CompletedProcess([], 1, stdout="", stderr=error)

        tenant = Tenant(
            name="tenant-a",
            namespace="tenant-a",
            vip="172.18.0.2",
            pod_cidr="10.0.0.0/16",
            service_cidr="10.1.0.0/16",
            dns_ip="10.1.0.10",
            domain="tenant-a.capi.local",
            storage_host_path=Path("/tmp/tenant-a"),
            cnpg_cluster="cluster",
            workers=1,
        )
        with patch.object(tenant_lib, "MANAGEMENT_CATALOG", catalog):
            with self.assertRaisesRegex(RuntimeError, "Machine inspection failed"):
                verify_tenant_management_ownership(
                    {"OWNERSHIP_LABEL": "owner", "LAB_PREFIX": "lab"},
                    Client(),
                    tenant,
                )
        self.assertTrue(
            any(
                "--raw=/apis/cluster.x-k8s.io/unavailable/"
                "namespaces/tenant-a/machines" in arguments
                for arguments in calls
            )
        )

    def test_spec_hash_matches_rust_canonical_contract(self) -> None:
        document = tenant_document()
        expected = hashlib.sha256(
            b'{"kubernetesVersion":"1.36.4","workers":2,"provider":{"type":"local","databases":3}}'
        ).hexdigest()
        self.assertEqual(expected, tenant_spec_hash(document))
        document["spec"]["kubernetesVersion"] = "v1.36.4"
        self.assertEqual(expected, tenant_spec_hash(document))
        document["status"]["provider"]["allocation"]["podCIDR"] = "10.99.0.0/16"
        self.assertEqual(expected, tenant_spec_hash(document))

    def test_spec_networks_are_never_allocation_fallbacks(self) -> None:
        document = tenant_document()
        document["spec"].update(podCIDR="10.1.0.0/16", serviceCIDR="10.2.0.0/16")
        with self.assertRaisesRegex(RuntimeError, "v1alpha2"):
            tenant_spec_hash(document)
        del document["status"]["provider"]["allocation"]
        with self.assertRaisesRegex(RuntimeError, "allocation"):
            tenant_allocation(document)

    def test_allocation_requires_canonical_disjoint_ipv4_networks(self) -> None:
        for field, value in (
            ("slotId", ""), ("endpoint", "172.18.255.10:6443"),
            ("endpoint", "10.73.0.1"),
            ("podCIDR", "10.73.0.1/16"), ("serviceCIDR", "10.73.0.0/16"),
            ("serviceCIDR", "::/64"), ("serviceCIDR", "10.144.0.0/30"),
        ):
            with self.subTest(field=field, value=value):
                document = tenant_document()
                document["status"]["provider"]["allocation"][field] = value
                with self.assertRaisesRegex(RuntimeError, "allocation"):
                    tenant_allocation(document)

    def test_lease_name_and_exact_markers_match_rust(self) -> None:
        client = Mock()
        client.kubectl.return_value = CompletedProcess(
            [], 0, stdout=json.dumps(lease_inventory([allocation_lease()]))
        )
        identity = verify_allocation_lease(CONFIG, client, tenant_document())
        self.assertEqual("lease-uid", identity["uid"])
        self.assertEqual("tenant-slot-" + hashlib.sha256(b"slot-0").hexdigest()[:51],
                         allocation_lease_name("slot-0"))
        client.kubectl.assert_called_once_with(
            "get", f"--raw={LEASE.inventory_path}"
        )

    def test_foreign_replaced_malformed_or_duplicate_claims_are_rejected(self) -> None:
        cases = [[], [allocation_lease(), allocation_lease()]]
        for field, value in (
            ("namespace", "foreign"), ("uid", ""), ("resourceVersion", ""),
            ("ownerReferences", [{"uid": "foreign"}]), ("deletionTimestamp", "now"),
        ):
            lease = allocation_lease()
            lease["metadata"][field] = value
            cases.append([lease])
        for field, value in (
            ("apiVersion", "wrong/v1"),
            ("kind", "Wrong"),
        ):
            lease = allocation_lease()
            lease[field] = value
            cases.append([lease])
        lease = allocation_lease()
        lease["metadata"]["name"] = 7
        cases.append([lease])
        for field in ("tenant", "tenant-uid", "spec-hash", "foundation-hash", "endpoint", "pod-cidr", "service-cidr"):
            lease = allocation_lease()
            lease["metadata"]["annotations"]["tenancy.cnpg-vcluster.io/" + field] = "foreign"
            cases.append([lease])
        lease = allocation_lease()
        lease["spec"] = {"holderIdentity": "foreign"}
        cases.append([lease])
        for leases in cases:
            with self.subTest(leases=leases):
                client = Mock()
                client.kubectl.return_value = CompletedProcess(
                    [], 0, stdout=json.dumps(lease_inventory(leases))
                )
                with self.assertRaisesRegex(RuntimeError, "Lease"):
                    verify_allocation_lease(CONFIG, client, tenant_document())

    def test_wait_logs_condition_and_classification_transitions_without_messages(self) -> None:
        def result(
            classification="progressing", reason="WaitingForWorkers", generation=1,
            *, reverse=False, message="",
        ):
            conditions = [
                {
                    "type": "Ready",
                    "status": "True" if classification == "ready" else "False",
                    "reason": reason,
                    "observedGeneration": generation,
                    "message": message,
                    "lastTransitionTime": message,
                },
                {"type": "Accepted", "status": "True", "reason": "Valid"},
            ]
            return {
                "classification": classification,
                "observedGeneration": generation,
                "conditions": list(reversed(conditions)) if reverse else conditions,
            }

        results = [
            result(message=f"password={'first-secret'}"),
            result(reverse=True, message=f"password={'second-secret'}"),
            result(reason="WaitingForDatabases"),
            result(classification="degraded", reason="WaitingForDatabases"),
            result(generation=2),
            result(classification="ready", generation=2),
        ]
        self.assertIn("first-secret", json.dumps(results))
        self.assertIn("second-secret", json.dumps(results))
        documents = [None] + [
            {"metadata": {"name": "tenant-a", "generation": 2 if index >= 4 else 1}}
            for index in range(len(results))
        ]
        output = io.StringIO()

        def wait(description, timeout, interval, predicate):
            self.assertEqual(("Tenant tenant-a Ready", 60, 1), (description, timeout, interval))
            for _ in range(len(documents)):
                value = predicate()
                if value:
                    return value
            self.fail("readiness was never observed")

        with (
            patch("scripts.lib.controller_scenarios.ManagementClient"),
            patch("scripts.lib.controller_scenarios.tenant_document", side_effect=documents),
            patch("scripts.lib.controller_scenarios.evaluate_tenant", side_effect=results),
            patch("scripts.lib.controller_scenarios.wait_for", side_effect=wait),
            patch("scripts.lib.controller_scenarios.time.monotonic", side_effect=range(7)),
            redirect_stdout(output),
        ):
            document = wait_tenant_ready(Path("."), {
                "TENANT_CONTROL_PLANE_TIMEOUT": "10s",
                "WORKER_REGISTRATION_TIMEOUT": "20s",
                "CNPG_TIMEOUT": "30s",
                "WAIT_POLL_INTERVAL": "1s",
            }, "tenant-a")
        self.assertEqual(documents[-1], document)
        transitions = [
            json.loads(line.removeprefix("CAPI_TENANT_TRANSITION "))
            for line in output.getvalue().splitlines()
        ]
        self.assertEqual(
            ["absent", "progressing", "progressing", "degraded", "progressing", "ready"],
            [transition["classification"] for transition in transitions],
        )
        self.assertEqual(list(range(1, 7)), [item["seconds"] for item in transitions])
        self.assertEqual("WaitingForDatabases", transitions[2]["conditions"][1]["reason"])
        self.assertEqual(2, transitions[-1]["generation"])
        self.assertNotIn("first-secret", output.getvalue())
        self.assertNotIn("second-secret", output.getvalue())
        self.assertNotIn('"message"', output.getvalue())
        self.assertNotIn("lastTransitionTime", output.getvalue())

    def test_wait_timeout_preserves_last_status_and_redacts_diagnostics(self) -> None:
        status = {
            "classification": "degraded",
            "conditions": [{"type": "Ready", "status": "False", "reason": "MissingWorkers"}],
            "blockers": [f"password={'do-not-log'}"],
        }
        self.assertIn("do-not-log", json.dumps(status))

        def wait(_description, _timeout, _interval, predicate):
            predicate()
            raise RuntimeError("timed out waiting")

        with (
            patch("scripts.lib.controller_scenarios.ManagementClient"),
            patch("scripts.lib.controller_scenarios.tenant_document", return_value={"metadata": {}}),
            patch("scripts.lib.controller_scenarios.evaluate_tenant", return_value=status),
            patch("scripts.lib.controller_scenarios.wait_for", side_effect=wait),
            redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(RuntimeError) as failure:
                wait_tenant_ready(Path("."), {
                    "TENANT_CONTROL_PLANE_TIMEOUT": "1s",
                    "WORKER_REGISTRATION_TIMEOUT": "1s",
                    "CNPG_TIMEOUT": "1s",
                    "WAIT_POLL_INTERVAL": "1s",
                }, "tenant-a")
        self.assertIn("MissingWorkers", str(failure.exception))
        self.assertNotIn("do-not-log", str(failure.exception))
        self.assertIn("REDACTED", str(failure.exception))

    def test_manifest_name_is_read_from_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "custom.yaml"
            path.write_text(
                "apiVersion: tenancy.cnpg-vcluster.io/v1alpha3\n"
                "kind: Tenant\n"
                "metadata:\n"
                "  labels:\n"
                "    example: value\n"
                "  name: tenant-a\n"
                "spec:\n"
                "  workers: 1\n",
                encoding="utf-8",
            )
            self.assertEqual("tenant-a", manifest_tenant_name(path))

    def test_tenant_is_derived_from_controller_status(self) -> None:
        with patch(
            "scripts.lib.controller_scenarios.run",
            return_value=CompletedProcess(
                [],
                0,
                stdout='[{"Mountpoint":"/var/lib/docker/volumes/tenant-a/_data"}]',
                stderr="",
            ),
        ):
            tenant = tenant_from_document(
                Path("."),
                {
                    "LAB_PREFIX": "lab",
                    "SPIKE_API_PORT": "6443",
                    "SPIKE_CLUSTER_DOMAIN": "spike.capi.local",
                    "SPIKE_CNPG_CLUSTER": "capi-postgres",
                },
                tenant_document(),
            )
        self.assertEqual("tenant-a", tenant.name)
        self.assertEqual("172.18.255.10", tenant.vip)
        self.assertEqual("10.143.0.10", tenant.dns_ip)
        self.assertEqual(2, tenant.workers)
        self.assertEqual(3, tenant.database_count)

    def test_snapshot_uses_live_management_and_host_identities(self) -> None:
        class Client:
            def __init__(self, wrong_observed: bool = False) -> None:
                self.index = 0
                self.wrong_observed = wrong_observed

            def kubectl(self, *arguments):
                path = next(
                    argument.removeprefix("--raw=")
                    for argument in arguments
                    if argument.startswith("--raw=")
                )
                if path == LEASE.inventory_path:
                    return CompletedProcess(
                        [], 0, stdout=json.dumps(lease_inventory([allocation_lease()])), stderr=""
                    )
                definition = next(
                    definition
                    for definition in MANAGEMENT_CATALOG
                    if path
                    in {
                        definition.collection_path(
                            "tenant-a" if definition.namespaced else None
                        ),
                        definition.object_path(
                            "tenant-a" if definition.namespaced else None,
                            definition.expected_name("tenant-a") or "",
                        ),
                    }
                )
                if definition.evidence_policy == "observed":
                    items = (
                        [{
                            **({
                                "apiVersion": "wrong/v1",
                                "kind": "Wrong",
                            } if self.wrong_observed else {}),
                            "metadata": {
                                "name": "worker-a",
                                "namespace": "tenant-a",
                                "uid": "machine-uid",
                            }
                        }]
                        if definition.kind == "Machine"
                        else []
                    )
                    return CompletedProcess(
                        [],
                        0,
                        stdout=json.dumps({
                            "apiVersion": definition.api_version,
                            "kind": f"{definition.kind}List",
                            "items": items,
                        }),
                        stderr="",
                    )
                self.index += 1
                return CompletedProcess(
                    [],
                    0,
                    stdout=json.dumps({
                        "apiVersion": definition.api_version,
                        "kind": definition.kind,
                        "metadata": {
                            "name": path.rsplit("/", 1)[1],
                            "namespace": "tenant-a" if definition.namespaced else None,
                            "uid": f"resource-{self.index}",
                        },
                    }),
                    stderr="",
                )

        with patch(
            "scripts.lib.controller_scenarios.run",
            side_effect=[
                CompletedProcess(
                    [],
                    0,
                    stdout='[{"Name":"volume","CreatedAt":"now","Mountpoint":"/volume","Labels":{"owned":"true"}}]',
                    stderr="",
                ),
                CompletedProcess(
                    [],
                    0,
                    stdout="worker-b bbbb\nworker-a aaaa\n",
                    stderr="",
                ),
                CompletedProcess(
                    [], 0, stdout="worker-b bbbb\nworker-a aaaa\ntenant-a-lb cccc\n", stderr="",
                ),
                CompletedProcess(
                    [],
                    0,
                    stdout='[{"Name":"volume","CreatedAt":"now","Mountpoint":"/volume","Labels":{"owned":"true"}}]',
                    stderr="",
                ),
                CompletedProcess(
                    [],
                    0,
                    stdout="worker-b bbbb\nworker-a aaaa\n",
                    stderr="",
                ),
                CompletedProcess(
                    [], 0, stdout="worker-b bbbb\nworker-a aaaa\ntenant-a-lb cccc\n", stderr="",
                ),
            ],
        ):
            snapshot = tenant_snapshot(
                CONFIG,
                Client(),
                tenant_document(),
            )
        self.assertEqual("tenant-uid", snapshot["uid"])
        self.assertEqual(
            [
                "worker-a aaaa",
                "worker-b bbbb",
            ],
            snapshot["workerContainers"],
        )
        self.assertEqual(9, len(snapshot["managementResources"]))
        self.assertEqual(
            {
                (
                    definition.api_version,
                    definition.kind,
                    "tenant-a" if definition.namespaced else "",
                    definition.expected_name("tenant-a"),
                )
                for definition in MANAGEMENT_CATALOG
                if definition.evidence_policy == "named"
            },
            {
                (api_version, kind, namespace, name)
                for api_version, kind, namespace, name, _uid
                in snapshot["managementResources"]
                if kind in {
                    definition.kind
                    for definition in MANAGEMENT_CATALOG
                    if definition.evidence_policy == "named"
                }
            },
        )
        self.assertIn(
            (
                "cluster.x-k8s.io/v1beta2",
                "Machine",
                "tenant-a",
                "worker-a",
                "machine-uid",
            ),
            snapshot["managementResources"],
        )
        self.assertEqual(
            ("coordination.k8s.io/v1", "Lease"),
            (
                snapshot["allocationLease"]["apiVersion"],
                snapshot["allocationLease"]["kind"],
            ),
        )
        self.assertIn("tenant-a-lb cccc", snapshot["providerContainers"])
        with (
            patch(
                "scripts.lib.controller_scenarios.run",
                side_effect=[
                    CompletedProcess([], 0, stdout='[{"Name":"volume"}]', stderr=""),
                    CompletedProcess([], 0, stdout="worker-a aaaa\n", stderr=""),
                    CompletedProcess([], 0, stdout="tenant-a-lb cccc\n", stderr=""),
                ],
            ),
            self.assertRaisesRegex(RuntimeError, "identity is incomplete"),
        ):
            tenant_snapshot(CONFIG, Client(wrong_observed=True), tenant_document())

    def test_endpoint_gate_cleans_partially_applied_tenant(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "tenant.yaml"
            manifest.write_text(
                "apiVersion: tenancy.cnpg-vcluster.io/v1alpha3\n"
                "kind: Tenant\nmetadata:\n  name: tenant-a\n",
                encoding="utf-8",
            )
            with (
                patch(
                    "scripts.endpoint.management_status",
                    return_value={"apiReady": False},
                ),
                patch("scripts.endpoint.create_management"),
                patch(
                    "scripts.endpoint.apply_controller_tenant",
                    side_effect=RuntimeError("readiness failed"),
                ),
                patch("scripts.endpoint.delete_controller_tenant") as delete,
            ):
                with self.assertRaisesRegex(RuntimeError, "readiness failed"):
                    run_endpoint_gate(root, {}, manifest=manifest)
            delete.assert_called_once_with(root, {}, "tenant-a")
            self.assertFalse((root / ".runtime" / "evidence").exists())

    def test_endpoint_gate_prints_evidence_without_evidence_file(self) -> None:
        tenant = type(
            "Tenant",
            (),
            {
                "name": "tenant-a",
                "namespace": "tenant-a-system",
                "vip": "172.18.0.10",
            },
        )()
        machine = {
            "metadata": {"name": "worker-a", "uid": "machine-uid"},
            "status": {"nodeRef": {"name": "node-a"}},
        }
        node = {"metadata": {"name": "node-a", "uid": "node-uid"}}
        secret_reads = 0

        class Client:
            def kubectl(self, *arguments, **_kwargs):
                nonlocal secret_reads
                joined = " ".join(arguments)
                if "get machines." in joined:
                    return CompletedProcess(
                        [], 0, stdout=json.dumps({"items": [machine]}), stderr=""
                    )
                if "devmachines." in joined:
                    return CompletedProcess(
                        [], 0, stdout=json.dumps({"metadata": {}}), stderr=""
                    )
                if "kamajicontrolplanes." in joined:
                    return CompletedProcess(
                        [],
                        0,
                        stdout=json.dumps({"metadata": {"annotations": {}}}),
                        stderr="",
                    )
                if "secrets/" in joined:
                    secret_reads += 1
                    return CompletedProcess(
                        [],
                        0,
                        stdout=json.dumps(
                            {
                                "data": {"value": "Ym9vdHN0cmFw"},
                                "metadata": {},
                            }
                        ),
                        stderr="",
                    )
                raise AssertionError(arguments)

        def tenant_kubectl(*_arguments, **kwargs):
            arguments = _arguments[3:]
            if arguments[:2] == ("get", "node/node-a"):
                return CompletedProcess(
                    [], 0, stdout=json.dumps(node), stderr=""
                )
            return CompletedProcess([], 1, stdout="", stderr="NotFound")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "tenant.yaml"
            manifest.write_text(
                "apiVersion: tenancy.cnpg-vcluster.io/v1alpha3\n"
                "kind: Tenant\nmetadata:\n  name: tenant-a\n",
                encoding="utf-8",
            )
            write_private_file(
                root / ".runtime/tenants/tenant-a/kubeconfig",
                "certificate-authority-data: Y2E=\n",
            )
            output = io.StringIO()
            with (
                patch(
                    "scripts.endpoint.management_status",
                    return_value={"apiReady": False},
                ),
                patch("scripts.endpoint.create_management"),
                patch(
                    "scripts.endpoint.apply_controller_tenant",
                    return_value=(Client(), tenant, {}),
                ),
                patch(
                    "scripts.endpoint.verify_tenant_management_ownership",
                    return_value={},
                ),
                patch("scripts.endpoint.verify_tenant_control_plane_contract"),
                patch(
                    "scripts.machines.worker_snapshot",
                    return_value=["worker-a"],
                ),
                patch("scripts.endpoint.verify_authoritative_endpoint"),
                patch("scripts.endpoint.verify_worker_runtime"),
                patch("scripts.endpoint._verify_bootstrap_secret"),
                patch(
                    "scripts.endpoint._tenant_kubectl",
                    side_effect=tenant_kubectl,
                ),
                patch("scripts.endpoint.write_storage_marker"),
                patch(
                    "scripts.endpoint.read_storage_marker",
                    return_value="phase3-marker\n",
                ),
                redirect_stdout(output),
            ):
                run_endpoint_gate(
                    root,
                    {"SPIKE_API_PORT": "6443"},
                    cleanup=False,
                    manifest=manifest,
                )
            payload = json.loads(output.getvalue())
            self.assertEqual("tenant-a", payload["cluster"])
            self.assertEqual("worker-a", payload["machine"])
            self.assertEqual(1, secret_reads)
            self.assertFalse((root / ".runtime/evidence").exists())
