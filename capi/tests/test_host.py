from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.lib.host import HostError, resolve_host_just


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
