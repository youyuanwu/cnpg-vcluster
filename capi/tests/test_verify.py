from __future__ import annotations

import tempfile
import unittest
import json
import base64
from pathlib import Path
from unittest.mock import Mock, patch

from scripts.status import _control_plane_layer_status
from scripts.verify import (
    _cluster_identity,
    _endpoint_matches,
    _management_absence,
    _reject_kubernetes_credential,
)


class VerifyTests(unittest.TestCase):
    @staticmethod
    def _resource(endpoint: dict[str, object], *, kcp: bool = False):
        resource = {
            "metadata": {"generation": 1, "annotations": {}},
            "spec": {"controlPlaneEndpoint": endpoint},
            "status": {
                "conditions": [
                    {
                        "type": "Available",
                        "status": "True",
                        "observedGeneration": 1,
                    }
                ]
            },
        }
        if kcp:
            resource["status"]["initialization"] = {
                "controlPlaneInitialized": True
            }
        return resource

    @staticmethod
    def _result(payload):
        return type(
            "Result",
            (),
            {
                "returncode": 0,
                "stdout": json.dumps(payload),
                "stderr": "",
            },
        )()

    def test_authoritative_endpoint_requires_exact_host_and_port(self) -> None:
        tenant = type("Tenant", (), {"vip": "172.18.0.10"})()
        config = {"SPIKE_API_PORT": "6443"}
        self.assertTrue(
            _endpoint_matches(
                {"host": "172.18.0.10", "port": 6443},
                tenant,
                config,
            )
        )
        self.assertFalse(
            _endpoint_matches(
                {"host": "172.18.0.11", "port": 6443},
                tenant,
                config,
            )
        )
        self.assertFalse(
            _endpoint_matches(
                {"host": "172.18.0.10", "port": 7443},
                tenant,
                config,
            )
        )

    def test_management_absence_rejects_inspection_failure(self) -> None:
        client = Mock()
        client.kubectl.side_effect = [
            type(
                "Result",
                (),
                {
                    "returncode": 0,
                    "stdout": json.dumps({"items": []}),
                    "stderr": "",
                },
            )(),
            type(
                "Result",
                (),
                {
                    "returncode": 1,
                    "stdout": "",
                    "stderr": "lookup management API: host not found",
                },
            )(),
        ]
        tenants = [
            type("Tenant", (), {"cnpg_cluster": "tenant-a-postgres"})(),
            type("Tenant", (), {"cnpg_cluster": "tenant-b-postgres"})(),
        ]
        with self.assertRaisesRegex(RuntimeError, "inspection failed"):
            _management_absence(
                {"DATABASE_NAMESPACE": "database"},
                client,
                tenants,
                {"tenant-a": {}, "tenant-b": {}},
            )

    def test_cluster_identity_rejects_devcluster_and_kcp_endpoint_drift(self) -> None:
        tenant = type(
            "Tenant",
            (),
            {
                "name": "tenant-a",
                "namespace": "tenant-a",
                "vip": "172.18.0.10",
                "pod_cidr": "10.70.0.0/16",
                "service_cidr": "10.140.0.0/16",
                "domain": "tenant-a.local",
                "cnpg_cluster": "tenant-a-postgres",
            },
        )()
        config = {"SPIKE_API_PORT": "6443"}
        expected = {"host": tenant.vip, "port": 6443}
        cluster = self._resource(expected)
        cluster["spec"]["clusterNetwork"] = {
            "pods": {"cidrBlocks": [tenant.pod_cidr]},
            "services": {"cidrBlocks": [tenant.service_cidr]},
            "serviceDomain": tenant.domain,
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            kubeconfig = root / ".runtime" / "tenants" / tenant.name / "kubeconfig"
            kubeconfig.parent.mkdir(parents=True)
            kubeconfig.write_text(
                f"server: https://{tenant.vip}:6443\n"
                f"certificate-authority-data: "
                f"{base64.b64encode(b'ca').decode()}\n",
                encoding="utf-8",
            )
            kubeconfig.chmod(0o600)
            drift_cases = (
                (
                    self._resource({"host": "172.18.0.11", "port": 6443}),
                    self._resource(expected, kcp=True),
                ),
                (
                    self._resource(expected),
                    self._resource({"host": tenant.vip, "port": 7443}, kcp=True),
                ),
            )
            for devcluster, kcp in drift_cases:
                client = Mock()
                client.kubectl.side_effect = [
                    self._result(cluster),
                    self._result(devcluster),
                    self._result(kcp),
                ]
                with self.assertRaisesRegex(RuntimeError, "identity drift"):
                    _cluster_identity(root, config, client, tenant)

    def test_status_rejects_devcluster_and_kcp_endpoint_drift(self) -> None:
        tenant = type(
            "Tenant",
            (),
            {"name": "tenant-a", "namespace": "tenant-a", "vip": "172.18.0.10"},
        )()
        config = {"SPIKE_API_PORT": "6443"}
        expected = {"host": tenant.vip, "port": 6443}
        cluster = self._resource(expected)
        drift_cases = (
            (
                self._resource({"host": "172.18.0.11", "port": 6443}),
                self._resource(expected, kcp=True),
            ),
            (
                self._resource(expected),
                self._resource({"host": tenant.vip, "port": 7443}, kcp=True),
            ),
        )
        for devcluster, kcp in drift_cases:
            client = Mock()
            client.kubectl.side_effect = [
                self._result(devcluster),
                self._result(kcp),
            ]
            status = _control_plane_layer_status(
                config, client, tenant, cluster
            )
            self.assertFalse(status["ready"])
            self.assertFalse(status["authoritativeEndpoints"])

    def _assert_inconclusive(self, stderr: str) -> None:
        source = type("Tenant", (), {"name": "tenant-a", "vip": "172.18.0.10"})()
        target = type("Tenant", (), {"name": "tenant-b", "vip": "172.18.0.11"})()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            kubeconfig = root / ".runtime" / "tenants" / source.name / "kubeconfig"
            kubeconfig.parent.mkdir(parents=True)
            kubeconfig.write_text(
                "server: https://172.18.0.10:6443\n",
                encoding="utf-8",
            )
            kubeconfig.chmod(0o600)
            reachable = type(
                "Result",
                (),
                {"returncode": 0, "stdout": "node\n", "stderr": ""},
            )()
            rejected = type(
                "Result",
                (),
                {"returncode": 1, "stdout": "", "stderr": stderr},
            )()
            with (
                patch("scripts.verify._tenant_kubectl", return_value=reachable),
                patch("scripts.verify.run", return_value=rejected),
            ):
                with self.assertRaisesRegex(RuntimeError, "inconclusive"):
                    _reject_kubernetes_credential(
                        root,
                        {
                            "SPIKE_API_PORT": "6443",
                            "KUBECTL_REQUEST_TIMEOUT": "5s",
                        },
                        source,
                        target,
                    )

    def test_cross_kubernetes_transport_failure_is_not_rejection(self) -> None:
        self._assert_inconclusive("connection refused")

    def test_named_forbidden_identity_is_not_credential_rejection(self) -> None:
        self._assert_inconclusive(
            'Error from server (Forbidden): User "tenant-a-admin" cannot list nodes'
        )
