from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import Mock, patch

from scripts.lib.controller_client import apply_tenant_document, tenant_manifest_document
from scripts.test_controller_allocation import _race_lease_creates, controller_replicas, verify_api_boundaries
from scripts.network import _verify_static_kube_proxy
from scripts.cnpg import verify_restart_persistence
from scripts.test_tenant_lifecycle import _apply
from tests.test_controller_scenarios import allocation_lease


def response(payload=None, *, error=""):
    return CompletedProcess([], 1 if error else 0, stdout=json.dumps(payload) if payload is not None else "", stderr=error)


CONFIG = {
    "KUBERNETES_VERSION": "v1.36.4", "CONDITION_TIMEOUT": "30s",
    "TENANT_CONTROL_PLANE_TIMEOUT": "30s", "CNPG_TIMEOUT": "30s",
    "DATABASE_NAMESPACE": "database",
}


class LiveAPIContractsTests(unittest.TestCase):
    def test_client_has_only_three_fields_and_always_uses_strict_ssa(self):
        client = Mock()
        document = tenant_manifest_document(CONFIG, "tenant-a")
        self.assertEqual({"kubernetesVersion": "1.36.4", "workers": 1, "databases": 1}, document["spec"])
        self.assertEqual("tenancy.cnpg-vcluster.io/v1alpha2", document["apiVersion"])
        apply_tenant_document(client, document)
        self.assertIn("--validate=strict", client.kubectl.call_args.args)
        self.assertIn("--server-side", client.kubectl.call_args.args)
        self.assertEqual(document, json.loads(client.kubectl.call_args.kwargs["input_text"]))

    def test_live_api_matrix_covers_count_types_limits_and_version_syntax(self):
        client = Mock()
        observed = []

        def kubectl(*args, **kwargs):
            self.assertIn("--dry-run=server", args)
            spec = json.loads(kwargs["input_text"])["spec"]
            observed.append(spec)
            for field in ("workers", "databases"):
                if type(spec[field]) is not int or not 1 <= spec[field] <= 3:
                    return response(error=f"invalid {field}")
            if spec["kubernetesVersion"] in ("1.36", "1.36.4-extra", "", "vv1.36.4"):
                return response(error="invalid kubernetesVersion")
            return response({})

        client.kubectl.side_effect = kubectl
        with patch("scripts.test_controller_allocation.verify_controller_api") as existing:
            verify_api_boundaries(client, CONFIG)
        existing.assert_called_once_with(CONFIG, client)
        self.assertEqual(21, len(observed))
        self.assertEqual(["1.36.4", "v1.36.4", "0.0.0"], [spec["kubernetesVersion"] for spec in observed[-3:]])

    def test_api_acceptance_of_bad_count_or_syntax_fails_gate(self):
        client = Mock()
        client.kubectl.return_value = response({})
        with patch("scripts.test_controller_allocation.verify_controller_api"):
            with self.assertRaisesRegex(RuntimeError, "invalid workers"):
                verify_api_boundaries(client, CONFIG)

    def test_lease_create_race_requires_exactly_one_winner(self):
        lease = allocation_lease()
        client = Mock()
        client.kubectl.side_effect = [response(lease), response(error="AlreadyExists")]
        self.assertEqual(lease, _race_lease_creates(client, lease))
        contenders = [
            json.loads(call.kwargs["input_text"])["metadata"] for call in client.kubectl.call_args_list
        ]
        self.assertEqual(1, len({item["name"] for item in contenders}))
        self.assertEqual(2, len({item["annotations"]["tenancy.cnpg-vcluster.io/tenant-uid"] for item in contenders}))
        for results in (
            [response(lease), response(lease)],
            [response(lease), response(error="Forbidden")],
        ):
            client.kubectl.side_effect = results
            with self.assertRaisesRegex(RuntimeError, "one winner"):
                _race_lease_creates(client, lease)

    def test_stopping_controller_waits_for_all_old_pods(self):
        client = Mock()
        client.json.side_effect = [{"items": [{"metadata": {"uid": "old"}}]}, {"items": []}]

        def wait(_description, _timeout, _interval, predicate):
            self.assertIsNone(predicate())
            self.assertTrue(predicate())

        with patch("scripts.test_controller_allocation.wait_for", side_effect=wait):
            controller_replicas(client, CONFIG, 0)
        self.assertIn("--replicas=0", client.kubectl.call_args.args)

    def test_three_tenant_gate_uses_one_worker_and_database_each(self):
        client = Mock()
        with (
            patch("scripts.test_tenant_lifecycle.ManagementClient", return_value=client),
            patch("scripts.test_tenant_lifecycle.wait_tenant_ready", return_value={}),
            patch("scripts.test_tenant_lifecycle.tenant_from_document"),
            patch("scripts.test_tenant_lifecycle.export_tenant_kubeconfig"),
        ):
            for name in ("tenant-a", "tenant-b", "tenant-c"):
                _apply(Path("."), CONFIG, name)
        specs = [json.loads(call.kwargs["input_text"])["spec"] for call in client.kubectl.call_args_list]
        self.assertEqual([{"kubernetesVersion": "1.36.4", "workers": 1, "databases": 1}] * 3, specs)


