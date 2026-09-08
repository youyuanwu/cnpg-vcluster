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
    "cache",
    "tools",
    "prepare-host",
    "preflight",
    "create-management",
    "test-endpoint",
    "test-endpoint-negative",
    "test-spike",
    "test-network-negative",
    "test-machines",
    "test-storage",
    "test-storage-negative",
    "test-persistence",
    "test-persistence-negative",
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
    "test-management",
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
    check("[phase " not in result.stdout, "task list still marks blocked recipes")
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
    for key in (
        "VIP_POOL_START_OFFSET_FROM_BROADCAST",
        "VIP_POOL_END_OFFSET_FROM_BROADCAST",
        "TENANT_A_API_VIP_SLOT",
        "TENANT_B_API_VIP_SLOT",
        "SPIKE_API_VIP_SLOT",
    ):
        check(key in config, f"missing VIP configuration {key}")


def check_repository_boundaries() -> None:
    tracked_paw = output("git", "ls-files", ".paw").stdout.strip()
    check(not tracked_paw, "PAW artifacts must not be tracked")
    tracked_generated = output("git", "ls-files", "capi/.tools", "capi/.runtime").stdout.strip()
    check(not tracked_generated, "generated CAPI tool/runtime state must not be tracked")
    for candidate in (".tools/probe", ".runtime/probe"):
        result = output("git", "check-ignore", candidate, check_result=False)
        check(result.returncode == 0, f"{candidate} is not ignored")
    check(not any(path.is_symlink() for path in ROOT.rglob("*")), "symlinks below capi are forbidden")
    check((ROOT / "scripts" / "post_renderer.py").stat().st_mode & 0o111 != 0, "post-renderer is not executable")
    repository = ROOT.parent
    for path in ("Makefile", "kamaji", "vcluster"):
        result = subprocess.run(
            ["git", "diff", "--quiet", "main", "--", path],
            cwd=repository,
            check=False,
        )
        check(result.returncode == 0, f"baseline path changed during CAPI work: {path}")
    production = [
        ROOT / "Justfile",
        *(ROOT / "config").glob("*"),
        *(
            path
            for path in (ROOT / "scripts").rglob("*.py")
            if path.name != "test_static.py"
        ),
        *(ROOT / "manifests").rglob("*"),
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
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            check(token not in text, f"{path.relative_to(ROOT)} contains forbidden token {token!r}")


def check_documentation() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    design = (ROOT / "docs" / "high-level-design.md").read_text(
        encoding="utf-8"
    )
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    root_readme = (ROOT.parent / "README.md").read_text(encoding="utf-8")
    readme_flat = " ".join(readme.split())
    design_flat = " ".join(design.split())
    notices_flat = " ".join(notices.split())
    required_readme = (
        "CAPD `DevCluster` and `DevMachine` resources are development-only",
        "sharing the host kernel",
        "900 KiB",
        "Break-glass finalizer removal",
        "independently provisioned AKS management cluster",
        "does not create or configure Azure resources",
        "Status, conditions, and exits",
        "CAPI implementation does not emit it",
        "Cluster in healthy state",
        "26.8.6-edge",
        "capped at 100 references",
        "HAProxy container remains required",
        "authoritative tenant endpoint",
        "Prebound static hostPath PVs have no node affinity",
        "This is a local persistence proof only.",
        "Kubernetes controllers own CAPI and provider resource reconciliation.",
        "Host code owns only exact kind/CAPD Docker identities, tenant Docker volumes, runtime records, and host setting restoration.",
        "while retaining its PVC, PV, and bytes.",
    )
    required_design = (
        "SkipInfraClusterPatch=true",
        "DynamicInfrastructureClusterPatch=false",
        "ClusterResourceSet packages the initial sources",
        "Azure CSI",
        "CAPZ self-managed `AzureCluster`/`AzureMachine` workers",
        "AzureCluster.spec.controlPlaneEnabled: false",
        "| Identity |",
        "| Add-ons |",
        "| Verification |",
        "No Azure CLI",
        "representative tenant, requires one three-instance PostgreSQL",
        "future tenants are not separate AKS clusters",
    )
    for token in required_readme:
        check(
            token in readme_flat,
            f"CAPI README lacks documentation assertion: {token}",
        )
    for token in required_design:
        check(
            token in design_flat,
            f"CAPI design lacks documentation assertion: {token}",
        )
    for project in (
        "Cluster API",
        "Kamaji CAPI provider",
        "CloudNativePG",
        "PostgreSQL",
        "BusyBox",
    ):
        check(project in notices, f"third-party notices omit {project}")
    direct_etcd = (
        "| etcd | directly pinned server `v3.5.17` and setup image `v3.5.6` "
        "| Apache-2.0 |"
    )
    check(
        direct_etcd in notices_flat,
        "third-party notices do not pin the complete direct etcd row",
    )
    check(
        "<https://github.com/etcd-io/etcd>" in notices,
        "third-party notices omit the direct etcd source URL",
    )
    check(
        "three independent local CloudNativePG experiments" in root_readme,
        "root README does not index all three labs",
    )
    check(
        (ROOT / "licenses" / "README.md").is_file(),
        "license reference directory is missing",
    )


def main() -> int:
    check(compileall.compile_dir(ROOT / "scripts", quiet=1), "Python source compilation failed")
    check(compileall.compile_dir(ROOT / "tests", quiet=1), "Python test compilation failed")
    check_recipes()
    check_configuration()
    check_repository_boundaries()
    check_documentation()
    print("static checks passed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except StaticFailure as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
