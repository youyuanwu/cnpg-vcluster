from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts.network import run_network_gate


class NetworkFailureTests(unittest.TestCase):
    def test_partial_network_failure_cleans_tenant(self) -> None:
        client = Mock()
        tenant = Mock()
        with patch(
            "scripts.network.run_endpoint_gate",
            return_value=(client, tenant, {}),
        ), patch(
            "scripts.network.apply_addons",
            side_effect=RuntimeError("injected"),
        ), patch("scripts.network.delete_addons") as delete_addons, patch(
            "scripts.network.delete_tenant"
        ) as delete_tenant:
            with self.assertRaises(RuntimeError):
                run_network_gate(Path("."), {}, cleanup=False)
        delete_addons.assert_called_once()
        delete_tenant.assert_called_once()
