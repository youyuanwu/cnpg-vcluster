from __future__ import annotations

import json
import tempfile
import threading
import unittest
import os
from contextlib import redirect_stdout
from io import StringIO
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
    enforce_offline_node_egress,
)


class ImagePreloadTests(unittest.TestCase):
    def test_offline_node_guard_allows_lab_networks_and_rejects_web_egress(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record = root / ".runtime/management/network.json"
            record.parent.mkdir(parents=True)
            record.write_text(json.dumps({"subnet": "172.18.0.0/16"}))
            with (
                patch.dict(
                    os.environ,
                    {"CAPI_OFFLINE_ENFORCED": "1"},
                ),
                patch(
                    "scripts.lib.images.run",
                    side_effect=[
                        CompletedProcess([], 0, stdout="", stderr=""),
                        CompletedProcess([], 0, stdout="1 1\n", stderr="refused"),
                    ],
                ) as run,
            ):
                output = StringIO()
                with redirect_stdout(output):
                    enforce_offline_node_egress(
                        root,
                        {
                            "MANAGEMENT_POD_CIDR": "10.210.0.0/16",
                            "TENANT_A_POD_CIDR": "10.70.0.0/16",
                        },
                        "worker-a",
                    )
            scripts = [
                call.args[0][-1]
                for call in run.call_args_list
                if call.args[0][:3] == ["docker", "exec", "worker-a"]
                and "iptables" in call.args[0][-1]
            ]
            script = scripts[0]
            self.assertIn("-d 172.18.0.0/16 -j RETURN", script)
            self.assertIn("-d 10.70.0.0/16 -j RETURN", script)
            self.assertIn("--dports 80,443 -j REJECT", script)
            probe = run.call_args_list[-1].args[0]
            self.assertIn("timeout 3", probe[-1])
            self.assertIn("iptables -Z", probe[-1])
            self.assertIn('$9 == "1.1.1.1"', probe[-1])
            evidence = json.loads(
                output.getvalue().removeprefix("CAPI_OFFLINE_EGRESS ")
            )
            self.assertEqual(evidence["rejectedPackets"], 1)

    def test_offline_node_guard_rejects_successful_or_broken_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record = root / ".runtime/management/network.json"
            record.parent.mkdir(parents=True)
            record.write_text(json.dumps({"subnet": "172.18.0.0/16"}))
            for evidence, message in (
                ("0 1\n", "denial was not proven"),
                ("1 0\n", "denial was not proven"),
                ("127 1\n", "could not verify denial"),
            ):
                with (
                    patch.dict(os.environ, {"CAPI_OFFLINE_ENFORCED": "1"}),
                    patch(
                        "scripts.lib.images.run",
                        side_effect=[
                            CompletedProcess([], 0, stdout="", stderr=""),
                            CompletedProcess(
                                [], 0, stdout=evidence, stderr="probe"
                            ),
                        ],
                    ),
                ):
                    with self.assertRaisesRegex(RuntimeError, message):
                        enforce_offline_node_egress(
                            root,
                            {"MANAGEMENT_POD_CIDR": "10.210.0.0/16"},
                            "worker-a",
                        )

    def test_container_import_tags_exact_digest_name(self) -> None:
        exact = "example/image:v1@sha256:" + "a" * 64
        config = {
            "DOWNLOAD_TIMEOUT": "1s",
            "TEST_IMAGE": exact,
            "TEST_IMAGE_TAGGED": "example/image:v1",
        }
        missing = CompletedProcess([], 1, stdout="", stderr="missing")
        success = CompletedProcess([], 0, stdout="", stderr="")
        present = CompletedProcess(
            [],
            0,
            stdout=f"{exact}\n└── application/vnd.oci.image.index.v1+json @{exact.rsplit('@', 1)[1]}\n",
            stderr="",
        )
        with (
            patch(
                "scripts.lib.images.active_generation",
                return_value=Path("/cache/generations/g1"),
            ),
            patch(
                "scripts.lib.images.run",
                side_effect=[missing, success, success, success, success, success, present],
            ) as run,
        ):
            import_container_images(
                Path("/repo"), config, "worker-a", ("TEST_IMAGE",)
            )
        commands = [call.args[0] for call in run.call_args_list]
        tag_commands = [command for command in commands if "tag" in command]
        self.assertEqual(tag_commands[0][-1], exact)
        self.assertEqual(
            tag_commands[1][-1],
            "docker.io/example/image:v1@sha256:" + "a" * 64,
        )
        self.assertEqual(
            tag_commands[2][-1],
            "docker.io/example/image@sha256:" + "a" * 64,
        )

    def test_container_import_fails_when_exact_name_is_still_missing(self) -> None:
        exact = "example/image:v1@sha256:" + "a" * 64
        config = {
            "DOWNLOAD_TIMEOUT": "1s",
            "TEST_IMAGE": exact,
            "TEST_IMAGE_TAGGED": "example/image:v1",
        }
        missing = CompletedProcess([], 1, stdout="", stderr="missing")
        success = CompletedProcess([], 0, stdout="", stderr="")
        with (
            patch(
                "scripts.lib.images.active_generation",
                return_value=Path("/cache/generations/g1"),
            ),
            patch(
                "scripts.lib.images.run",
                side_effect=[missing, success, success, success, success, success, missing],
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
