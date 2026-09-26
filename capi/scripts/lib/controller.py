from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from scripts.lib.config import parse_duration
from scripts.lib.controller_cutover import (
    controller_lifecycle_epoch,
    delete_legacy_controller,
    delete_named,
    require_clean_controller_cutover,
    verify_legacy_webhook_absent,
)
from scripts.lib.controller_foundation import (
    canonical_hash as _foundation_checksum,
    foundation_payload as _foundation_payload,
)
from scripts.lib.files import ensure_private_dir, write_private_file
from scripts.lib.kube import ManagementClient, wait_for
from scripts.lib.process import run
from scripts.tools import verify_all_inputs

if TYPE_CHECKING:
    from scripts.cache import VerifiedCache


CONTROLLER_NAMESPACE = "tenant-system"
CONTROLLER_DEPLOYMENT = "tenant-controller"
TENANT_CRD = "tenants.tenancy.cnpg-vcluster.io"
CONTROLLER_LIFECYCLE_EPOCH = "rust-operator-v1"
STATIC_MANAGER_FLAGS = ("-C", "target-feature=+crt-static")


def controller_requires_cutover(installed_epoch: str | None) -> bool:
    return installed_epoch != CONTROLLER_LIFECYCLE_EPOCH


def cargo_environment(root: Path, *, offline: bool = True) -> dict[str, str]:
    root = root.resolve()
    environment = {
        key: value for key, value in os.environ.items()
        if key not in {
            "RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS", "RUSTC", "RUSTC_WRAPPER",
            "RUSTC_WORKSPACE_WRAPPER", "CARGO_BUILD_TARGET",
            "CARGO_BUILD_RUSTFLAGS", "CARGO_BUILD_RUSTC",
            "CARGO_BUILD_RUSTC_WRAPPER", "CARGO_BUILD_RUSTC_WORKSPACE_WRAPPER",
        } and not key.startswith(("CARGO_TARGET_", "CARGO_PROFILE_", "GO", "KUBEBUILDER_"))
    }
    for key, directory in (
        ("CARGO_HOME", root / ".tools" / "cargo-home"),
        ("CARGO_TARGET_DIR", root / ".tools" / "cargo-target"),
        ("TMPDIR", root / ".tools" / "cargo-work"),
    ):
        ensure_private_dir(directory)
        environment[key] = str(directory)
    environment["CARGO_NET_OFFLINE"] = str(
        offline or os.environ.get("CAPI_OFFLINE_ENFORCED") == "1"
    ).lower()
    environment["RUSTUP_AUTO_INSTALL"] = "0"
    return environment


def rust_toolchain(root: Path) -> tuple[str, dict[str, str], str]:
    environment = cargo_environment(root)
    binaries = {name: shutil.which(name) for name in ("rustc", "cargo")}
    if not all(binaries.values()):
        raise RuntimeError("installed rustc >= 1.89 and Cargo are required; no toolchain is downloaded")
    environment["RUSTC"] = binaries["rustc"]
    identities = [
        run(
            [binaries[name], "--version", "--verbose"],
            cwd=root / "controller", env=environment, timeout=30,
        ).stdout.strip()
        for name in ("rustc", "cargo")
    ]
    version = re.search(r"^rustc (\d+)\.(\d+)\.(\d+)", identities[0])
    if not version or tuple(map(int, version.groups())) < (1, 89, 0):
        raise RuntimeError(f"installed rustc >= 1.89 is required: {identities[0]}")
    return binaries["cargo"], environment, "\n".join(identities)


def fetch_controller_dependencies(
    root: Path, config: dict[str, str], *, offline: bool | None = None,
) -> None:
    cargo, environment, _ = rust_toolchain(root)
    if offline is None:
        offline = os.environ.get("CAPI_OFFLINE_ENFORCED") == "1"
    environment["CARGO_NET_OFFLINE"] = str(offline).lower()
    run(
        [cargo, "fetch", "--locked", *(["--offline"] if offline else [])],
        cwd=root / "controller", env=environment,
        timeout=parse_duration(config["COMMAND_TIMEOUT"]) * 8,
    )


def _cargo(root: Path, config: dict[str, str], arguments: list[str]) -> None:
    cargo, environment, _ = rust_toolchain(root)
    run(
        [cargo, *arguments], cwd=root / "controller", env=environment,
        timeout=parse_duration(config["COMMAND_TIMEOUT"]) * 8,
    )


