from __future__ import annotations

import os
import re
import shutil
import stat
from pathlib import Path

from scripts.lib.host import restore_inotify, validate_inotify_state
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
from scripts.lib.addons import delete_addons
from scripts.lib.tenants import (
    configured_tenants,
    delete_tenant,
    inspect_management_resource,
    spike_tenant,
    verify_tenant_management_ownership,
)
from scripts.lib.process import run
from scripts.lib.registry import (
    delete_offline_registry,
    registry_name,
    validate_registry_state_files,
)


def _validate_runtime_inventory(root: Path) -> None:
    runtime = root / ".runtime"
    if not runtime.exists():
        return
    registry_record = runtime / "management" / "offline-registry.json"
    registry_data = runtime / "management" / "offline-registry-data"
    registry_record_present = os.path.lexists(registry_record)
    registry_data_present = os.path.lexists(registry_data)
    if registry_record_present != registry_data_present:
        raise RuntimeError("offline registry runtime state is partial")
    if registry_record_present:
        validate_registry_state_files(root)
    allowed_files = {
        "host/inotify.json",
        "host/.lock",
        "management/identity.json",
        "management/network.json",
        "management/kubeconfig",
        "management/offline-registry.json",
        "retained-management.json",
        "rendered/cert-manager.yaml",
        "rendered/kind.yaml",
        "rendered/kamaji.yaml",
        "rendered/metallb-pool.yaml",
        "rendered/metallb.yaml",
        "rendered/providers/capi-bootstrap-components.yaml",
        "rendered/providers/capi-core-components.yaml",
        "rendered/providers/capd-components.yaml",
        "rendered/providers/kamaji-capi-components.yaml",
    }
    allowed_dynamic = (
        re.compile(
            r"^rendered/tenants/(capi-worker-spike|tenant-a|tenant-b)/"
            r"(control-plane|workers|worker-templates|worker-deployment|"
            r"invalid-control-plane|invalid-worker)\.yaml$"
        ),
        re.compile(r"^tenants/(capi-worker-spike|tenant-a|tenant-b)/kubeconfig$"),
        re.compile(
            r"^storage/(capi-worker-spike|tenant-a|tenant-b)/"
            r"volume\.json$"
        ),
        re.compile(r"^evidence/endpoint-failure\.txt$"),
        re.compile(r"^evidence/endpoint-success\.json$"),
        re.compile(
            r"^evidence/negative-condition-[a-z0-9.-]+-[a-z0-9.-]+\.json$"
        ),
        re.compile(r"^evidence/cnpg-(success\.json|failure\.txt)$"),
        re.compile(r"^evidence/(create|verify)-(success\.json|failure\.txt)$"),
        re.compile(r"^evidence/preload-(capi-worker-spike|tenant-a|tenant-b)\.json$"),
        re.compile(
            r"^evidence/break-glass-[a-z0-9.-]+-[a-z0-9.-]+-[a-z0-9.-]+"
            r"\.json$"
        ),
        re.compile(r"^deletions/(tenant-a|tenant-b)\.json$"),
        re.compile(r"^rendered/negative/foreign-node\.json$"),
        re.compile(
            r"^rendered/addons/(capi-worker-spike|tenant-a|tenant-b)/"
            r"(calico|kube-proxy)\.yaml$"
        ),
        re.compile(
            r"^rendered/addons/(capi-worker-spike|tenant-a|tenant-b)/"
            r"(resource-set|inventory|repair-[a-z0-9-]+)\.json$"
        ),
        re.compile(
            r"^rendered/storage/(capi-worker-spike|tenant-a|tenant-b)/"
            r"smoke\.yaml$"
        ),
        re.compile(
            r"^rendered/cnpg/(capi-worker-spike|tenant-a|tenant-b)/"
            r"((operator|cluster|static-pvs)\.yaml|cross-db-[a-z0-9-]+\.json)$"
        ),
        re.compile(r"^rendered/registry-hosts-[a-z0-9.-]+\.toml$"),
        re.compile(
            r"^tenants/cross-(tenant-a-to-tenant-b|tenant-b-to-tenant-a)"
            r"\.kubeconfig$"
        ),
        re.compile(r"^tenants/cross-(tenant-a|tenant-b)-postgres\.env$"),
    )
    for path in runtime.rglob("*"):
        relative = path.relative_to(runtime).as_posix()
        if relative == "management/offline-registry-data" or relative.startswith(
            "management/offline-registry-data/"
        ):
            continue
        details = path.lstat()
        if stat.S_ISLNK(details.st_mode):
            raise RuntimeError(f"runtime path is a symlink: {relative}")
        if path.is_dir():
            if details.st_uid != os.getuid() or details.st_mode & 0o077:
                raise RuntimeError(f"runtime directory is not private: {relative}")
            continue
        if relative not in allowed_files and not any(
            pattern.fullmatch(relative) for pattern in allowed_dynamic
        ):
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


