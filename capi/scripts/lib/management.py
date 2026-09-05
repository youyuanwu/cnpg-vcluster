from __future__ import annotations

import ipaddress
import json
import os
import shutil
import stat
import tarfile
import tempfile
from pathlib import Path

from .config import parse_duration
from .files import IntegrityError, verify_sha256, write_private_file
from .kube import ManagementClient, wait_for
from .ownership import IdentityRecord, OwnershipError
from .process import run
from .rendering import replace_known_images


def _kind(root: Path) -> Path:
    return root / ".tools" / "bin" / "kind"


def _identity_path(root: Path) -> Path:
    return root / ".runtime" / "management" / "identity.json"


def _network_path(root: Path) -> Path:
    return root / ".runtime" / "management" / "network.json"


def _kubeconfig_path(root: Path) -> Path:
    return root / ".runtime" / "management" / "kubeconfig"


def _kind_clusters(root: Path, config: dict[str, str]) -> set[str]:
    result = run(
        [str(_kind(root)), "get", "clusters"],
        timeout=parse_duration(config["COMMAND_TIMEOUT"]),
    )
    return set(result.stdout.split())


def _container_payload(config: dict[str, str]) -> dict[str, object] | None:
    name = f"{config['KIND_CLUSTER_NAME']}-control-plane"
    result = run(["docker", "inspect", name], timeout=30, check=False)
    if result.returncode != 0:
        return None
    payload = json.loads(result.stdout)
    if len(payload) != 1:
        raise RuntimeError(f"unexpected management container inventory for {name}")
    return payload[0]


def _observed_identity(config: dict[str, str], payload: dict[str, object]) -> IdentityRecord:
    labels = payload.get("Config", {}).get("Labels") or {}
    return IdentityRecord(
        kind="container",
        name=f"{config['KIND_CLUSTER_NAME']}-control-plane",
        identifier=str(payload["Id"]),
        labels={
            config["OWNERSHIP_LABEL"]: labels.get(config["OWNERSHIP_LABEL"], ""),
            "io.x-k8s.kind.cluster": labels.get("io.x-k8s.kind.cluster", ""),
        },
    )


def require_management_ownership(root: Path, config: dict[str, str]) -> IdentityRecord:
    clusters = _kind_clusters(root, config)
    payload = _container_payload(config)
    cluster_present = config["KIND_CLUSTER_NAME"] in clusters
    record_path = _identity_path(root)
    record_present = record_path.is_file()
    if not (cluster_present and payload and record_present):
        raise OwnershipError("management kind cluster state is partial or unowned")
    expected = IdentityRecord.load(record_path)
    observed = _observed_identity(config, payload)
    expected.require_exact(observed)
    if observed.labels["io.x-k8s.kind.cluster"] != config["KIND_CLUSTER_NAME"]:
        raise OwnershipError("management container kind label mismatch")
    return observed


def validate_management_kubeconfig(root: Path, config: dict[str, str]) -> None:
    path = _kubeconfig_path(root)
    details = path.lstat()
    if (
        stat.S_ISLNK(details.st_mode)
        or not stat.S_ISREG(details.st_mode)
        or details.st_uid != os.getuid()
        or details.st_mode & 0o077
    ):
        raise OwnershipError("management kubeconfig is not an owner-only regular file")
    expected = run(
        [
            str(_kind(root)),
            "get",
            "kubeconfig",
            "--name",
            config["KIND_CLUSTER_NAME"],
        ],
        timeout=60,
    ).stdout
    if path.read_text(encoding="utf-8") != expected:
        raise OwnershipError("management kubeconfig does not match the exact kind cluster")


