from __future__ import annotations

import tempfile
import unittest
import json
import os
from pathlib import Path
from unittest.mock import patch

from scripts.lib.host import (
    HostError,
    _read_state_record,
    prepare_inotify,
    resolve_host_just,
)


class HostTests(unittest.TestCase):
    def test_rejects_just_from_tools_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / ".tools" / "bin" / "just"
            executable.parent.mkdir(parents=True)
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            with patch("scripts.lib.host.shutil.which", return_value=str(executable)):
                with self.assertRaises(HostError):
                    resolve_host_just(root, {"JUST_VERSION": "1.58.0"})

    def test_rejects_symlink_resolving_into_tools_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / ".tools" / "bin" / "just"
            executable.parent.mkdir(parents=True)
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            link = root / "bin" / "just"
            link.parent.mkdir()
            link.symlink_to(executable)
            with patch("scripts.lib.host.shutil.which", return_value=str(link)):
                with self.assertRaises(HostError):
                    resolve_host_just(root, {"JUST_VERSION": "1.58.0"})

    def test_rejects_foreign_inotify_record(self) -> None:
        config = {
            "LAB_PREFIX": "expected",
            "OWNERSHIP_LABEL": "expected.owner",
        }
        with tempfile.TemporaryDirectory() as temporary:
            host_dir = Path(temporary) / "host"
            host_dir.mkdir(mode=0o700)
            path = host_dir / "inotify.json"
            path.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "lab_prefix": "foreign",
                        "ownership_label": "expected.owner",
                        "owner_uid": os.getuid(),
                        "max_user_instances": 128,
                        "max_user_watches": 524288,
                    }
                ),
                encoding="utf-8",
            )
            path.chmod(0o600)
            descriptor = os.open(host_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with self.assertRaises(HostError):
                    _read_state_record(descriptor, path, config)
            finally:
                os.close(descriptor)

    def test_rejects_broad_inotify_record_permissions(self) -> None:
        config = {
            "LAB_PREFIX": "expected",
            "OWNERSHIP_LABEL": "expected.owner",
        }
        with tempfile.TemporaryDirectory() as temporary:
            host_dir = Path(temporary) / "host"
            host_dir.mkdir(mode=0o700)
            path = host_dir / "inotify.json"
            path.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "lab_prefix": "expected",
                        "ownership_label": "expected.owner",
                        "owner_uid": os.getuid(),
                        "max_user_instances": 128,
                        "max_user_watches": 524288,
                    }
                ),
                encoding="utf-8",
            )
            path.chmod(0o644)
            descriptor = os.open(host_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with self.assertRaises(HostError):
                    _read_state_record(descriptor, path, config)
            finally:
                os.close(descriptor)

    def test_rejects_symlinked_runtime_parent(self) -> None:
        config = {
            "LAB_PREFIX": "expected",
            "OWNERSHIP_LABEL": "expected.owner",
        }
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as target:
            root = Path(temporary)
            (root / ".runtime").symlink_to(target, target_is_directory=True)
            with self.assertRaises(HostError):
                prepare_inotify(root, config)
