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
    def test_parallel_jobs_keep_a_required_pr_gate(self) -> None:
        fast, e2e, gate = job("fast-checks"), job("e2e"), job("capi-tests")
        self.assertNotIn("    needs:", fast + e2e)
        self.assertNotIn("    if:", fast)
        self.assertIn("if: github.event_name != 'push'", e2e)
        self.assertIn("name: CAPI tests\n", gate)
        self.assertIn("if: always()", gate)
        self.assertIn("needs: [fast-checks, e2e]", gate)
        for command in (
            "just test-unit", "just test-static", "just controller-tools",
            "just controller-verify", "just controller-vet", "just controller-test",
        ):
            self.assertIn(command, fast)
        self.assertNotIn("just cache", fast)
        self.assertNotIn("just controller-test", e2e)
        self.assertIn("just test-e2e", e2e)
        self.assertLess(e2e.index("just cache"), e2e.index("just test-e2e"))
        self.assertNotIn("just tools", e2e)

    def test_pr_push_manual_and_schedule_have_separate_concurrency(self) -> None:
        events = WORKFLOW.split("permissions:", 1)[0]
        for event in ("pull_request:", "push:", "workflow_dispatch:", "schedule:"):
            self.assertIn(f"  {event}", events)
        self.assertIn("      - main", events)
        self.assertIn('cron: "23 4 * * 1"', events)
        self.assertNotIn("paths:", events)
        self.assertIn("${{ github.event_name }}-${{ github.ref }}", WORKFLOW)
        self.assertIn("cancel-in-progress: true", WORKFLOW)

    def test_cache_allowlist_excludes_oci_and_compiled_build_caches(self) -> None:
        for name in ("fast-checks", "e2e"):
            body = job(name)
            self.assertLess(body.index("umask 077"), body.index("uses: actions/cache"))
            self.assertIn("mkdir -p .tools/", body)
        cached_paths = []
        for match in re.finditer(r"(?m)^ {10}path: (.+)$", WORKFLOW):
            if match[1] == "|":
                for line in WORKFLOW[match.end():].splitlines()[1:]:
                    if not line.startswith(" " * 12):
                        break
                    cached_paths.append(line.strip())
            else:
                cached_paths.append(match[1])
        self.assertCountEqual(
            [
                "capi/.tools/controller-inputs/go-linux-amd64.tar.gz",
                "capi/.tools/controller-inputs/envtest-linux-amd64.tar.gz",
                "capi/.tools/go-mod-cache",
                "capi/.tools/go-mod-cache",
            ],
            cached_paths,
        )
        self.assertNotIn("restore-keys:", WORKFLOW)
        self.assertIn("hashFiles('capi/config/versions.env')", WORKFLOW)
        module_keys = re.findall(r"(?m)^\s+key: (controller-modules-.*)$", WORKFLOW)
        self.assertEqual(2, len(module_keys))
        self.assertEqual(module_keys[0], module_keys[1])
        self.assertIn("'capi/controller/go.mod', 'capi/controller/go.sum'", module_keys[0])

    def test_gate_never_accepts_failed_or_unexpectedly_skipped_checks(self) -> None:
        script = textwrap.dedent(job("capi-tests").split("        run: |\n", 1)[1])
        for event in ("pull_request", "workflow_dispatch", "schedule", "push"):
            for fast in ("success", "failure", "skipped", "cancelled"):
                for e2e in ("success", "failure", "skipped", "cancelled"):
                    with self.subTest(event=event, fast=fast, e2e=e2e):
                        result = subprocess.run(
                            ["bash", "--noprofile", "--norc", "-e", "-c", script],
                            env={
                                **os.environ,
                                "FAST_RESULT": fast,
                                "E2E_RESULT": e2e,
                                "EVENT_NAME": event,
                            },
                            capture_output=True,
                            check=False,
                        )
                        expected = fast == "success" and e2e == (
                            "skipped" if event == "push" else "success"
                        )
                        self.assertEqual(expected, result.returncode == 0)
