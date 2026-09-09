from __future__ import annotations

import unittest
from pathlib import Path
from subprocess import CompletedProcess
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts.preflight import (
    PreflightError,
    configured_networks,
    run_retained_preflight,
    verify_images,
    verify_management_name,
)
from scripts.preflight import verify_privileged_probe
from scripts.lib.process import CommandError


BASE = {
    "MANAGEMENT_POD_CIDR": "10.210.0.0/16",
    "MANAGEMENT_SERVICE_CIDR": "10.211.0.0/16",
    "TENANT_A_POD_CIDR": "10.70.0.0/16",
    "TENANT_A_SERVICE_CIDR": "10.140.0.0/16",
    "TENANT_B_POD_CIDR": "10.71.0.0/16",
    "TENANT_B_SERVICE_CIDR": "10.141.0.0/16",
    "SPIKE_POD_CIDR": "10.72.0.0/16",
    "SPIKE_SERVICE_CIDR": "10.142.0.0/16",
}


class PreflightTests(unittest.TestCase):
    def test_retained_preflight_verifies_without_install_or_overlap(self) -> None:
        calls: list[str] = []
        with (
            patch(
                "scripts.preflight.parse_duration", return_value=30
            ),
            patch(
                "scripts.preflight.verify_cache",
                side_effect=lambda *_: calls.append("cache") or object(),
            ),
            patch(
                "scripts.preflight.verify_all_inputs",
                side_effect=lambda *_: calls.append("inputs"),
            ),
            patch(
                "scripts.preflight.verify_tools",
                side_effect=lambda *_: calls.append("tools"),
            ),
            patch(
                "scripts.preflight.configured_networks",
                side_effect=lambda *_: calls.append("networks"),
            ),
            patch(
                "scripts.preflight.verify_docker",
                side_effect=lambda *_: calls.append("docker"),
            ),
            patch(
                "scripts.preflight.verify_management_name",
                side_effect=lambda *_: calls.append("management"),
            ),
            patch(
                "scripts.preflight.verify_inotify",
                side_effect=lambda *_: calls.append("inotify"),
            ),
            patch(
                "scripts.preflight.verify_images",
                side_effect=lambda *_: calls.append("images"),
            ),
            patch(
                "scripts.preflight.verify_privileged_probe",
                side_effect=lambda *_: calls.append("probe"),
            ),
            patch("scripts.preflight.prepare_tools") as prepare,
            patch("scripts.preflight.verify_no_network_overlap") as overlap,
        ):
            run_retained_preflight(
                Path("/tmp/example"), {"COMMAND_TIMEOUT": "30s"}
            )
        self.assertEqual(
            calls,
            [
                "cache",
                "inputs",
                "tools",
                "networks",
                "docker",
                "management",
                "inotify",
                "images",
                "probe",
            ],
        )
        prepare.assert_not_called()
        overlap.assert_not_called()

    def test_image_verification_is_local_cache_only(self) -> None:
        with patch("scripts.preflight.verify_cache") as verify:
            verify_images(Path("/tmp/example"), {"TEST": "value"})
        verify.assert_called_once_with(Path("/tmp/example"), {"TEST": "value"})

    def test_accepts_disjoint_networks(self) -> None:
        self.assertEqual(len(configured_networks(BASE)), 8)

    def test_rejects_overlap(self) -> None:
        values = dict(BASE)
        values["SPIKE_POD_CIDR"] = values["TENANT_A_POD_CIDR"]
        with self.assertRaises(PreflightError):
            configured_networks(values)

    def test_rejects_unowned_reserved_management_name(self) -> None:
        config = {
            "KIND_CLUSTER_NAME": "example",
            "OWNERSHIP_LABEL": "example.owner",
        }
        with TemporaryDirectory() as temporary:
            with patch(
                "scripts.preflight.run",
                return_value=CompletedProcess([], 0, stdout="container-id\n", stderr=""),
            ):
                with self.assertRaises(PreflightError):
                    verify_management_name(Path(temporary), config, 30)

    def test_timed_out_probe_cleans_owned_container(self) -> None:
        commands: list[list[str]] = []

        def fake_run(command: list[str], **_: object) -> CompletedProcess[str]:
            commands.append(command)
            if len(command) > 1 and command[1] == "run":
                cid_path = Path(command[command.index("--cidfile") + 1])
                cid_path.write_text("owned-probe-id\n", encoding="utf-8")
                raise CommandError(tuple(command), 124, "timeout")
            if len(command) > 1 and command[1] == "inspect":
                return CompletedProcess(
                    command,
                    0,
                    stdout=(
                        '[{"Id":"owned-probe-id","Name":"/test-preflight-fixed",'
                        '"Config":{"Labels":{"test.owner":"true",'
                        '"cnpg-vcluster.capi/role":"probe"}}}]'
                    ),
                    stderr="",
                )
            return CompletedProcess(command, 0, stdout="", stderr="")

        config = {
            "PREFLIGHT_PROBE_TIMEOUT": "1s",
            "LAB_PREFIX": "test",
            "OWNERSHIP_LABEL": "test.owner",
            "VERIFY_IMAGE": "busybox:1.37.0@sha256:" + "0" * 64,
        }
        with patch("scripts.preflight.uuid.uuid4") as generated_uuid:
            generated_uuid.return_value.hex = "fixed"
            with (
                patch("scripts.preflight.run", side_effect=fake_run),
                patch("scripts.preflight.restore_host_image"),
            ):
                with self.assertRaises(PreflightError):
                    verify_privileged_probe(Path("/tmp/example"), config)
        self.assertIn(["docker", "rm", "-f", "owned-probe-id"], commands)
        self.assertTrue(any("--pull=never" in command for command in commands))

    def test_probe_refuses_arbitrary_cidfile_identifier(self) -> None:
        commands: list[list[str]] = []

        def fake_run(command: list[str], **_: object) -> CompletedProcess[str]:
            commands.append(command)
            if len(command) > 1 and command[1] == "run":
                cid_path = Path(command[command.index("--cidfile") + 1])
                cid_path.write_text("foreign-id\n", encoding="utf-8")
                raise CommandError(tuple(command), 124, "timeout")
            if len(command) > 1 and command[1] == "inspect":
                return CompletedProcess(
                    command,
                    0,
                    stdout=(
                        '[{"Id":"foreign-id","Name":"/foreign",'
                        '"Config":{"Labels":{"foreign":"true"}}}]'
                    ),
                    stderr="",
                )
            return CompletedProcess(command, 0, stdout="", stderr="")

        config = {
            "PREFLIGHT_PROBE_TIMEOUT": "1s",
            "LAB_PREFIX": "test",
            "OWNERSHIP_LABEL": "test.owner",
            "VERIFY_IMAGE": "busybox:1.37.0@sha256:" + "0" * 64,
        }
        with patch("scripts.preflight.uuid.uuid4") as generated_uuid:
            generated_uuid.return_value.hex = "fixed"
            with (
                patch("scripts.preflight.run", side_effect=fake_run),
                patch("scripts.preflight.restore_host_image"),
            ):
                with self.assertRaises(PreflightError):
                    verify_privileged_probe(Path("/tmp/example"), config)
        self.assertNotIn(["docker", "rm", "-f", "foreign-id"], commands)

    def test_probe_refuses_foreign_same_name_container(self) -> None:
        commands: list[list[str]] = []

        def fake_run(command: list[str], **_: object) -> CompletedProcess[str]:
            commands.append(command)
            if len(command) > 1 and command[1] == "run":
                return CompletedProcess(command, 125, stdout="", stderr="name conflict")
            if len(command) > 1 and command[1] == "inspect":
                return CompletedProcess(
                    command,
                    0,
                    stdout=(
                        '[{"Id":"foreign-id","Name":"/test-preflight-fixed",'
                        '"Config":{"Labels":{"foreign":"true"}}}]'
                    ),
                    stderr="",
                )
            return CompletedProcess(command, 0, stdout="foreign-id\n", stderr="")

        config = {
            "PREFLIGHT_PROBE_TIMEOUT": "1s",
            "LAB_PREFIX": "test",
            "OWNERSHIP_LABEL": "test.owner",
            "VERIFY_IMAGE": "busybox:1.37.0@sha256:" + "0" * 64,
        }
        with patch("scripts.preflight.uuid.uuid4") as generated_uuid:
            generated_uuid.return_value.hex = "fixed"
            with (
                patch("scripts.preflight.run", side_effect=fake_run),
                patch("scripts.preflight.restore_host_image"),
            ):
                with self.assertRaises(PreflightError):
                    verify_privileged_probe(Path("/tmp/example"), config)
        self.assertNotIn(["docker", "rm", "-f", "foreign-id"], commands)
