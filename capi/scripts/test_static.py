#!/usr/bin/env python3
from __future__ import annotations

import compileall
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.lib.config import load_configuration


EXPECTED_RECIPES = {
    "tools",
    "prepare-host",
    "preflight",
    "create-management",
    "test-endpoint",
    "test-spike",
    "test-machines",
    "test-storage",
    "test-persistence",
    "create",
    "repair",
    "status",
    "diagnose",
    "verify",
    "destroy-tenant",
    "destroy",
    "break-glass",
    "test-unit",
    "test-static",
    "test-tenant-lifecycle",
    "test-e2e",
}


class StaticFailure(RuntimeError):
    pass


def check(condition: bool, message: str) -> None:
    if not condition:
        raise StaticFailure(message)


def output(*command: str, check_result: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        list(command),
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if check_result and result.returncode != 0:
        raise StaticFailure(f"{' '.join(command)} failed:\n{result.stdout}{result.stderr}")
    return result


def check_recipes() -> None:
    result = output("just", "--justfile", str(ROOT / "Justfile"), "--list", "--unsorted")
    recipes = {
        match.group(1)
        for line in result.stdout.splitlines()
        if (match := re.match(r"\s{4}([a-zA-Z0-9_-]+)", line))
    }
    check(EXPECTED_RECIPES <= recipes, f"missing recipes: {sorted(EXPECTED_RECIPES - recipes)}")
    check("[implemented]" in result.stdout, "task list does not mark implemented recipes")
    check("[phase 2]" in result.stdout, "task list does not mark future blocked recipes")
    unavailable = output(
        "python3",
        "scripts/lab.py",
        "unavailable",
        "create-management",
        check_result=False,
    )
    check(unavailable.returncode != 0, "unimplemented lifecycle command reported success")


def check_configuration() -> None:
    config = load_configuration(ROOT)
    versions_text = (ROOT / "config" / "versions.env").read_text(encoding="utf-8")
    check(":latest" not in versions_text, "mutable latest image tag is forbidden")
    image_keys = sorted(
        key for key, value in config.items() if key.endswith("_IMAGE") and "_TAGGED" not in key
    )
    check(bool(image_keys), "no immutable images are configured")
    for key in image_keys:
        value = config[key]
        check("@sha256:" in value, f"{key} is not digest pinned")
        check(f"{key}_TAGGED" in config, f"{key} lacks tagged provenance")
    url_keys = sorted(key for key in config if key.endswith("_URL"))
    for key in url_keys:
        prefix = key.removesuffix("_URL")
        check(f"{prefix}_SHA256" in config, f"{key} lacks SHA-256")
    check(config["CAPI_CONTRACT"] == "v1beta2", "CAPI contract must be v1beta2")
    check(config["KAMAJI_CAPI_CONTRACT"] == "v1beta2", "Kamaji provider contract must be v1beta2")


def check_repository_boundaries() -> None:
    tracked_paw = output("git", "ls-files", ".paw").stdout.strip()
    check(not tracked_paw, "PAW artifacts must not be tracked")
    tracked_generated = output("git", "ls-files", "capi/.tools", "capi/.runtime").stdout.strip()
    check(not tracked_generated, "generated CAPI tool/runtime state must not be tracked")
    for candidate in (".tools/probe", ".runtime/probe"):
        result = output("git", "check-ignore", candidate, check_result=False)
        check(result.returncode == 0, f"{candidate} is not ignored")
    check(not any(path.is_symlink() for path in ROOT.rglob("*")), "symlinks below capi are forbidden")
    production = [
        ROOT / "Justfile",
        *(ROOT / "config").glob("*"),
        *(
            path
            for path in (ROOT / "scripts").rglob("*.py")
            if path.name != "test_static.py"
        ),
    ]
    forbidden = (
        "../kamaji/.tools",
        "../kamaji/.runtime",
        "../vcluster/.tools",
        "../vcluster/.runtime",
        "AzureCluster",
        "CAPZ",
        "az login",
        "az aks",
    )
    for path in production:
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            check(token not in text, f"{path.relative_to(ROOT)} contains forbidden token {token!r}")


def main() -> int:
    check(compileall.compile_dir(ROOT / "scripts", quiet=1), "Python source compilation failed")
    check(compileall.compile_dir(ROOT / "tests", quiet=1), "Python test compilation failed")
    check_recipes()
    check_configuration()
    check_repository_boundaries()
    print("static checks passed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except StaticFailure as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
