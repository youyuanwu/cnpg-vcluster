from __future__ import annotations

import json
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from scripts.status import (
    collect_management_status,
    collect_tenant_status,
    _cnpg_layer_status,
    management_status_healthy,
)


class StatusTests(unittest.TestCase):
    def test_cnpg_status_uses_requested_database_count(self) -> None:
        def collect(expected: int, observed: int):
            tenant = type(
                "Tenant",
                (),
                {
                    "name": "tenant-c",
                    "cnpg_cluster": "tenant-c-postgres",
                    "database_count": expected,
                },
            )()

            def kubectl(*arguments, **_kwargs):
                command = " ".join(str(item) for item in arguments)
                if "cluster/tenant-c-postgres" in command:
                    payload = {
                        "status": {
                            "phase": "Cluster in healthy state",
                            "currentPrimary": "tenant-c-postgres-1",
                        }
                    }
                elif "deployment/cnpg-controller-manager" in command:
                    payload = {
                        "spec": {
                            "replicas": 1,
                            "template": {
                                "spec": {
                                    "containers": [{"image": "cnpg@example"}]
                                }
                            },
                        },
                        "status": {"availableReplicas": 1},
                    }
                elif " get pods " in f" {command} ":
                    payload = {
                        "items": [
                            {
                                "metadata": {
                                    "name": f"tenant-c-postgres-{ordinal}"
                                },
                                "spec": {"nodeName": "worker-a"},
                                "status": {
                                    "conditions": [
                                        {"type": "Ready", "status": "True"}
                                    ]
                                },
                            }
                            for ordinal in range(1, observed + 1)
                        ]
                    }
                else:
                    payload = {
                        "items": [
                            {
                                "metadata": {
                                    "name": f"tenant-c-postgres-{ordinal}"
                                },
                                "spec": {"volumeName": f"pv-{ordinal}"},
                                "status": {"phase": "Bound"},
                            }
                            for ordinal in range(1, observed + 1)
                        ]
                    }
                return CompletedProcess(arguments, 0, stdout=json.dumps(payload))

            with patch("scripts.status._tenant_kubectl", side_effect=kubectl):
                return _cnpg_layer_status(
                    Path("."),
                    {
                        "DATABASE_NAMESPACE": "database",
                        "CNPG_NAMESPACE": "cnpg-system",
                        "CNPG_CONTROLLER_IMAGE": "cnpg@example",
                    },
                    tenant,
                )

        one = collect(1, 1)
        self.assertTrue(one["ready"])
        three = collect(3, 3)
        self.assertTrue(three["ready"])
        self.assertEqual(three["nodes"], ["worker-a"])
        self.assertFalse(collect(3, 2)["ready"])

    def test_management_health_requires_complete_exact_inventory(self) -> None:
        result = {
            "host": {"ready": True},
            "management": {"apiReady": True},
            "providers": [{"available": True} for _ in range(4)],
            "components": [{"available": True} for _ in range(7)],
            "auxiliary": {"ready": True},
            "kamaji": {"available": True, "datastoreReady": True},
        }
        self.assertTrue(management_status_healthy(result))
        result["providers"] = result["providers"][:-1]
        self.assertFalse(management_status_healthy(result))

    def test_management_collection_stops_when_api_is_unready(self) -> None:
        with (
            patch(
                "scripts.status.management_status",
                return_value={"apiReady": False},
            ),
            patch(
                "scripts.status.read_inotify",
                side_effect=[128, 1048576],
            ),
            patch("scripts.status.ManagementClient") as client,
        ):
            result = collect_management_status(
                Path("/tmp/example"),
                {
                    "MIN_INOTIFY_INSTANCES": "128",
                    "MIN_INOTIFY_WATCHES": "1048576",
                },
            )
        self.assertFalse(management_status_healthy(result))
        client.assert_not_called()

    def test_strict_tenant_status_propagates_inspection_failure(self) -> None:
        tenant = type(
            "Tenant",
            (),
            {
                "name": "tenant-a",
                "vip": "172.18.0.10",
                "domain": "tenant.test",
                "cnpg_cluster": "postgres",
            },
        )()
        with (
            patch(
                "scripts.status._control_plane_layer_status",
                return_value={"ready": True},
            ),
            patch(
                "scripts.status.network_status",
                side_effect=RuntimeError("tenant API connection refused"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "connection refused"):
                collect_tenant_status(
                    Path("."),
                    {"SPIKE_API_PORT": "6443"},
                    object(),
                    tenant,
                    {},
                    strict=True,
                )

    def test_strict_management_status_propagates_provider_inspection_failure(
        self,
    ) -> None:
        with (
            patch(
                "scripts.status.management_status",
                return_value={"apiReady": True},
            ),
            patch(
                "scripts.status.read_inotify",
                side_effect=[128, 1048576],
            ),
            patch("scripts.status.ManagementClient", return_value=object()),
            patch(
                "scripts.status.provider_status",
                side_effect=RuntimeError("provider API connection refused"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "connection refused"):
                collect_management_status(
                    Path("/tmp/example"),
                    {
                        "MIN_INOTIFY_INSTANCES": "128",
                        "MIN_INOTIFY_WATCHES": "1048576",
                    },
                    strict=True,
                )


if __name__ == "__main__":
    unittest.main()