def reconcile_kind(root: Path, config: dict[str, str]) -> ManagementClient:
    clusters = _kind_clusters(root, config)
    payload = _container_payload(config)
    cluster_present = config["KIND_CLUSTER_NAME"] in clusters
    record_path = _identity_path(root)
    if cluster_present or payload or record_path.exists():
        require_management_ownership(root, config)
    else:
        result = run(
            [
                str(_kind(root)),
                "create",
                "cluster",
                "--name",
                config["KIND_CLUSTER_NAME"],
                "--config",
                str(root / "config" / "kind.yaml"),
                "--wait",
                config["KIND_CREATE_TIMEOUT"],
            ],
            timeout=parse_duration(config["KIND_CREATE_TIMEOUT"]) + 60,
            check=False,
        )
        if result.returncode != 0:
            run(
                [str(_kind(root)), "delete", "cluster", "--name", config["KIND_CLUSTER_NAME"]],
                timeout=parse_duration(config["DELETE_TIMEOUT"]),
                check=False,
            )
            raise RuntimeError(f"kind creation failed: {result.stdout}{result.stderr}")
        payload = _container_payload(config)
        if payload is None:
            raise RuntimeError("kind did not create the expected management container")
        _observed_identity(config, payload).save(record_path)

    kubeconfig = run(
        [
            str(_kind(root)),
            "get",
            "kubeconfig",
            "--name",
            config["KIND_CLUSTER_NAME"],
        ],
        timeout=60,
    ).stdout
    write_private_file(_kubeconfig_path(root), kubeconfig)
    validate_management_kubeconfig(root, config)
    client = ManagementClient(root, config)
    client.kubectl("get", "--raw=/readyz")
    server_version = client.kubectl("version", "-o", "json").stdout
    if json.loads(server_version)["serverVersion"]["gitVersion"] != config["KUBERNETES_VERSION"]:
        raise RuntimeError("management Kubernetes version mismatch")
    return client


def reconcile_network(root: Path, config: dict[str, str]) -> dict[str, object]:
    payload = _container_payload(config)
    if payload is None:
        raise RuntimeError("management container is absent")
    attachments = payload.get("NetworkSettings", {}).get("Networks") or {}
    if len(attachments) != 1:
        raise RuntimeError("management container must have exactly one Docker network")
    network_name = next(iter(attachments))
    network_payload = json.loads(
        run(["docker", "network", "inspect", network_name], timeout=30).stdout
    )[0]
    subnet_values = [
        entry["Subnet"]
        for entry in network_payload.get("IPAM", {}).get("Config", [])
        if entry.get("Subnet") and ":" not in entry["Subnet"]
    ]
    if len(subnet_values) != 1:
        raise RuntimeError("management Docker network must have one IPv4 subnet")
    subnet = ipaddress.ip_network(subnet_values[0])
    used = {
        ipaddress.ip_address(container["IPv4Address"].split("/", 1)[0])
        for container in (network_payload.get("Containers") or {}).values()
        if container.get("IPv4Address")
    }
    start = subnet.broadcast_address - int(config["VIP_POOL_START_OFFSET_FROM_BROADCAST"])
    end = subnet.broadcast_address - int(config["VIP_POOL_END_OFFSET_FROM_BROADCAST"])
    pool = list(ipaddress.summarize_address_range(start, end))
    reserved = [start + offset for offset in range(int(end) - int(start) + 1)]
    if any(address in used or address not in subnet for address in reserved):
        raise RuntimeError("configured management VIP range is not free")
    slots = {
        "tenant-a": str(reserved[int(config["TENANT_A_API_VIP_SLOT"])]),
        "tenant-b": str(reserved[int(config["TENANT_B_API_VIP_SLOT"])]),
        "spike": str(reserved[int(config["SPIKE_API_VIP_SLOT"])]),
    }
    record = {
        "schema": 1,
        "network": network_name,
        "network_id": network_payload["Id"],
        "subnet": str(subnet),
        "pool_start": str(start),
        "pool_end": str(end),
        "pool_cidrs": [str(item) for item in pool],
        "slots": slots,
    }
    path = _network_path(root)
    if path.exists():
        details = path.lstat()
        if (
            stat.S_ISLNK(details.st_mode)
            or not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.getuid()
            or details.st_mode & 0o077
        ):
            raise RuntimeError("management network record is not owner-only")
        current = json.loads(path.read_text(encoding="utf-8"))
        if current != record:
            raise RuntimeError("management network identity changed")
    else:
        write_private_file(path, json.dumps(record, sort_keys=True) + "\n")
    return record


