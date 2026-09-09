from __future__ import annotations

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
from scripts.lib.addons import delete_addons
from scripts.lib.tenants import delete_tenant
from scripts.storage import _delete_storage, run_storage_gate
from scripts.lib.config import parse_duration


class SQLProbeCleanupError(RuntimeError):
    pass


def _render_operator(root: Path, config: dict[str, str], tenant) -> Path:
    source = root / ".tools" / "inputs" / "cnpg.yaml"
    verify_sha256(source, config["CNPG_MANIFEST_SHA256"])
    content = source.read_text(encoding="utf-8")
    tagged = config["CNPG_CONTROLLER_IMAGE_TAGGED"]
    if content.count(tagged) != 2:
        raise IntegrityError("unexpected CNPG operator image count")
    content = content.replace(tagged, config["CNPG_CONTROLLER_IMAGE"])
    path = root / ".runtime" / "rendered" / "cnpg" / tenant.name / "operator.yaml"
    write_private_file(path, content)
    return path


def _render_cluster(root: Path, config: dict[str, str], tenant) -> tuple[Path, Path]:
    replacements = {
        "${CNPG_CLUSTER}": tenant.cnpg_cluster,
        "${POSTGRES_IMAGE}": config["POSTGRES_IMAGE"],
        "${STORAGE_CLASS}": config["SPIKE_STORAGE_CLASS"],
        "${STORAGE_PATH}": config["SPIKE_STORAGE_CONTAINER_PATH"],
    }
    rendered = []
    for source_name, destination_name in (
        ("static-pvs.yaml.tpl", "static-pvs.yaml"),
        ("cluster.yaml.tpl", "cluster.yaml"),
    ):
        content = (root / "manifests" / "cnpg" / source_name).read_text(
            encoding="utf-8"
        )
        for placeholder, value in replacements.items():
            content = content.replace(placeholder, value)
        if "${" in content:
            raise IntegrityError(f"unresolved CNPG template: {source_name}")
        path = root / ".runtime" / "rendered" / "cnpg" / tenant.name / destination_name
        write_private_file(path, content)
        rendered.append(path)
    return rendered[0], rendered[1]


