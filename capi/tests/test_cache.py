from __future__ import annotations

import io
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from scripts.cache import (
    ACTIVE_SCHEMA,
    CACHE_SCHEMA,
    IMAGE_PLATFORM,
    _verify_archive_metadata,
    restore_host_image,
    verify_cache,
)
from scripts.lib.files import IntegrityError


EXACT = "example.invalid/lab/image:v1@sha256:" + "a" * 64
TAGGED = "example.invalid/lab/image:v1"


def write_archive(path: Path, *, digest: str = "sha256:" + "a" * 64) -> None:
    index = {
        "schemaVersion": 2,
        "manifests": [
            {
                "digest": digest,
                "annotations": {"io.containerd.image.name": TAGGED},
            }
        ],
    }
    with tarfile.open(path, "w") as archive:
        data = json.dumps(index).encode()
        member = tarfile.TarInfo("index.json")
        member.size = len(data)
        archive.addfile(member, io.BytesIO(data))
    path.chmod(0o600)


class CacheTests(unittest.TestCase):
    def test_archive_requires_tagged_and_digest_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "image.tar"
            write_archive(archive)
            _verify_archive_metadata(archive, TAGGED, EXACT)
            write_archive(archive, digest="sha256:" + "b" * 64)
            with self.assertRaises(IntegrityError):
                _verify_archive_metadata(archive, TAGGED, EXACT)

    def test_complete_inventory_verifies_and_tampering_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            generation = root / ".tools/cache/generations/g1"
            images = generation / "images"
            images.mkdir(parents=True, mode=0o700)
            for directory in (
                root / ".tools",
                root / ".tools/cache",
                root / ".tools/cache/generations",
                generation,
                images,
            ):
                directory.chmod(0o700)
            archive = images / "test_image.tar"
            write_archive(archive)
            import hashlib

            checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
            requirements = {
                "inputs": [],
                "authoredInputs": [],
                "provenance": [],
                "images": [
                    {
                        "key": "TEST_IMAGE",
                        "tagged": TAGGED,
                        "digest": EXACT,
                        "platform": IMAGE_PLATFORM,
                    }
                ],
            }
            inventory = {
                "schema": CACHE_SCHEMA,
                "platform": IMAGE_PLATFORM,
                "requirements": requirements,
                "imageArchives": [
                    {
                        "key": "TEST_IMAGE",
                        "path": "images/test_image.tar",
                        "sha256": checksum,
                    }
                ],
            }
            (generation / "inventory.json").write_text(
                json.dumps(inventory), encoding="utf-8"
            )
            (generation / "inventory.json").chmod(0o600)
            active = root / ".tools/cache/active.json"
            active.write_text(
                json.dumps({"schema": ACTIVE_SCHEMA, "generation": "g1"}),
                encoding="utf-8",
            )
            active.chmod(0o600)
            config = {"TEST_IMAGE": EXACT, "TEST_IMAGE_TAGGED": TAGGED}
            with (
                patch("scripts.cache._requirements", return_value=requirements),
                patch("scripts.cache.verify_all_inputs"),
            ):
                verify_cache(root, config)
                with archive.open("ab") as output:
                    output.write(b"tampered")
                with self.assertRaises(IntegrityError):
                    verify_cache(root, config)

    def test_missing_active_generation_fails_without_creating_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            active = root / ".tools/cache/active.json"
            active.parent.mkdir(parents=True, mode=0o700)
            (root / ".tools").chmod(0o700)
            (root / ".tools/cache").chmod(0o700)
            active.write_text(
                json.dumps({"schema": ACTIVE_SCHEMA, "generation": "missing"}),
                encoding="utf-8",
            )
            active.chmod(0o600)
            with self.assertRaises(IntegrityError):
                verify_cache(root, {"TEST_IMAGE": EXACT, "TEST_IMAGE_TAGGED": TAGGED})
            self.assertFalse((root / ".tools/cache/generations/missing").exists())

    def test_restore_loads_archive_then_requires_exact_repo_digest(self) -> None:
        config = {
            "DOWNLOAD_TIMEOUT": "1s",
            "TEST_IMAGE": EXACT,
            "TEST_IMAGE_TAGGED": TAGGED,
        }
        missing = CompletedProcess([], 1, stdout="", stderr="missing")
        present = CompletedProcess(
            [],
            0,
            stdout=json.dumps(["example.invalid/lab/image@sha256:" + "a" * 64]),
            stderr="",
        )
        with (
            patch("scripts.cache.archive_path", return_value=Path("/cache/image.tar")),
            patch("scripts.cache.run", side_effect=[missing, CompletedProcess([], 0, "", ""), present]) as run,
        ):
            restore_host_image(Path("/repo"), config, "TEST_IMAGE")
        self.assertIn(
            ["docker", "image", "load", "--input", "/cache/image.tar"],
            [call.args[0] for call in run.call_args_list],
        )


if __name__ == "__main__":
    unittest.main()
