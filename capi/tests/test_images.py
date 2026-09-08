from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from scripts.lib.images import (
    MANAGEMENT_IMAGE_KEYS,
    WORKER_IMAGE_KEYS,
    preload_worker_images,
    references,
    import_container_images,
    _container_has_image,
)


class ImagePreloadTests(unittest.TestCase):
    def test_container_import_assigns_exact_index_name(self) -> None:
        exact = "example/image:v1@sha256:" + "a" * 64
        config = {"DOWNLOAD_TIMEOUT": "1s", "TEST_IMAGE": exact}
        missing = CompletedProcess([], 1, stdout="", stderr="missing")
        success = CompletedProcess([], 0, stdout="", stderr="")
        present = CompletedProcess(
            [],
            0,
            stdout=f"{exact}\n└── application/vnd.oci.image.index.v1+json @{exact.rsplit('@', 1)[1]}\n",
            stderr="",
        )
        with (
            patch("scripts.lib.images.archive_path", return_value=Path("/cache/image.tar")),
            patch(
                "scripts.lib.images.run",
                side_effect=[missing, success, success, success, present],
            ) as run,
        ):
            import_container_images(
                Path("/repo"), config, "worker-a", ("TEST_IMAGE",)
            )
        commands = [call.args[0] for call in run.call_args_list]
        import_command = next(command for command in commands if "import" in command)
        self.assertIn("--index-name", import_command)
        self.assertEqual(
            import_command[import_command.index("--index-name") + 1],
            exact,
        )

    def test_container_import_fails_when_exact_name_is_still_missing(self) -> None:
        exact = "example/image:v1@sha256:" + "a" * 64
        config = {"DOWNLOAD_TIMEOUT": "1s", "TEST_IMAGE": exact}
        missing = CompletedProcess([], 1, stdout="", stderr="missing")
        success = CompletedProcess([], 0, stdout="", stderr="")
        with (
            patch("scripts.lib.images.archive_path", return_value=Path("/cache/image.tar")),
            patch(
                "scripts.lib.images.run",
                side_effect=[missing, success, success, success, missing],
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "lacks imported exact image"):
                import_container_images(
                    Path("/repo"), config, "worker-a", ("TEST_IMAGE",)
                )

    def test_container_image_name_with_wrong_target_digest_is_rejected(self) -> None:
        exact = "example/image:v1@sha256:" + "a" * 64
        result = CompletedProcess(
            [],
            0,
            stdout=(
                f"{exact}\n└── application/vnd.oci.image.index.v1+json "
                f"@sha256:{'b' * 64}\n"
            ),
            stderr="",
        )
        with patch("scripts.lib.images.run", return_value=result):
            self.assertFalse(_container_has_image("worker-a", exact, 1))

    def test_references_are_deterministic_exact_digests(self) -> None:
        config = {
            key: f"example.invalid/{key.lower()}:v1@sha256:{index:064x}"
            for index, key in enumerate(WORKER_IMAGE_KEYS, 1)
        }
        observed = references(config, WORKER_IMAGE_KEYS)
        self.assertEqual(observed, tuple(sorted(observed)))
        self.assertTrue(all("@sha256:" in item for item in observed))

    def test_worker_preloads_overlap_and_evidence_is_sorted(self) -> None:
        barrier = threading.Barrier(2)
        tenant = type("Tenant", (), {"name": "tenant-a"})()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def concurrent_import(*_, container: str | None = None, **__) -> None:
                barrier.wait(timeout=2)

            def importer(root, config, name, keys):
                self.assertEqual(keys, WORKER_IMAGE_KEYS)
                barrier.wait(timeout=2)

            with (
                patch(
                    "scripts.lib.images.wait_pre_cni_workers",
                    return_value=("worker-b", "worker-a"),
                ),
                patch("scripts.lib.images.import_container_images", side_effect=importer),
            ):
                evidence = preload_worker_images(root, {}, object(), tenant)
            self.assertTrue(evidence["overlap"])
            self.assertEqual(
                [item["node"] for item in evidence["nodes"]],
                ["worker-a", "worker-b"],
            )
            stored = json.loads(
                (root / ".runtime/evidence/preload-tenant-a.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertTrue(stored["overlap"])

    def test_worker_preload_failures_are_reported_in_sorted_order(self) -> None:
        tenant = type("Tenant", (), {"name": "tenant-a"})()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def importer(_, __, name, ___):
                if name in {"worker-b", "worker-a"}:
                    raise RuntimeError("token=super-secret injected")

            with (
                patch(
                    "scripts.lib.images.wait_pre_cni_workers",
                    return_value=("worker-b", "worker-a"),
                ),
                patch("scripts.lib.images.import_container_images", side_effect=importer),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "worker-a:.*worker-b:"
                ):
                    preload_worker_images(root, {}, object(), tenant)
            evidence = json.loads(
                (root / ".runtime/evidence/preload-tenant-a.json").read_text(
                    encoding="utf-8"
                )
            )
            errors = " ".join(item.get("error", "") for item in evidence["nodes"])
            self.assertNotIn("super-secret", errors)
            self.assertIn("REDACTED", errors)


if __name__ == "__main__":
    unittest.main()