def _render_cert_manager(root: Path, config: dict[str, str], client: ManagementClient) -> Path:
    chart = root / ".tools" / "inputs" / f"cert-manager-{config['CERT_MANAGER_VERSION']}.tgz"
    verify_sha256(chart, config["CERT_MANAGER_CHART_SHA256"])
    arguments = [
        "template",
        "cert-manager",
        str(chart),
        "--namespace",
        "cert-manager",
        "--include-crds",
        "--set",
        "crds.enabled=true",
    ]
    for prefix, image_key in (
        ("image", "CERT_MANAGER_CONTROLLER_IMAGE"),
        ("webhook.image", "CERT_MANAGER_WEBHOOK_IMAGE"),
        ("cainjector.image", "CERT_MANAGER_CAINJECTOR_IMAGE"),
        ("startupapicheck.image", "CERT_MANAGER_STARTUPAPICHECK_IMAGE"),
    ):
        arguments.extend(("--set", f"{prefix}.tag={config['CERT_MANAGER_VERSION']}"))
        arguments.extend(("--set", f"{prefix}.digest={config[image_key].rsplit('@', 1)[1]}"))
    rendered = client.helm(*arguments).stdout
    for image_key in (
        "CERT_MANAGER_CONTROLLER_IMAGE",
        "CERT_MANAGER_WEBHOOK_IMAGE",
        "CERT_MANAGER_CAINJECTOR_IMAGE",
        "CERT_MANAGER_STARTUPAPICHECK_IMAGE",
    ):
        if config[image_key] not in rendered:
            raise IntegrityError(f"cert-manager render is missing {config[image_key]}")
    path = root / ".runtime" / "rendered" / "cert-manager.yaml"
    write_private_file(path, rendered)
    return path


def reconcile_cert_manager(root: Path, config: dict[str, str], client: ManagementClient) -> None:
    _render_cert_manager(root, config, client)
    chart = root / ".tools" / "inputs" / f"cert-manager-{config['CERT_MANAGER_VERSION']}.tgz"
    arguments = [
        "upgrade",
        "--install",
        "cert-manager",
        str(chart),
        "--namespace",
        "cert-manager",
        "--create-namespace",
        "--atomic",
        "--wait",
        "--timeout",
        config["CERT_MANAGER_TIMEOUT"],
        "--set",
        "crds.enabled=true",
    ]
    for prefix, image_key in (
        ("image", "CERT_MANAGER_CONTROLLER_IMAGE"),
        ("webhook.image", "CERT_MANAGER_WEBHOOK_IMAGE"),
        ("cainjector.image", "CERT_MANAGER_CAINJECTOR_IMAGE"),
        ("startupapicheck.image", "CERT_MANAGER_STARTUPAPICHECK_IMAGE"),
    ):
        arguments.extend(("--set", f"{prefix}.tag={config['CERT_MANAGER_VERSION']}"))
        arguments.extend(("--set", f"{prefix}.digest={config[image_key].rsplit('@', 1)[1]}"))
    client.helm(*arguments)
    for deployment in ("cert-manager", "cert-manager-cainjector", "cert-manager-webhook"):
        client.kubectl(
            "-n",
            "cert-manager",
            "rollout",
            "status",
            f"deployment/{deployment}",
            f"--timeout={config['CERT_MANAGER_TIMEOUT']}",
        )
    expected_images = {
        "cert-manager": config["CERT_MANAGER_CONTROLLER_IMAGE"],
        "cert-manager-cainjector": config["CERT_MANAGER_CAINJECTOR_IMAGE"],
        "cert-manager-webhook": config["CERT_MANAGER_WEBHOOK_IMAGE"],
    }
    for deployment, expected in expected_images.items():
        image = client.kubectl(
            "-n",
            "cert-manager",
            "get",
            f"deployment/{deployment}",
            "-o",
            "jsonpath={.spec.template.spec.containers[0].image}",
        ).stdout
        if image != expected:
            raise RuntimeError(f"cert-manager live image mismatch for {deployment}")