def inspect_host_residue(config: dict[str, str]) -> dict[str, list[str]]:
    clusters = [config["SPIKE_NAME"], *config["TENANT_NAMES"].split()]
    containers = []
    for name in clusters:
        containers.extend(
            run(
                [
                    "docker",
                    "ps",
                    "-aq",
                    "--filter",
                    f"label=io.x-k8s.kind.cluster={name}",
                ],
                timeout=30,
            ).stdout.split()
        )
    probes = run(
        [
            "docker",
            "ps",
            "-aq",
            "--filter",
            f"label={config['OWNERSHIP_LABEL']}=true",
            "--filter",
            "label=cnpg-vcluster.capi/role=probe",
        ],
        timeout=30,
    ).stdout.split()
    registries = run(
        [
            "docker",
            "ps",
            "-aq",
            "--filter",
            f"name=^{registry_name(config)}$",
        ],
        timeout=30,
    ).stdout.split()
    volumes = []
    for name in clusters:
        volume_name = f"{config['LAB_PREFIX']}-{name}-storage"
        response = run(
            ["docker", "volume", "inspect", volume_name],
            timeout=30,
            check=False,
        )
        if response.returncode == 0:
            volumes.append(volume_name)
        elif "no such volume" not in response.stderr.lower():
            raise RuntimeError(
                f"Docker volume inspection failed for {volume_name}: "
                f"{response.stderr}"
            )
    return {
        "containers": sorted(set(containers)),
        "probes": sorted(set(probes)),
        "registries": sorted(set(registries)),
        "volumes": sorted(volumes),
    }


def destroy(root: Path, config: dict[str, str]) -> None:
    _validate_runtime_inventory(root)
    validate_inotify_state(root, config)
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
        client = ManagementClient(root, config)
        cluster_crd = client.kubectl(
            "get",
            "crd/clusters.cluster.x-k8s.io",
            check=False,
        )
        if cluster_crd.returncode == 0:
            tenants = [spike_tenant(root, config), *configured_tenants(root, config)]
        elif re.search(
            r"Error from server \(NotFound\):",
            cluster_crd.stderr,
            re.IGNORECASE,
        ):
            tenants = []
        else:
            raise RuntimeError(
                f"CAPI Cluster CRD inspection failed during cleanup: "
                f"{cluster_crd.stderr}"
            )
        configured_names = set(config["TENANT_NAMES"].split())
        for tenant in tenants:
            deletion_journal = (
                root / ".runtime" / "deletions" / f"{tenant.name}.json"
            )
            cluster = inspect_management_resource(
                client, tenant, f"cluster/{tenant.name}"
            )
            if tenant.name in configured_names and cluster is not None:
                from scripts.destroy_tenant import (
                    finish_prepared_tenant_deletion,
                    prepare_tenant_deletion,
                )
                from scripts.lib.tenants import ensure_tenant_kubeconfig

                owned = verify_tenant_management_ownership(config, client, tenant)
                kubeconfig_path = (
                    root / ".runtime" / "tenants" / tenant.name / "kubeconfig"
                )
                kcp = owned.get("kamajicontrolplane", {})
                initialized = (
                    kcp.get("status", {})
                    .get("initialization", {})
                    .get("controlPlaneInitialized")
                    is True
                )
                if not kubeconfig_path.is_file() and not initialized:
                    delete_tenant(root, config, client, tenant)
                    continue
                ensure_tenant_kubeconfig(root, config, client, tenant)
                prepare_tenant_deletion(
                    root, config, client, tenant, cluster
                )
                finish_prepared_tenant_deletion(
                    root, config, client, tenant
                )
                continue
            if cluster is not None and deletion_journal.is_file():
                from scripts.destroy_tenant import validate_deletion_journal

                validate_deletion_journal(
                    root, tenant, str(cluster["metadata"]["uid"])
                )
            if cluster is None and deletion_journal.is_file():
                from scripts.destroy_tenant import finish_journaled_tenant_deletion

                finish_journaled_tenant_deletion(
                    root, config, client, tenant
                )
                continue
            if not (
                root / ".runtime" / "tenants" / tenant.name / "kubeconfig"
            ).is_file():
                delete_tenant(root, config, client, tenant)
                continue
            from scripts.lib.tenants import _tenant_kubectl
            from scripts.cnpg import cnpg_artifacts_present, delete_cnpg
            from scripts.storage import _delete_storage

            if cnpg_artifacts_present(root, config, tenant):
                delete_cnpg(root, config, tenant)
            storage_present = False
            for resource in (
                "pvc/storage-smoke",
                f"pv/{tenant.name}-storage-smoke",
                f"storageclass/{config['SPIKE_STORAGE_CLASS']}",
            ):
                response = _tenant_kubectl(
                    root,
                    config,
                    tenant,
                    "get",
                    resource,
                    check=False,
                )
                if response.returncode == 0:
                    storage_present = True
                elif not re.search(
                    r"Error from server \(NotFound\):",
                    response.stderr,
                    re.IGNORECASE,
                ):
                    raise RuntimeError(
                        f"storage cleanup inspection failed for {resource}: "
                        f"{response.stderr}"
                    )
            if storage_present:
                _delete_storage(root, config, tenant)
            delete_addons(root, config, client, tenant)
            delete_tenant(root, config, client, tenant)
        delete_offline_registry(root, config)
        _delete_kubernetes_stack(root, config, client)
        delete_management(root, config)
    else:
        delete_offline_registry(root, config)
        residue = inspect_host_residue(config)
        if any(residue.values()):
            raise RuntimeError(
                "management state is absent while provider-owned host residue "
                f"remains; preserving runtime and host settings: {residue}"
            )
    restore_inotify(root, config)
    runtime = root / ".runtime"
    if runtime.exists():
        shutil.rmtree(runtime)
    print("management experiment resources removed")
