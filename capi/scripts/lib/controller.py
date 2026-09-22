from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING

from scripts.cache import canonical_tagged
from scripts.lib.config import parse_duration
from scripts.lib.files import ensure_private_dir, write_private_file
from scripts.lib.images import WORKER_IMAGE_KEYS
from scripts.lib.kube import ManagementClient, wait_for
from scripts.lib.management import require_management_ownership
from scripts.lib.process import run
from scripts.tools import verify_all_inputs

if TYPE_CHECKING:
    from scripts.cache import VerifiedCache


CONTROLLER_NAMESPACE = "tenant-system"
CONTROLLER_DEPLOYMENT = "tenant-controller"
TENANT_CRD = "tenants.tenancy.cnpg-vcluster.io"


def _foundation_checksum(data: dict[str, object]) -> str:
    immutable = dict(data)
    immutable.pop("mutationEnabled", None)
    encoded = json.dumps(immutable, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


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


def controller_source_digest(root: Path, config: dict[str, str]) -> str:
    digest = hashlib.sha256()
    controller = root / "controller"
    inputs = [
        *(
            candidate
            for candidate in controller.rglob("*")
            if candidate.is_file()
        ),
        root / "config" / "versions.env",
        root / ".tools" / "inputs" / "calico.yaml",
        root / ".tools" / "inputs" / "cnpg.yaml",
    ]
    for path in sorted(inputs):
        relative = (
            path.relative_to(root).as_posix()
            if path.is_relative_to(root)
            else str(path)
        )
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    digest.update(config["GO_VERSION"].encode())
    digest.update(config["CONTROLLER_RUNTIME_VERSION"].encode())
    digest.update(config["CONTROLLER_TOOLS_VERSION"].encode())
    return digest.hexdigest()


def controller_image(root: Path, config: dict[str, str]) -> str:
    return (
        f"{config['TENANT_CONTROLLER_IMAGE_REPOSITORY']}:"
        f"{controller_source_digest(root, config)[:16]}"
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
    verify_all_inputs(root, config)
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
    verified_cache: VerifiedCache,
    registry: dict[str, object] | None,
) -> dict[str, object]:
    identity = require_management_ownership(root, config)
    reserved = sorted(
        {
            value
            for key, value in config.items()
            if key.endswith("_CIDR") and "/" in value
        }
    )
    allowed_subnets = sorted({"127.0.0.0/8", str(network["subnet"]), *reserved})
    versions = {
        key: value
        for key, value in sorted(config.items())
        if key.endswith("_VERSION")
        or key in {
            "CAPI_CONTRACT",
            "KAMAJI_CAPI_CONTRACT",
            "CONTROLLER_RUNTIME_VERSION",
            "CONTROLLER_TOOLS_VERSION",
            "GO_VERSION",
        }
    }
    archives = [
        {
            "key": entry["key"],
            "path": entry["path"],
            "sha256": entry["sha256"],
            "reference": config[entry["key"]],
            "tagged": canonical_tagged(config[f"{entry['key']}_TAGGED"]),
            "worker": entry["key"] in WORKER_IMAGE_KEYS,
        }
        for entry in verified_cache.inventory["imageArchives"]
    ]
    data = {
        "schema": 2,
        "managementContainerId": identity.identifier,
        "managementContainerLabels": {
            **identity.labels,
            "io.x-k8s.kind.role": "control-plane",
        },
        "networkId": network["network_id"],
        "subnet": network["subnet"],
        "poolStart": network["pool_start"],
        "poolEnd": network["pool_end"],
        "reservedCIDRs": reserved,
        "allowedSubnets": allowed_subnets,
        "kubernetesVersion": config["KUBERNETES_VERSION"],
        "controllerImage": image,
        "mutationEnabled": False,
        "offlineEnforced": os.environ.get("CAPI_OFFLINE_ENFORCED") == "1",
        "versions": versions,
        "cache": {
            "generation": verified_cache.generation.name,
            "stateSHA256": verified_cache.state_sha256,
            "activeSHA256": hashlib.sha256(
                (root / ".tools" / "cache" / "active.json").read_bytes()
            ).hexdigest(),
            "imageArchives": archives,
        },
        "registry": (
            {
                "address": registry["address"],
                "port": 5000,
                "generation": registry["generation"],
                "identifier": registry["identifier"],
            }
            if registry is not None
            else None
        ),
        "inputs": {
            "ownershipLabel": config["OWNERSHIP_LABEL"],
            "labPrefix": config["LAB_PREFIX"],
            "apiPort": int(config["SPIKE_API_PORT"]),
            "clusterDomain": config["SPIKE_CLUSTER_DOMAIN"],
            "nodeImage": config["KIND_NODE_IMAGE"],
            "cacheHostPath": str(root / ".tools" / "cache"),
            "cacheContainerPath": "/var/lib/capi-image-cache",
            "storageContainerPath": config["SPIKE_STORAGE_CONTAINER_PATH"],
            "konnectivityServerImage": config["KONNECTIVITY_SERVER_IMAGE"],
            "konnectivityAgentImage": config["KONNECTIVITY_AGENT_IMAGE"],
        },
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
            "foundation.sha256": _foundation_checksum(data),
        },
    }


