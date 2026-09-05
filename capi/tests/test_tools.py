from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.lib.files import IntegrityError
from scripts.tools import _verify_crd, _verify_tag
from subprocess import CompletedProcess
from unittest.mock import patch


class ToolSchemaTests(unittest.TestCase):
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
