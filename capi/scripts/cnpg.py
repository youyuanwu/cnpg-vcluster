from __future__ import annotations

import base64
import json
import re
import time
from pathlib import Path

from scripts.lib.files import IntegrityError, verify_sha256, write_private_file
from scripts.lib.kube import wait_for
from scripts.lib.process import run
from scripts.lib.redaction import redact
from scripts.lib.tenants import NOT_FOUND, _tenant_kubectl
from scripts.lib.tenants import storage_volume_name
from scripts.lib.controller_scenarios import (
    delete_controller_tenant,
    wait_tenant_ready,
)
from scripts.storage import _delete_storage, run_storage_gate
from scripts.lib.config import parse_duration


class SQLProbeCleanupError(RuntimeError):
    pass


def _database_count(tenant) -> int:
    return int(getattr(tenant, "database_count", 3))


def _anti_affinity_type(tenant) -> str:
    workers = int(getattr(tenant, "workers", 3))
    return "required" if _database_count(tenant) <= workers else "preferred"


def _static_pv_items(config: dict[str, str], tenant) -> str:
    items = []
    for ordinal in range(1, _database_count(tenant) + 1):
        items.append(
            f"""\
  - apiVersion: v1
    kind: PersistentVolume
    metadata:
      name: {tenant.cnpg_cluster}-pv-{ordinal}
    spec:
      capacity:
        storage: 1Gi
      accessModes: [ReadWriteOnce]
      persistentVolumeReclaimPolicy: Retain
      storageClassName: {config["SPIKE_STORAGE_CLASS"]}
      claimRef:
        namespace: database
        name: {tenant.cnpg_cluster}-{ordinal}
      hostPath:
        path: {config["SPIKE_STORAGE_CONTAINER_PATH"]}/volumes/cnpg/{ordinal}
        type: DirectoryOrCreate"""
        )
    return "\n".join(items)


def _cnpg_ready(root: Path, config: dict[str, str], tenant) -> bool:
    operator = _tenant_kubectl(
        root,
        config,
        tenant,
        "-n",
        config["CNPG_NAMESPACE"],
        "get",
        "deployment/cnpg-controller-manager",
        "-o",
        "json",
        check=False,
    )
    if operator.returncode != 0:
        return False
    operator_payload = json.loads(operator.stdout)
    if (
        operator_payload["status"].get("availableReplicas", 0)
        != operator_payload["spec"].get("replicas", 0)
        or operator_payload["spec"]["template"]["spec"]["containers"][0]["image"]
        != config["CNPG_CONTROLLER_IMAGE"]
    ):
        return False
    response = _tenant_kubectl(
        root,
        config,
        tenant,
        "-n",
        config["DATABASE_NAMESPACE"],
        "get",
        f"cluster/{tenant.cnpg_cluster}",
        "-o",
        "json",
        check=False,
    )
    if response.returncode != 0:
        return False
    cluster = json.loads(response.stdout)
    pods = json.loads(
        _tenant_kubectl(
            root,
            config,
            tenant,
            "-n",
            config["DATABASE_NAMESPACE"],
            "get",
            "pods",
            "-l",
            f"cnpg.io/cluster={tenant.cnpg_cluster}",
            "-o",
            "json",
        ).stdout
    )["items"]
    ready_pods = [
        pod
        for pod in pods
        if any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in pod.get("status", {}).get("conditions", [])
        )
    ]
    pvcs = json.loads(
        _tenant_kubectl(
            root,
            config,
            tenant,
            "-n",
            config["DATABASE_NAMESPACE"],
            "get",
            "pvc",
            "-l",
            f"cnpg.io/cluster={tenant.cnpg_cluster}",
            "-o",
            "json",
        ).stdout
    )["items"]
    expected = _database_count(tenant)
    if len(pvcs) != expected or any(
        pvc["status"].get("phase") != "Bound" for pvc in pvcs
    ):
        return False
    for pvc in pvcs:
        pv = json.loads(
            _tenant_kubectl(
                root,
                config,
                tenant,
                "get",
                f"pv/{pvc['spec']['volumeName']}",
                "-o",
                "json",
            ).stdout
        )
        if pv["status"].get("phase") != "Bound" or "nodeAffinity" in pv["spec"]:
            return False
    endpoint = _tenant_kubectl(
        root,
        config,
        tenant,
        "-n",
        config["DATABASE_NAMESPACE"],
        "get",
        f"endpoints/{tenant.cnpg_cluster}-rw",
        "-o",
        "jsonpath={.subsets[0].addresses[0].ip}",
        check=False,
    )
    return (
        cluster.get("status", {}).get("phase") == "Cluster in healthy state"
        and cluster.get("status", {}).get("readyInstances") == expected
        and len(ready_pods) == expected
        and all(pod["spec"].get("nodeName") for pod in ready_pods)
        and all(
            next(
                container
                for container in pod["spec"]["containers"]
                if container["name"] == "postgres"
            )["image"]
            == config["POSTGRES_IMAGE"]
            for pod in ready_pods
        )
        and endpoint.returncode == 0
        and bool(endpoint.stdout)
    )


