from __future__ import annotations

import io
import hashlib
import json
import subprocess
import tempfile
import tarfile
import unittest
import os
from types import SimpleNamespace
from contextlib import contextmanager
from pathlib import Path

from scripts.lib.files import IntegrityError
from scripts.lib.config import load_configuration
from scripts.lib.database_controller import install_azure_database_runtime
from scripts.tools import (
    DOWNLOADS,
    AZURE_CHART_INPUTS,
    _download,
    _install_trunk,
    _install_wasm_bindgen,
    _verify_crd,
    _verify_private_input,
    _verify_tag,
    acquire_azure_charts,
    prepare_tools,
)
from subprocess import CompletedProcess
from unittest.mock import patch


class ToolSchemaTests(unittest.TestCase):
    def test_azure_installer_uses_verified_cache_and_recovers_each_chart(self) -> None:
        payloads = [b"CNPG chart", b"Azure Disk chart"]
        config = {
            "CNPG_CONTROLLER_IMAGE": "ghcr.io/cloudnative-pg/cloudnative-pg:1.30.0@sha256:" + "a" * 64,
            "AZURE_DISK_CSI_IMAGE": "mcr.microsoft.com/oss/azuredisk:v1.32.12@sha256:" + "b" * 64,
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            generation = root / ".tools" / "cache" / "generation"
            inputs = generation / "inputs"
            inputs.mkdir(parents=True)
            for parent in (root / ".tools", root / ".tools" / "cache", generation, inputs):
                parent.chmod(0o700)
            for (name, _, checksum), payload in zip(AZURE_CHART_INPUTS, payloads):
                config[checksum] = hashlib.sha256(payload).hexdigest()
                (inputs / name).write_bytes(payload)
                (inputs / name).chmod(0o600)

            attempts = []

            def helm(arguments, *, timeout, check=True):
                if arguments[1] == "status":
                    self.assertEqual(timeout, 30)
                    self.assertFalse(check)
                    return CompletedProcess(arguments, 1, "", "release not found")
                self.assertEqual(timeout, 150)
                self.assertEqual(arguments[1:3], ["upgrade", "--install"])
                self.assertIn("--atomic", arguments)
                self.assertIn("--wait", arguments)
                release = arguments[3]
                self.assertEqual(
                    arguments[arguments.index("--kubeconfig") + 1],
                    str(root / "tenant.kubeconfig"),
                )
                image = config[
                    "CNPG_CONTROLLER_IMAGE" if release == "cnpg"
                    else "AZURE_DISK_CSI_IMAGE"
                ]
                repository, tag = image.split(":", 1)
                keys = (
                    ("image.repository", "image.tag") if release == "cnpg"
                    else ("image.azuredisk.repository", "image.azuredisk.tag")
                )
                self.assertIn(f"{keys[0]}={repository}", arguments)
                self.assertIn(f"{keys[1]}={tag}", arguments)
                self.assertEqual(
                    Path(arguments[4]).read_bytes(),
                    payloads[0 if release == "cnpg" else 1],
                )
                attempts.append(release)
                if len(attempts) == 1:
                    raise RuntimeError("transient CNPG failure")
                return CompletedProcess(arguments, 0, "", "")

            with (
                patch("scripts.lib.database_controller.verify_cache",
                      return_value=SimpleNamespace(generation=generation)) as verify,
                patch("scripts.lib.database_controller.run", side_effect=helm),
                patch("scripts.tools.urllib.request.urlopen",
                      side_effect=AssertionError("unexpected network")),
            ):
                with self.assertRaisesRegex(RuntimeError, "cnpg: transient"):
                    install_azure_database_runtime(root, config, root / "tenant.kubeconfig")
                self.assertEqual(attempts, ["cnpg", "azuredisk"])
                install_azure_database_runtime(root, config, root / "tenant.kubeconfig")
                self.assertEqual(attempts, ["cnpg", "azuredisk", "cnpg", "azuredisk"])
                self.assertEqual(verify.call_count, 2)
                (inputs / AZURE_CHART_INPUTS[0][0]).write_bytes(b"tampered")
                with self.assertRaises(IntegrityError):
                    install_azure_database_runtime(root, config, root / "tenant.kubeconfig")
                self.assertEqual(len(attempts), 4)
            self.assertFalse(any((root / ".runtime" / "azure-database-installer").iterdir()))

    def test_azure_installer_retries_disk_after_bounded_timeout(self) -> None:
        config = {
            "CNPG_CONTROLLER_IMAGE": "ghcr.io/cloudnative-pg/cloudnative-pg:1.30.0@sha256:" + "a" * 64,
            "AZURE_DISK_CSI_IMAGE": "mcr.microsoft.com/oss/azuredisk:v1.32.12@sha256:" + "b" * 64,
        }
        payloads = (b"cnpg", b"disk")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = root / ".tools" / "cache" / "generation" / "inputs"
            inputs.mkdir(parents=True)
            for parent in (root / ".tools", inputs.parent.parent, inputs.parent, inputs):
                parent.chmod(0o700)
            for (name, _, checksum), data in zip(AZURE_CHART_INPUTS, payloads):
                config[checksum] = hashlib.sha256(data).hexdigest()
                (inputs / name).write_bytes(data)
                (inputs / name).chmod(0o600)
            releases = []

            def helm(args, *, timeout, check=True):
                if args[1] == "status":
                    return CompletedProcess(args, 1, "", "release not found")
                releases.append(args[3])
                self.assertEqual(timeout, 150)
                if args[3] == "azuredisk" and releases.count("azuredisk") == 1:
                    raise subprocess.TimeoutExpired(args, timeout)
                return CompletedProcess(args, 0, "", "")

            with (
                patch("scripts.lib.database_controller.verify_cache",
                      return_value=SimpleNamespace(generation=inputs.parent)),
                patch("scripts.lib.database_controller.run", side_effect=helm),
            ):
                with self.assertRaisesRegex(RuntimeError, "azuredisk:"):
                    install_azure_database_runtime(root, config, root / "tenant.kubeconfig")
                install_azure_database_runtime(root, config, root / "tenant.kubeconfig")
            self.assertEqual(releases, ["cnpg", "azuredisk", "cnpg", "azuredisk"])

    def test_azure_installer_skips_matching_healthy_releases(self) -> None:
        config = {
            "CNPG_CONTROLLER_IMAGE": "ghcr.io/cloudnative-pg/cloudnative-pg:1.30.0@sha256:" + "a" * 64,
            "AZURE_DISK_CSI_IMAGE": "mcr.microsoft.com/oss/azuredisk:v1.32.12@sha256:" + "b" * 64,
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = root / ".tools" / "cache" / "generation" / "inputs"
            inputs.mkdir(parents=True)
            for parent in (root / ".tools", inputs.parent.parent, inputs.parent, inputs):
                parent.chmod(0o700)
            for name, _, checksum in AZURE_CHART_INPUTS:
                data = name.encode()
                config[checksum] = hashlib.sha256(data).hexdigest()
                (inputs / name).write_bytes(data)
                (inputs / name).chmod(0o600)
            descriptions = {}
            installed = []
            missing = set()

            def helm(args, *, timeout, check=True):
                if args[0].endswith("/kubectl"):
                    reference = args[args.index("get") + 1]
                    if reference in missing:
                        return CompletedProcess(args, 0, "", "")
                    if reference.startswith("csidriver/"):
                        return CompletedProcess(args, 0, json.dumps({
                            "metadata": {"name": "disk.csi.azure.com", "uid": "driver-uid"},
                        }), "")
                    namespace = args[args.index("-n") + 1]
                    release = "cnpg" if namespace == "cnpg-system" else "azuredisk"
                    return CompletedProcess(args, 0, json.dumps({
                        "metadata": {
                            "name": reference.split("/", 1)[1], "namespace": namespace,
                            "uid": "workload-uid", "generation": 1,
                        },
                        "spec": {"replicas": 1, "template": {"spec": {"containers": [{
                            "name": "manager" if release == "cnpg" else "azuredisk",
                            "image": config[
                                "CNPG_CONTROLLER_IMAGE" if release == "cnpg"
                                else "AZURE_DISK_CSI_IMAGE"
                            ],
                        }]}}},
                        "status": {
                            "observedGeneration": 1, "availableReplicas": 1,
                            "updatedReplicas": 1, "desiredNumberScheduled": 1,
                            "numberReady": 1, "updatedNumberScheduled": 1,
                        },
                    }), "")
                release = args[2] if args[1] == "status" else args[3]
                if args[1] == "status":
                    if release not in descriptions:
                        return CompletedProcess(args, 1, "", "release not found")
                    return CompletedProcess(
                        args, 0, json.dumps({"info": {
                            "status": "deployed", "description": descriptions[release],
                        }}), "",
                    )
                descriptions[release] = args[args.index("--description") + 1]
                installed.append(release)
                return CompletedProcess(args, 0, "", "")

            with (
                patch("scripts.lib.database_controller.verify_cache",
                      return_value=SimpleNamespace(generation=inputs.parent)),
                patch("scripts.lib.database_controller.run", side_effect=helm),
            ):
                install_azure_database_runtime(root, config, root / "tenant.kubeconfig")
                install_azure_database_runtime(root, config, root / "tenant.kubeconfig")
                self.assertEqual(installed, ["cnpg", "azuredisk"])
                config["AZURE_DISK_CSI_IMAGE"] = (
                    "mcr.microsoft.com/oss/azuredisk:v1.32.12@sha256:" + "c" * 64
                )
                install_azure_database_runtime(root, config, root / "tenant.kubeconfig")
                self.assertEqual(installed, ["cnpg", "azuredisk", "azuredisk"])
                missing.add("deployment/cnpg-cloudnative-pg")
                install_azure_database_runtime(root, config, root / "tenant.kubeconfig")
                self.assertEqual(installed, ["cnpg", "azuredisk", "azuredisk", "cnpg"])
                missing.clear()
                missing.add("csidriver/disk.csi.azure.com")
                install_azure_database_runtime(root, config, root / "tenant.kubeconfig")
                self.assertEqual(installed[-1], "azuredisk")

    def test_azure_chart_cache_is_checksum_verified_and_retries_independently(self) -> None:
        self.assertEqual(
            {"azure-cnpg-0.29.0.tgz", "azure-disk-csi-1.32.12.tgz"},
            {filename for filename, _, _ in AZURE_CHART_INPUTS},
        )
        payloads = [b"cnpg chart", b"disk csi chart"]
        config = {"DOWNLOAD_TIMEOUT": "1s"}
        for (_, url_key, sha_key), payload in zip(AZURE_CHART_INPUTS, payloads):
            config[url_key] = f"https://example.invalid/{url_key}"
            config[sha_key] = hashlib.sha256(payload).hexdigest()

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = root / ".tools" / "inputs"
            inputs.mkdir(parents=True)
            (root / ".tools").chmod(0o700)
            inputs.chmod(0o700)
            cnpg, disk = (inputs / filename for filename, _, _ in AZURE_CHART_INPUTS)
            cnpg.write_bytes(payloads[0])
            disk.write_bytes(payloads[1])
            cnpg.chmod(0o600)
            disk.chmod(0o600)
            with patch("scripts.tools.urllib.request.urlopen", side_effect=AssertionError("network")):
                self.assertEqual((cnpg, disk), acquire_azure_charts(root, config, offline=True))
            disk.write_bytes(b"invalid")
            with self.assertRaises(IntegrityError), patch(
                "scripts.tools.urllib.request.urlopen", side_effect=AssertionError("network")
            ):
                acquire_azure_charts(root, config, offline=True)
            self.assertEqual(payloads[0], cnpg.read_bytes())
            with patch("scripts.tools.urllib.request.urlopen", return_value=io.BytesIO(payloads[1])) as network:
                self.assertEqual((cnpg, disk), acquire_azure_charts(root, config))
            network.assert_called_once()
            self.assertEqual(payloads[1], disk.read_bytes())
            disk.unlink()
            with patch("scripts.tools.urllib.request.urlopen", side_effect=OSError("offline")):
                with self.assertRaises(IntegrityError):
                    acquire_azure_charts(root, config)
            self.assertEqual(payloads[0], cnpg.read_bytes())
            self.assertFalse(disk.exists())

    def test_azure_chart_download_is_bounded_and_never_persists_partial_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / ".tools" / "inputs" / "chart.tgz"
            with patch("scripts.tools.urllib.request.urlopen", return_value=io.BytesIO(b"too big")):
                with self.assertRaisesRegex(IntegrityError, "exceeds"):
                    _download("https://example.invalid/chart", destination, 1, max_bytes=3)
            self.assertFalse(destination.exists())
            with (
                patch("scripts.tools.urllib.request.urlopen", return_value=io.BytesIO(b"slow")),
                patch("scripts.tools.time.monotonic", side_effect=[0, 2]),
            ):
                with self.assertRaisesRegex(IntegrityError, "exceeded 1s"):
                    _download("https://example.invalid/chart", destination, 1, max_bytes=100)
            self.assertFalse(destination.exists())

    def test_admin_build_tool_acquisition_is_exact_and_checksum_pinned(
        self,
    ) -> None:
        config = {
            "DOWNLOAD_TIMEOUT": "1s",
            "TRUNK_VERSION": "0.21.14",
            "TRUNK_URL": "https://example.invalid/trunk.tar.gz",
            "TRUNK_SHA256": "a" * 64,
            "WASM_BINDGEN_VERSION": "0.2.129",
            "WASM_BINDGEN_URL": "https://example.invalid/wasm.tar.gz",
            "WASM_BINDGEN_SHA256": "b" * 64,
        }
        root = Path("/repo/capi")
        trunk = Path("/cache/trunk.tar.gz")
        wasm = Path("/cache/wasm.tar.gz")
        with (
            patch(
                "scripts.tools._ensure_download",
                side_effect=[trunk, wasm],
            ) as download,
            patch("scripts.tools._install_trunk") as install_trunk,
            patch(
                "scripts.tools._install_wasm_bindgen"
            ) as install_wasm,
        ):
            from scripts.tools import acquire_admin_build_tools

            acquire_admin_build_tools(root, config)
        self.assertEqual(2, download.call_count)
        self.assertEqual(
            {
                "trunk-x86_64-unknown-linux-gnu.tar.gz",
                "wasm-bindgen-0.2.129-x86_64-unknown-linux-musl.tar.gz",
            },
            {call.args[1] for call in download.call_args_list},
        )
        install_trunk.assert_called_once_with(
            trunk,
            root / ".tools/bin/trunk",
            "0.21.14",
        )
        install_wasm.assert_called_once_with(
            wasm,
            root / ".tools/bin/wasm-bindgen",
            "0.2.129",
        )

    def test_trunk_download_is_exact_official_release_asset(self) -> None:
        config = load_configuration(Path(__file__).resolve().parents[1])
        self.assertEqual("0.21.14", config["TRUNK_VERSION"])
        self.assertEqual(
            "https://github.com/trunk-rs/trunk/releases/download/v0.21.14/"
            "trunk-x86_64-unknown-linux-gnu.tar.gz",
            config["TRUNK_URL"],
        )
        self.assertEqual(
            "f2b4680cd239693a646a2795e4633c625328d7b2a044fbe749fa3a2fe9e7036b",
            config["TRUNK_SHA256"],
        )
        self.assertIn(
            (
                "trunk-x86_64-unknown-linux-gnu.tar.gz",
                "TRUNK_URL",
                "TRUNK_SHA256",
            ),
            DOWNLOADS,
        )

    def test_trunk_install_extracts_one_binary_and_checks_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "trunk.tar.gz"
            with tarfile.open(archive, "w:gz") as bundle:
                member = tarfile.TarInfo("trunk")
                content = b"trunk executable"
                member.size = len(content)
                bundle.addfile(member, io.BytesIO(content))
            destination = root / "bin" / "trunk"
            with patch(
                "scripts.tools.run",
                return_value=CompletedProcess([], 0, "trunk 0.21.14\n", ""),
            ):
                _install_trunk(archive, destination, "0.21.14")
            self.assertEqual(content, destination.read_bytes())
            self.assertEqual(0o755, destination.stat().st_mode & 0o777)

            with patch(
                "scripts.tools.run",
                return_value=CompletedProcess([], 0, "trunk 0.21.13\n", ""),
            ), self.assertRaises(IntegrityError):
                _install_trunk(archive, destination, "0.21.14")
            self.assertFalse(destination.exists())

    def test_wasm_bindgen_install_extracts_exact_versioned_binary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "wasm-bindgen.tar.gz"
            member_name = (
                "wasm-bindgen-0.2.129-x86_64-unknown-linux-musl/"
                "wasm-bindgen"
            )
            with tarfile.open(archive, "w:gz") as bundle:
                member = tarfile.TarInfo(member_name)
                content = b"wasm-bindgen executable"
                member.size = len(content)
                bundle.addfile(member, io.BytesIO(content))
            destination = root / "bin" / "wasm-bindgen"
            with patch(
                "scripts.tools.run",
                return_value=CompletedProcess(
                    [], 0, "wasm-bindgen 0.2.129\n", ""
                ),
            ):
                _install_wasm_bindgen(archive, destination, "0.2.129")
            self.assertEqual(content, destination.read_bytes())
            self.assertEqual(0o755, destination.stat().st_mode & 0o777)

    def test_prepare_tools_uses_local_cache_without_network_commands(self) -> None:
        verified = object()
        with (
            patch("scripts.cache.verify_cache", return_value=verified),
            patch("scripts.cache.materialize_inputs") as materialize,
            patch("scripts.tools._install_tools") as install,
            patch("scripts.tools.run") as run,
        ):
            result = prepare_tools(Path("example"), {})
        self.assertIs(result, verified)
        materialize.assert_called_once_with(
            Path("example"), {}, verified=verified
        )
        install.assert_called_once_with(
            Path("example"), {}, inputs_verified=True
        )
        run.assert_not_called()

    def test_prepare_tools_verifies_complete_materialized_inputs_before_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = root / ".tools" / "inputs"
            inputs.mkdir(parents=True)
            config = {"CERT_MANAGER_VERSION": "v1"}
            expected = {
                filename for filename, _, _ in DOWNLOADS
            } | {"cert-manager-v1.tgz", "cert-manager-v1.digest"}
            for name in expected:
                (inputs / name).write_text("fixture", encoding="utf-8")
            with (
                patch("scripts.cache.verify_cache", return_value=object()),
                patch("scripts.tools.verify_all_inputs") as verify_inputs,
                patch("scripts.cache.materialize_inputs"),
                patch("scripts.tools._install_tools"),
            ):
                prepare_tools(root, config)
            verify_inputs.assert_called_once_with(root, config, inputs)

    def test_cached_input_rejects_symlink_and_broad_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target"
            target.write_text("content", encoding="utf-8")
            target.chmod(0o600)
            link = root / "link"
            link.symlink_to(target)
            with self.assertRaises(IntegrityError):
                _verify_private_input(link)
            target.chmod(0o644)
            with self.assertRaises(IntegrityError):
                _verify_private_input(target)

    def test_cached_input_symlink_swap_is_not_followed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "input"
            source.write_text("trusted", encoding="utf-8")
            source.chmod(0o600)
            foreign = root / "foreign"
            foreign.write_text("foreign", encoding="utf-8")
            foreign.chmod(0o600)
            original_open = os.open
            swapped = False

            @contextmanager
            def parent_directory(_: Path):
                descriptor = original_open(root, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    yield descriptor
                finally:
                    os.close(descriptor)

            def swapping_open(name, flags, *args, **kwargs):
                nonlocal swapped
                if name == "input" and not swapped:
                    swapped = True
                    source.unlink()
                    source.symlink_to(foreign)
                return original_open(name, flags, *args, **kwargs)

            with (
                patch("scripts.tools.private_directory", parent_directory),
                patch("scripts.tools.os.open", side_effect=swapping_open),
            ):
                with self.assertRaises(IntegrityError):
                    _verify_private_input(source)

    def test_served_and_storage_must_belong_to_requested_version(self) -> None:
        manifest = """\
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: examples.example.io
spec:
  conversion:
    strategy: Webhook
  versions:
  - name: v1beta2
    served: false
    storage: false
  - name: v1beta1
    served: true
    storage: true
"""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "components.yaml"
            path.write_text(manifest, encoding="utf-8")
            with self.assertRaises(IntegrityError):
                _verify_crd(
                    path,
                    "examples.example.io",
                    "v1beta2",
                    conversion="Webhook",
                )

    def test_annotated_tag_requires_peeled_source_commit(self) -> None:
        output = (
                "tag-object refs/tags/v1.2.3\n"
                "source-commit refs/tags/v1.2.3^{}\n"
        )
        with patch(
                "scripts.tools.run",
                return_value=CompletedProcess([], 0, stdout=output, stderr=""),
        ):
                _verify_tag("https://example.invalid/repo.git", "v1.2.3", "source-commit", 1)
                with self.assertRaises(IntegrityError):
                    _verify_tag("https://example.invalid/repo.git", "v1.2.3", "tag-object", 1)

    def test_accepts_requested_served_storage_version(self) -> None:
        manifest = """\
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: examples.example.io
spec:
  conversion:
    strategy: Webhook
  versions:
  - name: v1beta2
    served: true
    storage: true
"""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "components.yaml"
            path.write_text(manifest, encoding="utf-8")
            _verify_crd(
                path,
                "examples.example.io",
                "v1beta2",
                conversion="Webhook",
            )
