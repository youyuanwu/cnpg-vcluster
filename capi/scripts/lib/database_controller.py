from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from scripts.lib.config import parse_duration
from scripts.lib.controller import STATIC_MANAGER_FLAGS, rust_toolchain, verify_static_manager
from scripts.lib.files import ensure_private_dir
from scripts.lib.process import run


def source_digest(root: Path) -> str:
    digest = hashlib.sha256()
    paths = sorted([
        *root.joinpath("database-controller", "src").rglob("*.rs"),
        *root.joinpath("database-runtime", "src").rglob("*.rs"),
        *root.joinpath("database-controller", "config").rglob("*.yaml"),
        *root.joinpath("database-controller", "config").rglob("*.json"),
        *root.joinpath("database-controller", "config").rglob("*.tpl"),
        root / "database-controller" / "Dockerfile",
        root / "database-controller" / "Cargo.toml",
        root / "database-runtime" / "Cargo.toml",
        root.parent / "Cargo.toml",
        root.parent / "Cargo.lock",
        root.parent / "rust-toolchain.toml",
    ])
    for path in paths:
        digest.update(path.relative_to(root.parent).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def build_admission_image(
    root: Path,
    config: dict[str, str],
    image: str,
) -> str:
    cargo, _ = rust_toolchain(root)
    run(
        [cargo, "fetch", "--locked"],
        cwd=root.parent, timeout=parse_duration(config["COMMAND_TIMEOUT"]) * 8,
    )
    run(
        [cargo, "run", "--locked", "--offline", "-p", "tenant-database-controller",
         "--bin", "generate", "--", "--check"],
        cwd=root.parent, timeout=parse_duration(config["COMMAND_TIMEOUT"]) * 8,
    )
    run(
        [cargo, "rustc", "--locked", "--offline", "--release",
         "-p", "tenant-database-controller", "--bin", "admission",
         "--", *STATIC_MANAGER_FLAGS],
        cwd=root.parent, timeout=parse_duration(config["COMMAND_TIMEOUT"]) * 8,
    )
    binary = root.parent / "target" / "release" / "admission"
    verify_static_manager(binary)
    build_root = root / ".runtime" / "rendered" / "database-admission-build"
    if build_root.exists():
        shutil.rmtree(build_root)
    ensure_private_dir(build_root)
    try:
        shutil.copy2(root / "database-controller" / "Dockerfile", build_root / "Dockerfile")
        shutil.copy2(binary, build_root / "admission")
        run(
            ["docker", "build", "--pull=false", "-t", image, str(build_root)],
            timeout=parse_duration(config["COMMAND_TIMEOUT"]) * 8,
        )
    finally:
        shutil.rmtree(build_root)
    return image


def admission_manifests(root: Path, *, azure: bool = False) -> tuple[Path, ...]:
    config = root / "database-controller" / "config"
    return (
        config / "rbac" / "gates-namespace.yaml",
        config / "crd" / "bases" / "tenancy.cnpg-vcluster.io_tenantdatabases.yaml",
        *sorted(
            path for path in (config / "rbac").glob("*.yaml")
            if not path.name.endswith("-namespace.yaml")
            and (azure or path.name != "controller-cluster-role-azure.yaml")
            and (not azure or path.name != "controller-cluster-role.yaml")
        ),
        config / "admission" / "issuer.yaml",
        config / "admission" / "ca-certificate.yaml",
        config / "admission" / "ca-issuer.yaml",
        config / "admission" / "serving-certificate.yaml",
        config / "admission" / "service.yaml",
    )


def admission_webhooks(root: Path) -> tuple[Path, Path]:
    directory = root / "database-controller" / "config" / "admission"
    return directory / "mutating-webhook.yaml", directory / "validating-webhook.yaml"


def render_admission_deployment(root: Path, image: str) -> str:
    template = (
        root / "database-controller" / "config" / "admission" / "deployment.yaml.tpl"
    ).read_text(encoding="utf-8")
    document = template.replace(
        "${ADMISSION_IMAGE}", image
    ).replace("${SOURCE_DIGEST}", source_digest(root))
    if "${" in document:
        raise RuntimeError("database admission Deployment has unresolved variables")
    return document