def _render_metallb(root: Path, config: dict[str, str]) -> Path:
    source = root / ".tools" / "inputs" / "metallb-native.yaml"
    verify_sha256(source, config["METALLB_MANIFEST_SHA256"])
    content = source.read_text(encoding="utf-8")
    for tagged_key, pinned_key in (
        ("METALLB_CONTROLLER_IMAGE_TAGGED", "METALLB_CONTROLLER_IMAGE"),
        ("METALLB_SPEAKER_IMAGE_TAGGED", "METALLB_SPEAKER_IMAGE"),
    ):
        tagged = config[tagged_key]
        if content.count(tagged) != 1:
            raise IntegrityError(f"unexpected MetalLB image count for {tagged}")
        content = content.replace(tagged, config[pinned_key])
    path = root / ".runtime" / "rendered" / "metallb.yaml"
    write_private_file(path, content)
    return path


def _render_metallb_pool(root: Path, config: dict[str, str], network: dict[str, object]) -> Path:
    content = (root / "manifests" / "management" / "metallb-pool.yaml.tpl").read_text(
        encoding="utf-8"
    )
    replacements = {
        "${OWNERSHIP_LABEL}": config["OWNERSHIP_LABEL"],
        "${LAB_PREFIX}": config["LAB_PREFIX"],
        "${VIP_POOL_START}": str(network["pool_start"]),
        "${VIP_POOL_END}": str(network["pool_end"]),
    }
    for placeholder, value in replacements.items():
        if content.count(placeholder) < 1:
            raise IntegrityError(f"MetalLB pool template is missing {placeholder}")
        content = content.replace(placeholder, value)
    if "${" in content:
        raise IntegrityError("MetalLB pool template has unresolved placeholders")
    path = root / ".runtime" / "rendered" / "metallb-pool.yaml"
    write_private_file(path, content)
    return path


def reconcile_metallb(
    root: Path,
    config: dict[str, str],
    client: ManagementClient,
    network: dict[str, object],
) -> None:
    manifest = _render_metallb(root, config)
    client.kubectl("apply", "--server-side", "--field-manager=capi-kamaji-lab", "-f", str(manifest))
    for crd in ("ipaddresspools.metallb.io", "l2advertisements.metallb.io"):
        client.kubectl(
            "wait",
            "--for=condition=Established",
            f"crd/{crd}",
            f"--timeout={config['METALLB_TIMEOUT']}",
        )
    client.kubectl(
        "-n",
        "metallb-system",
        "rollout",
        "status",
        "deployment/controller",
        f"--timeout={config['METALLB_TIMEOUT']}",
    )
    client.kubectl(
        "-n",
        "metallb-system",
        "rollout",
        "status",
        "daemonset/speaker",
        f"--timeout={config['METALLB_TIMEOUT']}",
    )
    live_images = {
        "controller": client.kubectl(
            "-n",
            "metallb-system",
            "get",
            "deployment/controller",
            "-o",
            "jsonpath={.spec.template.spec.containers[0].image}",
        ).stdout,
        "speaker": client.kubectl(
            "-n",
            "metallb-system",
            "get",
            "daemonset/speaker",
            "-o",
            "jsonpath={.spec.template.spec.containers[0].image}",
        ).stdout,
    }
    if live_images != {
        "controller": config["METALLB_CONTROLLER_IMAGE"],
        "speaker": config["METALLB_SPEAKER_IMAGE"],
    }:
        raise RuntimeError("MetalLB live image identities do not match")
    client.kubectl("apply", "-f", str(_render_metallb_pool(root, config, network)))


