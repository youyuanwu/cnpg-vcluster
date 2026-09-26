from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from scripts.lib.controller_state import (
    activation_ticket,
    require_clean_controller_state,
)

ROOT = Path(__file__).resolve().parents[1]


def response(value="", code=0, error=""):
    if isinstance(value, dict):
        value = json.dumps(value)
    return CompletedProcess([], code, stdout=value, stderr=error)


class Client:
    def __init__(self, handler):
        self.handler = handler

    def kubectl(self, *args, **kwargs):
        result = self.handler(*args, **kwargs)
        if kwargs.get("check", True) and result.returncode:
            raise RuntimeError(result.stderr)
        return result

    def json(self, *args):
        return json.loads(self.kubectl(*args, "-o", "json").stdout)


def clean_handler(*args, **_kwargs):
    if "tenants.tenancy.cnpg-vcluster.io" in args:
        return response("")
    if "leases.coordination.k8s.io" in args:
        return response({"items": []})
    if "namespaces" in args or "secrets" in args:
        return response({"items": []})
    if "get" in args and "-A" in args:
        return response("")
    if "get" in args:
        return response(code=1, error="NotFound")
    return response()


def prepare_root(directory: str) -> Path:
    root = Path(directory)
    target = root / "controller/config"
    target.mkdir(parents=True)
    target.joinpath("management-resources.json").write_bytes(
        (ROOT / "controller/config/management-resources.json").read_bytes()
    )
    return root


class ControllerStateTests(unittest.TestCase):
    def test_generated_management_catalog_is_complete_and_unique(self) -> None:
        catalog = json.loads(
            (ROOT / "controller/config/management-resources.json").read_text()
        )
        identities = {
            (entry["apiVersion"], entry["kind"], entry["plural"])
            for entry in catalog
        }
        self.assertEqual(len(identities), len(catalog))
        for required in (
            ("cluster.x-k8s.io/v1beta2", "Cluster", "clusters"),
            (
                "controlplane.cluster.x-k8s.io/v1alpha2",
                "KamajiControlPlane",
                "kamajicontrolplanes",
            ),
            ("kamaji.clastix.io/v1alpha1", "TenantControlPlane", "tenantcontrolplanes"),
            ("v1", "Namespace", "namespaces"),
            ("v1", "Secret", "secrets"),
            ("coordination.k8s.io/v1", "Lease", "leases"),
        ):
            self.assertIn(required, identities)
        self.assertTrue(
            all(
                set(entry)
                == {
                    "apiVersion",
                    "kind",
                    "plural",
                    "namespaced",
                    "role",
                    "inventoryPolicy",
                    "exemptions",
                }
                for entry in catalog
            )
        )

    def test_activation_ticket_is_bound_to_candidate_and_time(self) -> None:
        ticket = activation_ticket("hash-a", "token-a")
        self.assertEqual(
            ticket["data"]["configurationHash"],
            "hash-a",
        )
        self.assertEqual(ticket["data"]["token"], "token-a")
        self.assertEqual(ticket["data"]["hostClean"], "true")
        self.assertTrue(ticket["data"]["createdAt"].endswith("Z"))

    def test_clean_state_accepts_only_empty_authoritative_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "scripts.lib.controller_state.run",
            return_value=response(""),
        ):
            require_clean_controller_state(prepare_root(directory), Client(clean_handler))

    def test_provider_tenant_lease_and_host_residue_block(self) -> None:
        cases = ("tenant", "provider", "lease", "volume", "container", "legacy-file")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = prepare_root(directory)
                if case == "legacy-file":
                    path = root / ".runtime/management/tenant-endpoints.json"
                    path.parent.mkdir(parents=True)
                    path.write_text("{}")

                def handle(*args, **kwargs):
                    if case == "tenant" and "tenants.tenancy.cnpg-vcluster.io" in args:
                        return response("tenant.tenancy.cnpg-vcluster.io/tenant-a")
                    if case == "provider" and "clusters.cluster.x-k8s.io" in args:
                        return response("cluster.cluster.x-k8s.io/tenant-a")
                    if case == "lease" and "leases.coordination.k8s.io" in args:
                        return response({"items": [{"metadata": {
                            "labels": {"tenancy.cnpg-vcluster.io/slot-id": "slot-a"}
                        }}]})
                    return clean_handler(*args, **kwargs)

                def docker(*args, **_kwargs):
                    command = args[0]
                    if case == "volume" and command[:4] == [
                        "docker", "volume", "ls", "-q"
                    ] and "--filter" not in command:
                        return response("tenant-a-storage")
                    if case == "container" and command[:3] == [
                        "docker", "ps", "-aq"
                    ] and "label=io.x-k8s.kind.role=worker" in command:
                        return response("container-a")
                    return response("")

                with patch(
                    "scripts.lib.controller_state.run",
                    side_effect=docker,
                ), self.assertRaises(RuntimeError):
                    require_clean_controller_state(root, Client(handle))

    def test_inventory_errors_never_mean_absence(self) -> None:
        def handle(*args, **kwargs):
            if "clusters.cluster.x-k8s.io" in args:
                return response(code=1, error="Forbidden")
            return clean_handler(*args, **kwargs)

        with tempfile.TemporaryDirectory() as directory, patch(
            "scripts.lib.controller_state.run",
            return_value=response(""),
        ), self.assertRaisesRegex(RuntimeError, "failed to inspect"):
            require_clean_controller_state(prepare_root(directory), Client(handle))


if __name__ == "__main__":
    unittest.main()
