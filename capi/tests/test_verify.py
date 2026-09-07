from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.verify import _reject_kubernetes_credential


class VerifyTests(unittest.TestCase):
    def test_cross_kubernetes_transport_failure_is_not_rejection(self) -> None:
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
            refused = type(
                "Result",
                (),
                {
                    "returncode": 1,
                    "stdout": "",
                    "stderr": "connection refused",
                },
            )()
            with (
                patch("scripts.verify._tenant_kubectl", return_value=reachable),
                patch("scripts.verify.run", return_value=refused),
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
