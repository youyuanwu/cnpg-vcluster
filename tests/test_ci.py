from __future__ import annotations

import os
import re
import subprocess
import textwrap
import unittest
from pathlib import Path


WORKFLOW = (
    Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"
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
            job(name) for name in (
                "fast-checks", "e2e", "high-capacity", "capi-tests",
            )
        )
        self.assertNotIn("    needs:", fast + high)
        self.assertRegex(e2e, r"(?m)^    needs: fast-checks$")
        self.assertNotIn("    if:", fast)
        self.assertNotRegex(e2e, r"(?m)^    if:")
        self.assertIn("if: github.event_name == 'schedule' || github.event_name == 'workflow_dispatch'", high)
        self.assertIn("name: CAPI tests\n", gate)
        self.assertIn("if: always()", gate)
        self.assertIn("needs: [fast-checks, e2e, high-capacity]", gate)
        for command in (
            "just test-unit", "just test-static",
            "just test-azure-operator-contracts", "just controller-fetch",
            "just controller-verify", "just controller-lint", "just controller-test",
            "just controller-metrics", "just controller-build",
            "just database-controller-verify", "just database-controller-lint",
            "just database-controller-test", "just database-controller-metrics",
            "just database-controller-build",
            "just admin-fetch", "just admin-generate-check", "just admin-lint",
            "just admin-test", "just admin-metrics", "just admin-package-check",
        ):
            self.assertIn(command, fast)
        self.assertIn("just cache admin-build", fast)
        self.assertNotIn("run: just cache\n", fast)
        e2e_sequence = (
            "just cache", "just controller-fetch", "just admin-fetch",
            "just test-e2e-offline",
        )
        for command in e2e_sequence:
            self.assertIn(command, e2e)
        self.assertEqual(
            sorted(e2e.index(command) for command in e2e_sequence),
            [e2e.index(command) for command in e2e_sequence],
        )
        self.assertIn("just test-e2e-offline", e2e)
        self.assertIn("name: Diagnose failed end-to-end", e2e)
        self.assertIn("if: failure()", e2e)
        self.assertIn("logs deployment/database-controller", e2e)
        self.assertIn("CAPI_PREBUILT_DATABASE_CONTROLLER_BINARY", e2e)
        self.assertIn("database-manager", fast)
        self.assertIn("actions/upload-artifact@v6", fast)
        self.assertIn("controller-manager-${{ github.sha }}", fast)
        self.assertIn("actions/download-artifact@v7", e2e)
        self.assertIn("CAPI_PREBUILT_CONTROLLER_BINARY", e2e)
        self.assertIn("CAPI_PREBUILT_ADMIN_SERVER", e2e)
        self.assertIn("CAPI_PREBUILT_ADMIN_WEB", e2e)
        self.assertIn("path: .runtime/rendered/ci-artifact/", fast)
        self.assertLess(
            fast.index("just admin-package-check"),
            fast.index("actions/upload-artifact@v6"),
        )
        for live in (e2e, high):
            self.assertIn("uses: actions/cache@v6", live)
            self.assertIn("path: .tools/cache", live)
            self.assertNotIn("~/.cargo", live)
            self.assertIn("key: capi-full-v3-", live)
            key = next(
                line.strip() for line in live.splitlines()
                if line.strip().startswith("key: capi-full-v3-")
            )
            self.assertEqual(
                (
                    "config/versions.env",
                    "config/settings.env",
                    "config/kind.yaml",
                    "manifests/**",
                    "scripts/cache.py",
                    "scripts/tools.py",
                ),
                tuple(re.findall(r"'([^']+)'", key)),
            )
            self.assertNotIn("Cargo.lock", key)
            self.assertNotIn("rust-toolchain.toml", key)
            self.assertLess(
                live.index("uses: actions/cache@v6"),
                live.index("run: just cache"),
            )
        setup = (
            "just cache", "just controller-fetch", "just admin-fetch",
            "just tools", "just prepare-host", "just create-management",
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
        self.assertNotIn("azure-destructive", WORKFLOW)
        self.assertNotIn("secrets.CAPI_AZURE", WORKFLOW)
        self.assertLess(high.index("just test-e2e-offline"), high.index("just destroy"))
        cleanup = high[high.index("- name: Clean up high-capacity environment"):]
        self.assertIn("if: always()", cleanup)
        self.assertIn("run: just destroy", cleanup)
        for live in (e2e, high):
            for token in ("MIN_DOCKER_CPUS", "MIN_DOCKER_MEMORY_GIB",
                          "MIN_DOCKER_STORAGE_GIB", "timeout-minutes:"):
                self.assertIn(token, live)

    def test_cargo_uses_default_system_locations(self) -> None:
        self.assertNotIn("TMPDIR:", WORKFLOW)
        self.assertNotIn("CARGO_HOME", WORKFLOW)
        self.assertNotIn("CARGO_TARGET_DIR", WORKFLOW)
        self.assertNotIn(".tools/cargo-", WORKFLOW)
        for name in ("fast-checks", "e2e", "high-capacity"):
            self.assertIn(
                "run: install -d -m 700 .tools .runtime",
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

    def test_standard_rust_setup_and_cache_actions(self) -> None:
        self.assertEqual(
            3,
            WORKFLOW.count("uses: actions-rust-lang/setup-rust-toolchain@v2"),
        )
        self.assertNotIn("components:", WORKFLOW)
        for option in (
            "build-warnings:",
            "cache-workspaces:",
            "cache-shared-key:",
            "cache-bin:",
        ):
            self.assertNotIn(option, WORKFLOW)
        self.assertNotIn("Swatinem/rust-cache@", WORKFLOW)
        self.assertNotIn("dtolnay/rust-toolchain@", WORKFLOW)
        self.assertEqual(2, WORKFLOW.count("uses: actions/cache@v6"))
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
                                and e2e == "success"
                                and high
                                == (
                                    "success"
                                    if event in {"workflow_dispatch", "schedule"}
                                    else "skipped"
                                )
                            )
                            self.assertEqual(expected, result.returncode == 0)
