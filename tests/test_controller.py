from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import contextmanager, nullcontext
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from scripts.cache import VerifiedCache
from scripts.controller_tenant import main as controller_tenant_main
from scripts.test_controller_convergence import _restore_after_gate
from scripts.lib.controller import (
    _foundation_payload,
    _foundation_checksum,
    build_controller_image,
    controller_source_digest,
    delete_controller_tenants,
    delete_tenant_resource,
    delete_controller,
    render_controller_manager,
    stop_controller,
)
from scripts.lib.ownership import IdentityRecord
from scripts.lib.images import WORKER_IMAGE_KEYS


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
    def test_rendered_manager_contains_activation_token(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            template = root / "controller" / "config" / "manager"
            template.mkdir(parents=True)
            (template / "manager.yaml.tpl").write_text(
                "image: ${TENANT_CONTROLLER_IMAGE}\n"
                "version: ${SUPPORTED_KUBERNETES_VERSION}\n"
                "activation: ${CONTROLLER_ACTIVATION_TOKEN}\n",
                encoding="utf-8",
            )
            rendered = render_controller_manager(
                root,
                {"KUBERNETES_VERSION": "v1.36.4"},
                "example/controller:test",
                activation_token="token-a",
            )
            content = rendered.read_text(encoding="utf-8")
            self.assertIn("activation: token-a", content)

    def test_shutdown_stops_old_controller_before_returning(self) -> None:
        client = FakeManagementClient(
            [
                CompletedProcess([], 0, stdout="deployment", stderr=""),
                CompletedProcess([], 0, stdout="", stderr=""),
                CompletedProcess([], 0, stdout='{"items":[]}', stderr=""),
            ]
        )
        stop_controller(
            {"CONDITION_TIMEOUT": "1s"},
            client,
        )
        arguments = [call[0] for call in client.calls]
        self.assertEqual(
            (
                "-n",
                "tenant-system",
                "scale",
                "deployment/tenant-controller",
                "--replicas=0",
            ),
            arguments[1],
        )

    def test_delete_tenant_resource_uses_ordinary_kubernetes_delete(self) -> None:
        client = FakeManagementClient(
            [
                CompletedProcess([], 0, stdout="", stderr=""),
            ]
        )
        delete_tenant_resource(client, "tenant-a", wait=False)
        self.assertEqual(
            (
                "delete",
                "tenant/tenant-a",
                "--ignore-not-found=true",
                "--wait=false",
            ),
            client.calls[-1][0],
        )

    def test_whole_lab_requests_all_deletes_before_waiting(self) -> None:
        client = FakeManagementClient(
            [
                CompletedProcess(
                    [],
                    0,
                    stdout=json.dumps(
                        {
                            "items": [
                                {"metadata": {"name": "tenant-b"}},
                                {"metadata": {"name": "tenant-a"}},
                            ]
                        }
                    ),
                    stderr="",
                ),
                CompletedProcess([], 0, stdout="", stderr=""),
                CompletedProcess([], 0, stdout="", stderr=""),
                CompletedProcess([], 0, stdout="", stderr=""),
                CompletedProcess([], 0, stdout="", stderr=""),
            ]
        )
        delete_controller_tenants({"DELETE_TIMEOUT": "1s"}, client)
        arguments = [call[0] for call in client.calls]
        self.assertEqual(
            ("delete", "tenant/tenant-a", "--ignore-not-found=true", "--wait=false"),
            arguments[1],
        )
        self.assertEqual(
            ("delete", "tenant/tenant-b", "--ignore-not-found=true", "--wait=false"),
            arguments[2],
        )
        self.assertEqual(
            ("wait", "--for=delete", "tenant/tenant-a", "--timeout=1s"),
            arguments[3],
        )
        self.assertEqual(
            ("wait", "--for=delete", "tenant/tenant-b", "--timeout=1s"),
            arguments[4],
        )

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
            inputs = root / ".tools" / "inputs"
            inputs.mkdir(parents=True)
            (inputs / "calico.yaml").write_text("calico", encoding="utf-8")
            (inputs / "cnpg.yaml").write_text("cnpg", encoding="utf-8")
            config = {
                "TENANT_CONTROLLER_IMAGE_REPOSITORY": "example/controller",
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
                patch("scripts.lib.controller.generate_controller"),
                patch("scripts.lib.controller.verify_static_manager"),
                patch("scripts.lib.controller.controller_image", return_value="example/controller:rust"),
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
            inputs = root / ".tools" / "inputs"
            inputs.mkdir(parents=True)
            (inputs / "calico.yaml").write_text("calico", encoding="utf-8")
            (inputs / "cnpg.yaml").write_text("cnpg", encoding="utf-8")
            config = {
                "TENANT_CONTROLLER_IMAGE_REPOSITORY": "example/controller",
                "COMMAND_TIMEOUT": "1s",
            }
            with (
                patch(
                    "scripts.lib.controller.build_controller_binary",
                    return_value=binary,
                ),
                patch("scripts.lib.controller.verify_all_inputs"),
                patch("scripts.lib.controller.generate_controller"),
                patch("scripts.lib.controller.verify_static_manager"),
                patch("scripts.lib.controller.controller_image", return_value="example/controller:rust"),
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

    @patch("scripts.lib.controller.rust_toolchain", return_value=("cargo", "rustc 1.96"))
    def test_controller_digest_includes_assets_and_versions(self, _toolchain) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            root = repository
            (root / "controller").mkdir(parents=True)
            repository.joinpath("Cargo.toml").write_text(
                "[workspace]", encoding="utf-8",
            )
            repository.joinpath("Cargo.lock").write_text(
                "lock", encoding="utf-8",
            )
            repository.joinpath("rust-toolchain.toml").write_text(
                '[toolchain]\nchannel = "stable"\n', encoding="utf-8",
            )
            for filename in ("Cargo.toml", "Dockerfile"):
                (root / "controller" / filename).write_text(filename, encoding="utf-8")
            runtime = root / "database-runtime"
            runtime.mkdir()
            (runtime / "Cargo.toml").write_text("runtime", encoding="utf-8")
            manager = root / "controller/config/manager/manager.yaml.tpl"
            manager.parent.mkdir(parents=True)
            manager.write_text("manager")
            inputs = root / ".tools" / "inputs"
            inputs.mkdir(parents=True)
            (inputs / "calico.yaml").write_text("calico", encoding="utf-8")
            (inputs / "cnpg.yaml").write_text("cnpg", encoding="utf-8")
            config = {
                "KUBERNETES_VERSION": "v1.36.4",
            }
            before = controller_source_digest(root, config)
            (inputs / "calico.yaml").write_text("changed", encoding="utf-8")
            self.assertNotEqual(before, controller_source_digest(root, config))
            (inputs / "calico.yaml").write_text("calico", encoding="utf-8")
            config["KUBERNETES_VERSION"] = "v1.36.5"
            self.assertNotEqual(before, controller_source_digest(root, config))

    def test_foundation_payload_contains_cache_registry_and_slots(self) -> None:
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
                "KUBERNETES_VERSION": "v1.36.4",
                "CAPI_VERSION": "v1.14.1",
                "KAMAJI_CAPI_VERSION": "v0.20.0",
                "CAPI_CONTRACT": "v1beta2",
                "KAMAJI_CAPI_CONTRACT": "v1beta2",
                "MANAGEMENT_POD_CIDR": "10.0.0.0/16",
                "IMAGE": "example/image:v1@sha256:" + "c" * 64,
                "IMAGE_TAGGED": "example/image:v1",
                "OWNERSHIP_LABEL": "example.io/owned",
                "LAB_PREFIX": "example",
                "SPIKE_API_PORT": "6443",
                "SPIKE_API_VIP_SLOT": "2",
                "SPIKE_CLUSTER_DOMAIN": "example.local",
                "KIND_NODE_IMAGE": "kindest/node:v1@sha256:" + "d" * 64,
                "SPIKE_STORAGE_CONTAINER_PATH": "/var/lib/example",
                "KONNECTIVITY_SERVER_IMAGE": "example/server:v1@sha256:" + "e" * 64,
                "KONNECTIVITY_AGENT_IMAGE": "example/agent:v1@sha256:" + "f" * 64,
            }
            network = {
                "network_id": "network-id",
                "subnet": "172.18.0.0/16",
                "pool_start": "172.18.255.223",
                "pool_end": "172.18.255.238",
                "slots": {"spike": "172.18.255.225"},
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
            (root / "config").mkdir()
            (root / "config/tenant-allocation-slots.json").write_text(
                (Path(__file__).resolve().parents[1] / "config/tenant-allocation-slots.json").read_text()
            )
            for key in WORKER_IMAGE_KEYS:
                config[key] = config["IMAGE"]
                config[f"{key}_TAGGED"] = config["IMAGE_TAGGED"]
                cache.inventory["imageArchives"].append({
                    "key": key, "path": f"images/{key}.tar", "sha256": "a" * 64,
                })
            with patch(
                "scripts.lib.management.require_management_ownership",
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
            self.assertEqual(3, data["schema"])
            self.assertEqual(15, len(data["slots"]))
            self.assertNotIn("versions", data)
            self.assertEqual("images/image.tar", data["cache"]["imageArchives"][0]["path"])
            self.assertEqual(
                "docker.io/example/image:v1",
                data["cache"]["imageArchives"][0]["tagged"],
            )
            self.assertEqual("/var/lib/example", data["inputs"]["storageContainerPath"])
            original_hash = payload["data"]["foundation.sha256"]
            data["controllerImage"] = "controller:replacement"
            self.assertEqual(original_hash, _foundation_checksum(data))

    def test_public_apply_uses_current_local_locks(self) -> None:
        calls = []

        @contextmanager
        def lock(name):
            calls.append(f"enter-{name}")
            yield True
            calls.append(f"exit-{name}")

        with (
            patch("scripts.controller_tenant.load_configuration", return_value={}),
            patch("scripts.controller_tenant.e2e_lock", side_effect=lambda *_args, **_kwargs: lock("e2e")),
            patch("scripts.controller_tenant.tools_lock", side_effect=lambda *_args, **_kwargs: lock("tools")),
            patch("scripts.controller_tenant.apply_tenant") as apply,
        ):
            self.assertEqual(0, controller_tenant_main(["apply", "fixture.yaml"]))
        apply.assert_called_once()
        self.assertEqual(
            [
                "enter-e2e",
                "enter-tools",
                "exit-tools",
                "exit-e2e",
            ],
            calls,
        )

    def test_public_delete_uses_current_local_locks(self) -> None:
        calls = []

        @contextmanager
        def lock(name):
            calls.append(f"enter-{name}")
            yield True
            calls.append(f"exit-{name}")

        with (
            patch("scripts.controller_tenant.load_configuration", return_value={}),
            patch("scripts.controller_tenant.e2e_lock", side_effect=lambda *_args, **_kwargs: lock("e2e")),
            patch("scripts.controller_tenant.tools_lock", side_effect=lambda *_args, **_kwargs: lock("tools")),
            patch("scripts.controller_tenant.delete_controller_tenant") as delete,
        ):
            self.assertEqual(0, controller_tenant_main(["delete", "tenant-a"]))
        delete.assert_called_once_with(Path(__file__).resolve().parents[1], {}, "tenant-a")
        self.assertEqual(
            [
                "enter-e2e",
                "enter-tools",
                "exit-tools",
                "exit-e2e",
            ],
            calls,
        )

    def test_public_cache_clear_does_not_require_configuration(self) -> None:
        with (
            patch("scripts.controller_tenant.load_configuration") as load,
            patch(
                "scripts.controller_tenant.e2e_lock",
                return_value=nullcontext(),
            ),
            patch(
                "scripts.controller_tenant.tools_lock",
                return_value=nullcontext(),
            ),
            patch(
                "scripts.controller_tenant.clear_tenant_kubeconfig",
                return_value=True,
            ) as clear,
        ):
            self.assertEqual(
                0,
                controller_tenant_main(["clear-cache", "tenant-a"]),
            )
        load.assert_not_called()
        clear.assert_called_once_with(
            Path(__file__).resolve().parents[1],
            "tenant-a",
        )

    def test_public_cache_clear_all_uses_exact_operation(self) -> None:
        with (
            patch(
                "scripts.controller_tenant.e2e_lock",
                return_value=nullcontext(),
            ),
            patch(
                "scripts.controller_tenant.tools_lock",
                return_value=nullcontext(),
            ),
            patch(
                "scripts.controller_tenant.clear_all_tenant_kubeconfigs",
                return_value=["tenant-a", "tenant-b"],
            ) as clear,
        ):
            self.assertEqual(
                0,
                controller_tenant_main(["clear-cache", "--all"]),
            )
        clear.assert_called_once_with(Path(__file__).resolve().parents[1])

    def test_e2e_child_reuses_parent_lock(self) -> None:
        with (
            patch.dict(os.environ, {"CAPI_E2E_CHILD": "1"}),
            patch("scripts.controller_tenant.load_configuration", return_value={}),
            patch("scripts.controller_tenant.e2e_lock") as e2e,
            patch("scripts.controller_tenant.tools_lock") as tools,
            patch("scripts.controller_tenant.apply_tenant"),
        ):
            tools.return_value = nullcontext()
            self.assertEqual(0, controller_tenant_main(["apply", "fixture.yaml"]))
        e2e.assert_not_called()

    def test_gate_cleanup_failure_is_attached_to_primary_error(self) -> None:
        client = FakeManagementClient(
            [CompletedProcess([], 1, stdout="", stderr="cleanup blocked")]
        )
        primary = RuntimeError("primary failure")
        with (
            patch(
                "scripts.test_controller_convergence._tenant",
                side_effect=[{"metadata": {"name": "controller-phase2"}}, {"metadata": {"name": "controller-phase2"}}],
            ),
            patch(
                "scripts.test_controller_convergence.delete_tenant_resource",
                return_value=CompletedProcess([], 1, stdout="", stderr="cleanup blocked"),
            ),
        ):
            _restore_after_gate(
                {"DELETE_TIMEOUT": "1s"},
                client,
                primary,
            )
        self.assertTrue(
            any("cleanup is incomplete" in note for note in primary.__notes__)
        )

    def test_gate_inspection_failure_is_attached_to_primary_error(self) -> None:
        client = FakeManagementClient([])
        primary = RuntimeError("primary failure")
        with (
            patch(
                "scripts.test_controller_convergence._tenant",
                side_effect=RuntimeError("inspection failed"),
            ),
        ):
            _restore_after_gate(
                {"DELETE_TIMEOUT": "1s"},
                client,
                primary,
            )
        self.assertTrue(
            any("inspection failed" in note for note in primary.__notes__)
        )
