from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.status import (
    collect_management_status,
    collect_tenant_status,
    management_status_healthy,
)


class StatusTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
