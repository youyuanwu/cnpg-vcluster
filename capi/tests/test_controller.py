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
from scripts.test_controller_phase2 import _restore_after_gate
from scripts.lib.controller import (
    _foundation_payload,
    _foundation_checksum,
    build_controller_image,
    controller_source_digest,
    delete_controller,
    set_controller_mutation,
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

    def json(self, *arguments):
        return json.loads(self.kubectl(*arguments, "-o", "json").stdout)


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

    def test_build_stages_verified_assets_and_cleans_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "manager"
            binary.write_text("binary", encoding="utf-8")
            (root / "controller").mkdir()
            (root / "controller" / "Dockerfile").write_text(
                "FROM scratch\n", encoding="utf-8"
            )
            (root / "controller" / "main.go").write_text(
                "package main\n", encoding="utf-8"
            )
            (root / "config").mkdir()
            (root / "config" / "versions.env").write_text(
                "GO_VERSION=1\n", encoding="utf-8"
            )
            inputs = root / ".tools" / "inputs"
            inputs.mkdir(parents=True)
            (inputs / "calico.yaml").write_text("calico", encoding="utf-8")
            (inputs / "cnpg.yaml").write_text("cnpg", encoding="utf-8")
            config = {
                "TENANT_CONTROLLER_IMAGE_REPOSITORY": "example/controller",
                "GO_VERSION": "1",
                "CONTROLLER_RUNTIME_VERSION": "runtime",
                "CONTROLLER_TOOLS_VERSION": "tools",
                "COMMAND_TIMEOUT": "1s",
            }

            def verify_build(command, **_kwargs):
                build_root = root / ".runtime" / "rendered" / "controller-build"
                self.assertEqual("docker", command[0])
                self.assertEqual("binary", (build_root / "manager").read_text())
                self.assertEqual("calico", (build_root / "assets" / "calico.yaml").read_text())
                self.assertEqual("cnpg", (build_root / "assets" / "cnpg.yaml").read_text())
                return CompletedProcess(command, 0, stdout="", stderr="")

            with (
                patch(
                    "scripts.lib.controller.build_controller_binary",
                    return_value=binary,
                ),
                patch("scripts.lib.controller.verify_all_inputs") as verify,
                patch("scripts.lib.controller.run", side_effect=verify_build),
            ):
                image = build_controller_image(root, config)
            verify.assert_called_once_with(root, config)
            self.assertTrue(image.startswith("example/controller:"))
            self.assertFalse(
                (root / ".runtime" / "rendered" / "controller-build").exists()
            )

    def test_build_cleans_context_after_docker_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "manager"
            binary.write_text("binary", encoding="utf-8")
            (root / "controller").mkdir()
            (root / "controller" / "Dockerfile").write_text(
                "FROM scratch\n", encoding="utf-8"
            )
            (root / "controller" / "main.go").write_text(
                "package main\n", encoding="utf-8"
            )
            (root / "config").mkdir()
            (root / "config" / "versions.env").write_text(
                "GO_VERSION=1\n", encoding="utf-8"
            )
            inputs = root / ".tools" / "inputs"
            inputs.mkdir(parents=True)
            (inputs / "calico.yaml").write_text("calico", encoding="utf-8")
            (inputs / "cnpg.yaml").write_text("cnpg", encoding="utf-8")
            config = {
                "TENANT_CONTROLLER_IMAGE_REPOSITORY": "example/controller",
                "GO_VERSION": "1",
                "CONTROLLER_RUNTIME_VERSION": "runtime",
                "CONTROLLER_TOOLS_VERSION": "tools",
                "COMMAND_TIMEOUT": "1s",
            }
            with (
                patch(
                    "scripts.lib.controller.build_controller_binary",
                    return_value=binary,
                ),
                patch("scripts.lib.controller.verify_all_inputs"),
                patch(
                    "scripts.lib.controller.run",
                    side_effect=RuntimeError("docker failed"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "docker failed"):
                    build_controller_image(root, config)
            self.assertFalse(
                (root / ".runtime" / "rendered" / "controller-build").exists()
            )

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
            active = root / ".tools" / "cache" / "active.json"
            active.parent.mkdir(parents=True)
            active.write_text(
                '{"generation":"generation-1","schema":1}\n',
                encoding="utf-8",
            )
            cache = VerifiedCache(
                generation=generation,
                inventory={
                    "imageArchives": [
                        {
                            "key": "IMAGE",
                            "path": "images/image.tar",
                            "sha256": "a" * 64,
                        }
                    ]
                },
                state_sha256="b" * 64,
            )
            config = {
                "GO_VERSION": "1.27.1",
                "KUBERNETES_VERSION": "v1.36.4",
                "CAPI_VERSION": "v1.14.1",
                "KAMAJI_CAPI_VERSION": "v0.20.0",
                "CONTROLLER_RUNTIME_VERSION": "v0.24.1",
                "CONTROLLER_TOOLS_VERSION": "v0.21.0",
                "CAPI_CONTRACT": "v1beta2",
                "KAMAJI_CAPI_CONTRACT": "v1beta2",
                "MANAGEMENT_POD_CIDR": "10.0.0.0/16",
                "IMAGE": "example/image:v1@sha256:" + "c" * 64,
                "IMAGE_TAGGED": "example/image:v1",
                "OWNERSHIP_LABEL": "example.io/owned",
                "LAB_PREFIX": "example",
                "SPIKE_API_PORT": "6443",
                "SPIKE_CLUSTER_DOMAIN": "example.local",
                "KIND_NODE_IMAGE": "kindest/node:v1@sha256:" + "d" * 64,
                "SPIKE_STORAGE_CONTAINER_PATH": "/var/lib/example",
                "KONNECTIVITY_SERVER_IMAGE": "example/server:v1@sha256:" + "e" * 64,
                "KONNECTIVITY_AGENT_IMAGE": "example/agent:v1@sha256:" + "f" * 64,
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
                labels={
                    "io.x-k8s.kind.cluster": "management",
                    "io.x-k8s.kind.role": "control-plane",
                },
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
            self.assertEqual("images/image.tar", data["cache"]["imageArchives"][0]["path"])
            self.assertEqual(
                "docker.io/example/image:v1",
                data["cache"]["imageArchives"][0]["tagged"],
            )
            self.assertEqual("/var/lib/example", data["inputs"]["storageContainerPath"])
            original_hash = payload["data"]["foundation.sha256"]
            data["mutationEnabled"] = not data["mutationEnabled"]
            self.assertEqual(original_hash, _foundation_checksum(data))

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

    def test_temporary_mutation_updates_foundation_before_manager(self) -> None:
        foundation = json.dumps(
            {"schema": 2, "mutationEnabled": False},
            sort_keys=True,
            separators=(",", ":"),
        )
        deployment = {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "manager",
                                "args": [
                                    "--leader-elect=true",
                                    "--mutation-enabled=false",
                                ],
                            }
                        ]
                    }
                }
            }
        }
        client = FakeManagementClient(
            [
                CompletedProcess(
                    [],
                    0,
                    stdout=json.dumps(
                        {"data": {"foundation.json": foundation}}
                    ),
                    stderr="",
                ),
                CompletedProcess([], 0, stdout=json.dumps(deployment), stderr=""),
                CompletedProcess([], 0, stdout="", stderr=""),
                CompletedProcess([], 0, stdout="", stderr=""),
                CompletedProcess([], 0, stdout="", stderr=""),
                CompletedProcess([], 0, stdout="", stderr=""),
            ]
        )
        set_controller_mutation(
            {
                "CONDITION_TIMEOUT": "1s",
                "KUBERNETES_VERSION": "v1.36.4",
            },
            client,
            enabled=True,
        )
        arguments = [call[0] for call in client.calls]
        self.assertIn("configmap/tenant-foundation", arguments[2])
        self.assertIn("deployment/tenant-controller", arguments[3])
        deployment_patch = json.loads(
            client.calls[3][0][client.calls[3][0].index("-p") + 1]
        )
        self.assertIn(
            "--mutation-enabled=true",
            deployment_patch["spec"]["template"]["spec"]["containers"][0]["args"],
        )

    def test_gate_cleanup_failure_still_disables_mutation(self) -> None:
        client = FakeManagementClient(
            [CompletedProcess([], 1, stdout="", stderr="cleanup blocked")]
        )
        primary = RuntimeError("primary failure")
        with (
            patch(
                "scripts.test_controller_phase2._tenant",
                side_effect=[{"metadata": {"name": "controller-phase2"}}, {"metadata": {"name": "controller-phase2"}}],
            ),
            patch(
                "scripts.test_controller_phase2.set_controller_mutation"
            ) as mutation,
        ):
            _restore_after_gate(
                {"DELETE_TIMEOUT": "1s"},
                client,
                primary,
            )
        mutation.assert_called_once_with(
            {"DELETE_TIMEOUT": "1s"},
            client,
            enabled=False,
        )
        self.assertTrue(
            any("cleanup is incomplete" in note for note in primary.__notes__)
        )

    def test_gate_inspection_failure_still_disables_mutation(self) -> None:
        client = FakeManagementClient([])
        primary = RuntimeError("primary failure")
        with (
            patch(
                "scripts.test_controller_phase2._tenant",
                side_effect=RuntimeError("inspection failed"),
            ),
            patch(
                "scripts.test_controller_phase2.set_controller_mutation"
            ) as mutation,
        ):
            _restore_after_gate(
                {"DELETE_TIMEOUT": "1s"},
                client,
                primary,
            )
        mutation.assert_called_once()
        self.assertTrue(
            any("inspection failed" in note for note in primary.__notes__)
        )