class StaticNetworkTests(unittest.TestCase):
    def test_foreign_static_resource_is_restored_then_recreated_not_content_repaired(self):
        annotation = "tenancy.cnpg-vcluster.io/tenant-uid"
        original = {
            "metadata": {"uid": "original", "annotations": {annotation: "tenant-uid"}, "labels": {"owner": "lab"}},
            "data": {"config.conf": "expected"},
        }
        foreign = copy.deepcopy(original)
        foreign["metadata"]["annotations"][annotation] = "foreign-fixture"
        replacement = copy.deepcopy(original)
        replacement["metadata"]["uid"] = "replacement"
        client = Mock()
        tenant = type("Tenant", (), {"name": "tenant-a"})()

        def wait(_description, _timeout, _interval, predicate):
            return predicate()

        with (
            patch("scripts.lib.kube.ManagementClient", return_value=client),
            patch("scripts.network.tenant_document", return_value={"status": {"phase": "OwnershipInvalid"}}),
            patch("scripts.network.wait_tenant_ready"),
            patch("scripts.network.wait_for", side_effect=wait),
            patch("scripts.network._tenant_kubectl", side_effect=[
                response(original), response(), response(foreign), response(), response(), response(replacement),
            ]) as kubectl,
        ):
            _verify_static_kube_proxy(Path("."), CONFIG, tenant)
        calls = kubectl.call_args_list
        restoration = json.loads(calls[3].args[-1])
        self.assertEqual("original", restoration[0]["value"])
        self.assertEqual("foreign-fixture", restoration[1]["value"])
        self.assertEqual("tenant-uid", restoration[2]["value"])
        self.assertIn("delete", calls[4].args)
        self.assertFalse(any("config.conf" in str(call) for call in calls))


class RestartPersistenceTests(unittest.TestCase):
    def test_restart_must_replace_pod_and_preserve_storage_and_sql_marker(self):
        tenant = type("Tenant", (), {"name": "tenant-a", "cnpg_cluster": "postgres"})()

        def wait(_description, _timeout, _interval, predicate):
            self.assertTrue(predicate())

        with (
            patch("scripts.cnpg._write_marker") as write,
            patch("scripts.cnpg._verify_marker") as verify,
            patch("scripts.cnpg._verify_filesystem") as filesystem,
            patch("scripts.cnpg._storage_identity", return_value={"pvc": "uid:pv"}),
            patch("scripts.cnpg._cnpg_ready", return_value=True),
            patch("scripts.cnpg.wait_tenant_ready"),
            patch("scripts.cnpg.wait_for", side_effect=wait),
            patch("scripts.cnpg._tenant_kubectl", side_effect=[
                CompletedProcess([], 0, stdout="postgres-1", stderr=""),
                response({"metadata": {"uid": "old"}}), response(),
                response({"metadata": {"uid": "new"}}),
            ]),
        ):
            verify_restart_persistence(Path("."), CONFIG, tenant)
        write.assert_called_once()
        verify.assert_called_once()
        filesystem.assert_called_once()
