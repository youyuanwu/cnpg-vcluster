from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

from scripts.lib.config import parse_duration
from scripts.lib.files import ensure_private_dir, write_private_file
from scripts.lib.kube import ManagementClient, wait_for
from scripts.lib.management import require_management_ownership
from scripts.lib.process import run


CONTROLLER_NAMESPACE = "tenant-system"
CONTROLLER_DEPLOYMENT = "tenant-controller"
TENANT_CRD = "tenants.tenancy.cnpg-vcluster.io"


def go_environment(root: Path) -> dict[str, str]:
    go_root = root / ".tools" / "go"
    go_binary = root / ".tools" / "bin" / "go"
    if not go_binary.is_file() or not (go_root / "bin" / "go").is_file():
        raise RuntimeError("pinned Go toolchain is missing; run just cache && just tools")
    cache_root = root / ".tools" / "go-cache"
    module_cache = root / ".tools" / "go-mod-cache"
    ensure_private_dir(cache_root)
    ensure_private_dir(module_cache)
    environment = os.environ.copy()
    environment.update(
        {
            "GOROOT": str(go_root),
            "GOCACHE": str(cache_root),
            "GOMODCACHE": str(module_cache),
            "CGO_ENABLED": "0",
            "PATH": f"{root / '.tools' / 'bin'}:{environment.get('PATH', '')}",
        }
    )
    if os.environ.get("CAPI_OFFLINE_ENFORCED") == "1":
        environment["GOPROXY"] = "off"
        environment["GOSUMDB"] = "off"
    return environment


def generate_controller(root: Path, config: dict[str, str]) -> None:
    controller = root / "controller"
    environment = go_environment(root)
    timeout = parse_duration(config["COMMAND_TIMEOUT"]) * 4
    commands = (
        [
            str(root / ".tools" / "bin" / "go"),
            "tool",
            "controller-gen",
            "object",
            "paths=./api/...",
        ],
        [
            str(root / ".tools" / "bin" / "go"),
            "tool",
            "controller-gen",
            "crd:allowDangerousTypes=true",
            "rbac:roleName=tenant-controller-role",
            "paths=./...",
            "output:crd:artifacts:config=config/crd/bases",
            "output:rbac:artifacts:config=config/rbac",
        ],
    )
    for command in commands:
        run(command, cwd=controller, env=environment, timeout=timeout)


def test_controller(root: Path, config: dict[str, str]) -> None:
    environment = go_environment(root)
    environment["KUBEBUILDER_ASSETS"] = str(root / ".tools" / "envtest" / "envtest")
    run(
        [str(root / ".tools" / "bin" / "go"), "test", "./..."],
        cwd=root / "controller",
        env=environment,
        timeout=parse_duration(config["COMMAND_TIMEOUT"]) * 8,
    )


def controller_source_digest(root: Path) -> str:
    digest = hashlib.sha256()
    controller = root / "controller"
    for path in sorted(
        candidate
        for candidate in controller.rglob("*")
        if candidate.is_file()
    ):
        digest.update(path.relative_to(controller).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def controller_image(root: Path, config: dict[str, str]) -> str:
    return (
        f"{config['TENANT_CONTROLLER_IMAGE_REPOSITORY']}:"
        f"{controller_source_digest(root)[:16]}"
    )


def build_controller_binary(
    root: Path,
    config: dict[str, str],
) -> Path:
    output = root / ".runtime" / "rendered" / "controller" / "manager"
    ensure_private_dir(output.parent)
    environment = go_environment(root)
    run(
        [
            str(root / ".tools" / "bin" / "go"),
            "build",
            "-trimpath",
            "-ldflags=-s -w",
            "-o",
            str(output),
            "./cmd/manager",
        ],
        cwd=root / "controller",
        env=environment,
        timeout=parse_duration(config["COMMAND_TIMEOUT"]) * 8,
    )
    output.chmod(0o700)
    return output


def build_controller_image(
    root: Path,
    config: dict[str, str],
) -> str:
    binary = build_controller_binary(root, config)
    build_root = root / ".runtime" / "rendered" / "controller-build"
    shutil.rmtree(build_root, ignore_errors=True)
    ensure_private_dir(build_root)
    try:
        shutil.copy2(root / "controller" / "Dockerfile", build_root / "Dockerfile")
        shutil.copy2(binary, build_root / "manager")
        assets = build_root / "assets"
        ensure_private_dir(assets)
        for name in ("calico.yaml", "cnpg.yaml"):
            source = root / ".tools" / "inputs" / name
            if not source.is_file():
                raise RuntimeError(f"verified controller asset is missing: {source}")
            shutil.copy2(source, assets / name)
        image = controller_image(root, config)
        run(
            ["docker", "build", "--pull=false", "-t", image, str(build_root)],
            timeout=parse_duration(config["COMMAND_TIMEOUT"]) * 8,
        )
        return image
    finally:
        shutil.rmtree(build_root, ignore_errors=True)


def render_controller_manager(root: Path, config: dict[str, str], image: str) -> Path:
    template = (
        root / "controller" / "config" / "manager" / "manager.yaml.tpl"
    ).read_text(encoding="utf-8")
    rendered = (
        template.replace("${TENANT_CONTROLLER_IMAGE}", image)
        .replace(
            "${SUPPORTED_KUBERNETES_VERSION}",
            config["KUBERNETES_VERSION"].removeprefix("v"),
        )
    )
    destination = root / ".runtime" / "rendered" / "controller" / "manager.yaml"
    write_private_file(destination, rendered)
    return destination


def _foundation_payload(
    root: Path,
    config: dict[str, str],
    network: dict[str, object],
    image: str,
) -> dict[str, object]:
    identity = require_management_ownership(root, config)
    reserved = sorted(
        {
            value
            for key, value in config.items()
            if key.endswith("_CIDR") and "/" in value
        }
    )
    data = {
        "schema": 1,
        "managementContainerId": identity.identifier,
        "networkId": network["network_id"],
        "subnet": network["subnet"],
        "poolStart": network["pool_start"],
        "poolEnd": network["pool_end"],
        "reservedCIDRs": reserved,
        "kubernetesVersion": config["KUBERNETES_VERSION"],
        "controllerImage": image,
        "mutationEnabled": False,
    }
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": "tenant-foundation",
            "namespace": CONTROLLER_NAMESPACE,
        },
        "data": {
            "foundation.json": encoded,
            "foundation.sha256": hashlib.sha256(encoded.encode()).hexdigest(),
        },
    }