def _sql(root: Path, config: dict[str, str], tenant, sql: str) -> str:
    name = f"cnpg-sql-{time.time_ns()}"
    manifest = root / ".runtime" / "rendered" / "cnpg" / tenant.name / "sql.json"
    write_private_file(
        manifest,
        json.dumps(
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {"name": name, "namespace": config["DATABASE_NAMESPACE"]},
                "spec": {
                    "restartPolicy": "Never",
                    "automountServiceAccountToken": False,
                    "containers": [
                        {
                            "name": "psql",
                            "image": config["POSTGRES_IMAGE"],
                            "command": ["sleep", "300"],
                            "env": [
                                {
                                    "name": "PGPASSWORD",
                                    "valueFrom": {
                                        "secretKeyRef": {
                                            "name": f"{tenant.cnpg_cluster}-app",
                                            "key": "password",
                                        }
                                    },
                                }
                            ],
                        }
                    ],
                },
            },
            sort_keys=True,
        )
        + "\n",
    )
    try:
        _tenant_kubectl(root, config, tenant, "apply", "-f", str(manifest))
        _tenant_kubectl(
            root,
            config,
            tenant,
            "-n",
            config["DATABASE_NAMESPACE"],
            "wait",
            "--for=condition=Ready",
            f"pod/{name}",
            f"--timeout={config['SQL_TIMEOUT']}",
        )
        return _tenant_kubectl(
            root,
            config,
            tenant,
            "-n",
            config["DATABASE_NAMESPACE"],
            "exec",
            f"pod/{name}",
            "--",
            "env",
            "PGCONNECT_TIMEOUT=10",
            "psql",
            "-X",
            "-qAt",
            "-v",
            "ON_ERROR_STOP=1",
            "-h",
            f"{tenant.cnpg_cluster}-rw",
            "-U",
            "app",
            "-d",
            "app",
            "-c",
            sql,
        ).stdout.strip()
    finally:
        _tenant_kubectl(
            root,
            config,
            tenant,
            "-n",
            config["DATABASE_NAMESPACE"],
            "delete",
            f"pod/{name}",
            "--ignore-not-found",
            "--wait=true",
            check=False,
        )
        manifest.unlink(missing_ok=True)
        remaining = _tenant_kubectl(
            root,
            config,
            tenant,
            "-n",
            config["DATABASE_NAMESPACE"],
            "get",
            f"pod/{name}",
            check=False,
        )
        if remaining.returncode == 0 or not NOT_FOUND.search(remaining.stderr):
            raise SQLProbeCleanupError(
                f"SQL verification pod cleanup failed: {name}"
            )


def _write_marker(root: Path, config: dict[str, str], tenant) -> None:
    result = _sql(
        root,
        config,
        tenant,
        "CREATE TABLE IF NOT EXISTS verification(marker text PRIMARY KEY);"
        "INSERT INTO verification VALUES ('capi-marker') ON CONFLICT DO NOTHING;"
        "SELECT marker FROM verification;",
    )
    if not result.splitlines() or result.splitlines()[-1] != "capi-marker":
        raise RuntimeError("CNPG marker is not readable")


