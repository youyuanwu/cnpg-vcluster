#!/usr/bin/env python3
from __future__ import annotations

import compileall
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.lib.config import load_configuration, load_env_file


EXPECTED_RECIPES = {
    "default",
    "cache",
    "tools",
    "prepare-host",
    "preflight",
    "azure-preflight",
    "azure-create-foundation",
    "azure-create-management",
    "azure-foundation-status",
    "azure-destroy",
    "azure-test-tenant-lifecycle",
    "tenant-create",
    "tenant-status",
    "tenant-delete",
    "local-tenant-apply",
    "local-tenant-status",
    "local-tenant-delete",
    "controller-generate",
    "controller-fetch",
    "controller-verify",
    "controller-test",
    "controller-metrics",
    "controller-lint",
    "controller-build",
    "controller-image",
    "test-controller-convergence",
    "test-controller-readiness",
    "test-controller-deletion",
    "controller-tenant-status",
    "create-management",
    "dev-bootstrap",
    "dev-clean",
    "test-endpoint",
    "test-endpoint-negative",
    "test-spike",
    "test-network-negative",
    "test-machines",
    "test-storage",
    "test-storage-negative",
    "test-persistence",
    "test-persistence-negative",
    "diagnose",
    "destroy",
    "break-glass",
    "test-unit",
    "test-static",
    "test-management",
    "test-tenant-lifecycle",
    "test-e2e",
    "test-e2e-offline",
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
    check(
        EXPECTED_RECIPES == recipes,
        "recipe surface changed: "
        f"missing={sorted(EXPECTED_RECIPES - recipes)} "
        f"unexpected={sorted(recipes - EXPECTED_RECIPES)}",
    )
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
    from scripts.lib.tenant_spec import load_tenant_spec

    load_tenant_spec(
        ROOT / "config" / "tenants" / "examples" / "azure.json",
        expected_profile="azure",
        supported_versions={
            "azure": load_env_file(
                ROOT / "config" / "azure" / "defaults.env"
            )["AZURE_SUPPORTED_TENANT_KUBERNETES_VERSION"],
        },
    )
    for key in (
        "VIP_POOL_START_OFFSET_FROM_BROADCAST",
        "VIP_POOL_END_OFFSET_FROM_BROADCAST",
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
    check(not (repository / "Makefile").exists(), "obsolete root Makefile remains")
    check(not (repository / "vcluster").exists(), "obsolete vcluster lab remains")
    check(not (repository / "kamaji").exists(), "obsolete standalone Kamaji lab remains")
    required_controller_files = (
        "controller/Cargo.toml",
        "controller/Cargo.lock",
        "controller/src/bin/manager.rs",
        "controller/Dockerfile",
        "controller/API_COMPATIBILITY.md",
        "controller/config/crd/bases/tenancy.cnpg-vcluster.io_tenants.yaml",
        "controller/config/rbac/role.yaml",
        "config/tenants/examples/local.yaml",
        "config/tenants/tests/tenant-a.yaml",
        "config/tenants/tests/tenant-b.yaml",
        "config/tenants/tests/tenant-c.yaml",
        "scripts/controller_tenant.py",
        "scripts/controller_metrics.py",
        "scripts/lib/controller_cutover.py",
    )
    for relative in required_controller_files:
        check((ROOT / relative).is_file(), f"missing Tenant controller file {relative}")
    justfile = (ROOT / "Justfile").read_text(encoding="utf-8")
    check("controller-metrics:" in justfile, "controller metrics recipe is missing")
    controller = ROOT / "controller"
    check(not list(controller.rglob("*.go")), "local controller Go source remains")
    check(not list(controller.rglob("go.mod")) and not list(controller.rglob("go.sum")),
          "local controller Go module remains")
    check(not (controller / "config" / "staged").exists(), "staged artifacts remain")
    check(not (controller / "config" / "webhook").exists(), "local webhook manifests remain")
    crd = (controller / "config/crd/bases/tenancy.cnpg-vcluster.io_tenants.yaml").read_text(
        encoding="utf-8"
    )
    check("name: v1alpha2" in crd and "name: v1alpha1" not in crd
          and "controller-gen.kubebuilder.io" not in crd,
          "the authoritative Tenant CRD is not Rust v1alpha2")
    generator = (controller / "src/bin/generate.rs").read_text(encoding="utf-8")
    check('join("config")' in generator and
          '"crd/bases/tenancy.cnpg-vcluster.io_tenants.yaml"' in generator,
          "generator does not target the authoritative CRD")
    tracked = output("git", "ls-files", "--cached", "capi/controller").stdout.splitlines()
    present = output("git", "ls-files", "--deleted", "capi/controller").stdout.splitlines()
    live_tracked = set(tracked) - set(present)
    check(not any(name.endswith((".go", "/go.mod", "/go.sum")) or
                  "/config/webhook/" in name for name in live_tracked),
          "tracked legacy controller surface remains")
    manager = (controller / "config" / "manager" / "manager.yaml.tpl").read_text(encoding="utf-8")
    check(not re.search(r"webhook|tls|9443|serving-cert", manager, re.IGNORECASE),
          "manager still exposes local admission webhook or TLS")
    for relative in ("scripts", "config/versions.env", "Justfile"):
        paths = (ROOT / relative).rglob("*.py") if relative == "scripts" else (ROOT / relative,)
        for path in paths:
            text = path.read_text(encoding="utf-8")
            if path.name == "test_static.py" or path.name == "controller_cutover.py":
                continue
            check(not re.search(r"GO_VERSION|GO_URL|GO_SHA256|ENVTEST_|controller-gen\b|"
                                r"GOCACHE|GOMODCACHE|KUBEBUILDER_ASSETS|"
                                r"controller-tools|controller-vet|go-mod-cache|"
                                r"go-linux-amd64|envtest-linux-amd64", text),
                  f"legacy controller acquisition or environment in {path}")
    for relative in ("scripts/lib/controller.py", "scripts/endpoint.py"):
        text = (ROOT / relative).read_text(encoding="utf-8")
        check(not re.search(r'config[/"]\s*/?\s*["\']webhook|'
                            r'validating-webhook\.yaml|tenant-controller-serving-cert|'
                            r'--webhook-port|9443', text),
              f"local admission lifecycle remains in {relative}")
    workflow = (ROOT.parent / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    check(not re.search(r"go\.mod|go\.sum|envtest|controller-gen\b|"
                        r"controller-tools|controller-vet|go-mod-cache|"
                        r"go-linux-amd64|GO_VERSION|GOCACHE|GOMODCACHE", workflow),
          "CI still references local Go tooling")
    check(
        "--mutation-enabled=${CONTROLLER_MUTATION_ENABLED}" in manager,
        "Tenant controller mutation template placeholder is missing",
    )
    lifecycle_epoch = "rust-operator-v1"
    check(
        f'CONTROLLER_LIFECYCLE_EPOCH = "{lifecycle_epoch}"'
        in (ROOT / "scripts" / "lib" / "controller.py").read_text(
            encoding="utf-8"
        ),
        "controller installer lifecycle epoch is inconsistent",
    )
    tenant_dispatch = (ROOT / "scripts" / "tenant.py").read_text(encoding="utf-8")
    check(
        "from scripts.local_tenant import" not in tenant_dispatch,
        "public tenant dispatch still imports the legacy local mutator",
    )
    for relative in (
        "scripts/local_tenant.py",
        "scripts/create.py",
        "scripts/destroy_tenant.py",
        "scripts/verify.py",
        "config/tenants/examples/local.json",
        "config/tenants/tests/tenant-a.json",
        "config/tenants/tests/tenant-b.json",
        "config/tenants/tests/tenant-c.json",
        "scripts/test_controller_phase2.py",
        "scripts/test_controller_phase3.py",
    ):
        check(
            not (ROOT / relative).exists(),
            f"obsolete local mutation artifact remains: {relative}",
        )
    production_python = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "scripts").rglob("*.py")
        if path.name != "test_static.py"
    )
    for token in (
        "from scripts.create import",
        "from scripts.destroy_tenant import",
        "from scripts.local_tenant import",
        "from scripts.verify import",
        "def apply_control_plane(",
        "def apply_workers(",
        "def apply_addons(",
        "def delete_addons(",
        "def install_cnpg(",
        "def delete_cnpg(",
        "def preload_worker_images(",
        "def recorded_local_tenants(",
        "def load_local_tenant_spec(",
    ):
        check(
            token not in production_python,
            f"obsolete callable local mutator remains: {token}",
        )
    controller_python = "\n".join(
        (ROOT / relative).read_text(encoding="utf-8")
        for relative in (
            "scripts/lib/controller_scenarios.py",
            "scripts/test_controller_convergence.py",
            "scripts/test_controller_readiness.py",
            "scripts/test_controller_deletion.py",
            "scripts/test_controller_allocation.py",
            "scripts/test_tenant_lifecycle.py",
            "scripts/test_e2e.py",
        )
    )
    for token in (
        'status.get("stage")',
        'status.get("observedResources")',
        'status.get("tenantResources")',
        'status.get("dockerVolume")',
        'status.get("workerContainers")',
        'status.get("teardown")',
        'status.get("specHash")',
        'status.get("endpoint")',
        'spec["podCIDR"]',
        'spec["serviceCIDR"]',
        'spec["databaseCount"]',
        "tenant-endpoint-allocations",
    ):
        check(
            token not in controller_python,
            f"controller scenario still consumes removed status field: {token}",
        )
    network_source = (ROOT / "scripts" / "network.py").read_text(encoding="utf-8")
    check(
        "_drift_kube_proxy_and_wait_for_repair" not in network_source
        and "_verify_static_kube_proxy" in network_source
        and "_drift_machine_deployment_and_wait_for_repair" in network_source,
        "network gate must retain static ownership/recreation and dynamic repair, not static content repair",
    )
    check(
        "def delete_tenant(" not in (
            ROOT / "scripts" / "lib" / "tenants.py"
        ).read_text(encoding="utf-8"),
        "legacy imperative tenant deletion helper remains",
    )
    for relative, expected_name in (
        ("config/tenants/examples/local.yaml", "tenant-example"),
        ("config/tenants/tests/tenant-a.yaml", "tenant-a"),
        ("config/tenants/tests/tenant-b.yaml", "tenant-b"),
        ("config/tenants/tests/tenant-c.yaml", "tenant-c"),
    ):
        manifest = (ROOT / relative).read_text(encoding="utf-8")
        check(
            "apiVersion: tenancy.cnpg-vcluster.io/v1alpha2" in manifest
            and "kind: Tenant" in manifest
            and f"name: {expected_name}" in manifest
            and set(re.findall(r"^  ([a-zA-Z]+):", manifest.split("spec:\n")[1], re.MULTILINE))
            == {"kubernetesVersion", "workers", "databases"},
            f"invalid local Tenant manifest {relative}",
        )
    production = [
        *(ROOT / "config").glob("*"),
        *(
            path
            for path in (ROOT / "scripts").rglob("*.py")
            if path.name not in {"test_static.py", "azure.py"}
        ),
        *(ROOT / "manifests").rglob("*"),
    ]
    forbidden = (
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
    azure_source = (ROOT / "scripts" / "azure.py").read_text(encoding="utf-8")
    check(
        not re.search(
            r"[\"']vmss[\"']\s*,\s*[\"']delete[\"']",
            azure_source,
        ),
        "normal Azure lifecycle directly deletes a VMSS",
    )
    check(
        "/metadata/finalizers" not in azure_source,
        "normal Azure lifecycle patches Azure provider finalizers",
    )
    check(
        not re.search(
            r"(?:patch|replace).{0,200}(?:azurecluster|azuremachinepool|natgateway)"
            r".{0,200}finalizers",
            azure_source,
            re.IGNORECASE | re.DOTALL,
        ),
        "normal Azure lifecycle removes Azure provider finalizers",
    )


def check_documentation() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    design = (ROOT / "docs" / "high-level-design.md").read_text(
        encoding="utf-8"
    )
    azure_design = (ROOT / "docs" / "azure-experiment-design.md").read_text(
        encoding="utf-8"
    )
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    root_readme = (ROOT.parent / "README.md").read_text(encoding="utf-8")
    readme_flat = " ".join(readme.split())
    design_flat = " ".join(design.split())
    azure_design_flat = " ".join(azure_design.split())
    notices_flat = " ".join(notices.split())
    required_readme = (
        "CAPD `DevCluster` and `DevMachine` resources are development-only",
        "sharing the host kernel",
        "Break-glass finalizer removal",
        "Local tenants are Kubernetes `Tenant` resources",
        "Azure tenants retain the JSON specification and Python lifecycle",
        "`just tenant-delete azure <name> azure/<name>`",
        "Status, conditions, and exits",
        "26.8.6-edge",
        "HAProxy container remains required",
        "authoritative tenant endpoint",
        "Prebound static hostPath PVs have no node affinity",
        "This is a local persistence proof only.",
        "The Tenant controller is the single networking writer.",
        "Worker image delivery is bootstrap-owned",
        "one finalizer deletes the exact recorded CAPI Cluster",
        "while retaining its PVC, PV, and bytes.",
        "`just cache` is the explicit online acquisition",
        "The retained workflow is a development optimization, not a final gate",
        "`tools_cache`",
        "does not silently acquire missing content",
        "owner-only registry storage tree",
        "only on the private kind Docker network",
    )
    required_design = (
        "Ordinary Kubernetes DELETE is accepted",
        "There is no ClusterResourceSet",
        "`preKubeadmCommands`",
        "remove the finalizer last",
        "old controller Pod is proved absent",
        "multiple deleting Tenants do not acquire a shared destructive lock",
        "The legacy local JSON adapter",
        "CAPZ owns tenant MachinePools",
        "Azure tenants are not separate AKS clusters",
        "`just test-e2e-offline`",
        "materialized from the verified active cache",
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
    required_azure_design = (
        "`just tenant-delete azure <tenant> azure/<tenant>`",
        "`just azure-test-tenant-lifecycle`",
        "CAPZ remains responsible for VMSS deletion.",
        "Kubernetes UID/resourceVersion preconditions",
        "cnpg-vcluster-external-control-plane=true",
        "Foundation status rejects a missing, broadened, or conflicting selector.",
        "Targeted deletion to canonical absence",
        "Recreate from the same specification and reach Ready",
    )
    for token in required_azure_design:
        check(
            token in azure_design_flat,
            f"Azure design lacks documentation assertion: {token}",
        )
    check(
        "VMSS-backed tenant workers that run CloudNativePG"
        not in azure_design_flat,
        "Azure design claims unimplemented CloudNativePG behavior",
    )
    for project in (
        "Cluster API",
        "Kamaji CAPI provider",
        "CloudNativePG",
        "PostgreSQL",
        "BusyBox",
        "Distribution",
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
        "one Cluster API and CloudNativePG experiment" in root_readme,
        "root README does not identify the CAPI tenant lifecycle lab",
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
