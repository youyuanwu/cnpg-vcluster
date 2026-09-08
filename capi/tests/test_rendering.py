from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.lib.files import IntegrityError
from scripts.lib.rendering import render_release_manifest, replace_known_images


class RenderingTests(unittest.TestCase):
    def test_release_render_replaces_image_and_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.yaml"
            destination = Path(temporary) / "rendered.yaml"
            source.write_text(
                "image: example:v1\narg: ${FEATURE:=false}\nempty: ${VALUE:=\"\"}\n",
                encoding="utf-8",
            )
            render_release_manifest(
                source,
                destination,
                replacements={"example:v1": "example:v1@sha256:" + "a" * 64},
                variables={"FEATURE": "true"},
            )
            rendered = destination.read_text(encoding="utf-8")
            self.assertNotIn("${FEATURE", rendered)
            self.assertIn("arg: true", rendered)
            self.assertIn('empty: ""', rendered)
            self.assertIn("@sha256:", rendered)

    def test_release_render_rejects_unexpected_image_count(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.yaml"
            destination = Path(temporary) / "rendered.yaml"
            source.write_text("kind: ConfigMap\n", encoding="utf-8")
            with self.assertRaises(IntegrityError):
                render_release_manifest(
                    source,
                    destination,
                    replacements={"missing:v1": "missing@sha256:" + "a" * 64},
                    variables={},
                )

    def test_kamaji_image_replacement_rewrites_full_stream(self) -> None:
        config = {}
        content = []
        for name in (
            "KAMAJI",
            "KAMAJI_ETCD",
            "KAMAJI_ETCD_JOB",
            "KAMAJI_KUBECTL_JOB",
        ):
            config[f"{name}_IMAGE_TAGGED"] = f"example/{name.lower()}:v1"
            config[f"{name}_IMAGE"] = f"example/{name.lower()}:v1@sha256:" + "a" * 64
            content.append(config[f"{name}_IMAGE_TAGGED"])
        rendered = replace_known_images("\n".join(content), config)
        self.assertNotIn("example/kamaji:v1\n", rendered)
        self.assertEqual(rendered.count("@sha256:"), 4)

    def test_kamaji_post_render_accepts_partial_hook_stream(self) -> None:
        config = {}
        for name in (
            "KAMAJI",
            "KAMAJI_ETCD",
            "KAMAJI_ETCD_JOB",
            "KAMAJI_KUBECTL_JOB",
        ):
            config[f"{name}_IMAGE_TAGGED"] = f"example/{name.lower()}:v1"
            config[f"{name}_IMAGE"] = f"example/{name.lower()}:v1@sha256:" + "a" * 64
        rendered = replace_known_images(config["KAMAJI_IMAGE_TAGGED"], config)
        self.assertEqual(rendered, config["KAMAJI_IMAGE"])