def _verify_marker(root: Path, config: dict[str, str], tenant) -> None:
    result = _sql(
        root,
        config,
        tenant,
        "SELECT marker FROM verification WHERE marker='capi-marker';",
    )
    if not result.splitlines() or result.splitlines()[-1] != "capi-marker":
        raise RuntimeError("CNPG marker was not retained")


def verify_retained_marker(root: Path, config: dict[str, str], tenant) -> None:
    cluster = json.loads(
        _tenant_kubectl(
            root,
            config,
            tenant,
            "-n",
            config["DATABASE_NAMESPACE"],
            "get",
            f"cluster/{tenant.cnpg_cluster}",
            "-o",
            "json",
        ).stdout
    )
    primary = cluster.get("status", {}).get("currentPrimary")
    if not primary:
        raise RuntimeError("CNPG primary identity is absent")
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
    encoded_password = secret.get("data", {}).get("password")
    if not encoded_password:
        raise RuntimeError("CNPG application credential is absent")
    try:
        password = base64.b64decode(encoded_password, validate=True).decode()
    except (ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError("CNPG application credential is invalid") from exc
    result = _tenant_kubectl(
        root,
        config,
        tenant,
        "-n",
        config["DATABASE_NAMESPACE"],
        "exec",
        "-i",
        f"pod/{primary}",
        "--",
        "sh",
        "-ec",
        "IFS= read -r PGPASSWORD; export PGPASSWORD; "
        "exec psql -X -qAt -v ON_ERROR_STOP=1 "
        f"-h {tenant.cnpg_cluster}-rw -U app -d app "
        "-c \"SELECT marker FROM verification "
        "WHERE marker='capi-marker';\"",
        input_text=password + "\n",
    ).stdout.strip()
    if not result.splitlines() or result.splitlines()[-1] != "capi-marker":
        raise RuntimeError("CNPG marker was not retained")


def _verify_filesystem(config: dict[str, str], tenant) -> None:
    script = f"""\
set -eu
for ordinal in $(seq 1 {_database_count(tenant)}); do
  path=/data/volumes/cnpg/$ordinal/pgdata
  test -d "$path"
  echo "instance=$ordinal directory=$(stat -c %u:%g:%a "$path")"
  test "$(stat -c %u:%g "$path")" = 26:26
  mode=$(stat -c %a "$path")
  test "$mode" = 700
  file="$path/global/pg_control"
  test -f "$file"
  echo "instance=$ordinal file=$file metadata=$(stat -c %u:%g:%a "$file")"
  test "$(stat -c %u:%g "$file")" = 26:26
  test "$(stat -c %a "$file")" = 600
done
"""
    run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{storage_volume_name(config, tenant)}:/data:ro",
            "--entrypoint",
            "sh",
            config["VERIFY_IMAGE"],
            "-ec",
            script,
        ],
        timeout=60,
    )


def _storage_identity(root: Path, config: dict[str, str], tenant) -> dict[str, str]:
    pvcs = json.loads(
        _tenant_kubectl(
            root,
            config,
            tenant,
            "-n",
            config["DATABASE_NAMESPACE"],
            "get",
            "pvc",
            "-l",
            f"cnpg.io/cluster={tenant.cnpg_cluster}",
            "-o",
            "json",
        ).stdout
    )["items"]
    if len(pvcs) != _database_count(tenant):
        raise RuntimeError("CNPG PVC count does not match tenant specification")
    return {
        pvc["metadata"]["name"]: f"{pvc['metadata']['uid']}:{pvc['spec']['volumeName']}"
        for pvc in pvcs
    }


def _replace_machine(root: Path, config: dict[str, str], client, tenant) -> None:
    pods = json.loads(
        _tenant_kubectl(
            root,
            config,
            tenant,
            "-n",
            config["DATABASE_NAMESPACE"],
            "get",
            "pods",
            "-l",
            f"cnpg.io/cluster={tenant.cnpg_cluster}",
            "-o",
            "json",
        ).stdout
    )["items"]
    node = pods[0]["spec"]["nodeName"]
    client.kubectl(
        "-n",
        tenant.namespace,
        "delete",
        f"machine/{node}",
        "--wait=true",
        f"--timeout={config['DELETE_TIMEOUT']}",
    )
    wait_for(
        "CNPG after Machine replacement",
        parse_duration(config["CNPG_TIMEOUT"]),
        5,
        lambda: _cnpg_ready(root, config, tenant),
    )


