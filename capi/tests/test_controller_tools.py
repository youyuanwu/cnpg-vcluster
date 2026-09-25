from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Event
from unittest.mock import patch

from scripts.lib.controller import go_environment, test_controller as run_controller_tests
from scripts.lib.files import ensure_private_dir
from scripts.lib.locking import e2e_lock, tools_lock
from scripts.prepare_controller_tools import prepare_controller_tools
from scripts.tools import _install_go


class ControllerToolsTests(unittest.TestCase):
    @staticmethod
    def config() -> dict[str, str]:
        return {
            "DOWNLOAD_TIMEOUT": "30s",
            "GO_URL": "https://example.invalid/go",
            "GO_SHA256": "a" * 64,
            "ENVTEST_URL": "https://example.invalid/envtest",
            "ENVTEST_SHA256": "b" * 64,
        }

    def test_downloads_and_extraction_hold_locks_in_lifecycle_order(self) -> None:
        for child in ("0", "1"):
            with self.subTest(child=child):
                events = []

                @contextmanager
                def lock(name):
                    events.append(f"enter-{name}")
                    yield True
                    events.append(f"exit-{name}")

                with (
                    patch.dict(os.environ, {"CAPI_E2E_CHILD": child}),
                    patch(
                        "scripts.prepare_controller_tools.e2e_lock",
                        side_effect=lambda *_a, **_kw: lock("e2e"),
                    ) as e2e,
                    patch(
                        "scripts.prepare_controller_tools.tools_lock",
                        side_effect=lambda *_a, **_kw: lock("tools"),
                    ) as tools,
                    patch("scripts.prepare_controller_tools._ensure_download",
                          side_effect=lambda *_a: events.append("download")),
                    patch("scripts.prepare_controller_tools._install_go",
                          side_effect=lambda *_a: events.append("go")),
                    patch("scripts.prepare_controller_tools._install_envtest",
                          side_effect=lambda *_a: events.append("envtest")),
                ):
                    prepare_controller_tools(Path("."), self.config())
                expected = ["enter-tools", "download", "download", "go", "envtest", "exit-tools"]
                if child == "1":
                    e2e.assert_not_called()
                else:
                    expected = ["enter-e2e", *expected, "exit-e2e"]
                    e2e.assert_called_once_with(Path("."), exclusive=False)
                tools.assert_called_once_with(Path("."), exclusive=True)
                self.assertEqual(expected, events)

    def test_contending_preparation_waits_for_existing_locks(self) -> None:
        for name, held_lock in (("e2e", e2e_lock), ("tools", tools_lock)):
            with self.subTest(lock=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                attempted, downloaded = Event(), Event()

                @contextmanager
                def observed_lock(*args, **kwargs):
                    attempted.set()
                    with held_lock(*args, **kwargs):
                        yield True

                with (
                    patch.dict(os.environ, {"CAPI_E2E_CHILD": "0"}),
                    patch(f"scripts.prepare_controller_tools.{name}_lock",
                          side_effect=observed_lock),
                    patch("scripts.prepare_controller_tools._ensure_download",
                          side_effect=lambda *_a: downloaded.set()),
                    patch("scripts.prepare_controller_tools._install_go"),
                    patch("scripts.prepare_controller_tools._install_envtest"),
                    ThreadPoolExecutor(max_workers=1) as executor,
                ):
                    with held_lock(root, exclusive=True):
                        future = executor.submit(prepare_controller_tools, root, self.config())
                        self.assertTrue(attempted.wait(5), "preparation did not attempt the lock")
                        self.assertFalse(downloaded.wait(0.1), "download raced the existing lock")
                        self.assertFalse(future.done())
                    future.result(timeout=5)
                    self.assertTrue(downloaded.is_set())

    def test_controller_tools_verify_only_two_archives_without_oci(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = root / ".tools" / "controller-inputs"
            ensure_private_dir(inputs)
            config = {"DOWNLOAD_TIMEOUT": "30s"}
            for tool in ("GO", "ENVTEST"):
                filename = f"{tool.lower()}-linux-amd64.tar.gz"
                content = f"test {tool}".encode()
                (inputs / filename).write_bytes(content)
                config[f"{tool}_URL"] = f"https://example.invalid/{filename}"
                config[f"{tool}_SHA256"] = hashlib.sha256(content).hexdigest()
            with (
                patch("scripts.tools._download") as download,
                patch("scripts.prepare_controller_tools._install_go") as go,
                patch("scripts.prepare_controller_tools._install_envtest") as envtest,
            ):
                prepare_controller_tools(root, config)
            download.assert_not_called()
            go.assert_called_once_with(
                root, inputs / "go-linux-amd64.tar.gz", root / ".tools" / "bin",
            )
            envtest.assert_called_once_with(root, inputs / "envtest-linux-amd64.tar.gz")
            self.assertFalse((root / ".tools" / "cache").exists())

    def test_corrupt_cached_archive_is_not_installed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = root / ".tools" / "controller-inputs"
            ensure_private_dir(inputs)
            (inputs / "go-linux-amd64.tar.gz").write_bytes(b"corrupt")
            with (
                patch("scripts.tools._download", side_effect=RuntimeError("no download")),
                patch("scripts.prepare_controller_tools._install_go") as install,
            ):
                with self.assertRaisesRegex(RuntimeError, "no download"):
                    prepare_controller_tools(root, {
                        "DOWNLOAD_TIMEOUT": "30s",
                        "GO_URL": "https://example.invalid/go",
                        "GO_SHA256": "0" * 64,
                    })
                install.assert_not_called()

    def test_pinned_go_wrapper_and_envtest_use_the_cached_module_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            go_root = root / ".tools" / "go"
            ensure_private_dir(go_root / "bin")
            (go_root / "bin" / "go").touch()
            with patch("scripts.tools._extract_private_archive"):
                _install_go(root, root / "go.tar.gz", root / ".tools" / "bin")
            environment = go_environment(root)
            self.assertEqual(str(go_root), environment["GOROOT"])
            self.assertEqual(str(root / ".tools" / "go-mod-cache"), environment["GOMODCACHE"])
            self.assertEqual(str(root / ".tools" / "go-cache"), environment["GOCACHE"])
            self.assertEqual(str(root / ".tools" / "bin"), environment["PATH"].split(os.pathsep)[0])
            wrapper = (root / ".tools" / "bin" / "go").read_text(encoding="utf-8")
            self.assertIn(f"export GOROOT='{go_root}'", wrapper)
            with patch("scripts.lib.controller.run") as run:
                run_controller_tests(root, {"COMMAND_TIMEOUT": "30s"})
            self.assertEqual(
                str(root / ".tools" / "envtest" / "envtest"),
                run.call_args.kwargs["env"]["KUBEBUILDER_ASSETS"],
            )
