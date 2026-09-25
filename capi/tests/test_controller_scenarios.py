from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import Mock, patch

from scripts.endpoint import run_endpoint_gate
from scripts.lib.controller_scenarios import (
    manifest_tenant_name,
    tenant_from_document,
    tenant_snapshot,
    wait_tenant_ready,
)


def tenant_document() -> dict[str, object]:
    return {
        "metadata": {"name": "tenant-a", "uid": "tenant-uid"},
        "spec": {
            "kubernetesVersion": "1.36.4",
            "workers": 2,
            "databaseCount": 3,
            "podCIDR": "10.73.0.0/16",
            "serviceCIDR": "10.143.0.0/16",
        },
        "status": {
            "endpoint": "172.18.255.10:6443",
            "foundationHash": "foundation",
            "clusterUID": "cluster-uid",
        },
    }


class ControllerScenarioTests(unittest.TestCase):
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
                "apiVersion: tenancy.cnpg-vcluster.io/v1alpha1\n"
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
            def __init__(self) -> None:
                self.index = 0

            def kubectl(self, *_arguments):
                self.index += 1
                return CompletedProcess(
                    [],
                    0,
                    stdout=(
                        '{"metadata":{"uid":"resource-'
                        + str(self.index)
                        + '"}}'
                    ),
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
            ],
        ):
            snapshot = tenant_snapshot(
                {"LAB_PREFIX": "lab"},
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
        self.assertEqual(8, len(snapshot["managementResources"]))

    def test_endpoint_gate_cleans_partially_applied_tenant(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "tenant.yaml"
            manifest.write_text(
                "apiVersion: tenancy.cnpg-vcluster.io/v1alpha1\n"
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
