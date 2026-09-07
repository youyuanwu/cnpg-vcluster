from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import socket
import subprocess
import time
from pathlib import Path

from scripts.cnpg import (
    _cnpg_ready,
    _primary_failover,
    _replica_restart,
    _replace_machine,
    _storage_identity,
    _verify_filesystem,
    _verify_marker,
)
from scripts.create import create, stable_tenant_snapshot
from scripts.endpoint import _verify_bootstrap_secret
from scripts.lib.addons import verify_network
from scripts.lib.conditions import condition_true
from scripts.lib.files import write_private_file
from scripts.lib.kube import ManagementClient
from scripts.lib.process import run
from scripts.lib.redaction import redact
from scripts.lib.tenants import (
    _tenant_kubectl,
    configured_tenants,
    inspect_storage_volume,
    storage_volume_name,
    tenant_kubeconfig_path,
    write_storage_marker,
)
from scripts.machines import worker_snapshot


def _private_file(path: Path) -> None:
    details = path.lstat()
    if path.is_symlink() or not path.is_file() or details.st_uid != os.getuid():
        raise RuntimeError(f"credential file ownership is invalid: {path}")
    if details.st_mode & 0o077:
        raise RuntimeError(f"credential file is not owner-only: {path}")


def _ca_fingerprint(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    match = re.search(r"certificate-authority-data:\s*(\S+)", text)
    if not match:
        raise RuntimeError(f"kubeconfig has no embedded CA: {path}")
    return hashlib.sha256(base64.b64decode(match.group(1))).hexdigest()


def _cluster_identity(root: Path, config: dict[str, str], client, tenant):
    cluster = json.loads(
        client.kubectl(
            "-n",
            tenant.namespace,
            "get",
            f"cluster/{tenant.name}",
            "-o",
            "json",
        ).stdout
    )
    kcp = json.loads(
        client.kubectl(
            "-n",
            tenant.namespace,
            "get",
            f"kamajicontrolplane/{tenant.name}",
            "-o",
            "json",
        ).stdout
    )
    endpoint = cluster["spec"]["controlPlaneEndpoint"]
    network = cluster["spec"]["clusterNetwork"]
    if (
        endpoint != {"host": tenant.vip, "port": int(config["SPIKE_API_PORT"])}
        or network["pods"]["cidrBlocks"] != [tenant.pod_cidr]
        or network["services"]["cidrBlocks"] != [tenant.service_cidr]
        or network["serviceDomain"] != tenant.domain
        or not condition_true(cluster, "Available")
        or not condition_true(kcp, "Available")
        or kcp.get("status", {})
        .get("initialization", {})
        .get("controlPlaneInitialized")
        is not True
        or "cluster.x-k8s.io/paused" in (kcp["metadata"].get("annotations") or {})
    ):
        raise RuntimeError(f"tenant control-plane identity drift: {tenant.name}")
    path = tenant_kubeconfig_path(root, tenant)
    _private_file(path)
    if f"https://{tenant.vip}:{config['SPIKE_API_PORT']}" not in path.read_text(
        encoding="utf-8"
    ):
        raise RuntimeError(f"tenant kubeconfig endpoint drift: {tenant.name}")
    return {
        "endpoint": f"{tenant.vip}:{config['SPIKE_API_PORT']}",
        "ca": _ca_fingerprint(path),
        "podCIDR": tenant.pod_cidr,
        "serviceCIDR": tenant.service_cidr,
        "domain": tenant.domain,
        "database": tenant.cnpg_cluster,
    }


def _bootstrap_credentials(root: Path, config: dict[str, str], client, tenant) -> None:
    machines = json.loads(
        client.kubectl(
            "-n",
            tenant.namespace,
            "get",
            "machines",
            "-l",
            f"cluster.x-k8s.io/cluster-name={tenant.name}",
            "-o",
            "json",
        ).stdout
    )["items"]
    if len(machines) != tenant.workers:
        raise RuntimeError(f"unexpected Machine count: {tenant.name}")
    for machine in machines:
        name = machine["metadata"]["name"]
        kubeadm = json.loads(
            client.kubectl(
                "-n",
                tenant.namespace,
                "get",
                f"kubeadmconfig/{name}",
                "-o",
                "json",
            ).stdout
        )
        secret_name = kubeadm.get("status", {}).get("dataSecretName")
        if secret_name != name:
            raise RuntimeError(f"bootstrap Secret identity drift: {name}")
        _verify_bootstrap_secret(
            config,
            client,
            tenant,
            {"secret": secret_name, "machine": machine},
        )
        secret = json.loads(
            client.kubectl(
                "-n",
                tenant.namespace,
                "get",
                f"secret/{secret_name}",
                "-o",
                "json",
            ).stdout
        )
        bootstrap = base64.b64decode(secret["data"]["value"]).decode(
            "utf-8", "replace"
        )
        if f"{tenant.vip}:{config['SPIKE_API_PORT']}" not in bootstrap:
            raise RuntimeError(f"bootstrap endpoint drift: {name}")


def _storage_isolation(root: Path, config: dict[str, str], tenants, workers) -> None:
    volumes = {}
    for tenant in tenants:
        payload = inspect_storage_volume(storage_volume_name(config, tenant))
        if payload is None:
            raise RuntimeError(f"tenant storage volume is missing: {tenant.name}")
        volumes[tenant.name] = {
            "name": payload["Name"],
            "mountpoint": payload["Mountpoint"],
        }
        write_storage_marker(
            config,
            tenant,
            f"isolation/{tenant.name}",
            f"{tenant.name}\n",
        )
    if (
        len({item["name"] for item in volumes.values()}) != len(tenants)
        or len({item["mountpoint"] for item in volumes.values()}) != len(tenants)
    ):
        raise RuntimeError("tenant storage volume identities overlap")
    for tenant in tenants:
        other = next(item for item in tenants if item.name != tenant.name)
        for worker in workers[tenant.name]:
            result = run(
                [
                    "docker",
                    "exec",
                    worker,
                    "sh",
                    "-ec",
                    f"test \"$(cat '{config['SPIKE_STORAGE_CONTAINER_PATH']}/"
                    f"isolation/{tenant.name}')\" = '{tenant.name}'; "
                    f"test ! -e '{config['SPIKE_STORAGE_CONTAINER_PATH']}/"
                    f"isolation/{other.name}'",
                ],
                timeout=30,
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError(f"cross-tenant storage isolation failed: {worker}")


def _reject_kubernetes_credential(
    root: Path,
    config: dict[str, str],
    source,
    target,
) -> None:
    reachable = _tenant_kubectl(
        root,
        config,
        target,
        "get",
        "nodes",
        check=False,
    )
    if reachable.returncode != 0:
        raise RuntimeError(f"target Kubernetes API is not reachable: {target.name}")
    source_path = tenant_kubeconfig_path(root, source)
    content = source_path.read_text(encoding="utf-8").replace(
        f"https://{source.vip}:{config['SPIKE_API_PORT']}",
        f"https://{target.vip}:{config['SPIKE_API_PORT']}",
    )
    path = (
        root
        / ".runtime"
        / "tenants"
        / f"cross-{source.name}-to-{target.name}.kubeconfig"
    )
    write_private_file(path, content)
    try:
        _private_file(path)
        result = run(
            [
                str(root / ".tools" / "bin" / "kubectl"),
                "--kubeconfig",
                str(path),
                "--request-timeout",
                config["KUBECTL_REQUEST_TIMEOUT"],
                "--insecure-skip-tls-verify=true",
                "get",
                "nodes",
            ],
            timeout=60,
            check=False,
        )
        if result.returncode == 0:
            raise RuntimeError(
                f"{source.name} Kubernetes credential accessed {target.name}"
            )
        rejection = result.stdout + result.stderr
        unauthorized = re.search(r"\bunauthorized\b", rejection, re.IGNORECASE)
        anonymous_forbidden = (
            re.search(r"\bforbidden\b", rejection, re.IGNORECASE)
            and 'User "system:anonymous"' in rejection
        )
        if not unauthorized and not anonymous_forbidden:
            raise RuntimeError(
                f"cross-tenant Kubernetes rejection was inconclusive: {target.name}"
            )
    finally:
        path.unlink(missing_ok=True)


def _database_password(root: Path, config: dict[str, str], tenant) -> str:
    secret = json.loads(
        _tenant_kubectl(
            root,
            config,
            tenant,
            "-n",
            config["DATABASE_NAMESPACE"],
            "get",
            f"secret/{tenant.cnpg_cluster}-app",
            "-o",
            "json",
        ).stdout
    )
    return base64.b64decode(secret["data"]["password"]).decode("utf-8")


def _reject_postgres_credential(
    root: Path,
    config: dict[str, str],
    source,
    target,
    password: str,
) -> None:
    if "\n" in password or "\r" in password:
        raise RuntimeError("PostgreSQL credential contains an invalid env-file newline")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    path = root / ".runtime" / "tenants" / f"cross-{source.name}-postgres.env"
    write_private_file(path, f"PGPASSWORD={password}\n")
    command = [
        str(root / ".tools" / "bin" / "kubectl"),
        "--kubeconfig",
        str(tenant_kubeconfig_path(root, target)),
        "--request-timeout",
        config["KUBECTL_REQUEST_TIMEOUT"],
        "-n",
        config["DATABASE_NAMESPACE"],
        "port-forward",
        f"service/{target.cnpg_cluster}-rw",
        f"{port}:5432",
        "--address=127.0.0.1",
    ]
    forward = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        _private_file(path)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if forward.poll() is not None:
                raise RuntimeError(
                    f"PostgreSQL port-forward exited early: {target.name}"
                )
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    break
            except OSError:
                time.sleep(0.25)
        else:
            raise RuntimeError(f"PostgreSQL port-forward was not reachable: {target.name}")
        result = run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "host",
                "--env-file",
                str(path),
                config["POSTGRES_IMAGE"],
                "psql",
                "-X",
                "-qAt",
                "-v",
                "ON_ERROR_STOP=1",
                "-h",
                "127.0.0.1",
                "-p",
                str(port),
                "-U",
                "app",
                "-d",
                "app",
                "-c",
                "SELECT 1;",
            ],
            timeout=60,
            check=False,
        )
        if result.returncode == 0:
            raise RuntimeError(
                f"{source.name} PostgreSQL credential accessed {target.name}"
            )
        if "password authentication failed" not in (
            result.stdout + result.stderr
        ).lower():
            raise RuntimeError(
                f"cross-tenant PostgreSQL rejection was inconclusive: {target.name}"
            )
    finally:
        if forward.poll() is None:
            forward.terminate()
            try:
                forward.wait(timeout=10)
            except subprocess.TimeoutExpired:
                forward.kill()
                forward.wait(timeout=10)
        path.unlink(missing_ok=True)