def _prepare_kamaji_chart(root: Path, config: dict[str, str]) -> Path:
    source_archive = root / ".tools" / "inputs" / "kamaji-source.tar.gz"
    verify_sha256(source_archive, config["KAMAJI_SOURCE_SHA256"])
    charts_dir = root / ".tools" / "charts"
    charts_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    charts_dir.chmod(0o700)
    with tempfile.TemporaryDirectory(dir=charts_dir) as temporary:
        with tarfile.open(source_archive, "r:gz") as archive:
            archive.extractall(temporary, filter="data")
        source = Path(temporary) / config["KAMAJI_SOURCE_DIR"] / "charts" / "kamaji"
        verify_sha256(source / "Chart.lock", config["KAMAJI_CHART_LOCK_SHA256"])
        if config["KAMAJI_CHART_LOCK_DIGEST"] not in (source / "Chart.lock").read_text(
            encoding="utf-8"
        ):
            raise IntegrityError("Kamaji Chart.lock dependency digest mismatch")
        destination = charts_dir / "kamaji"
        replacement = charts_dir / ".kamaji-replacement"
        shutil.rmtree(replacement, ignore_errors=True)
        shutil.copytree(source, replacement)
        for path in replacement.rglob("*"):
            if path.is_symlink():
                raise IntegrityError(f"Kamaji chart contains a symlink: {path}")
            path.chmod(0o700 if path.is_dir() else 0o600)
        dependency_dir = replacement / "charts"
        dependency_dir.mkdir(mode=0o700)
        dependency = root / ".tools" / "inputs" / "kamaji-etcd-0.15.0.tgz"
        verify_sha256(dependency, config["KAMAJI_ETCD_CHART_SHA256"])
        shutil.copyfile(dependency, dependency_dir / dependency.name)
        (dependency_dir / dependency.name).chmod(0o600)
        backup = charts_dir / ".kamaji-old"
        shutil.rmtree(backup, ignore_errors=True)
        if destination.exists():
            destination.rename(backup)
        replacement.rename(destination)
        shutil.rmtree(backup, ignore_errors=True)
    return charts_dir / "kamaji"


def _render_kamaji(
    root: Path,
    config: dict[str, str],
    client: ManagementClient,
    chart: Path,
) -> Path:
    result = client.helm(
        "template",
        "kamaji",
        str(chart),
        "--namespace",
        config["MANAGEMENT_NAMESPACE"],
        "--include-crds",
        "--values",
        str(root / "manifests" / "management" / "kamaji-values.yaml"),
    )
    rendered = replace_known_images(result.stdout, config)
    path = root / ".runtime" / "rendered" / "kamaji.yaml"
    write_private_file(path, rendered)
    for image_key in (
        "KAMAJI_IMAGE",
        "KAMAJI_ETCD_IMAGE",
        "KAMAJI_ETCD_JOB_IMAGE",
        "KAMAJI_KUBECTL_JOB_IMAGE",
    ):
        if config[image_key] not in rendered:
            raise IntegrityError(f"Kamaji render is missing {config[image_key]}")
    return path


