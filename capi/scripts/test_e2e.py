#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.create import reconcile_tenant
from scripts.lib.config import load_configuration, parse_duration
from scripts.lib.host import read_inotify, resolve_host_just
from scripts.lib.locking import e2e_lock
from scripts.lib.process import run
from scripts.lib.redaction import redact
from scripts.tools import verify_all_inputs
from scripts.lib.timing import PhaseTimings


def run_just(
    root: Path,
    config: dict[str, str],
    *arguments: str,
    check: bool = True,
):
    return run(
        [
            str(resolve_host_just(root, config)),
            "--justfile",
            str(root / "Justfile"),
            *arguments,
        ],
        timeout=parse_duration(config["COMMAND_TIMEOUT"]) * 8,
        cwd=root,
        env={**os.environ, "CAPI_E2E_CHILD": "1"},
        check=check,
    )


def verify_no_lab_residue(config: dict[str, str]) -> None:
    if run(
        ["docker", "inspect", f"{config['KIND_CLUSTER_NAME']}-control-plane"],
        timeout=30,
        check=False,
    ).returncode == 0:
        raise RuntimeError("management container remained after teardown")
    for name in (*config["TENANT_NAMES"].split(), config["SPIKE_NAME"]):
        leftovers = run(
            [
                "docker",
                "ps",
                "-aq",
                "--filter",
                f"label=io.x-k8s.kind.cluster={name}",
            ],
            timeout=30,
        ).stdout.split()
        if leftovers:
            raise RuntimeError(f"provider Docker objects remain for {name}")
    volumes = run(
        [
            "docker",
            "volume",
            "ls",
            "-q",
            "--filter",
            f"label={config['OWNERSHIP_LABEL']}={config['LAB_PREFIX']}",
        ],
        timeout=30,
    ).stdout.split()
    if volumes:
        raise RuntimeError(f"owned Docker volumes remain: {volumes}")


def run_e2e() -> int:
    os.umask(0o077)
    config = load_configuration(ROOT)
    original_inotify = {
        "max_user_instances": read_inotify("max_user_instances"),
        "max_user_watches": read_inotify("max_user_watches"),
    }
    failure = None
    timings = PhaseTimings()
    try:
        with timings.phase("tools_cache"):
            run_just(ROOT, config, "tools")
        with timings.phase("initial_cleanup"):
            run_just(ROOT, config, "destroy")
        with timings.phase("host_preparation"):
            run_just(ROOT, config, "prepare-host")
        with timings.phase("management_bootstrap"):
            verify_all_inputs(ROOT, config)
            run_just(ROOT, config, "create-management")

        reconcile_tenant(ROOT, config, timings=timings)
        print("representative tenant PostgreSQL cluster is healthy")
    except BaseException as exc:
        failure = exc
    try:
        with timings.phase("teardown"):
            run_just(ROOT, config, "destroy")
            verify_no_lab_residue(config)
            if (ROOT / ".runtime").exists():
                raise RuntimeError("runtime remained after E2E teardown")
            for name, expected in original_inotify.items():
                if read_inotify(name) != expected:
                    raise RuntimeError(f"host inotify was not restored: {name}")
    except BaseException as cleanup:
        if failure is None:
            failure = cleanup
        else:
            failure.add_note(f"cleanup also failed: {redact(str(cleanup))}")
    timings.emit()
    if failure is not None:
        raise failure
    return 0


def main() -> int:
    with e2e_lock(ROOT, exclusive=True):
        return run_e2e()


if __name__ == "__main__":
    raise SystemExit(main())
