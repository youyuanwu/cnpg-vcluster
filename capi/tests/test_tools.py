from __future__ import annotations

import tempfile
import unittest
import os
from contextlib import contextmanager
from pathlib import Path

from scripts.lib.files import IntegrityError
from scripts.tools import _verify_crd, _verify_private_input, _verify_tag, prepare_tools
from subprocess import CompletedProcess
from unittest.mock import patch


class ToolSchemaTests(unittest.TestCase):
    def test_prepare_tools_uses_local_cache_without_network_commands(self) -> None:
        with (
            patch("scripts.cache.verify_cache"),
            patch("scripts.cache.materialize_inputs"),
            patch("scripts.tools._install_tools"),
            patch("scripts.tools.run") as run,
        ):
            prepare_tools(Path("/tmp/example"), {})
        run.assert_not_called()

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