def reconcile_kamaji(root: Path, config: dict[str, str], client: ManagementClient) -> None:
    chart = _prepare_kamaji_chart(root, config)
    _render_kamaji(root, config, client, chart)
    client.helm(
        "upgrade",
        "--install",
        "kamaji",
        str(chart),
        "--namespace",
        config["MANAGEMENT_NAMESPACE"],
        "--create-namespace",
        "--atomic",
        "--wait",
        "--timeout",
        config["KAMAJI_TIMEOUT"],
        "--values",
        str(root / "manifests" / "management" / "kamaji-values.yaml"),
        "--post-renderer",
        str(root / "scripts" / "post_renderer.py"),
    )
    for crd in (
        "datastores.kamaji.clastix.io",
        "kubeconfiggenerators.kamaji.clastix.io",
        "tenantcontrolplanes.kamaji.clastix.io",
    ):
        client.kubectl(
            "wait",
            "--for=condition=Established",
            f"crd/{crd}",
            f"--timeout={config['KAMAJI_TIMEOUT']}",
        )
        kamaji_image = client.kubectl(
            "-n",
            config["MANAGEMENT_NAMESPACE"],
            "get",
            "deployment/kamaji",
            "-o",
            "jsonpath={.spec.template.spec.containers[0].image}",
        ).stdout
        etcd_image = client.kubectl(
            "-n",
            config["MANAGEMENT_NAMESPACE"],
            "get",
            "statefulset/kamaji-etcd",
            "-o",
            "jsonpath={.spec.template.spec.containers[0].image}",
        ).stdout
        if kamaji_image != config["KAMAJI_IMAGE"] or etcd_image != config["KAMAJI_ETCD_IMAGE"]:
            raise RuntimeError("Kamaji live image identities do not match")
        pvc_payload = json.loads(
            client.kubectl(
                "-n",
                config["MANAGEMENT_NAMESPACE"],
                "get",
                "pvc",
                "-o",
                "json",
            ).stdout
        )
        bound_etcd_claims = [
            item
            for item in pvc_payload["items"]
            if item["metadata"]["name"].startswith("data-kamaji-etcd-")
            and item.get("status", {}).get("phase") == "Bound"
        ]
        if len(bound_etcd_claims) != 3:
            raise RuntimeError("Kamaji datastore does not have three Bound PVCs")
    client.kubectl(
        "-n",
        config["MANAGEMENT_NAMESPACE"],
        "rollout",
        "status",
        "deployment/kamaji",
        f"--timeout={config['KAMAJI_TIMEOUT']}",
    )
    client.kubectl(
        "-n",
        config["MANAGEMENT_NAMESPACE"],
        "rollout",
        "status",
        "statefulset/kamaji-etcd",
        f"--timeout={config['KAMAJI_TIMEOUT']}",
    )
    wait_for(
        "DataStore/default readiness",
        parse_duration(config["KAMAJI_TIMEOUT"]),
        parse_duration(config["WAIT_POLL_INTERVAL"]),
        lambda: client.kubectl(
            "get",
            "datastore/default",
            "-o",
            "jsonpath={.status.ready}",
            check=False,
        ).stdout
        == "true",
    )


def management_status(root: Path, config: dict[str, str]) -> dict[str, object]:
    clusters = _kind_clusters(root, config)
    payload = _container_payload(config)
    result: dict[str, object] = {
        "clusterReported": config["KIND_CLUSTER_NAME"] in clusters,
        "containerPresent": payload is not None,
        "ownershipRecord": _identity_path(root).is_file(),
        "kubeconfig": _kubeconfig_path(root).is_file(),
    }
    if all(result.values()):
        try:
            require_management_ownership(root, config)
            validate_management_kubeconfig(root, config)
            client = ManagementClient(root, config)
            result["apiReady"] = (
                client.kubectl("get", "--raw=/readyz", check=False).returncode == 0
            )
        except (RuntimeError, OwnershipError):
            result["apiReady"] = False
    else:
        result["apiReady"] = False
    return result


