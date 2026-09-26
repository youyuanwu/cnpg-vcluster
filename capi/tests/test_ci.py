from __future__ import annotations

import os
import re
import subprocess
import textwrap
import unittest
from pathlib import Path


WORKFLOW = (
    Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"
).read_text(encoding="utf-8")


def job(name: str) -> str:
    match = re.search(
        rf"(?ms)^  {re.escape(name)}:\n(.*?)(?=^  [a-z][a-z0-9-]*:\n|\Z)",
        WORKFLOW.split("jobs:\n", 1)[1],
    )
    if match is None:
        raise AssertionError(f"missing CI job: {name}")
    return match[1]


class CIWorkflowTests(unittest.TestCase):
    def test_tiered_checks_and_stable_aggregate(self) -> None:
        fast, e2e, high, gate = (
            job(name) for name in ("fast-checks", "e2e", "high-capacity", "capi-tests")
        )
        self.assertNotIn("    needs:", fast + e2e + high)
        self.assertNotIn("    if:", fast)
        self.assertIn("if: github.event_name == 'pull_request'", e2e)
        self.assertIn("if: github.event_name == 'schedule' || github.event_name == 'workflow_dispatch'", high)
        self.assertIn("name: CAPI tests\n", gate)
        self.assertIn("if: always()", gate)
        self.assertIn("needs: [fast-checks, e2e, high-capacity]", gate)
        for command in (
            "just test-unit", "just test-static", "just controller-fetch",
            "just controller-verify", "just controller-lint", "just controller-test",
            "just controller-build",
        ):
            self.assertIn(command, fast)
        self.assertNotIn("just cache", fast)
        self.assertLess(e2e.index("just cache"), e2e.index("just test-e2e"))
        self.assertIn("just test-e2e", e2e)
        self.assertNotIn("just test-e2e-offline", e2e)
        setup = (
            "just cache", "just tools", "just prepare-host",
            "just create-management",
        )
        for command in setup:
            self.assertIn(command, high)
        self.assertEqual(
            sorted(high.index(command) for command in setup),
            [high.index(command) for command in setup],
        )
        targeted = (
            "just test-controller-convergence", "just test-controller-readiness",
            "just test-controller-deletion", "just test-endpoint",
            "just test-endpoint-negative", "just test-spike",
            "just test-network-negative", "just test-machines",
            "just test-storage", "just test-storage-negative",
            "just test-persistence", "just test-persistence-negative",
            "just test-tenant-lifecycle",
        )
        for command in targeted:
            self.assertIn(command, high)
        setup_end = high.index("just create-management")
        self.assertTrue(all(setup_end < high.index(command) for command in targeted))
        self.assertLess(setup_end, high.index("just test-e2e-offline"))
        self.assertIn("just test-e2e-offline", high)
        self.assertLess(high.index("just test-e2e-offline"), high.index("just destroy"))
        cleanup = high[high.index("- name: Clean up high-capacity environment"):]
        self.assertIn("if: always()", cleanup)
        self.assertIn("run: just destroy", cleanup)
        for live in (e2e, high):
            for token in ("MIN_DOCKER_CPUS", "MIN_DOCKER_MEMORY_GIB",
                          "MIN_DOCKER_STORAGE_GIB", "timeout-minutes:"):
                self.assertIn(token, live)

    def test_cargo_work_directory_is_cleanup_safe(self) -> None:
        self.assertEqual(4, WORKFLOW.count(".tools/cargo-work"))
        self.assertNotIn(".runtime/cargo-work", WORKFLOW)
        self.assertIn(
            "TMPDIR: ${{ github.workspace }}/capi/.tools/cargo-work",
            WORKFLOW,
        )
        for name in ("fast-checks", "e2e", "high-capacity"):
            self.assertIn(
                "run: install -d -m 700 .tools .runtime .tools/cargo-home .tools/cargo-work",
                job(name),
            )

    def test_events_and_concurrency(self) -> None:
        events = WORKFLOW.split("permissions:", 1)[0]
        for event in ("pull_request:", "push:", "workflow_dispatch:", "schedule:"):
            self.assertIn(f"  {event}", events)
        self.assertIn("      - main", events)
        self.assertIn('cron: "23 4 * * 1"', events)
        self.assertNotIn("paths:", events)
        self.assertIn("${{ github.event_name }}-${{ github.ref }}", WORKFLOW)
        self.assertIn("cancel-in-progress: true", WORKFLOW)

    def test_optional_cargo_cache_uses_lock_compiler_and_platform(self) -> None:
        keys = re.findall(r"(?m)^\s+key: (controller-cargo-.*)$", WORKFLOW)
        self.assertEqual(2, len(keys))
        self.assertEqual(keys[0], keys[1])
        for token in ("runner.os", "runner.arch", "steps.compiler.outputs.identity",
                      "hashFiles('capi/controller/Cargo.lock')"):
            self.assertIn(token, keys[0])
        self.assertEqual(2, WORKFLOW.count("path: capi/.tools/cargo-home"))
        self.assertNotIn("restore-keys:", WORKFLOW)
        self.assertNotIn("cargo-target", WORKFLOW)
        self.assertNotIn("rustup", WORKFLOW)
        self.assertNotRegex(WORKFLOW, r"go\.mod|go\.sum|envtest|controller-gen|"
                            r"controller-tools|controller-vet|go-mod-cache|"
                            r"GO_VERSION|GOCACHE|GOMODCACHE")

    def test_gate_never_accepts_failed_or_unexpectedly_skipped_checks(self) -> None:
        script = textwrap.dedent(job("capi-tests").split("        run: |\n", 1)[1])
        for event in ("pull_request", "workflow_dispatch", "schedule", "push"):
            for fast in ("success", "failure", "skipped", "cancelled"):
                for e2e in ("success", "failure", "skipped", "cancelled"):
                    for high in ("success", "failure", "skipped", "cancelled"):
                        with self.subTest(event=event, fast=fast, e2e=e2e, high=high):
                            result = subprocess.run(
                                ["bash", "--noprofile", "--norc", "-e", "-c", script],
                                env={
                                    **os.environ, "FAST_RESULT": fast,
                                    "E2E_RESULT": e2e, "HIGH_CAPACITY_RESULT": high,
                                    "EVENT_NAME": event,
                                },
                                capture_output=True,
                                check=False,
                            )
                            expected = (
                                fast == "success"
                                and e2e == ("success" if event == "pull_request" else "skipped")
                                and high == ("success" if event in {"workflow_dispatch", "schedule"} else "skipped")
                            )
                            self.assertEqual(expected, result.returncode == 0)
