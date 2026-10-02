from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from collections.abc import Callable

from scripts.cache import verify_cache
from scripts.lib.config import parse_duration
from scripts.lib.files import ensure_private_dir, write_private_file
from scripts.lib.kube import ManagementClient
from scripts.lib.process import run
from scripts.tools import AZURE_CHART_INPUTS, AZURE_CHART_MAX_BYTES, _verify_private_input


CATALOG_CRD = "tenantdatabasecatalogs.tenancy.cnpg-vcluster.io"
LEGACY_CRD = "tenantdatabases.tenancy.cnpg-vcluster.io"
DATABASE_DEPLOYMENT = "database-controller"
DATABASE_IMAGE_PLACEHOLDER = "database-controller:configure-before-install"
DATABASE_DISK_CLIENT_ID_PLACEHOLDER = "database-disk-client-id:configure-before-install"


def _release_workloads_ready(
    root: Path, kubeconfig: Path, release: str, image: str,
) -> bool:
    workloads = (
        (("cnpg-system", "deployment/cnpg-cloudnative-pg", "manager"),)
        if release == "cnpg" else (
            ("kube-system", "deployment/csi-azuredisk-controller", "azuredisk"),
            ("kube-system", "daemonset/csi-azuredisk-node", "azuredisk"),
        )
    )
    kubectl = str(root / ".tools" / "bin" / "kubectl")

    def read(reference: str, namespace: str | None = None) -> dict | None:
        result = run(
            [kubectl, "--kubeconfig", str(kubeconfig),
             *(["-n", namespace] if namespace else []),
             "get", reference, "--ignore-not-found=true", "-o", "json"],
            timeout=30, check=False,
        )
        if result.returncode:
            raise RuntimeError(f"Azure database runtime inspection failed: {reference}")
        if not result.stdout.strip():
            return None
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise RuntimeError(f"Azure database runtime inspection is invalid: {reference}")
        return value

    for namespace, reference, container_name in workloads:
        object_ = read(reference, namespace)
        if object_ is None:
            return False
        metadata = object_.get("metadata", {})
        spec = object_.get("spec", {})
        status = object_.get("status", {})
        daemon = reference.startswith("daemonset/")
        desired = (status.get("desiredNumberScheduled") if daemon
                   else spec.get("replicas", 1)) if isinstance(spec, dict) and isinstance(status, dict) else None
        observed_generation = status.get("observedGeneration") if isinstance(status, dict) else None
        ready_count = status.get("numberReady" if daemon else "availableReplicas") if isinstance(status, dict) else None
        updated_count = status.get("updatedNumberScheduled" if daemon else "updatedReplicas") if isinstance(status, dict) else None
        template = spec.get("template") if isinstance(spec, dict) else None
        pod_spec = template.get("spec") if isinstance(template, dict) else None
        containers = pod_spec.get("containers") if isinstance(pod_spec, dict) else None
        if (
            not isinstance(metadata, dict) or not isinstance(spec, dict)
            or not isinstance(status, dict)
            or metadata.get("name") != reference.split("/", 1)[1]
            or metadata.get("namespace") != namespace
            or not isinstance(metadata.get("uid"), str) or not metadata["uid"]
            or metadata.get("deletionTimestamp")
            or not isinstance(metadata.get("generation"), int)
            or not isinstance(desired, int) or isinstance(desired, bool) or desired <= 0
            or not isinstance(observed_generation, int) or isinstance(observed_generation, bool)
            or observed_generation < metadata["generation"]
            or not isinstance(ready_count, int) or isinstance(ready_count, bool)
            or ready_count != desired
            or not isinstance(updated_count, int) or isinstance(updated_count, bool)
            or updated_count != desired
            or not isinstance(containers, list)
            or not any(
                isinstance(container, dict) and container.get("name") == container_name
                and container.get("image") == image
                for container in containers
            )
        ):
            return False
    if release == "azuredisk":
        driver = read("csidriver/disk.csi.azure.com")
        if driver is None:
            return False
        metadata = driver.get("metadata", {})
        if (
            metadata.get("name") != "disk.csi.azure.com"
            or not isinstance(metadata.get("uid"), str) or not metadata["uid"]
            or metadata.get("deletionTimestamp")
        ):
            return False
    return True