def set_controller_mutation(
    config: dict[str, str],
    client: ManagementClient,
    *,
    enabled: bool,
) -> None:
    foundation = client.json(
        "-n",
        CONTROLLER_NAMESPACE,
        "get",
        "configmap/tenant-foundation",
    )
    encoded = foundation["data"]["foundation.json"]
    data = json.loads(encoded)
    data["mutationEnabled"] = enabled
    updated = json.dumps(data, sort_keys=True, separators=(",", ":"))
    patch = {
        "data": {
            "foundation.json": updated,
            "foundation.sha256": _foundation_checksum(data),
        }
    }
    argument = f"--mutation-enabled={'true' if enabled else 'false'}"
    deployment = client.json(
        "-n",
        CONTROLLER_NAMESPACE,
        "get",
        f"deployment/{CONTROLLER_DEPLOYMENT}",
    )
    containers = deployment["spec"]["template"]["spec"]["containers"]
    manager = next(
        (item for item in containers if item.get("name") == "manager"),
        None,
    )
    if manager is None:
        raise RuntimeError("Tenant controller manager container is missing")
    args = [
        argument if value.startswith("--mutation-enabled=") else value
        for value in manager.get("args", [])
    ]
    if not any(value.startswith("--mutation-enabled=") for value in args):
        raise RuntimeError("Tenant controller mutation argument is missing")
    deployment_patch = {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "manager",
                            "args": args,
                        }
                    ]
                }
            }
        }
    }
    if enabled:
        client.kubectl(
            "-n",
            CONTROLLER_NAMESPACE,
            "patch",
            "configmap/tenant-foundation",
            "--type=merge",
            "-p",
            json.dumps(patch),
        )

    client.kubectl(
        "-n",
        CONTROLLER_NAMESPACE,
        "patch",
        f"deployment/{CONTROLLER_DEPLOYMENT}",
        "--type=strategic",
        "-p",
        json.dumps(deployment_patch),
    )
    client.kubectl(
        "-n",
        CONTROLLER_NAMESPACE,
        "rollout",
        "status",
        f"deployment/{CONTROLLER_DEPLOYMENT}",
        f"--timeout={config['CONDITION_TIMEOUT']}",
    )
    probe = {
        "apiVersion": "tenancy.cnpg-vcluster.io/v1alpha1",
        "kind": "Tenant",
        "metadata": {"name": "mutation-ready-probe"},
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
        "Tenant webhook readiness after mutation mode change",
        parse_duration(config["CONDITION_TIMEOUT"]),
        2,
        webhook_ready,
    )
    if not enabled:
        client.kubectl(
            "-n",
            CONTROLLER_NAMESPACE,
            "patch",
            "configmap/tenant-foundation",
            "--type=merge",
            "-p",
            json.dumps(patch),
        )


def delete_tenant_resource(
    client: ManagementClient,
    tenant_name: str,
    *,
    wait: bool,
    timeout: str | None = None,
    check: bool = True,
):
    arguments = [
        "delete",
        f"tenant/{tenant_name}",
        "--ignore-not-found=true",
        f"--wait={'true' if wait else 'false'}",
    ]
    if timeout is not None:
        arguments.append(f"--timeout={timeout}")
    return client.kubectl(*arguments, check=check)


def delete_controller_tenants(
    config: dict[str, str],
    client: ManagementClient,
) -> None:
    response = client.kubectl("get", TENANT_CRD, "-o", "json", check=False)
    if response.returncode != 0:
        output = f"{response.stdout}{response.stderr}".lower()
        if "not found" in output or "the server doesn't have a resource type" in output:
            return
        raise RuntimeError(
            f"failed to inspect controller Tenants for teardown: {response.stderr}"
        )
    if not response.stdout.strip():
        return
    tenants = json.loads(response.stdout)
    for item in sorted(tenants.get("items", []), key=lambda value: value["metadata"]["name"]):
        delete_tenant_resource(
            client,
            item["metadata"]["name"],
            wait=True,
            timeout=config["DELETE_TIMEOUT"],
        )


def reconcile_controller(
    root: Path,
    config: dict[str, str],
    client: ManagementClient,
    network: dict[str, object],
    verified_cache: VerifiedCache,
    registry: dict[str, object] | None,
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
    foundation = _foundation_payload(
        root,
        config,
        network,
        image,
        verified_cache,
        registry,
    )
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
    crd = client.kubectl("get", f"crd/{TENANT_CRD}", check=False)
    crd_present = crd.returncode == 0
    if crd.returncode != 0:
        output = f"{crd.stdout}{crd.stderr}".lower()
        if "notfound" not in output and "not found" not in output:
            raise RuntimeError(f"failed to inspect Tenant CRD before uninstall: {output}")
    if crd_present:
        client.kubectl(
            "delete",
            "tenants.tenancy.cnpg-vcluster.io",
            "--all",
            "--wait=true",
            f"--timeout={config['DELETE_TIMEOUT']}",
        )
        remaining = client.kubectl(
            "get",
            "tenants.tenancy.cnpg-vcluster.io",
            "-o",
            "name",
        ).stdout.strip()
        if remaining:
            raise RuntimeError(
                f"Tenant resources remain; refusing controller uninstall: {remaining}"
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