def _replica_restart(root: Path, config: dict[str, str], tenant) -> None:
    cluster = tenant.cnpg_cluster
    primary = _tenant_kubectl(
        root, config, tenant, "-n", config["DATABASE_NAMESPACE"], "get", f"cluster/{cluster}",
        "-o", "jsonpath={.status.currentPrimary}"
    ).stdout
    pods = json.loads(
        _tenant_kubectl(
            root, config, tenant, "-n", config["DATABASE_NAMESPACE"], "get", "pods",
            "-l", f"cnpg.io/cluster={cluster}", "-o", "json"
        ).stdout
    )["items"]
    replica = next(pod for pod in pods if pod["metadata"]["name"] != primary)
    name = replica["metadata"]["name"]
    uid = replica["metadata"]["uid"]
    pvc = next(
        volume["persistentVolumeClaim"]["claimName"]
        for volume in replica["spec"]["volumes"]
        if "persistentVolumeClaim" in volume
    )
    pv = _tenant_kubectl(
        root, config, tenant, "-n", config["DATABASE_NAMESPACE"], "get", f"pvc/{pvc}",
        "-o", "jsonpath={.spec.volumeName}"
    ).stdout
    _tenant_kubectl(root, config, tenant, "-n", config["DATABASE_NAMESPACE"], "delete", f"pod/{name}", "--wait=false")
    wait_for(
        "CNPG replica replacement",
        parse_duration(config["CNPG_TIMEOUT"]),
        5,
        lambda: (
            True
            if (
                (response := _tenant_kubectl(
                    root, config, tenant, "-n", config["DATABASE_NAMESPACE"], "get", f"pod/{name}",
                    "-o", "json", check=False
                )).returncode == 0
                and json.loads(response.stdout)["metadata"]["uid"] != uid
                and any(
                    item.get("type") == "Ready" and item.get("status") == "True"
                    for item in json.loads(response.stdout)["status"].get("conditions", [])
                )
            )
            else None
        ),
    )
    new_pv = _tenant_kubectl(
        root, config, tenant, "-n", config["DATABASE_NAMESPACE"], "get", f"pvc/{pvc}",
        "-o", "jsonpath={.spec.volumeName}"
    ).stdout
    new_pod = json.loads(
        _tenant_kubectl(
            root,
            config,
            tenant,
            "-n",
            config["DATABASE_NAMESPACE"],
            "get",
            f"pod/{name}",
            "-o",
            "json",
        ).stdout
    )
    new_pvc = next(
        volume["persistentVolumeClaim"]["claimName"]
        for volume in new_pod["spec"]["volumes"]
        if "persistentVolumeClaim" in volume
    )
    if new_pvc != pvc:
        raise RuntimeError("replacement replica PVC identity changed")
    if new_pv != pv:
        raise RuntimeError("replica PV identity changed")


def _primary_failover(root: Path, config: dict[str, str], tenant) -> None:
    cluster = tenant.cnpg_cluster
    old = _tenant_kubectl(
        root, config, tenant, "-n", config["DATABASE_NAMESPACE"], "get", f"cluster/{cluster}",
        "-o", "jsonpath={.status.currentPrimary}"
    ).stdout
    _tenant_kubectl(root, config, tenant, "-n", config["DATABASE_NAMESPACE"], "delete", f"pod/{old}", "--wait=false")
    wait_for(
        "CNPG primary failover",
        parse_duration(config["CNPG_TIMEOUT"]),
        5,
        lambda: (
            current
            if (current := _tenant_kubectl(
                root, config, tenant, "-n", config["DATABASE_NAMESPACE"], "get", f"cluster/{cluster}",
                "-o", "jsonpath={.status.currentPrimary}", check=False
            ).stdout) and current != old
            else None
        ),
    )
    wait_for(
        "CNPG health after failover",
        parse_duration(config["CNPG_TIMEOUT"]),
        5,
        lambda: _cnpg_ready(root, config, tenant),
    )