def install_azure_database_runtime(
    root: Path, config: dict[str, str], kubeconfig: Path,
    *, require_current: Callable[[], None] = lambda: None,
) -> None:
    verified = verify_cache(root, config)
    charts = []
    for filename, _, sha_key in AZURE_CHART_INPUTS:
        source = verified.generation / "inputs" / filename
        content = _verify_private_input(source, config[sha_key])
        if len(content) > AZURE_CHART_MAX_BYTES:
            raise RuntimeError(f"Azure database chart exceeds size limit: {filename}")
        charts.append(content)

    installers = (
        ("cnpg", "cnpg-system", "CNPG_CONTROLLER_IMAGE", ()),
        ("azuredisk", "kube-system", "AZURE_DISK_CSI_IMAGE",
         ("--set", "controller.allowEmptyCloudConfig=true")),
    )
    scratch = root / ".runtime" / "azure-database-installer"
    ensure_private_dir(scratch)
    failures = []
    with tempfile.TemporaryDirectory(dir=scratch) as directory:
        for (release, namespace, image_key, options), chart in zip(installers, charts):
            image = config[image_key]
            if not re.fullmatch(r"[^:@\s]+(?:/[^:@\s]+)+:[^:@\s]+@sha256:[a-f0-9]{64}", image):
                raise RuntimeError(f"{image_key} is not a digest-pinned image")
            repository, tag = image.split(":", 1)
            chart_path = Path(directory) / f"{release}.tgz"
            write_private_file(chart_path, chart)
            image_options = (
                ("image.repository", "image.tag") if release == "cnpg"
                else ("image.azuredisk.repository", "image.azuredisk.tag")
            )
            require_current()
            try:
                marker = (
                    f"cnpg-vcluster:verified:{hashlib.sha256(chart).hexdigest()}:"
                    f"{hashlib.sha256(image.encode()).hexdigest()}"
                )
                status = run(
                    [
                        str(root / ".tools" / "bin" / "helm"), "status", release,
                        "--kubeconfig", str(kubeconfig),
                        "--namespace", namespace, "-o", "json",
                    ],
                    timeout=30, check=False,
                )
                if status.returncode == 0:
                    details = json.loads(status.stdout)
                    if (
                        details.get("info", {}).get("status") == "deployed"
                        and details["info"].get("description") == marker
                        and _release_workloads_ready(
                            root, kubeconfig, release, image,
                        )
                    ):
                        continue
                run(
                    [
                        str(root / ".tools" / "bin" / "helm"),
                        "upgrade", "--install", release, str(chart_path),
                        "--kubeconfig", str(kubeconfig),
                        "--namespace", namespace, "--create-namespace",
                        "--set-string", f"{image_options[0]}={repository}",
                        "--set-string", f"{image_options[1]}={tag}",
                        *options, "--atomic", "--wait", "--timeout", "2m",
                        "--description", marker,
                    ],
                    timeout=150,
                )
            except (subprocess.SubprocessError, RuntimeError, ValueError) as exc:
                failures.append(f"{release}: {exc}")
    if failures:
        raise RuntimeError("Azure database installation unavailable: " + "; ".join(failures))


def build_database_controller_image(
    root: Path, config: dict[str, str], *, image: str | None = None,
) -> str:
    from scripts.lib.controller import (
        STATIC_MANAGER_FLAGS, fetch_controller_dependencies, rust_toolchain,
        verify_static_manager,
    )

    source = root / "database-controller"
    repository = root.parent
    inputs = [
        *source.joinpath("src").rglob("*.rs"),
        source / "Cargo.toml",
        source / "Dockerfile",
        *source.joinpath("config").rglob("*.yaml"),
        root / "database-runtime" / "Cargo.toml",
        *root.joinpath("database-runtime", "src").rglob("*.rs"),
        repository / "Cargo.toml",
        repository / "Cargo.lock",
        repository / "rust-toolchain.toml",
    ]
    cargo, compiler = rust_toolchain(root)
    digest = hashlib.sha256()
    for path in sorted(inputs):
        digest.update(str(path.relative_to(root if path.is_relative_to(root) else repository)).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    digest.update(compiler.encode())
    selected = image or (
        f"{config['TENANT_CONTROLLER_IMAGE_REPOSITORY']}-database:"
        f"{digest.hexdigest()[:16]}"
    )
    prebuilt = os.environ.get("CAPI_PREBUILT_DATABASE_CONTROLLER_BINARY")
    if prebuilt:
        executable = Path(prebuilt).resolve()
        if (
            not executable.is_file()
            or not executable.is_relative_to((root / ".tools" / "artifacts").resolve())
        ):
            raise RuntimeError("prebuilt database manager must be inside .tools/artifacts")
    else:
        fetch_controller_dependencies(root, config)
        result = run(
            [cargo, "rustc", "--locked", "--offline", "--release", "-p",
             "tenant-database-controller", "--bin", "manager",
             "--message-format=json-render-diagnostics", "--", *STATIC_MANAGER_FLAGS],
            cwd=source, timeout=parse_duration(config["COMMAND_TIMEOUT"]) * 8,
        )
        executables = [
            Path(message["executable"])
            for line in result.stdout.splitlines()
            for message in [json.loads(line)]
            if message.get("reason") == "compiler-artifact"
            and message.get("target", {}).get("name") == "manager"
            and message.get("target", {}).get("kind") == ["bin"]
            and isinstance(message.get("executable"), str)
        ]
        if len(executables) != 1 or not executables[0].is_file():
            raise RuntimeError("Cargo did not report exactly one database manager executable")
        executable = executables[0]
    verify_static_manager(executable)
    build_root = root / ".runtime" / "rendered" / "database-controller-build"
    shutil.rmtree(build_root, ignore_errors=True)
    ensure_private_dir(build_root)
    try:
        shutil.copy2(source / "Dockerfile", build_root / "Dockerfile")
        manager = build_root / "manager"
        shutil.copy2(executable, manager)
        manager.chmod(0o755)
        run(
            ["docker", "build", "--pull=false", "-t", selected, str(build_root)],
            timeout=parse_duration(config["COMMAND_TIMEOUT"]) * 8,
        )
    finally:
        shutil.rmtree(build_root, ignore_errors=True)
    return selected


def render_database_controller(
    root: Path, image: str, *, azure: bool = False,
    azure_identity_client_id: str | None = None,
) -> Path:
    if (
        image == DATABASE_IMAGE_PLACEHOLDER
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@-]+", image) is None
    ):
        raise RuntimeError("database controller image must be built before deployment")
    template = (
        root / "database-controller" / "config" / "deployment"
        / ("controller-azure.yaml" if azure else "controller.yaml")
    ).read_text(encoding="utf-8")
    if template.count(DATABASE_IMAGE_PLACEHOLDER) != 1:
        raise RuntimeError("database controller deployment image placeholder is invalid")
    if azure:
        if (
            azure_identity_client_id is None
            or re.fullmatch(
                r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}",
                azure_identity_client_id,
            ) is None
            or template.count(DATABASE_DISK_CLIENT_ID_PLACEHOLDER) != 1
        ):
            raise RuntimeError("dedicated Azure disk identity must be bound before deployment")
        template = template.replace(DATABASE_DISK_CLIENT_ID_PLACEHOLDER, azure_identity_client_id)
    destination = root / ".runtime" / "rendered" / "database-controller" / "controller.yaml"
    write_private_file(destination, template.replace(DATABASE_IMAGE_PLACEHOLDER, image))
    return destination