def _prepare_cnpg_directories(config: dict[str, str], tenant) -> None:
    run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{storage_volume_name(config, tenant)}:/data",
            "--entrypoint",
            "sh",
            config["VERIFY_IMAGE"],
            "-ec",
            "for ordinal in 1 2 3; do "
            "mkdir -p /data/volumes/cnpg/$ordinal; "
            "chown 26:26 /data/volumes/cnpg/$ordinal; "
            "chmod 700 /data/volumes/cnpg/$ordinal; "
            "done",
        ],
        timeout=60,
    )


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
    if len(pvcs) != 3 or any(
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
        and cluster.get("status", {}).get("readyInstances") == 3
        and len(ready_pods) == 3
        and len({pod["spec"]["nodeName"] for pod in ready_pods}) == 3
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


def install_cnpg(root: Path, config: dict[str, str], tenant) -> None:
    operator = _render_operator(root, config, tenant)
    _tenant_kubectl(
        root,
        config,
        tenant,
        "apply",
        "--server-side",
        "--field-manager=capi-kamaji-lab",
        "--force-conflicts",
        "-f",
        str(operator),
    )
    _tenant_kubectl(
        root,
        config,
        tenant,
        "-n",
        config["CNPG_NAMESPACE"],
        "rollout",
        "status",
        "deployment/cnpg-controller-manager",
        f"--timeout={config['CNPG_TIMEOUT']}",
    )
    pvs, cluster = _render_cluster(root, config, tenant)
    _prepare_cnpg_directories(config, tenant)
    _tenant_kubectl(root, config, tenant, "apply", "-f", str(pvs))
    _tenant_kubectl(root, config, tenant, "apply", "-f", str(cluster))
    wait_for(
        "CNPG cluster readiness",
        parse_duration(config["CNPG_TIMEOUT"]),
        5,
        lambda: _cnpg_ready(root, config, tenant),
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


def _verify_filesystem(config: dict[str, str], tenant) -> None:
    script = """\
set -eu
for ordinal in 1 2 3; do
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
    if len(pvcs) != 3:
        raise RuntimeError("CNPG does not have three PVCs")
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


def delete_cnpg(root: Path, config: dict[str, str], tenant) -> None:
    _, cluster = _render_cluster(root, config, tenant)
    pvs, _ = _render_cluster(root, config, tenant)
    _tenant_kubectl(
        root,
        config,
        tenant,
        "delete",
        "-f",
        str(cluster),
        "--ignore-not-found",
        "--wait=true",
        f"--timeout={config['DELETE_TIMEOUT']}",
    )
    _tenant_kubectl(
        root,
        config,
        tenant,
        "delete",
        "-f",
        str(pvs),
        "--ignore-not-found",
        "--wait=true",
        f"--timeout={config['DELETE_TIMEOUT']}",
    )
    leftovers = _tenant_kubectl(
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
        "name",
        check=False,
    )
    if leftovers.returncode == 0 and leftovers.stdout.strip():
        raise RuntimeError("CNPG PVCs remain after cluster deletion")
    if leftovers.returncode != 0 and not re.search(
        r"Error from server \(NotFound\):",
        leftovers.stderr,
        re.IGNORECASE,
    ):
        raise RuntimeError(
            f"CNPG PVC deletion inspection failed: {leftovers.stderr}"
        )
    operator = _render_operator(root, config, tenant)
    _tenant_kubectl(
        root,
        config,
        tenant,
        "delete",
        "-f",
        str(operator),
        "--ignore-not-found",
        "--wait=false",
    )

    def operator_absent():
        for arguments in (
            (
                "-n",
                config["CNPG_NAMESPACE"],
                "get",
                "deployment/cnpg-controller-manager",
            ),
            ("get", "crd/clusters.postgresql.cnpg.io"),
        ):
            response = _tenant_kubectl(
                root,
                config,
                tenant,
                *arguments,
                check=False,
            )
            if response.returncode == 0:
                return None
            if not re.search(
                r"Error from server \(NotFound\):",
                response.stderr,
                re.IGNORECASE,
            ):
                raise RuntimeError(
                    f"CNPG operator deletion inspection failed: "
                    f"{response.stderr}"
                )
        return True

    wait_for(
        "CNPG operator deletion",
        parse_duration(config["DELETE_TIMEOUT"]),
        parse_duration(config["WAIT_POLL_INTERVAL"]),
        operator_absent,
    )


def cnpg_artifacts_present(root: Path, config: dict[str, str], tenant) -> bool:
    checks = (
        (
            ("-n", config["DATABASE_NAMESPACE"]),
            "pvc",
        ),
        ((), f"pv/{tenant.cnpg_cluster}-pv-1"),
        ((), f"pv/{tenant.cnpg_cluster}-pv-2"),
        ((), f"pv/{tenant.cnpg_cluster}-pv-3"),
        (
            ("-n", config["CNPG_NAMESPACE"]),
            "deployment/cnpg-controller-manager",
        ),
        ((), "crd/clusters.postgresql.cnpg.io"),
    )
    for scope, resource in checks:
        arguments = [*scope, "get", resource]
        is_list = resource == "pvc"
        if is_list:
            arguments.extend(
                (
                    "-l",
                    f"cnpg.io/cluster={tenant.cnpg_cluster}",
                    "-o",
                    "name",
                )
            )
        response = _tenant_kubectl(
            root,
            config,
            tenant,
            *arguments,
            check=False,
        )
        if response.returncode == 0 and (not is_list or response.stdout.strip()):
            return True
        if response.returncode != 0 and not re.search(
            r"Error from server \(NotFound\):",
            response.stderr,
            re.IGNORECASE,
        ):
            raise RuntimeError(
                f"CNPG artifact inspection failed for {resource}: {response.stderr}"
            )
    return False


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
        install_cnpg(root, config, tenant)
        from scripts.status import collect_status, status_healthy

        if not status_healthy(collect_status(root, config)):
            raise RuntimeError("status does not report healthy CNPG topology")
        before = _storage_identity(root, config, tenant)
        _write_marker(root, config, tenant)
        _verify_filesystem(config, tenant)
        _replace_machine(root, config, client, tenant)
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
                delete_cnpg(root, config, tenant)
                _delete_storage(root, config, tenant)
                delete_addons(root, config, client, tenant)
                delete_tenant(root, config, client, tenant)
            except Exception as exc:
                success.unlink(missing_ok=True)
                write_private_file(failure, redact(str(exc)) + "\n")
                raise
    if evidence is None:
        raise RuntimeError("CNPG evidence was not produced")
    write_private_file(success, json.dumps(evidence, sort_keys=True) + "\n")
    print("CNPG persistence checks passed")