def _management_absence(config: dict[str, str], client, tenants, workers) -> None:
    nodes = json.loads(client.kubectl("get", "nodes", "-o", "json").stdout)["items"]
    tenant_nodes = set().union(*(set(items) for items in workers.values()))
    if tenant_nodes & {item["metadata"]["name"] for item in nodes}:
        raise RuntimeError("tenant Nodes appeared in the management API")
    for resource in (
        f"namespace/{config['DATABASE_NAMESPACE']}",
        "crd/clusters.postgresql.cnpg.io",
        *(
            f"pv/{tenant.cnpg_cluster}-pv-{ordinal}"
            for tenant in tenants
            for ordinal in (1, 2, 3)
        ),
    ):
        if client.kubectl("get", resource, check=False).returncode == 0:
            raise RuntimeError(f"tenant database resource appeared in management: {resource}")


def verify(root: Path, config: dict[str, str]) -> dict[str, object]:
    failure = root / ".runtime" / "evidence" / "verify-failure.txt"
    success = root / ".runtime" / "evidence" / "verify-success.json"
    failure.unlink(missing_ok=True)
    success.unlink(missing_ok=True)
    try:
        create(root, config)
        client = ManagementClient(root, config)
        tenants = configured_tenants(root, config)
        identities = {}
        workers = {}
        storage = {}
        passwords = {}
        for tenant in tenants:
            identities[tenant.name] = _cluster_identity(
                root, config, client, tenant
            )
            verify_network(root, config, tenant)
            workers[tenant.name] = worker_snapshot(
                root, config, client, tenant
            )
            _bootstrap_credentials(root, config, client, tenant)
            if not _cnpg_ready(root, config, tenant):
                raise RuntimeError(f"CNPG is not ready: {tenant.name}")
            _verify_marker(root, config, tenant)
            _verify_filesystem(config, tenant)
            storage[tenant.name] = _storage_identity(root, config, tenant)
            passwords[tenant.name] = _database_password(root, config, tenant)
        for key in (
            "endpoint",
            "ca",
            "podCIDR",
            "serviceCIDR",
            "domain",
            "database",
        ):
            values = [identity[key] for identity in identities.values()]
            if len(values) != len(set(values)):
                raise RuntimeError(f"tenant identity overlap: {key}")
        if set(workers[tenants[0].name]) & set(workers[tenants[1].name]):
            raise RuntimeError("tenant worker sets overlap")
        if len(set(passwords.values())) != len(passwords):
            raise RuntimeError("tenant PostgreSQL credentials are identical")
        _storage_isolation(root, config, tenants, workers)
        _management_absence(config, client, tenants, workers)
        for source, target in ((tenants[0], tenants[1]), (tenants[1], tenants[0])):
            _reject_kubernetes_credential(root, config, source, target)
            _reject_postgres_credential(
                root, config, source, target, passwords[source.name]
            )
        before_reconcile = {
            tenant.name: stable_tenant_snapshot(root, config, client, tenant)
            for tenant in tenants
        }
        create(root, config)
        after_reconcile = {
            tenant.name: stable_tenant_snapshot(root, config, client, tenant)
            for tenant in tenants
        }
        if before_reconcile != after_reconcile:
            raise RuntimeError("repeated create replaced healthy tenant workers")
        for tenant in tenants:
            before_storage = _storage_identity(root, config, tenant)
            _replace_machine(root, config, client, tenant)
            if _storage_identity(root, config, tenant) != before_storage:
                raise RuntimeError(
                    f"CNPG storage changed across Machine replacement: {tenant.name}"
                )
            _verify_marker(root, config, tenant)
            _replica_restart(root, config, tenant)
            _verify_marker(root, config, tenant)
            _primary_failover(root, config, tenant)
            _verify_marker(root, config, tenant)
        final_snapshots = {
            tenant.name: stable_tenant_snapshot(root, config, client, tenant)
            for tenant in tenants
        }
        if any(snapshot is None for snapshot in final_snapshots.values()):
            raise RuntimeError("final tenant identity snapshot is incomplete")
        evidence = {
            "tenantCompatibilityRevision": config[
                "TENANT_COMPATIBILITY_REVISION"
            ],
            "identities": identities,
            "tenants": final_snapshots,
            "storage": storage,
        }
        write_private_file(success, json.dumps(evidence, sort_keys=True) + "\n")
        print("two-tenant isolation checks passed")
        return evidence
    except Exception as exc:
        success.unlink(missing_ok=True)
        write_private_file(failure, redact(str(exc)) + "\n")
        raise
