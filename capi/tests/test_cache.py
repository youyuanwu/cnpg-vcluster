from __future__ import annotations

import http.client
import io
import hashlib
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import MagicMock, call, patch

from scripts.cache import (
    ACTIVE_SCHEMA,
    CACHE_SCHEMA,
    IMAGE_PLATFORM,
    VerifiedCache,
    _requirements,
    _registry_get,
    _verify_archive_metadata,
    restore_host_image,
    acquire_cache,
    verify_cache,
)
from scripts.lib.files import IntegrityError
from scripts.lib.files import write_private_file as real_write_private_file
from scripts.lib.config import load_configuration


TAGGED = "example.invalid/lab/image:v1"
CONFIG_BYTES = b'{"architecture":"amd64","os":"linux"}'
LAYER_BYTES = b"layer"
CONFIG_DIGEST = "sha256:" + hashlib.sha256(CONFIG_BYTES).hexdigest()
LAYER_DIGEST = "sha256:" + hashlib.sha256(LAYER_BYTES).hexdigest()
PLATFORM_MANIFEST = json.dumps(
    {
        "schemaVersion": 2,
        "config": {"digest": CONFIG_DIGEST},
        "layers": [{"digest": LAYER_DIGEST}],
    },
    sort_keys=True,
    separators=(",", ":"),
).encode()
PLATFORM_DIGEST = "sha256:" + hashlib.sha256(PLATFORM_MANIFEST).hexdigest()
SOURCE_INDEX = json.dumps(
    {
        "schemaVersion": 2,
        "manifests": [
            {
                "digest": PLATFORM_DIGEST,
                "platform": {"os": "linux", "architecture": "amd64"},
            }
        ],
    },
    sort_keys=True,
    separators=(",", ":"),
).encode()
SOURCE_DIGEST = "sha256:" + hashlib.sha256(SOURCE_INDEX).hexdigest()
EXACT = f"example.invalid/lab/image:v1@{SOURCE_DIGEST}"


