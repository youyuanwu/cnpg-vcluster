from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from scripts.cache import VerifiedCache
from scripts.controller_tenant import main as controller_tenant_main
from scripts.lib.controller import (
    _foundation_payload,
    build_controller_image,
    controller_source_digest,
    delete_controller,
)
from scripts.lib.ownership import IdentityRecord


class FakeManagementClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def kubectl(self, *arguments, **kwargs):
        self.calls.append((arguments, kwargs))
        response = next(self.responses)
        if kwargs.get("check", True) and response.returncode != 0:
            raise RuntimeError(response.stderr or response.stdout)
        return response


class ControllerIntegrationUnitTests(unittest.TestCase):
    def test_delete_controller_refuses_failed_crd_inspection(self) -> None:
        client = FakeManagementClient(
            [CompletedProcess([], 1, stdout="", stderr="Forbidden")]
        )
        with self.assertRaisesRegex(RuntimeError, "failed to inspect"):
            delete_controller(Path("."), {"DELETE_TIMEOUT": "1s"}, client)
        self.assertEqual(1, len(client.calls))

    def test_delete_controller_requires_authoritative_tenant_absence(self) -> None:
        client = FakeManagementClient(
            [
                CompletedProcess([], 0, stdout="crd", stderr=""),
                CompletedProcess([], 0, stdout="", stderr=""),
                CompletedProcess([], 0, stdout="tenant.tenancy.cnpg-vcluster.io/a\n", stderr=""),
            ]
        )
        with self.assertRaisesRegex(RuntimeError, "Tenant resources remain"):
            delete_controller(Path("."), {"DELETE_TIMEOUT": "1s"}, client)
        self.assertEqual(3, len(client.calls))

    def test_build_refuses_unverified_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "manager"
            binary.write_text("binary", encoding="utf-8")
            with (
                patch(
                    "scripts.lib.controller.build_controller_binary",
                    return_value=binary,
                ),
                patch(
                    "scripts.lib.controller.verify_all_inputs",
                    side_effect=RuntimeError("unverified"),
                ),
                patch("scripts.lib.controller.run") as run,
            ):
                with self.assertRaisesRegex(RuntimeError, "unverified"):
                    build_controller_image(
                        root,
                        {"TENANT_CONTROLLER_IMAGE_REPOSITORY": "example/controller"},
                    )
            run.assert_not_called()

    def test_controller_digest_includes_assets_and_versions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "controller").mkdir()
            (root / "controller" / "main.go").write_text("package main", encoding="utf-8")
            (root / "config").mkdir()
            (root / "config" / "versions.env").write_text("GO_VERSION=1\n", encoding="utf-8")
            inputs = root / ".tools" / "inputs"
            inputs.mkdir(parents=True)
            (inputs / "calico.yaml").write_text("calico", encoding="utf-8")
            (inputs / "cnpg.yaml").write_text("cnpg", encoding="utf-8")
            config = {
                "GO_VERSION": "1",
                "CONTROLLER_RUNTIME_VERSION": "runtime",
                "CONTROLLER_TOOLS_VERSION": "tools",
            }
            before = controller_source_digest(root, config)
            (inputs / "calico.yaml").write_text("changed", encoding="utf-8")
            self.assertNotEqual(before, controller_source_digest(root, config))
            (inputs / "calico.yaml").write_text("calico", encoding="utf-8")
            config["GO_VERSION"] = "2"
            self.assertNotEqual(before, controller_source_digest(root, config))

    def test_foundation_payload_contains_cache_registry_and_versions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            generation = root / "generation-1"
            cache = VerifiedCache(
                generation=generation,
                inventory={
                    "imageArchives": [
                        {"key": "IMAGE", "sha256": "archive-sha"}
                    ]
                },
                state_sha256="cache-sha",
            )
            config = {
                "GO_VERSION": "1.27.1",
                "KUBERNETES_VERSION": "v1.36.4",
                "CONTROLLER_RUNTIME_VERSION": "v0.24.1",
                "CONTROLLER_TOOLS_VERSION": "v0.21.0",
                "CAPI_CONTRACT": "v1beta2",
                "KAMAJI_CAPI_CONTRACT": "v1beta2",
                "MANAGEMENT_POD_CIDR": "10.0.0.0/16",
            }
            network = {
                "network_id": "network-id",
                "subnet": "172.18.0.0/16",
                "pool_start": "172.18.255.1",
                "pool_end": "172.18.255.2",
            }
            registry = {
                "address": "172.18.0.10",
                "generation": "registry-generation",
                "identifier": "registry-id",
            }
            identity = IdentityRecord(
                kind="container",
                name="management",
                identifier="container-id",
                labels={},
            )
            with patch(
                "scripts.lib.controller.require_management_ownership",
                return_value=identity,
            ):
                payload = _foundation_payload(
                    root,
                    config,
                    network,
                    "controller:image",
                    cache,
                    registry,
                )
            data = json.loads(payload["data"]["foundation.json"])
            self.assertEqual("generation-1", data["cache"]["generation"])
            self.assertEqual("172.18.0.10", data["registry"]["address"])
            self.assertIn("172.18.0.0/16", data["allowedSubnets"])
            self.assertEqual("v0.24.1", data["versions"]["CONTROLLER_RUNTIME_VERSION"])

    def test_private_apply_uses_all_lifecycle_locks(self) -> None:
        calls = []

        @contextmanager
        def lock(name):
            calls.append(f"enter-{name}")
            yield True
            calls.append(f"exit-{name}")

        with (
            patch("scripts.controller_tenant.load_configuration", return_value={}),
            patch("scripts.controller_tenant.e2e_lock", side_effect=lambda *_args, **_kwargs: lock("e2e")),
            patch("scripts.controller_tenant.profile_lock", side_effect=lambda *_args, **_kwargs: lock("profile")),
            patch("scripts.controller_tenant.tools_lock", side_effect=lambda *_args, **_kwargs: lock("tools")),
            patch("scripts.controller_tenant.apply_tenant") as apply,
        ):
            self.assertEqual(0, controller_tenant_main(["apply", "fixture.yaml"]))
        apply.assert_called_once()
        self.assertEqual(
            [
                "enter-e2e",
                "enter-profile",
                "enter-tools",
                "exit-tools",
                "exit-profile",
                "exit-e2e",
            ],
            calls,
        )