def reconcile_controller(
    root: Path,
    config: dict[str, str],
    client: ManagementClient,
    network: dict[str, object],
) -> None:
    image = build_controller_image(root, config)
    run(
        [
            str(root / ".tools" / "bin" / "kind"),
            "load",
            "docker-image",
            image,
            "--name",
            config["KIND_CLUSTER_NAME"],
        ],
        timeout=parse_duration(config["COMMAND_TIMEOUT"]) * 4,
    )
    manager = render_controller_manager(root, config, image)
    paths = (
        root / "controller" / "config" / "namespace" / "namespace.yaml",
        root / "controller" / "config" / "crd" / "bases",
        root / "controller" / "config" / "rbac",
        root / "controller" / "config" / "webhook" / "service.yaml",
        root / "controller" / "config" / "webhook" / "certificate.yaml",
        manager,
        root / "controller" / "config" / "webhook" / "validating-webhook.yaml",
    )
    for path in paths:
        client.kubectl(
            "apply",
            "--server-side",
            "--field-manager=cnpg-vcluster-controller",
            "--force-conflicts",
            "-f",
            str(path),
        )
    foundation = _foundation_payload(root, config, network, image)
    client.kubectl(
        "apply",
        "--server-side",
        "--field-manager=cnpg-vcluster-controller",
        "--force-conflicts",
        "-f",
        "-",
        input_text=json.dumps(foundation),
    )
    timeout = config["CONDITION_TIMEOUT"]
    client.kubectl(
        "wait",
        "--for=condition=Established",
        f"crd/{TENANT_CRD}",
        f"--timeout={timeout}",
    )
    client.kubectl(
        "wait",
        "--for=condition=Ready",
        "certificate/tenant-controller-serving-cert",
        "-n",
        CONTROLLER_NAMESPACE,
        f"--timeout={timeout}",
    )
    client.kubectl(
        "rollout",
        "status",
        f"deployment/{CONTROLLER_DEPLOYMENT}",
        "-n",
        CONTROLLER_NAMESPACE,
        f"--timeout={timeout}",
    )
    webhook = client.json(
        "get",
        "validatingwebhookconfiguration",
        "tenant-controller-validating-webhook",
    )
    ca_bundle = webhook["webhooks"][0]["clientConfig"].get("caBundle", "")
    if not ca_bundle:
        raise RuntimeError("Tenant validating webhook CA bundle is empty")
    probe = {
        "apiVersion": "tenancy.cnpg-vcluster.io/v1alpha1",
        "kind": "Tenant",
        "metadata": {"name": "webhook-readiness-probe"},
        "spec": {
            "kubernetesVersion": config["KUBERNETES_VERSION"].removeprefix("v"),
            "workers": 1,
            "databaseCount": 1,
            "podCIDR": "10.252.0.0/16",
            "serviceCIDR": "10.253.0.0/16",
        },
    }

    def webhook_ready() -> bool | None:
        result = client.kubectl(
            "create",
            "--dry-run=server",
            "-f",
            "-",
            input_text=json.dumps(probe),
            check=False,
        )
        return True if result.returncode == 0 else None

    wait_for(
        "Tenant validating webhook readiness",
        parse_duration(config["CONDITION_TIMEOUT"]),
        2,
        webhook_ready,
    )


def delete_controller(
    root: Path,
    config: dict[str, str],
    client: ManagementClient,
) -> None:
    client.kubectl(
        "delete",
        "tenants.tenancy.cnpg-vcluster.io",
        "--all",
        "--ignore-not-found",
        "--wait=true",
        f"--timeout={config['DELETE_TIMEOUT']}",
        check=False,
    )
    paths = (
        root / "controller" / "config" / "webhook" / "validating-webhook.yaml",
        root / ".runtime" / "rendered" / "controller" / "manager.yaml",
        root / "controller" / "config" / "webhook" / "certificate.yaml",
        root / "controller" / "config" / "webhook" / "service.yaml",
        root / "controller" / "config" / "rbac",
        root / "controller" / "config" / "crd" / "bases",
        root / "controller" / "config" / "namespace" / "namespace.yaml",
    )
    for path in paths:
        if path.exists():
            client.kubectl(
                "delete",
                "-f",
                str(path),
                "--ignore-not-found",
                "--wait=false",
                check=False,
            )
    shutil.rmtree(
        root / ".runtime" / "rendered" / "controller",
        ignore_errors=True,
    )
