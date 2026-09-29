from __future__ import annotations

import io
import tempfile
import tarfile
import unittest
import os
from contextlib import contextmanager
from pathlib import Path

from scripts.lib.files import IntegrityError
from scripts.lib.config import load_configuration
from scripts.tools import (
    DOWNLOADS,
    _install_trunk,
    _install_wasm_bindgen,
    _verify_crd,
    _verify_private_input,
    _verify_tag,
    prepare_tools,
)
from subprocess import CompletedProcess
from unittest.mock import patch


class ToolSchemaTests(unittest.TestCase):
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