def _evidence_payload(root: Path, config: dict[str, str], client, tenant) -> dict[str, object]:
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
    nodes = json.loads(
        _tenant_kubectl(root, config, tenant, "get", "nodes", "-o", "json").stdout
    )["items"]
    pods = json.loads(
        _tenant_kubectl(
            root,
            config,
            tenant,
            "-n",
            config["DATABASE_NAMESPACE"],
            "get",
            "pods",
            "-l",
            f"cnpg.io/cluster={tenant.cnpg_cluster}",
            "-o",
            "json",
        ).stdout
    )["items"]
    pvcs = json.loads(
        _tenant_kubectl(
            root,
            config,
            tenant,
            "-n",
            config["DATABASE_NAMESPACE"],
            "get",
            "pvc",
            "-l",
            f"cnpg.io/cluster={tenant.cnpg_cluster}",
            "-o",
            "json",
        ).stdout
    )["items"]
    pvs = []
    for pvc in pvcs:
        pvs.append(
            json.loads(
                _tenant_kubectl(
                    root,
                    config,
                    tenant,
                    "get",
                    f"pv/{pvc['spec']['volumeName']}",
                    "-o",
                    "json",
                ).stdout
            )
        )
    return {
        "cluster": tenant.cnpg_cluster,
        "operatorImage": config["CNPG_CONTROLLER_IMAGE"],
        "postgresImage": config["POSTGRES_IMAGE"],
        "revision": config["CNPG_COMPATIBILITY_REVISION"],
        "machines": {
            item["metadata"]["name"]: item["metadata"]["uid"] for item in machines
        },
        "nodes": {
            item["metadata"]["name"]: item["metadata"]["uid"] for item in nodes
        },
        "databasePods": {
            item["metadata"]["name"]: item["metadata"]["uid"] for item in pods
        },
        "storage": {
            pvc["metadata"]["name"]: {
                "pvcUID": pvc["metadata"]["uid"],
                "pv": pvc["spec"]["volumeName"],
                "pvUID": next(
                    pv["metadata"]["uid"]
                    for pv in pvs
                    if pv["metadata"]["name"] == pvc["spec"]["volumeName"]
                ),
            }
            for pvc in pvcs
        },
    }


def run_cnpg_gate(root: Path, config: dict[str, str]) -> None:
    failure = root / ".runtime" / "evidence" / "cnpg-failure.txt"
    success = root / ".runtime" / "evidence" / "cnpg-success.json"
    failure.unlink(missing_ok=True)
    success.unlink(missing_ok=True)
    client = None
    tenant = None
    evidence = None
    try:
        client, tenant, _ = run_storage_gate(root, config, cleanup=False)
        if not _cnpg_ready(root, config, tenant):
            raise RuntimeError("controller-created CNPG topology is not healthy")
        before = _storage_identity(root, config, tenant)
        _write_marker(root, config, tenant)
        _verify_filesystem(config, tenant)
        _replace_machine(root, config, client, tenant)
        wait_tenant_ready(root, config, tenant.name)
        if _storage_identity(root, config, tenant) != before:
            raise RuntimeError("CNPG storage identity changed across Machine replacement")
        _verify_marker(root, config, tenant)
        _replica_restart(root, config, tenant)
        _verify_marker(root, config, tenant)
        _primary_failover(root, config, tenant)
        _verify_marker(root, config, tenant)
        _verify_filesystem(config, tenant)
        evidence = _evidence_payload(root, config, client, tenant)
    except Exception as exc:
        write_private_file(failure, redact(str(exc)) + "\n")
        raise
    finally:
        if client is not None and tenant is not None:
            try:
                _delete_storage(root, config, tenant)
                delete_controller_tenant(root, config, tenant)
            except Exception as exc:
                success.unlink(missing_ok=True)
                write_private_file(failure, redact(str(exc)) + "\n")
                raise
    if evidence is None:
        raise RuntimeError("CNPG evidence was not produced")
    write_private_file(success, json.dumps(evidence, sort_keys=True) + "\n")
    print("CNPG persistence checks passed")