def write_archive(
    path: Path,
    *,
    tagged: str = TAGGED,
    architecture: str = "amd64",
) -> str:
    source_index = json.dumps(
        {
            "schemaVersion": 2,
            "manifests": [
                {
                    "digest": PLATFORM_DIGEST,
                    "platform": {"os": "linux", "architecture": architecture},
                }
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    source_digest = "sha256:" + hashlib.sha256(source_index).hexdigest()
    index = {
        "schemaVersion": 2,
        "manifests": [
            {
                "digest": source_digest,
                "annotations": {"io.containerd.image.name": tagged},
            }
        ],
    }
    with tarfile.open(path, "w") as archive:
        def add(name: str, data: bytes) -> None:
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))

        add("index.json", json.dumps(index).encode())
        add(f"blobs/sha256/{source_digest.removeprefix('sha256:')}", source_index)
        add(
            f"blobs/sha256/{PLATFORM_DIGEST.removeprefix('sha256:')}",
            PLATFORM_MANIFEST,
        )
        add(f"blobs/sha256/{CONFIG_DIGEST.removeprefix('sha256:')}", CONFIG_BYTES)
        add(f"blobs/sha256/{LAYER_DIGEST.removeprefix('sha256:')}", LAYER_BYTES)
    path.chmod(0o600)
    return f"example.invalid/lab/image:v1@{source_digest}"


class CacheTests(unittest.TestCase):
    def test_cargo_lock_and_compiler_are_cache_requirements(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            controller = root / "controller"
            controller.mkdir()
            (controller / "Cargo.toml").write_text("[package]\n")
            (controller / "Cargo.lock").write_text("locked inputs")
            config = load_configuration(Path(__file__).resolve().parents[1])
            with patch("scripts.lib.controller.rust_toolchain",
                       return_value=("cargo", {}, "compiler A")):
                original = _requirements(config, root)
                self.assertEqual(original["cargo"]["lockSha256"],
                                 hashlib.sha256(b"locked inputs").hexdigest())
            with patch("scripts.lib.controller.rust_toolchain",
                       return_value=("cargo", {}, "compiler B")):
                self.assertNotEqual(original, _requirements(config, root))
            (controller / "Cargo.lock").write_text("changed")
            with patch("scripts.lib.controller.rust_toolchain",
                       return_value=("cargo", {}, "compiler A")):
                self.assertNotEqual(original, _requirements(config, root))
            (controller / "Cargo.lock").unlink()
            with self.assertRaisesRegex(IntegrityError, "Cargo.lock"):
                _requirements(config, root)

    def test_verified_cache_rejects_missing_offline_cargo_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "controller").mkdir()
            (root / "controller/Cargo.toml").write_text("[package]\n")
            with (
                patch("scripts.cache.active_generation", return_value=root),
                patch("scripts.cache._requirements", return_value={"cargo": {}}),
                patch("scripts.cache._load_inventory_header", return_value={}),
                patch("scripts.cache._cache_state_sha256", return_value="state"),
                patch("scripts.lib.controller.fetch_controller_dependencies",
                      side_effect=RuntimeError("missing locked crate")) as fetch,
            ):
                with self.assertRaisesRegex(RuntimeError, "missing locked crate"):
                    verify_cache(root, {})
                fetch.assert_called_once_with(root, {}, offline=True)

    def test_online_cache_does_not_publish_before_locked_cargo_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "controller").mkdir()
            (root / "controller/Cargo.toml").write_text("[package]\n")
            with (
                patch("scripts.cache.acquire_tools"),
                patch("scripts.lib.controller.fetch_controller_dependencies",
                      side_effect=RuntimeError("locked fetch failed")) as fetch,
            ):
                with self.assertRaisesRegex(RuntimeError, "locked fetch failed"):
                    acquire_cache(root, {"DOWNLOAD_TIMEOUT": "1s"})
            fetch.assert_called_once_with(root, {"DOWNLOAD_TIMEOUT": "1s"})
            self.assertFalse((root / ".tools/cache/active.json").exists())
            self.assertEqual([], list((root / ".tools/cache/generations").iterdir()))

    def test_registry_get_retries_incomplete_response_body(self) -> None:
        incomplete = MagicMock()
        incomplete.__enter__.return_value.read.side_effect = (
            http.client.IncompleteRead(b"partial", 8)
        )
        complete = MagicMock()
        complete.__enter__.return_value.read.return_value = b"complete"
        with (
            patch(
                "scripts.cache.urllib.request.urlopen",
                side_effect=[incomplete, complete],
            ) as urlopen,
            patch("scripts.cache.time.sleep") as sleep,
        ):
            data = _registry_get(
                "registry.example",
                "project/image",
                "blobs/sha256:example",
                30,
            )
        self.assertEqual(b"complete", data)
        self.assertEqual(2, urlopen.call_count)
        sleep.assert_called_once_with(1)

    def test_registry_get_rejects_repeated_incomplete_response_body(self) -> None:
        responses = []
        for _ in range(4):
            response = MagicMock()
            response.__enter__.return_value.read.side_effect = (
                http.client.IncompleteRead(b"partial", 8)
            )
            responses.append(response)
        with (
            patch(
                "scripts.cache.urllib.request.urlopen",
                side_effect=responses,
            ) as urlopen,
            patch("scripts.cache.time.sleep") as sleep,
        ):
            with self.assertRaisesRegex(
                IntegrityError,
                "registry response was incomplete",
            ):
                _registry_get(
                    "registry.example",
                    "project/image",
                    "blobs/sha256:example",
                    30,
                )
        self.assertEqual(4, urlopen.call_count)
        self.assertEqual([call(1), call(2), call(4)], sleep.call_args_list)

    def test_archive_requires_tagged_and_digest_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "image.tar"
            write_archive(archive)
            _verify_archive_metadata(archive, TAGGED, EXACT)
            with self.assertRaises(IntegrityError):
                _verify_archive_metadata(
                    archive,
                    TAGGED,
                    "example.invalid/lab/image:v1@sha256:" + "b" * 64,
                )

    def test_archive_rejects_platform_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "image.tar"
            exact = write_archive(archive, architecture="arm64")
            with self.assertRaises(IntegrityError):
                _verify_archive_metadata(archive, TAGGED, exact)

    def test_archive_rejects_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target.tar"
            write_archive(target)
            link = root / "link.tar"
            link.symlink_to(target)
            with self.assertRaises(IntegrityError):
                _verify_archive_metadata(link, TAGGED, EXACT)

    def test_archive_normalizes_docker_hub_tag(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "image.tar"
            write_archive(
                archive,
                tagged="docker.io/example/image:v1",
            )
            _verify_archive_metadata(
                archive,
                "example/image:v1",
                EXACT.replace("example.invalid/lab/image", "example/image"),
            )

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
            config = {
                "TEST_IMAGE": EXACT,
                "TEST_IMAGE_TAGGED": TAGGED,
                "CERT_MANAGER_VERSION": "v1",
            }
            with (
                patch("scripts.cache._requirements", return_value=requirements),
                patch("scripts.cache.verify_all_inputs"),
            ):
                verified = verify_cache(root, config)
                self.assertEqual(verified.generation, generation)
                with patch(
                    "scripts.cache.verify_generation",
                    side_effect=AssertionError("unchanged generation was rehashed"),
                ):
                    cached = verify_cache(root, config)
                self.assertEqual(cached.state_sha256, verified.state_sha256)
                with archive.open("ab") as output:
                    output.write(b"tampered")
                with self.assertRaises(IntegrityError):
                    verify_cache(root, config)

    def test_pin_drift_rejects_previous_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            active = root / ".tools/cache/active.json"
            generation = root / ".tools/cache/generations/g1"
            generation.mkdir(parents=True, mode=0o700)
            for directory in (
                root / ".tools",
                root / ".tools/cache",
                root / ".tools/cache/generations",
                generation,
            ):
                directory.chmod(0o700)
            (generation / "inventory.json").write_text(
                json.dumps(
                    {
                        "schema": CACHE_SCHEMA,
                        "platform": IMAGE_PLATFORM,
                        "requirements": {"old": True},
                        "imageArchives": [],
                    }
                ),
                encoding="utf-8",
            )
            (generation / "inventory.json").chmod(0o600)
            active.write_text(
                json.dumps({"schema": ACTIVE_SCHEMA, "generation": "g1"}),
                encoding="utf-8",
            )
            active.chmod(0o600)
            with patch("scripts.cache._requirements", return_value={"new": True}):
                with self.assertRaises(IntegrityError):
                    verify_cache(root, {"TEST_IMAGE": EXACT, "TEST_IMAGE_TAGGED": TAGGED})

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

    def test_failed_refresh_preserves_previous_active_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = root / ".tools/cache"
            cache.mkdir(parents=True, mode=0o700)
            (root / ".tools").chmod(0o700)
            cache.chmod(0o700)
            active = cache / "active.json"
            active.write_text(
                json.dumps({"schema": ACTIVE_SCHEMA, "generation": "old"}),
                encoding="utf-8",
            )
            active.chmod(0o600)
            with (
                patch("scripts.cache.uuid.uuid4") as generated,
                patch("scripts.cache.acquire_tools", side_effect=RuntimeError("offline")),
            ):
                generated.return_value.hex = "new"
                with self.assertRaises(RuntimeError):
                    acquire_cache(root, {"DOWNLOAD_TIMEOUT": "1s"})
            self.assertEqual(
                json.loads(active.read_text(encoding="utf-8"))["generation"],
                "old",
            )
            self.assertFalse((cache / "generations/new").exists())

    def test_reacquiring_identical_cache_keeps_foundation_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.joinpath(".tools/cache").mkdir(parents=True, mode=0o700)
            for directory in (root / ".tools", root / ".tools/cache"):
                directory.chmod(0o700)
            archive_content = [b"same"]

            def tools(*_, tools_dir: Path, **__) -> None:
                (tools_dir / "bin").mkdir()

            def archive(_config, _key, destination, _timeout) -> None:
                destination.parent.mkdir()
                destination.write_bytes(archive_content[0])

            def verified_cache(path, _config, *, force=False):
                active = json.loads((path / ".tools/cache/active.json").read_text())
                return VerifiedCache(
                    path / ".tools/cache/generations" / active["generation"], {}, "state",
                )

            with (
                patch("scripts.cache.acquire_tools", side_effect=tools),
                patch("scripts.cache.image_keys", return_value=("TEST_IMAGE",)),
                patch("scripts.cache._archive_image", side_effect=archive),
                patch("scripts.cache._requirements", return_value={"inputs": "pinned"}),
                patch("scripts.cache.verify_generation") as verify,
                patch("scripts.cache.verify_cache", side_effect=verified_cache),
                patch("scripts.cache.materialize_inputs"),
            ):
                config = {"DOWNLOAD_TIMEOUT": "1s"}
                acquire_cache(root, config)
                active = root / ".tools/cache/active.json"
                first = json.loads(active.read_text())["generation"]
                acquire_cache(root, config)
                self.assertEqual(json.loads(active.read_text())["generation"], first)
                self.assertEqual(
                    [path.name for path in (root / ".tools/cache/generations").iterdir()],
                    [first],
                )
                self.assertEqual(verify.call_args_list[-1].args[2].name, first)
                archive_content[0] = b"changed"
                acquire_cache(root, config)
                self.assertNotEqual(json.loads(active.read_text())["generation"], first)

    def test_active_pointer_publication_failure_keeps_old_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = root / ".tools/cache"
            cache.mkdir(parents=True, mode=0o700)
            for directory in (root / ".tools", cache):
                directory.chmod(0o700)
            active = cache / "active.json"
            active.write_text(
                json.dumps({"schema": ACTIVE_SCHEMA, "generation": "old"}),
                encoding="utf-8",
            )
            active.chmod(0o600)

            def fake_acquire_tools(*_, tools_dir: Path, **__) -> None:
                (tools_dir / "bin").mkdir(mode=0o700)
                (tools_dir / "inputs").mkdir(mode=0o700)

            def publishing_write(path: Path, content) -> None:
                if path.name == "active.json":
                    raise OSError("simulated pointer failure")
                real_write_private_file(path, content)

            with (
                patch("scripts.cache.uuid.uuid4") as generated,
                patch("scripts.cache.acquire_tools", side_effect=fake_acquire_tools) as acquire,
                patch("scripts.cache.image_keys", return_value=()),
                patch("scripts.cache._requirements", return_value={}),
                patch("scripts.cache.verify_generation"),
                patch("scripts.cache.write_private_file", side_effect=publishing_write),
            ):
                generated.return_value.hex = "new"
                with self.assertRaises(OSError):
                    acquire_cache(root, {"DOWNLOAD_TIMEOUT": "1s"})
            self.assertEqual(
                json.loads(active.read_text(encoding="utf-8"))["generation"],
                "old",
            )
            self.assertFalse((cache / "generations/new").exists())
            self.assertEqual(
                acquire.call_args.kwargs["tools_dir"],
                cache / "generations/new",
            )

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
            stdout=json.dumps([f"example.invalid/lab/image@{SOURCE_DIGEST}"]),
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