def generate_controller(
    root: Path, config: dict[str, str], *, check: bool = False,
) -> None:
    fetch_controller_dependencies(root, config)
    _cargo(root, config, [
        "run", "--locked", "--offline", "--bin", "generate", "--",
        *(["--check"] if check else []),
    ])


def test_controller(root: Path, config: dict[str, str]) -> None:
    fetch_controller_dependencies(root, config)
    _cargo(root, config, ["test", "--locked", "--offline", "--all-targets", "--all-features"])


def vet_controller(root: Path, config: dict[str, str]) -> None:
    fetch_controller_dependencies(root, config)
    _cargo(root, config, ["fmt", "--all", "--check"])
    _cargo(root, config, [
        "clippy", "--locked", "--offline", "--all-targets", "--all-features",
        "--", "-D", "warnings",
    ])


def controller_source_digest(root: Path, config: dict[str, str]) -> str:
    digest = hashlib.sha256()
    controller = root / "controller"
    inputs = [
        *controller.joinpath("src").rglob("*.rs"),
        controller / "Cargo.toml",
        controller / "Cargo.lock",
        controller / "Dockerfile",
        *controller.joinpath("config", "crd").rglob("*.yaml"),
        *controller.joinpath("config", "rbac").rglob("*.yaml"),
        controller / "config" / "manager" / "manager.yaml.tpl",
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
    _, _, identity = rust_toolchain(root)
    digest.update(json.dumps({
        "compiler": identity,
        "command": ["cargo", "rustc", "--locked", "--offline", "--release",
                    "--bin", "manager", "--", *STATIC_MANAGER_FLAGS],
        "kubernetesVersion": config["KUBERNETES_VERSION"],
    }, sort_keys=True).encode())
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
    fetch_controller_dependencies(root, config)
    cargo, environment, _ = rust_toolchain(root)
    target = root.resolve() / ".tools" / "cargo-target" / "offline-verification"
    shutil.rmtree(target, ignore_errors=True)
    ensure_private_dir(target)
    environment["CARGO_TARGET_DIR"] = str(target)
    output = root / ".runtime" / "rendered" / "controller" / "manager"
    ensure_private_dir(output.parent)
    output.unlink(missing_ok=True)
    run(
        [
            cargo, "rustc", "--locked", "--offline", "--release", "--bin", "manager",
            "--", *STATIC_MANAGER_FLAGS,
        ],
        cwd=root / "controller",
        env=environment,
        timeout=parse_duration(config["COMMAND_TIMEOUT"]) * 8,
    )
    verify_static_manager(target / "release" / "manager")
    shutil.copy2(target / "release" / "manager", output)
    output.chmod(0o700)
    return output


def verify_static_manager(binary: Path) -> None:
    with binary.open("rb") as stream:
        if stream.read(4) != b"\x7fELF":
            raise RuntimeError("controller manager is not an ELF executable")
    headers = run(["readelf", "-lW", str(binary)], timeout=30).stdout
    dynamic = run(["readelf", "-dW", str(binary)], timeout=30).stdout
    if re.search(r"\bINTERP\b", headers) or re.search(r"\bNEEDED\b", dynamic):
        raise RuntimeError("controller manager must be static: ELF INTERP/NEEDED found")


def build_controller_image(
    root: Path,
    config: dict[str, str],
) -> str:
    verify_all_inputs(root, config)
    generate_controller(root, config, check=True)
    binary = build_controller_binary(root, config)
    verify_static_manager(binary)
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


def render_controller_manager(
    root: Path,
    config: dict[str, str],
    image: str,
    *,
    mutation_enabled: bool,
) -> Path:
    template = (
        root / "controller" / "config" / "manager" / "manager.yaml.tpl"
    ).read_text(encoding="utf-8")
    rendered = (
        template.replace("${TENANT_CONTROLLER_IMAGE}", image)
        .replace(
            "${SUPPORTED_KUBERNETES_VERSION}",
            config["KUBERNETES_VERSION"].removeprefix("v"),
        )
        .replace("${CONTROLLER_LIFECYCLE_EPOCH}", CONTROLLER_LIFECYCLE_EPOCH)
        .replace(
            "${CONTROLLER_MUTATION_ENABLED}",
            "true" if mutation_enabled else "false",
        )
    )
    destination = root / ".runtime" / "rendered" / "controller" / "manager.yaml"
    write_private_file(destination, rendered)
    return destination


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
    if data.get("schema") != 3 or foundation["data"].get(
        "foundation.sha256"
    ) != _foundation_checksum(data):
        raise RuntimeError("controller mutation requires a verified schema-3 foundation")
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
    verify_running_controller(client, data["controllerImage"], mutation_enabled=enabled)
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
    tenants = json.loads(response.stdout)
    if not isinstance(tenants.get("items"), list):
        raise RuntimeError("failed to inspect controller Tenant inventory for teardown")
    names = sorted(
        item["metadata"]["name"] for item in tenants.get("items", [])
    )
    for name in names:
        delete_tenant_resource(
            client,
            name,
            wait=False,
        )
    for name in names:
        client.kubectl(
            "wait",
            "--for=delete",
            f"tenant/{name}",
            f"--timeout={config['DELETE_TIMEOUT']}",
        )


def stop_controller_for_cutover(
    config: dict[str, str],
    client: ManagementClient,
) -> None:
    deployment = client.kubectl(
        "-n",
        CONTROLLER_NAMESPACE,
        "get",
        f"deployment/{CONTROLLER_DEPLOYMENT}",
        check=False,
    )
    if deployment.returncode != 0:
        output = f"{deployment.stdout}{deployment.stderr}".lower()
        if "notfound" not in output and "not found" not in output:
            raise RuntimeError(
                f"failed to inspect Tenant controller before cutover: {deployment.stderr}"
            )
    else:
        client.kubectl(
            "-n",
            CONTROLLER_NAMESPACE,
            "scale",
            f"deployment/{CONTROLLER_DEPLOYMENT}",
            "--replicas=0",
        )

    def old_pods_absent() -> bool | None:
        pods = client.json(
            "-n",
            CONTROLLER_NAMESPACE,
            "get",
            "pods",
            "-l",
            "app.kubernetes.io/name=tenant-controller",
        )
        if not isinstance(pods.get("items"), list):
            raise RuntimeError("failed to inspect old Tenant controller Pod inventory")
        return True if not pods["items"] else None

    wait_for(
        "old Tenant controller Pods to terminate",
        parse_duration(config["CONDITION_TIMEOUT"]),
        2,
        old_pods_absent,
    )


def verify_running_controller_epoch(
    client: ManagementClient,
    expected: str,
) -> None:
    deployment_epoch = controller_lifecycle_epoch(client)
    if deployment_epoch != expected:
        raise RuntimeError(
            "Tenant controller Deployment lifecycle epoch mismatch: "
            f"expected {expected}, observed {deployment_epoch or '<missing>'}"
        )
    pods = client.json(
        "-n",
        CONTROLLER_NAMESPACE,
        "get",
        "pods",
        "-l",
        "app.kubernetes.io/name=tenant-controller",
    ).get("items", [])
    if not pods:
        raise RuntimeError("Tenant controller has no running Pod")
    prefix = "--lifecycle-epoch="
    for pod in pods:
        manager = next(
            (
                container
                for container in pod.get("spec", {}).get("containers", [])
                if container.get("name") == "manager"
            ),
            None,
        )
        if manager is None:
            raise RuntimeError("Tenant controller Pod manager container is missing")
        epochs = [
            value.removeprefix(prefix)
            for value in manager.get("args", [])
            if value.startswith(prefix)
        ]
        if epochs != [expected]:
            name = pod.get("metadata", {}).get("name", "<unknown>")
            raise RuntimeError(
                f"Tenant controller Pod {name} lifecycle epoch mismatch"
            )


def verify_running_controller(
    client: ManagementClient, image: str, *, mutation_enabled: bool,
) -> None:
    verify_running_controller_epoch(client, CONTROLLER_LIFECYCLE_EPOCH)
    deployment = client.json(
        "-n", CONTROLLER_NAMESPACE, "get", f"deployment/{CONTROLLER_DEPLOYMENT}",
    )
    spec, status = deployment["spec"], deployment.get("status", {})
    if (
        spec.get("replicas") != 1 or spec.get("strategy", {}).get("type") != "Recreate"
        or status.get("readyReplicas") != 1 or status.get("updatedReplicas") != 1
        or status.get("observedGeneration") != deployment["metadata"]["generation"]
    ):
        raise RuntimeError("Tenant controller must have one ready Recreate replica")
    pods = client.json(
        "-n", CONTROLLER_NAMESPACE, "get", "pods",
        "-l", "app.kubernetes.io/name=tenant-controller",
    )["items"]
    if len(pods) != 1 or pods[0]["metadata"].get("deletionTimestamp"):
        raise RuntimeError("Tenant controller must have exactly one non-terminating Pod")
    for pod_spec in (spec["template"]["spec"], pods[0]["spec"]):
        manager = next(item for item in pod_spec["containers"] if item["name"] == "manager")
        args = manager.get("args", [])
        expected = {
            "--leader-elect": "true",
            "--mutation-enabled": str(mutation_enabled).lower(),
            "--controller-image": image,
            "--lifecycle-epoch": CONTROLLER_LIFECYCLE_EPOCH,
        }
        if manager["image"] != image or any(
            [arg for arg in args if arg.startswith(f"{key}=")] != [f"{key}={value}"]
            for key, value in expected.items()
        ):
            raise RuntimeError("Tenant controller image or runtime arguments do not match")
    pod = pods[0]
    if not any(
        condition.get("type") == "Ready" and condition.get("status") == "True"
        for condition in pod.get("status", {}).get("conditions", [])
    ):
        raise RuntimeError("Tenant controller Pod is not Ready")
    for endpoint in ("healthz", "readyz"):
        client.kubectl(
            "get", "--raw",
            f"/api/v1/namespaces/{CONTROLLER_NAMESPACE}/pods/"
            f"{pod['metadata']['name']}:8081/proxy/{endpoint}",
        )


def verify_controller_crd(client: ManagementClient) -> None:
    crd = client.json("get", f"crd/{TENANT_CRD}")
    versions = crd["spec"].get("versions", [])
    if (
        len(versions) != 1 or versions[0].get("name") != "v1alpha2"
        or versions[0].get("served") is not True or versions[0].get("storage") is not True
        or crd.get("status", {}).get("storedVersions") != ["v1alpha2"]
        or "status" not in versions[0].get("subresources", {})
        or crd["spec"].get("conversion", {}).get("strategy", "None") != "None"
    ):
        raise RuntimeError("Tenant CRD must serve and store only v1alpha2 with a status subresource")


def verify_controller_api(config: dict[str, str], client: ManagementClient) -> None:
    name = f"contract-probe-{uuid.uuid4().hex[:12]}"
    probe = {
        "apiVersion": "tenancy.cnpg-vcluster.io/v1alpha2",
        "kind": "Tenant",
        "metadata": {"name": name},
        "spec": {
            "kubernetesVersion": config["KUBERNETES_VERSION"].removeprefix("v"),
            "workers": 1, "databases": 1,
        },
    }

    def create_dry(document, mode="strict", rejected=None):
        result = client.kubectl(
            "create", "--dry-run=server", f"--validate={mode}", "-f", "-", "-o", "json",
            input_text=json.dumps(document), check=False,
        )
        if rejected:
            if (
                result.returncode == 0 or rejected not in result.stderr
                or (rejected != "unknown field" and not re.search(
                    r"\binvalid\b", result.stderr, re.IGNORECASE,
                ))
            ):
                raise RuntimeError(f"Tenant API did not reject {rejected}: {result.stderr}")
            return None
        if result.returncode != 0:
            raise RuntimeError(f"Tenant API dry-run failed: {result.stderr}")
        value = json.loads(result.stdout)
        if value["spec"] != probe["spec"]:
            raise RuntimeError("Tenant API did not prune unknown spec fields")
        if mode == "warn" and "unknown field" not in result.stderr:
            raise RuntimeError("Tenant API Warn did not report the unknown field")
        if mode == "ignore" and "unknown field" in result.stderr:
            raise RuntimeError("Tenant API Ignore unexpectedly warned")
        return value

    create_dry(probe)
    unknown = {**probe, "spec": {**probe["spec"], "unexpected": True}}
    for mode in ("warn", "ignore"):
        create_dry(unknown, mode)
    create_dry(unknown, rejected="unknown field")
    for field, value in (("workers", 0), ("databases", 4), ("kubernetesVersion", "bad")):
        create_dry({**probe, "spec": {**probe["spec"], field: value}}, rejected=field)
    create_dry({**probe, "metadata": {"name": "invalid.name"}}, rejected="Tenant name")

    response = client.kubectl(
        "create", "--validate=strict", "-f", "-", "-o", "json",
        input_text=json.dumps({**probe, "status": {"phase": "Ready"}}),
    )
    try:
        created = json.loads(response.stdout)
        if created.get("status"):
            raise RuntimeError("Tenant create must ignore user-supplied status")
        for field, value in (
            ("workers", 2), ("databases", 2),
            ("kubernetesVersion", "0.0.0" if probe["spec"]["kubernetesVersion"] != "0.0.0" else "1.0.0"),
        ):
            result = client.kubectl(
                "patch", f"tenant/{name}", "--type=merge", "--dry-run=server",
                "-p", json.dumps({"spec": {field: value}}), check=False,
            )
            if result.returncode == 0 or "Tenant spec is immutable" not in result.stderr:
                raise RuntimeError("Tenant CEL spec immutability gate failed")
        response = client.kubectl(
            "patch", f"tenant/{name}", "--type=merge", "--dry-run=server",
            "-p", json.dumps({"status": {"phase": "Ready"}}), "-o", "json",
        )
        if json.loads(response.stdout).get("status", {}).get("phase") == "Ready":
            raise RuntimeError("Tenant main resource unexpectedly allows status writes")
        response = client.kubectl(
            "patch", f"tenant/{name}", "--subresource=status", "--type=merge",
            "--dry-run=server", "-p",
            json.dumps({"spec": {"workers": 2}, "status": {"phase": "Ready"}}),
            "-o", "json",
        )
        result = json.loads(response.stdout)
        if result.get("status", {}).get("phase") != "Ready" or result["spec"] != probe["spec"]:
            raise RuntimeError("Tenant status subresource isolation gate failed")
    finally:
        delete_named(config, client, None, f"tenant/{name}")


def verify_controller_image(
    config: dict[str, str], client: ManagementClient, image: str,
) -> None:
    name = f"tenant-controller-probe-{uuid.uuid4().hex[:8]}"
    job = {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": name, "namespace": CONTROLLER_NAMESPACE},
        "spec": {
            "backoffLimit": 0, "activeDeadlineSeconds": parse_duration(config["CONDITION_TIMEOUT"]),
            "template": {"spec": {
                "restartPolicy": "Never", "serviceAccountName": CONTROLLER_DEPLOYMENT,
                "containers": [{
                    "name": "probe", "image": image, "imagePullPolicy": "Never",
                    "args": ["--probe-in-cluster"],
                    "env": [{"name": "KUBERNETES_SERVICE_HOST", "value": "kubernetes.default.svc"}],
                }],
            }},
        },
    }
    client.kubectl("create", "-f", "-", input_text=json.dumps(job))
    try:
        client.kubectl(
            "-n", CONTROLLER_NAMESPACE, "wait", "--for=condition=Complete",
            f"job/{name}", f"--timeout={config['CONDITION_TIMEOUT']}",
        )
    finally:
        delete_named(config, client, CONTROLLER_NAMESPACE, f"job/{name}")


def reconcile_controller(
    root: Path,
    config: dict[str, str],
    client: ManagementClient,
    network: dict[str, object],
    verified_cache: VerifiedCache,
    registry: dict[str, object] | None,
) -> None:
    installed_epoch = controller_lifecycle_epoch(client)
    requires_cutover = controller_requires_cutover(installed_epoch)
    image = build_controller_image(root, config)
    enabled_foundation = _foundation_payload(
        root, config, network, image, verified_cache, registry, mutation_enabled=True,
    )
    desired_data = enabled_foundation["data"]
    desired_raw = json.loads(desired_data["foundation.json"])
    desired_hash = _foundation_checksum(desired_raw)
    if desired_raw.get("schema") != 3 or desired_data["foundation.sha256"] != desired_hash:
        raise RuntimeError("generated controller foundation is invalid")
    current = client.kubectl(
        "-n", CONTROLLER_NAMESPACE, "get", "configmap/tenant-foundation",
        "--ignore-not-found=true", "-o", "json",
    )
    current_data = None
    foundation_matches = False
    if current.stdout.strip():
        try:
            current_data = json.loads(current.stdout)["data"]
            encoded = current_data["foundation.json"]
            raw = json.loads(encoded)
            foundation_matches = (
                isinstance(raw, dict)
                and raw.get("schema") == 3
                and set(raw) == set(desired_raw)
                and isinstance(raw.get("controllerImage"), str)
                and bool(raw["controllerImage"])
                and isinstance(raw.get("mutationEnabled"), bool)
                and current_data.get("foundation.sha256") == _foundation_checksum(raw)
                and current_data["foundation.sha256"] == desired_hash
            )
        except (KeyError, TypeError, ValueError):
            current_data = None
    if requires_cutover or not foundation_matches:
        require_clean_controller_cutover(root, config, client)
    reuse_enabled = False
    if not requires_cutover and current_data == desired_data:
        deployment = client.json(
            "-n", CONTROLLER_NAMESPACE, "get", f"deployment/{CONTROLLER_DEPLOYMENT}",
        )
        manager = next(
            (item for item in deployment["spec"]["template"]["spec"]["containers"]
             if item["name"] == "manager"), {},
        )
        reuse_enabled = (
            manager.get("image") == image
            and [arg for arg in manager.get("args", []) if arg.startswith("--mutation-enabled=")]
            == ["--mutation-enabled=true"]
        )
    if requires_cutover:
        stop_controller_for_cutover(config, client)
        delete_legacy_controller(root, config, client)
    verify_legacy_webhook_absent(client)
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
    manager = render_controller_manager(
        root,
        config,
        image,
        mutation_enabled=reuse_enabled,
    )
    paths = (
        root / "controller" / "config" / "namespace" / "namespace.yaml",
        root / "controller" / "config" / "crd" / "bases",
        root / "controller" / "config" / "rbac" / "role.yaml",
        root / "controller" / "config" / "rbac" / "service-account.yaml",
        root / "controller" / "config" / "rbac" / "role-binding.yaml",
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
    timeout = config["CONDITION_TIMEOUT"]
    client.kubectl(
        "wait",
        "--for=condition=Established",
        f"crd/{TENANT_CRD}",
        f"--timeout={timeout}",
    )
    verify_controller_crd(client)
    client.kubectl(
        "apply", "--server-side", "--field-manager=cnpg-vcluster-controller",
        "--force-conflicts", "-f", str(manager),
    )
    client.kubectl(
        "rollout",
        "status",
        f"deployment/{CONTROLLER_DEPLOYMENT}",
        "-n",
        CONTROLLER_NAMESPACE,
        f"--timeout={timeout}",
    )
    verify_running_controller(client, image, mutation_enabled=reuse_enabled)
    verify_controller_image(config, client, image)
    if not reuse_enabled:
        verify_controller_api(config, client)
    foundation = enabled_foundation if reuse_enabled else _foundation_payload(
        root, config, network, image, verified_cache, registry, mutation_enabled=False,
    )
    client.kubectl(
        "apply", "--server-side", "--field-manager=cnpg-vcluster-controller",
        "--force-conflicts", "-f", "-", input_text=json.dumps(foundation),
    )
    verify_controller_crd(client)
    verify_legacy_webhook_absent(client)
    if not reuse_enabled:
        set_controller_mutation(config, client, enabled=True)
    render_controller_manager(root, config, image, mutation_enabled=True)


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
    stop_controller_for_cutover(config, client)
    require_clean_controller_cutover(root, config, client)
    delete_legacy_controller(root, config, client)
    for namespace, resource in (
        ("tenant-system", "configmap/tenant-foundation"),
        ("tenant-system", "lease/tenant-controller.tenancy.cnpg-vcluster.io"),
        (None, "clusterrolebinding/tenant-controller"),
        (None, "clusterrole/tenant-controller-role"),
        ("tenant-system", "serviceaccount/tenant-controller"),
        (None, "namespace/tenant-system"),
    ):
        delete_named(config, client, namespace, resource)
    shutil.rmtree(
        root / ".runtime" / "rendered" / "controller",
        ignore_errors=True,
    )
