from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

from scripts.lib.host import restore_inotify
from scripts.lib.kube import ManagementClient
from scripts.lib.management import (
    _render_cert_manager,
    _render_metallb,
    _render_metallb_pool,
    delete_management,
    management_status,
    reconcile_network,
    require_management_ownership,
    validate_management_kubeconfig,
)
from scripts.lib.providers import delete_providers


def _validate_runtime_inventory(root: Path) -> None:
    runtime = root / ".runtime"
    if not runtime.exists():
        return
    allowed_files = {
        "host/inotify.json",
        "host/.lock",
        "management/identity.json",
        "management/network.json",
        "management/kubeconfig",
        "rendered/cert-manager.yaml",
        "rendered/kamaji.yaml",
        "rendered/metallb-pool.yaml",
        "rendered/metallb.yaml",
        "rendered/providers/capi-bootstrap-components.yaml",
        "rendered/providers/capi-core-components.yaml",
        "rendered/providers/capd-components.yaml",
        "rendered/providers/kamaji-capi-components.yaml",
    }
    for path in runtime.rglob("*"):
        relative = path.relative_to(runtime).as_posix()
        details = path.lstat()
        if stat.S_ISLNK(details.st_mode):
            raise RuntimeError(f"runtime path is a symlink: {relative}")
        if path.is_dir():
            if details.st_uid != os.getuid() or details.st_mode & 0o077:
                raise RuntimeError(f"runtime directory is not private: {relative}")
            continue
        if relative not in allowed_files:
            raise RuntimeError(f"unexpected runtime file blocks cleanup: {relative}")
        if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid():
            raise RuntimeError(f"runtime file is not an owned regular file: {relative}")
        if details.st_mode & 0o077:
            raise RuntimeError(f"runtime file is not owner-only: {relative}")


def _delete_kubernetes_stack(root: Path, config: dict[str, str], client: ManagementClient) -> None:
    delete_providers(root, config, client)

    client.helm(
        "uninstall",
        "kamaji",
        "--namespace",
        config["MANAGEMENT_NAMESPACE"],
        "--timeout",
        config["DELETE_TIMEOUT"],
        check=False,
    )
    rendered_kamaji = root / ".runtime" / "rendered" / "kamaji.yaml"
    if rendered_kamaji.is_file():
        client.kubectl(
            "delete",
            "-f",
            str(rendered_kamaji),
            "--ignore-not-found",
            "--wait=false",
            check=False,
        )
    client.kubectl(
        "delete",
        "namespace",
        config["MANAGEMENT_NAMESPACE"],
        "--ignore-not-found",
        "--wait=true",
        f"--timeout={config['DELETE_TIMEOUT']}",
        check=False,
    )

    network = reconcile_network(root, config)
    pool = _render_metallb_pool(root, config, network)
    client.kubectl("delete", "-f", str(pool), "--ignore-not-found", check=False)
    metallb = _render_metallb(root, config)
    client.kubectl(
        "delete",
        "-f",
        str(metallb),
        "--ignore-not-found",
        "--wait=false",
        check=False,
    )
    client.kubectl(
        "delete",
        "namespace",
        "metallb-system",
        "--ignore-not-found",
        "--wait=true",
        f"--timeout={config['DELETE_TIMEOUT']}",
        check=False,
    )

    client.helm(
        "uninstall",
        "cert-manager",
        "--namespace",
        "cert-manager",
        "--timeout",
        config["DELETE_TIMEOUT"],
        check=False,
    )
    cert_manager = _render_cert_manager(root, config, client)
    client.kubectl(
        "delete",
        "-f",
        str(cert_manager),
        "--ignore-not-found",
        "--wait=false",
        check=False,
    )
    client.kubectl(
        "delete",
        "namespace",
        "cert-manager",
        "--ignore-not-found",
        "--wait=true",
        f"--timeout={config['DELETE_TIMEOUT']}",
        check=False,
    )


def destroy(root: Path, config: dict[str, str]) -> None:
    _validate_runtime_inventory(root)
    status = management_status(root, config)
    any_management = any(
        status.get(key)
        for key in ("clusterReported", "containerPresent", "ownershipRecord", "kubeconfig")
    )
    if any_management:
        require_management_ownership(root, config)
        validate_management_kubeconfig(root, config)
        if not status.get("apiReady"):
            raise RuntimeError("owned management API is not reachable; refusing partial cleanup")
        _delete_kubernetes_stack(root, config, ManagementClient(root, config))
        delete_management(root, config)
    restore_inotify(root, config)
    runtime = root / ".runtime"
    if runtime.exists():
        shutil.rmtree(runtime)
    print("management experiment resources removed")
