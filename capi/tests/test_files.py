from __future__ import annotations

import hashlib
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.lib.files import (
    IntegrityError,
    has_owner_only_permissions,
    private_file_exists,
    read_private_file,
    unlink_private_file,
    verify_sha256,
    write_private_file,
)


class FileTests(unittest.TestCase):
    def test_private_atomic_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "nested" / "secret"
            write_private_file(path, "value\n")
            self.assertEqual(path.read_text(encoding="utf-8"), "value\n")
            self.assertTrue(has_owner_only_permissions(path))
            self.assertEqual(path.parent.stat().st_mode & 0o077, 0)

    def test_private_metadata_changes_are_directory_synced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "nested" / "secret"
            events = []
            real_mkdir = os.mkdir
            real_replace = os.replace
            real_unlink = os.unlink
            real_fsync = os.fsync

            def tracked_mkdir(*args, **kwargs):
                result = real_mkdir(*args, **kwargs)
                events.append("mkdir")
                return result

            def tracked_replace(*args, **kwargs):
                result = real_replace(*args, **kwargs)
                events.append("replace")
                return result

            def tracked_unlink(*args, **kwargs):
                result = real_unlink(*args, **kwargs)
                events.append("unlink")
                return result

            def tracked_fsync(descriptor):
                result = real_fsync(descriptor)
                events.append("fsync")
                return result

            with (
                patch("scripts.lib.files.os.mkdir", side_effect=tracked_mkdir),
                patch("scripts.lib.files.os.replace", side_effect=tracked_replace),
                patch("scripts.lib.files.os.unlink", side_effect=tracked_unlink),
                patch("scripts.lib.files.os.fsync", side_effect=tracked_fsync),
            ):
                write_private_file(path, "value\n")
                unlink_private_file(path)

            for operation in ("mkdir", "replace", "unlink"):
                index = events.index(operation)
                self.assertEqual(events[index + 1], "fsync")

    def test_checksum_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "input"
            path.write_bytes(b"payload")
            expected = hashlib.sha256(b"payload").hexdigest()
            verify_sha256(path, expected)
            with self.assertRaises(IntegrityError):
                verify_sha256(path, "0" * 64)

    def test_checksum_rejects_missing_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(IntegrityError):
                verify_sha256(Path(temporary) / "missing", "0" * 64)

    def test_private_write_rejects_symlinked_runtime_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target"
            target.mkdir()
            (root / ".runtime").symlink_to(target, target_is_directory=True)
            with self.assertRaises(IntegrityError):
                write_private_file(
                    root / ".runtime" / "evidence" / "secret",
                    "value\n",
                )
            self.assertFalse((target / "evidence" / "secret").exists())

    def test_private_write_rejects_broad_runtime_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / ".runtime"
            runtime.mkdir(mode=0o755)
            with self.assertRaises(IntegrityError):
                write_private_file(
                    runtime / "evidence" / "secret",
                    "value\n",
                )

    def test_private_write_rejects_nested_symlinked_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / ".runtime"
            runtime.mkdir(mode=0o700)
            target = root / "target"
            target.mkdir(mode=0o700)
            (runtime / "evidence").symlink_to(
                target, target_is_directory=True
            )
            with self.assertRaises(IntegrityError):
                write_private_file(
                    runtime / "evidence" / "secret",
                    "value\n",
                )
            self.assertFalse((target / "secret").exists())

    def test_private_write_rejects_symlink_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / ".runtime"
            runtime.mkdir(mode=0o700)
            evidence = runtime / "evidence"
            evidence.mkdir(mode=0o700)
            target = root / "target"
            target.write_text("original\n", encoding="utf-8")
            (evidence / "secret").symlink_to(target)
            with self.assertRaises(IntegrityError):
                write_private_file(evidence / "secret", "replacement\n")
            self.assertEqual(target.read_text(encoding="utf-8"), "original\n")

    def test_private_write_rejects_non_directory_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / ".runtime"
            runtime.write_text("collision\n", encoding="utf-8")
            with self.assertRaises(IntegrityError):
                write_private_file(
                    runtime / "evidence" / "secret",
                    "value\n",
                )

    def test_private_read_and_exists_reject_symlinked_runtime_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target"
            target.mkdir(mode=0o700)
            secret = target / "secret"
            secret.write_text("value\n", encoding="utf-8")
            secret.chmod(0o600)
            (root / ".runtime").symlink_to(target, target_is_directory=True)
            with self.assertRaises(IntegrityError):
                read_private_file(root / ".runtime" / "secret")
            with self.assertRaises(IntegrityError):
                private_file_exists(root / ".runtime" / "secret")

    def test_private_read_rejects_fifo_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / ".runtime"
            runtime.mkdir(mode=0o700)
            fifo = runtime / "identity.json"
            os.mkfifo(fifo, mode=0o600)
            started = time.monotonic()
            with self.assertRaises(IntegrityError):
                read_private_file(fifo)
            self.assertLess(time.monotonic() - started, 1.0)
