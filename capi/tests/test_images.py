from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.lib.images import (
    MANAGEMENT_IMAGE_KEYS,
    WORKER_IMAGE_KEYS,
    preload_worker_images,
    references,
)


class ImagePreloadTests(unittest.TestCase):
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
                    raise RuntimeError("injected")

            with (
                patch(
                    "scripts.lib.images.wait_pre_cni_workers",
                    return_value=("worker-b", "worker-a"),
                ),
                patch("scripts.lib.images.import_container_images", side_effect=importer),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "worker-a, worker-b"
                ):
                    preload_worker_images(root, {}, object(), tenant)


if __name__ == "__main__":
    unittest.main()
