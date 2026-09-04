from __future__ import annotations

import unittest
from pathlib import Path
from subprocess import CompletedProcess
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts.preflight import PreflightError, configured_networks, verify_management_name


BASE = {
    "MANAGEMENT_POD_CIDR": "10.210.0.0/16",
    "MANAGEMENT_SERVICE_CIDR": "10.211.0.0/16",
    "TENANT_A_POD_CIDR": "10.70.0.0/16",
    "TENANT_A_SERVICE_CIDR": "10.140.0.0/16",
    "TENANT_B_POD_CIDR": "10.71.0.0/16",
    "TENANT_B_SERVICE_CIDR": "10.141.0.0/16",
    "SPIKE_POD_CIDR": "10.72.0.0/16",
    "SPIKE_SERVICE_CIDR": "10.142.0.0/16",
}


class PreflightTests(unittest.TestCase):
    def test_accepts_disjoint_networks(self) -> None:
        self.assertEqual(len(configured_networks(BASE)), 8)

    def test_rejects_overlap(self) -> None:
        values = dict(BASE)
        values["SPIKE_POD_CIDR"] = values["TENANT_A_POD_CIDR"]
        with self.assertRaises(PreflightError):
            configured_networks(values)

    def test_rejects_unowned_reserved_management_name(self) -> None:
        config = {
            "KIND_CLUSTER_NAME": "example",
            "OWNERSHIP_LABEL": "example.owner",
        }
        with TemporaryDirectory() as temporary:
            with patch(
                "scripts.preflight.run",
                return_value=CompletedProcess([], 0, stdout="container-id\n", stderr=""),
            ):
                with self.assertRaises(PreflightError):
                    verify_management_name(Path(temporary), config, 30)
