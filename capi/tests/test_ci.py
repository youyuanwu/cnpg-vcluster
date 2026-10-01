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
        fast, e2e, high, azure, gate = (
            job(name) for name in (
                "fast-checks", "e2e", "high-capacity", "azure-destructive", "capi-tests",
            )
        )
        self.assertNotIn("    needs:", fast + high)
        self.assertRegex(e2e, r"(?m)^    needs: fast-checks$")
        self.assertNotIn("    if:", fast)
        self.assertIn("if: github.event_name == 'pull_request'", e2e)
        self.assertIn("if: github.event_name == 'schedule' || github.event_name == 'workflow_dispatch'", high)
        self.assertIn("name: CAPI tests\n", gate)
        self.assertIn("if: always()", gate)
        self.assertIn("needs: [fast-checks, e2e, high-capacity, azure-destructive]", gate)
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
        self.assertLess(e2e.index("just cache"), e2e.index("just test-e2e"))
        self.assertIn("just test-e2e-offline", e2e)
        self.assertIn("CAPI_PREBUILT_DATABASE_CONTROLLER_BINARY", e2e)
        self.assertIn("database-manager", fast)
        self.assertIn("actions/upload-artifact@v6", fast)
        self.assertIn("controller-manager-${{ github.sha }}", fast)
        self.assertIn("actions/download-artifact@v7", e2e)
        self.assertIn("CAPI_PREBUILT_CONTROLLER_BINARY", e2e)
        self.assertIn("CAPI_PREBUILT_ADMIN_SERVER", e2e)
        self.assertIn("CAPI_PREBUILT_ADMIN_WEB", e2e)
        self.assertIn("path: capi/.runtime/rendered/ci-artifact/", fast)
        self.assertLess(
            fast.index("just admin-package-check"),
            fast.index("actions/upload-artifact@v6"),
        )
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
        self.assertIn("just azure-test-tenant-lifecycle", azure)
        self.assertIn("just azure-foundation-status", azure)
        self.assertIn("CAPI_AZURE_FOUNDATION_INVENTORY", azure)
        self.assertIn("CAPI_AZURE_MANAGEMENT_KUBECONFIG", azure)
        self.assertIn("id-token: write", azure)
        self.assertIn("azure/login@v2", azure)
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
        self.assertNotIn("actions/cache@", WORKFLOW)
        self.assertNotRegex(WORKFLOW, r"go\.mod|go\.sum|envtest|controller-gen|"
                            r"controller-tools|controller-vet|go-mod-cache|"
                            r"GO_VERSION|GOCACHE|GOMODCACHE")

    def test_gate_never_accepts_failed_or_unexpectedly_skipped_checks(self) -> None:
        script = textwrap.dedent(job("capi-tests").split("        run: |\n", 1)[1])
        for event in ("pull_request", "workflow_dispatch", "schedule", "push"):
            for fast in ("success", "failure", "skipped", "cancelled"):
                for e2e in ("success", "failure", "skipped", "cancelled"):
                    for high in ("success", "failure", "skipped", "cancelled"):
                        for azure in ("success", "failure", "skipped", "cancelled"):
                            with self.subTest(event=event, fast=fast, e2e=e2e,
                                              high=high, azure=azure):
                                result = subprocess.run(
                                    ["bash", "--noprofile", "--norc", "-e", "-c", script],
                                    env={
                                        **os.environ, "FAST_RESULT": fast,
                                        "E2E_RESULT": e2e, "HIGH_CAPACITY_RESULT": high,
                                        "AZURE_RESULT": azure,
                                        "EVENT_NAME": event,
                                    },
                                    capture_output=True,
                                    check=False,
                                )
                                expected = (
                                    fast == "success"
                                    and e2e == ("success" if event == "pull_request" else "skipped")
                                    and high == ("success" if event in {"workflow_dispatch", "schedule"} else "skipped")
                                    and azure == ("success" if event in {"workflow_dispatch", "schedule"} else "skipped")
                                )
                                self.assertEqual(expected, result.returncode == 0)
