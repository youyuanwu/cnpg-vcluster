#!/usr/bin/env python3
from __future__ import annotations

import compileall
import json
import re
import subprocess
import sys
import tomllib
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


def catalog_identity_literals(catalog: list[dict[str, object]]) -> set[str]:
    result = set()
    for entry in catalog:
        api_version = str(entry["apiVersion"])
        group, separator, _version = api_version.partition("/")
        if separator or entry["kind"] not in {"Namespace", "Secret"}:
            result.add(f"{str(entry['kind']).lower()}/")
        if separator:
            result.update({
                api_version,
                f"{entry['plural']}.{group}",
            })
        elif entry["kind"] not in {"Namespace", "Secret"}:
            result.update({
                f"{entry['plural']}/",
                f"/api/{api_version}/{entry['plural']}",
            })
    return result


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

    azure_example = load_tenant_spec(
        ROOT / "config" / "tenants" / "examples" / "azure.json",
        expected_profile="azure",
        supported_versions={
            "azure": load_env_file(
                ROOT / "config" / "azure" / "defaults.env"
            )["AZURE_SUPPORTED_TENANT_KUBERNETES_VERSION"],
        },
    )
    check(
        azure_example.workers == 3,
        "Azure lifecycle example must request exactly three workers",
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
    check((repository / "Cargo.toml").is_file(), "root Cargo workspace is missing")
    check((repository / "Cargo.lock").is_file(), "root Cargo lockfile is missing")
    check(
        (repository / "rust-toolchain.toml").is_file(),
        "root Rust toolchain file is missing",
    )
    toolchain = tomllib.loads(
        (repository / "rust-toolchain.toml").read_text(encoding="utf-8")
    ).get("toolchain", {})
    check(
        toolchain.get("channel") == "1.98.1"
        and toolchain.get("profile") == "minimal"
        and set(toolchain.get("components", [])) == {"clippy", "rustfmt"},
        "root Rust toolchain must select minimal Rust 1.98.1 with clippy and rustfmt",
    )
    workspace_manifest = tomllib.loads(
        (repository / "Cargo.toml").read_text(encoding="utf-8")
    )
    controller_manifest = tomllib.loads(
        (ROOT / "controller" / "Cargo.toml").read_text(encoding="utf-8")
    )
    check(
        "capi/controller" in workspace_manifest.get("workspace", {}).get("members", []),
        "controller is not a root Cargo workspace member",
    )
    workspace_dependencies = workspace_manifest.get("workspace", {}).get(
        "dependencies", {}
    )
    for section in ("dependencies", "dev-dependencies"):
        for name, declaration in controller_manifest.get(section, {}).items():
            check(
                declaration == {"workspace": True}
                and name in workspace_dependencies,
                f"controller {section} dependency is not workspace-owned: {name}",
            )
    check(
        "profile" not in controller_manifest
        and "release" in workspace_manifest.get("profile", {}),
        "release profile must be owned by the root Cargo workspace",
    )
    required_controller_files = (
        "controller/Cargo.toml",
        "controller/src/bin/manager.rs",
        "controller/Dockerfile",
        "controller/Dockerfile.azure",
        "controller/API_COMPATIBILITY.md",
        "controller/config/crd/bases/tenancy.cnpg-vcluster.io_tenants.yaml",
        "controller/config/rbac/role.yaml",
        "controller/config/management-resources.json",
        "config/tenants/examples/local.yaml",
        "config/tenants/tests/tenant-a.yaml",
        "config/tenants/tests/tenant-b.yaml",
        "config/tenants/tests/tenant-c.yaml",
        "scripts/controller_tenant.py",
        "scripts/controller_metrics.py",
        "scripts/lib/controller_catalog.py",
        "scripts/lib/controller_state.py",
    )
    for relative in required_controller_files:
        check((ROOT / relative).is_file(), f"missing Tenant controller file {relative}")
    justfile = (ROOT / "Justfile").read_text(encoding="utf-8")
    check("controller-metrics:" in justfile, "controller metrics recipe is missing")
    check(
        "scripts/controller_metrics.py --max 12000" in justfile,
        "controller production-line threshold is not enforced",
    )
    controller = ROOT / "controller"
    integration_targets = sorted(
        path.name for path in (controller / "tests").glob("*.rs")
    )
    check(
        integration_targets
        == ["adapters.rs", "allocation.rs", "controller.rs", "finalization.rs"],
        f"unexpected controller integration targets: {integration_targets}",
    )
    check(
        (controller / "tests/support/kube.rs").is_file(),
        "shared controller Kubernetes test support is missing",
    )
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
    azure_manager = (
        controller / "config" / "manager" / "manager-azure.yaml.tpl"
    ).read_text(encoding="utf-8")
    check("--provider=azure" in azure_manager,
          "Azure manager does not select the Azure provider")
    check(not re.search(r"docker.sock|tenant-foundation|activation|calico|cnpg",
                        azure_manager, re.IGNORECASE),
          "Azure manager retains local-only dependencies")
    for relative in ("scripts", "config/versions.env", "Justfile"):
        paths = (ROOT / relative).rglob("*.py") if relative == "scripts" else (ROOT / relative,)
        for path in paths:
            text = path.read_text(encoding="utf-8")
            if path.name == "test_static.py":
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
    jobs = workflow.split("jobs:\n", 1)[1]
    fast_checks = jobs.split("  fast-checks:", 1)[1].split("  e2e:", 1)[0]
    e2e = jobs.split("  e2e:", 1)[1].split("  high-capacity:", 1)[0]
    check(
        re.search(r"(?m)^    needs: fast-checks$", e2e) is not None,
        "PR E2E must depend exactly on fast-checks",
    )
    for token in (
        "actions/upload-artifact@v6",
        "controller-manager-${{ github.sha }}",
        "path: capi/.runtime/rendered/controller/manager",
    ):
        check(token in fast_checks, f"fast-check artifact wiring is missing {token}")
    for token in (
        "actions/download-artifact@v7",
        "controller-manager-${{ github.sha }}",
        "CAPI_PREBUILT_CONTROLLER_BINARY",
        "capi/.tools/artifacts/${{ github.sha }}",
    ):
        check(token in e2e, f"PR E2E artifact wiring is missing {token}")
    high_capacity = workflow.split("  high-capacity:", 1)[1].split(
        "  capi-tests:", 1
    )[0]
    check(
        "CAPI_PREBUILT_CONTROLLER_BINARY" not in high_capacity,
        "high-capacity validation must retain an independent controller build",
    )
    check("--activation-token=${CONTROLLER_ACTIVATION_TOKEN}" in manager,
          "Tenant controller activation token placeholder is missing")
    tenant_dispatch = (ROOT / "scripts" / "tenant.py").read_text(encoding="utf-8")
    tenant_spec = (ROOT / "scripts" / "lib" / "tenant_spec.py").read_text(
        encoding="utf-8"
    )
    tenant_runtime = (ROOT / "scripts" / "lib" / "tenant_runtime.py").read_text(
        encoding="utf-8"
    )
    tenant_timing = (ROOT / "scripts" / "lib" / "tenant_timing.py").read_text(
        encoding="utf-8"
    )
    locking = (ROOT / "scripts" / "lib" / "locking.py").read_text(
        encoding="utf-8"
    )
    check(
        "from scripts.local_tenant import" not in tenant_dispatch,
        "public tenant dispatch still imports the legacy local mutator",
    )
    check(
        'PROFILE = "azure"' in tenant_spec
        and '"databaseCount"' not in tenant_spec
        and '"local"' not in tenant_spec,
        "schema-1 Tenant specifications must remain Azure-only",
    )
    check(
        '"lifecycle" / PROFILE' in tenant_runtime
        and '"local"' not in tenant_runtime,
        "durable Tenant lifecycle paths must remain Azure-only",
    )
    check(
        "from .tenant_spec import PROFILE" in tenant_timing
        and "profile: str" not in tenant_timing
        and "/ profile" not in tenant_timing,
        "Tenant timing evidence must remain Azure-only",
    )
    check(
        "def azure_lock(" in locking
        and "def profile_lock(" not in locking
        and "local.lock" not in locking,
        "obsolete local profile locking remains",
    )
    catalog_consumers = "\n".join(
        (ROOT / relative).read_text(encoding="utf-8")
        for relative in (
            "controller/src/resources/controlplane.rs",
            "controller/src/resources/workers.rs",
            "scripts/lib/controller_state.py",
            "scripts/lib/tenants.py",
            "scripts/lib/controller_scenarios.py",
            "scripts/lib/addons.py",
            "scripts/network.py",
            "scripts/machines.py",
            "scripts/endpoint.py",
            "scripts/tools.py",
            "scripts/lib/providers.py",
            "scripts/test_e2e.py",
        )
    )
    catalog = json.loads(
        (ROOT / "controller/config/management-resources.json").read_text()
    )
    identities = catalog_identity_literals(catalog)
    check(
        catalog_identity_literals([{
            "apiVersion": "example.io/v1",
            "kind": "Widget",
            "plural": "widgets",
        }]) == {"example.io/v1", "widgets.example.io", "widget/"},
        "catalog identity guard does not cover newly added kinds",
    )
    check(
        catalog_identity_literals([{
            "apiVersion": "v1",
            "kind": "NewCore",
            "plural": "newcores",
        }]) == {"newcore/", "newcores/", "/api/v1/newcores"},
        "catalog identity guard does not cover new core kinds",
    )
    for identity in identities:
        check(
            identity not in catalog_consumers,
            f"catalog-owned management identity remains duplicated: {identity}",
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
            == {"kubernetesVersion", "workers", "provider"}
            and re.search(
                r"^  provider:\n    type: local\n    databases: [1-3]$",
                manifest,
                re.MULTILINE,
            )
            is not None,
            f"invalid local Tenant manifest {relative}",
        )
    production = [
        *(ROOT / "config").glob("*"),
        *(
            path
            for path in (ROOT / "scripts").rglob("*.py")
            if path.name != "test_static.py"
            and path != ROOT / "scripts" / "azure.py"
            and (ROOT / "scripts" / "lib" / "azure") not in path.parents
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
    azure_sources = [
        ROOT / "scripts" / "azure.py",
        *(ROOT / "scripts" / "lib" / "azure").rglob("*.py"),
    ]
    azure_source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in azure_sources
    )
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