def install_database_controller(
    root: Path, client: ManagementClient, image: str, *, azure: bool = False,
    azure_identity_client_id: str | None = None,
) -> None:
    manifest = render_database_controller(
        root, image, azure=azure, azure_identity_client_id=azure_identity_client_id,
    )
    if azure:
        client.kubectl(
            "-n", "tenant-system", "annotate", "serviceaccount/database-controller",
            f"azure.workload.identity/client-id={azure_identity_client_id}",
            "--overwrite",
        )
    client.kubectl(
        "apply", "--server-side", "--field-manager=cnpg-vcluster-database-controller",
        "--force-conflicts", "-f", str(manifest),
    )


def _optional_crd(client: ManagementClient, name: str, output: str) -> str:
    result = client.kubectl(
        "get", f"crd/{name}", "--ignore-not-found=true", "-o", output,
        check=False,
    )
    if result.returncode != 0:
        if re.search(r"\bnot\s*found\b|\bnotfound\b", result.stderr, re.I):
            return ""
        raise RuntimeError(f"failed to inspect CRD {name}: {result.stderr}")
    return result.stdout.strip()


def require_absent_legacy_database_crd(client: ManagementClient) -> None:
    if _optional_crd(client, LEGACY_CRD, "name"):
        raise RuntimeError(
            f"Legacy TenantDatabase CRD {LEGACY_CRD} is installed; "
            "clean-install-only cutover is blocked before mutation. "
            "Do not delete the CRD automatically. Separately prove that every "
            "served version has no retained instances, that in-flight CREATE "
            "requests have terminal outcomes on every API server, and that "
            "legacy workloads, storage, and admission resources are safely "
            "retired before removing it through an operator-approved cleanup."
        )


def catalog_manifests(root: Path, *, azure: bool = False) -> tuple[Path, ...]:
    config = root / "database-controller" / "config"
    return (
        config / "crd" / "bases" / "tenancy.cnpg-vcluster.io_tenantdatabasecatalogs.yaml",
        *sorted(
            path for path in (config / "rbac").glob("*.yaml")
            if (azure or path.name != "controller-cluster-role-azure.yaml")
            and (not azure or path.name != "controller-cluster-role.yaml")
        ),
    )


def inspect_catalog_inventory(client: ManagementClient) -> None:
    observed = _optional_crd(client, CATALOG_CRD, "name")
    if not observed:
        return
    result = client.kubectl(
        "get", "--raw=/apis/tenancy.cnpg-vcluster.io/v1alpha1/tenantdatabasecatalogs",
        check=False,
    )
    try:
        listing = json.loads(result.stdout)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("TenantDatabaseCatalog inventory is invalid") from exc
    if (
        result.returncode != 0 or not isinstance(listing, dict)
        or listing.get("apiVersion") != "tenancy.cnpg-vcluster.io/v1alpha1"
        or listing.get("kind") != "TenantDatabaseCatalogList"
        or not isinstance(listing.get("metadata"), dict)
        or listing["metadata"].get("continue", "") != ""
        or not isinstance(listing.get("items"), list)
    ):
        raise RuntimeError("TenantDatabaseCatalog inventory is invalid")
    if listing["items"]:
        raise RuntimeError("retained TenantDatabaseCatalogs block Tenant API cutover")