def management_component_status(
    config: dict[str, str],
    client: ManagementClient,
) -> list[dict[str, object]]:
    components = (
        ("Deployment", "cert-manager", "cert-manager", "CERT_MANAGER_CONTROLLER_IMAGE"),
        (
            "Deployment",
            "cert-manager",
            "cert-manager-cainjector",
            "CERT_MANAGER_CAINJECTOR_IMAGE",
        ),
        (
            "Deployment",
            "cert-manager",
            "cert-manager-webhook",
            "CERT_MANAGER_WEBHOOK_IMAGE",
        ),
        ("Deployment", "metallb-system", "controller", "METALLB_CONTROLLER_IMAGE"),
        ("DaemonSet", "metallb-system", "speaker", "METALLB_SPEAKER_IMAGE"),
        ("Deployment", config["MANAGEMENT_NAMESPACE"], "kamaji", "KAMAJI_IMAGE"),
        (
            "StatefulSet",
            config["MANAGEMENT_NAMESPACE"],
            "kamaji-etcd",
            "KAMAJI_ETCD_IMAGE",
        ),
    )
    result: list[dict[str, object]] = []
    for kind, namespace, name, image_key in components:
        response = client.kubectl(
            "-n",
            namespace,
            "get",
            f"{kind.lower()}/{name}",
            "-o",
            "json",
            check=False,
        )
        if response.returncode != 0:
            result.append({"name": f"{namespace}/{name}", "available": False})
            continue
        payload = json.loads(response.stdout)
        desired = payload.get("spec", {}).get("replicas")
        if kind == "DaemonSet":
            desired = payload.get("status", {}).get("desiredNumberScheduled", 0)
            available = payload.get("status", {}).get("numberAvailable", 0)
        else:
            desired = desired or 0
            available = payload.get("status", {}).get("availableReplicas", 0)
            if kind == "StatefulSet":
                available = payload.get("status", {}).get("readyReplicas", 0)
        image = payload["spec"]["template"]["spec"]["containers"][0]["image"]
        result.append(
            {
                "name": f"{namespace}/{name}",
                "available": desired == available and desired > 0 and image == config[image_key],
                "desired": desired,
                "availableReplicas": available,
                "image": image,
            }
        )
    return result


def management_auxiliary_status(
    config: dict[str, str],
    client: ManagementClient,
) -> dict[str, object]:
    endpoints = {}
    for name, namespace in (
        ("cert-manager-webhook", "cert-manager"),
        ("metallb-webhook-service", "metallb-system"),
        ("kamaji-webhook-service", config["MANAGEMENT_NAMESPACE"]),
    ):
        response = client.kubectl(
            "-n",
            namespace,
            "get",
            f"endpoints/{name}",
            "-o",
            "jsonpath={.subsets[0].addresses[0].ip}",
            check=False,
        )
        endpoints[f"{namespace}/{name}"] = response.returncode == 0 and bool(
            response.stdout
        )
    pvc_response = client.kubectl(
        "-n",
        config["MANAGEMENT_NAMESPACE"],
        "get",
        "pvc",
        "-o",
        "json",
        check=False,
    )
    bound_claims = 0
    if pvc_response.returncode == 0:
        bound_claims = sum(
            1
            for item in json.loads(pvc_response.stdout)["items"]
            if item["metadata"]["name"].startswith("data-kamaji-etcd-")
            and item.get("status", {}).get("phase") == "Bound"
        )
    return {
        "webhooks": endpoints,
        "datastoreBoundPVCs": bound_claims,
        "ready": all(endpoints.values()) and bound_claims == 3,
    }


def delete_management(root: Path, config: dict[str, str]) -> None:
    clusters = _kind_clusters(root, config)
    payload = _container_payload(config)
    record = _identity_path(root)
    if not (config["KIND_CLUSTER_NAME"] in clusters or payload or record.exists()):
        return
    require_management_ownership(root, config)
    run(
        [str(_kind(root)), "delete", "cluster", "--name", config["KIND_CLUSTER_NAME"]],
        timeout=parse_duration(config["DELETE_TIMEOUT"]),
    )
    if _container_payload(config) is not None:
        raise RuntimeError("management container remained after kind deletion")
    for path in (_kubeconfig_path(root), _network_path(root), _identity_path(root)):
        path.unlink(missing_ok=True)
