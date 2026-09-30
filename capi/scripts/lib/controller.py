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
from scripts.lib.controller_state import (
    activation_ticket,
    delete_named,
    require_clean_controller_state,
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
TENANT_CUTOVER_POLICY = "tenant-api-cutover-create-lock"
TENANT_API_CUTOVER_READY = True
STATIC_MANAGER_FLAGS = ("-C", "target-feature=+crt-static")


def tenant_cutover_lock_documents() -> list[dict[str, object]]:
    policy = {
        "apiVersion": "admissionregistration.k8s.io/v1",
        "kind": "ValidatingAdmissionPolicy",
        "metadata": {"name": TENANT_CUTOVER_POLICY},
        "spec": {
            "failurePolicy": "Fail",
            "matchConstraints": {
                "resourceRules": [
                    {
                        "apiGroups": ["tenancy.cnpg-vcluster.io"],
                        "apiVersions": ["v1alpha2", "v1alpha3"],
                        "operations": ["CREATE"],
                        "resources": ["tenants"],
                        "scope": "Cluster",
                    }
                ]
            },
            "validations": [
                {
                    "expression": "false",
                    "message": "Tenant creation is locked during API cutover",
                }
            ],
        },
    }
    binding = {
        "apiVersion": "admissionregistration.k8s.io/v1",
        "kind": "ValidatingAdmissionPolicyBinding",
        "metadata": {"name": TENANT_CUTOVER_POLICY},
        "spec": {
            "policyName": TENANT_CUTOVER_POLICY,
            "validationActions": ["Deny"],
        },
    }
    return [policy, binding]


def tenant_cutover_lock_cleanup_refs() -> tuple[str, str]:
    return (
        f"validatingadmissionpolicybinding/{TENANT_CUTOVER_POLICY}",
        f"validatingadmissionpolicy/{TENANT_CUTOVER_POLICY}",
    )


def require_empty_tenant_cutover(
    tenants: object,
    provider_residue: list[str],
) -> None:
    items = tenants.get("items") if isinstance(tenants, dict) else None
    if not isinstance(items, list):
        raise RuntimeError("Tenant cutover inventory is invalid")
    if items:
        raise RuntimeError("retained Tenants block Tenant API cutover")
    if provider_residue:
        raise RuntimeError(
            "provider residue blocks Tenant API cutover: "
            + ", ".join(sorted(provider_residue))
        )


def tenant_api_generation(crd: object) -> str:
    if not isinstance(crd, dict):
        raise RuntimeError("Tenant CRD inventory is invalid")
    versions = crd.get("spec", {}).get("versions")
    stored = crd.get("status", {}).get("storedVersions")
    if not isinstance(versions, list) or len(versions) != 1:
        raise RuntimeError("Tenant CRD version inventory is invalid")
    version = versions[0]
    name = version.get("name") if isinstance(version, dict) else None
    if (
        name not in {"v1alpha2", "v1alpha3"}
        or version.get("served") is not True
        or version.get("storage") is not True
        or stored != [name]
    ):
        raise RuntimeError("Tenant CRD generation is inconsistent")
    return name


def require_tenant_cutover_double_check(
    first_tenants: object,
    second_tenants: object,
    provider_residue: list[str],
) -> None:
    require_empty_tenant_cutover(first_tenants, provider_residue)
    require_empty_tenant_cutover(second_tenants, provider_residue)


def tenant_api_cutover_state(crd: object) -> str:
    if not isinstance(crd, dict):
        raise RuntimeError("Tenant CRD inventory is invalid")
    versions = crd.get("spec", {}).get("versions")
    stored = crd.get("status", {}).get("storedVersions")
    if not isinstance(versions, list) or not isinstance(stored, list):
        raise RuntimeError("Tenant CRD version inventory is invalid")
    names = {
        version.get("name")
        for version in versions
        if isinstance(version, dict)
    }
    if names == {"v1alpha2", "v1alpha3"} and stored in (
        ["v1alpha2"],
        ["v1alpha2", "v1alpha3"],
        ["v1alpha3"],
    ):
        return "transitioning"
    return tenant_api_generation(crd)


def tenant_crd_transition_document(
    observed: dict[str, object],
    desired: dict[str, object],
) -> dict[str, object]:
    if tenant_api_generation(observed) != "v1alpha2":
        raise RuntimeError("Tenant CRD transition source is invalid")
    if tenant_api_generation({
        **desired,
        "status": {"storedVersions": ["v1alpha3"]},
    }) != "v1alpha3":
        raise RuntimeError("Tenant CRD transition target is invalid")
    old = json.loads(json.dumps(observed["spec"]["versions"][0]))
    old["served"] = False
    old["storage"] = False
    transition = json.loads(json.dumps(desired))
    transition["spec"]["versions"] = [
        old,
        transition["spec"]["versions"][0],
    ]
    return transition


def desired_tenant_crd(
    root: Path,
    client: ManagementClient,
) -> dict[str, object]:
    rendered = client.kubectl(
        "create",
        "--dry-run=client",
        "-f",
        str(
            root
            / "controller/config/crd/bases"
            / "tenancy.cnpg-vcluster.io_tenants.yaml"
        ),
        "-o",
        "json",
    ).stdout
    try:
        document = json.loads(rendered)
    except json.JSONDecodeError as exc:
        raise RuntimeError("generated Tenant CRD is invalid") from exc
    if not isinstance(document, dict):
        raise RuntimeError("generated Tenant CRD is invalid")
    return document


def require_tenant_api_cutover_ready() -> None:
    if not TENANT_API_CUTOVER_READY:
        raise RuntimeError(
            "Tenant API v1alpha3 activation is blocked until the Azure allocation lifecycle is complete"
        )


def apply_tenant_cutover_lock(
    config: dict[str, str],
    client: ManagementClient,
) -> None:
    try:
        for document in tenant_cutover_lock_documents():
            client.kubectl(
                "apply",
                "--server-side",
                "--field-manager=cnpg-vcluster-tenant-api-cutover",
                "--force-conflicts",
                "-f",
                "-",
                input_text=json.dumps(document),
            )
    except Exception as failure:
        try:
            remove_tenant_cutover_lock(config, client)
        except Exception as cleanup:
            failure.add_note(f"partial Tenant cutover lock cleanup failed: {cleanup}")
        raise


def tenant_cutover_lock_present(client: ManagementClient) -> bool:
    present = []
    for resource in tenant_cutover_lock_cleanup_refs():
        present.append(bool(client.kubectl(
            "get", resource, "--ignore-not-found=true", "-o", "name",
        ).stdout.strip()))
    if present[0] != present[1]:
        raise RuntimeError("Tenant cutover lock is partially installed")
    return present[0]


def verify_tenant_cutover_lock(client: ManagementClient, generation: str) -> None:
    document = {
        "apiVersion": f"tenancy.cnpg-vcluster.io/{generation}",
        "kind": "Tenant",
        "metadata": {"name": f"cutover-lock-probe-{uuid.uuid4().hex[:12]}"},
        "spec": {
            "kubernetesVersion": "1.36.4",
            "workers": 1,
            "provider": {"type": "local", "databases": 1},
        },
    }
    for _ in range(5):
        response = client.kubectl(
            "create", "--dry-run=server", "-f", "-",
            input_text=json.dumps(document), check=False,
        )
        if (
            response.returncode == 0
            or "Tenant creation is locked during API cutover"
            not in response.stderr
        ):
            raise RuntimeError("Tenant cutover create lock is not effective")


def remove_tenant_cutover_lock(config: dict[str, str], client: ManagementClient) -> None:
    for resource in tenant_cutover_lock_cleanup_refs():
        client.kubectl(
            "delete",
            resource,
            "--ignore-not-found=true",
            "--wait=true",
            f"--timeout={config['DELETE_TIMEOUT']}",
        )


def prepare_tenant_api_cutover(
    root: Path,
    config: dict[str, str],
    client: ManagementClient,
) -> bool:
    observed = client.kubectl(
        "get",
        f"crd/{TENANT_CRD}",
        "--ignore-not-found=true",
        "-o",
        "json",
    ).stdout.strip()
    if not observed:
        return tenant_cutover_lock_present(client)
    current = json.loads(observed)
    generation = tenant_api_cutover_state(current)
    if generation == "v1alpha3":
        return tenant_cutover_lock_present(client)
    transition = generation == "transitioning"
    if transition:
        if not tenant_cutover_lock_present(client):
            raise RuntimeError("Tenant CRD transition is missing its create lock")
    else:
        apply_tenant_cutover_lock(config, client)
    desired = desired_tenant_crd(root, client)
    try:
        verify_tenant_cutover_lock(
            client,
            "v1alpha3" if transition else generation,
        )
        if not transition:
            require_clean_controller_state(root, client)
        stop_controller(config, client)
        require_clean_controller_state(root, client)
        if not transition:
            client.kubectl(
                "apply",
                "--server-side",
                "--field-manager=cnpg-vcluster-tenant-api-cutover",
                "--force-conflicts",
                "-f",
                "-",
                input_text=json.dumps(
                    tenant_crd_transition_document(current, desired)
                ),
            )
            transition = True
        verify_tenant_cutover_lock(client, "v1alpha3")
        require_clean_controller_state(root, client)
        client.kubectl(
            "patch",
            f"crd/{TENANT_CRD}",
            "--subresource=status",
            "--type=merge",
            "-p",
            json.dumps({"status": {"storedVersions": ["v1alpha3"]}}),
        )
        client.kubectl(
            "apply",
            "--server-side",
            "--field-manager=cnpg-vcluster-controller",
            "--force-conflicts",
            "-f",
            "-",
            input_text=json.dumps(desired),
        )
        require_clean_controller_state(root, client)
        return True
    except Exception:
        if not transition:
            remove_tenant_cutover_lock(config, client)
        raise


def rust_toolchain(root: Path) -> tuple[str, str]:
    binaries = {name: shutil.which(name) for name in ("rustc", "cargo")}
    if not all(binaries.values()):
        raise RuntimeError("rustc >= 1.89 and Cargo are required")
    identities = [
        run(
            [binaries[name], "--version", "--verbose"],
            cwd=root / "controller", timeout=30,
        ).stdout.strip()
        for name in ("rustc", "cargo")
    ]
    version = re.search(r"^rustc (\d+)\.(\d+)\.(\d+)", identities[0])
    if not version or tuple(map(int, version.groups())) < (1, 89, 0):
        raise RuntimeError(f"installed rustc >= 1.89 is required: {identities[0]}")
    return binaries["cargo"], "\n".join(identities)


def fetch_controller_dependencies(
    root: Path, config: dict[str, str], *, offline: bool | None = None,
) -> None:
    cargo, _ = rust_toolchain(root)
    if offline is None:
        offline = os.environ.get("CAPI_OFFLINE_ENFORCED") == "1"
    run(
        [cargo, "fetch", "--locked", *(["--offline"] if offline else [])],
        cwd=root / "controller",
        timeout=parse_duration(config["COMMAND_TIMEOUT"]) * 8,
    )


def _cargo(root: Path, config: dict[str, str], arguments: list[str]) -> None:
    cargo, _ = rust_toolchain(root)
    run(
        [cargo, *arguments], cwd=root / "controller",
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
    repository = root.parent
    inputs = [
        *controller.joinpath("src").rglob("*.rs"),
        repository / "Cargo.toml",
        repository / "Cargo.lock",
        repository / "rust-toolchain.toml",
        controller / "Cargo.toml",
        controller / "Dockerfile",
        *controller.joinpath("config", "crd").rglob("*.yaml"),
        *controller.joinpath("config", "rbac").rglob("*.yaml"),
        controller / "config" / "manager" / "manager.yaml.tpl",
        root / ".tools" / "inputs" / "calico.yaml",
        root / ".tools" / "inputs" / "cnpg.yaml",
    ]
    for path in sorted(inputs):
        relative = path.relative_to(
            root if path.is_relative_to(root) else repository
        ).as_posix()
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    _, identity = rust_toolchain(root)
    digest.update(json.dumps({
        "compiler": identity,
        "command": [
            "cargo", "rustc", "--locked", "--offline", "--release",
            "--bin", "manager", "--message-format=json-render-diagnostics",
            "--", *STATIC_MANAGER_FLAGS,
        ],
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
    output = root / ".runtime" / "rendered" / "controller" / "manager"
    ensure_private_dir(output.parent)
    output.unlink(missing_ok=True)
    prebuilt = os.environ.get("CAPI_PREBUILT_CONTROLLER_BINARY")
    if prebuilt:
        source = Path(prebuilt).resolve()
        expected_root = (root / ".tools" / "artifacts").resolve()
        if (
            not source.is_file()
            or not source.is_relative_to(expected_root)
        ):
            raise RuntimeError(
                "configured prebuilt controller binary is missing or outside .tools/artifacts"
            )
        verify_static_manager(source)
        shutil.copy2(source, output)
        output.chmod(0o700)
        return output
    fetch_controller_dependencies(root, config)
    cargo, _ = rust_toolchain(root)
    result = run(
        [
            cargo, "rustc", "--locked", "--offline", "--release", "--bin", "manager",
            "--message-format=json-render-diagnostics",
            "--", *STATIC_MANAGER_FLAGS,
        ],
        cwd=root / "controller",
        timeout=parse_duration(config["COMMAND_TIMEOUT"]) * 8,
    )
    executables = [
        Path(message["executable"])
        for line in result.stdout.splitlines()
        for message in [json.loads(line)]
        if (
            message.get("reason") == "compiler-artifact"
            and message.get("target", {}).get("name") == "manager"
            and isinstance(message.get("executable"), str)
        )
    ]
    if len(executables) != 1 or not executables[0].is_file():
        raise RuntimeError("Cargo did not report exactly one manager executable")
    verify_static_manager(executables[0])
    shutil.copy2(executables[0], output)
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


def build_azure_controller_image(
    root: Path,
    config: dict[str, str],
    image: str,
) -> str:
    generate_controller(root, config, check=True)
    binary = build_controller_binary(root, config)
    verify_static_manager(binary)
    build_root = root / ".runtime" / "rendered" / "azure-controller-build"
    shutil.rmtree(build_root, ignore_errors=True)
    ensure_private_dir(build_root)
    try:
        shutil.copy2(
            root / "controller" / "Dockerfile.azure",
            build_root / "Dockerfile",
        )
        shutil.copy2(binary, build_root / "manager")
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
    activation_token: str,
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
        .replace("${CONTROLLER_ACTIVATION_TOKEN}", activation_token)
    )
    destination = root / ".runtime" / "rendered" / "controller" / "manager.yaml"
    write_private_file(destination, rendered)
    return destination


def render_azure_controller_manager(
    root: Path,
    supported_kubernetes_version: str,
    image: str,
) -> Path:
    template = (
        root / "controller" / "config" / "manager" / "manager-azure.yaml.tpl"
    ).read_text(encoding="utf-8")
    rendered = (
        template.replace("${TENANT_CONTROLLER_IMAGE}", image)
        .replace(
            "${SUPPORTED_KUBERNETES_VERSION}",
            supported_kubernetes_version.removeprefix("v"),
        )
    )
    destination = (
        root / ".runtime" / "rendered" / "azure-controller" / "manager.yaml"
    )
    write_private_file(destination, rendered)
    return destination


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


def stop_controller(
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
                f"failed to inspect Tenant controller before shutdown: {deployment.stderr}"
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


def verify_running_controller(
    client: ManagementClient, image: str, *, activation_token: str,
) -> None:
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
            "--controller-image": image,
            "--activation-token": activation_token,
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
        len(versions) != 1 or versions[0].get("name") != "v1alpha3"
        or versions[0].get("served") is not True or versions[0].get("storage") is not True
        or crd.get("status", {}).get("storedVersions") != ["v1alpha3"]
        or "status" not in versions[0].get("subresources", {})
        or crd["spec"].get("conversion", {}).get("strategy", "None") != "None"
    ):
        raise RuntimeError("Tenant CRD must serve and store only v1alpha3 with a status subresource")


def verify_controller_api(
    config: dict[str, str],
    client: ManagementClient,
    *,
    require_allocation: bool = False,
) -> None:
    name = f"contract-probe-{uuid.uuid4().hex[:12]}"
    probe = {
        "apiVersion": "tenancy.cnpg-vcluster.io/v1alpha3",
        "kind": "Tenant",
        "metadata": {"name": name},
        "spec": {
            "kubernetesVersion": config["KUBERNETES_VERSION"].removeprefix("v"),
            "workers": 1,
            "provider": {"type": "local", "databases": 1},
        },
    }

    def create_dry(document, mode="strict", rejected=None, expected_spec=None):
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
        if value["spec"] != (expected_spec or probe["spec"]):
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
    invalid_specs = (
        ("workers", {**probe["spec"], "workers": 0}),
        (
            "databases",
            {
                **probe["spec"],
                "provider": {**probe["spec"]["provider"], "databases": 4},
            },
        ),
        ("kubernetesVersion", {**probe["spec"], "kubernetesVersion": "bad"}),
    )
    for field, invalid_spec in invalid_specs:
        create_dry({**probe, "spec": invalid_spec}, rejected=field)
    create_dry({**probe, "metadata": {"name": "invalid.name"}}, rejected="Tenant name")
    azure_spec = {
        "kubernetesVersion": probe["spec"]["kubernetesVersion"],
        "workers": 3,
        "provider": {"type": "azure"},
    }
    create_dry({**probe, "spec": azure_spec}, expected_spec=azure_spec)
    for invalid_provider, rejected in (
        ({"type": "azure", "databases": 1}, "databases"),
        ({"type": "azure", "podCIDR": "10.244.0.0/16"}, "unknown field"),
        ({"type": "azure", "serviceCIDR": "10.96.0.0/16"}, "unknown field"),
    ):
        create_dry(
            {**probe, "spec": {**azure_spec, "provider": invalid_provider}},
            rejected=rejected,
        )

    response = client.kubectl(
        "create", "--validate=strict", "-f", "-", "-o", "json",
        input_text=json.dumps({**probe, "status": {"phase": "Ready"}}),
    )
    try:
        created = json.loads(response.stdout)
        if created.get("status"):
            raise RuntimeError("Tenant create must ignore user-supplied status")
        if require_allocation:
            def allocation_ready():
                observed = client.json("get", f"tenant/{name}")
                allocation = (
                    observed.get("status", {})
                    .get("provider", {})
                    .get("allocation")
                )
                return True if isinstance(allocation, dict) and allocation.get("slotId") else None

            wait_for(
                "Tenant API cutover allocation probe",
                parse_duration(config["CONDITION_TIMEOUT"]),
                1,
                allocation_ready,
            )
        for patch in (
            {"workers": 2},
            {"provider": {"databases": 2}},
            {
                "kubernetesVersion": (
                    "0.0.0"
                    if probe["spec"]["kubernetesVersion"] != "0.0.0"
                    else "1.0.0"
                )
            },
        ):
            result = client.kubectl(
                "patch", f"tenant/{name}", "--type=merge", "--dry-run=server",
                "-p", json.dumps({"spec": patch}), check=False,
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
    require_tenant_api_cutover_ready()
    image = build_controller_image(root, config)
    foundation = _foundation_payload(
        root, config, network, image, verified_cache, registry,
    )
    desired_data = foundation["data"]
    desired_raw = json.loads(desired_data["foundation.json"])
    desired_hash = _foundation_checksum(desired_raw)
    if desired_raw.get("schema") != 3 or desired_data["foundation.sha256"] != desired_hash:
        raise RuntimeError("generated controller foundation is invalid")
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
    cutover_locked = prepare_tenant_api_cutover(root, config, client)
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
    previous = {
        name: client.kubectl(
            "-n",
            CONTROLLER_NAMESPACE,
            "get",
            name,
            "--ignore-not-found=true",
            "-o",
            "json",
        ).stdout.strip()
        for name in (
            "configmap/tenant-foundation",
            "configmap/tenant-controller-state",
            f"deployment/{CONTROLLER_DEPLOYMENT}",
        )
    }
    accepted_hash = None
    if previous["configmap/tenant-controller-state"]:
        accepted_hash = json.loads(
            previous["configmap/tenant-controller-state"]
        ).get("data", {}).get("configurationHash")
    replacement = accepted_hash != desired_hash
    token = uuid.uuid4().hex if replacement else ""
    if replacement:
        require_clean_controller_state(root, client)
    try:
        if replacement:
            stop_controller(config, client)
            require_clean_controller_state(root, client)
            if previous["configmap/tenant-controller-state"]:
                previous_state = json.loads(
                    previous["configmap/tenant-controller-state"]
                )
                if "rollbackToken" in previous_state.get("data", {}):
                    client.kubectl(
                        "-n",
                        CONTROLLER_NAMESPACE,
                        "patch",
                        "configmap/tenant-controller-state",
                        "--type=merge",
                        "-p",
                        json.dumps({
                            "metadata": {
                                "resourceVersion": previous_state["metadata"][
                                    "resourceVersion"
                                ]
                            },
                            "data": {"rollbackToken": None},
                        }),
                    )
        manager = render_controller_manager(
            root,
            config,
            image,
            activation_token=token,
        )
        client.kubectl(
            "apply", "--server-side", "--field-manager=cnpg-vcluster-controller",
            "--force-conflicts", "-f", "-", input_text=json.dumps(foundation),
        )
        if replacement:
            client.kubectl(
                "apply",
                "--server-side",
                "--field-manager=cnpg-vcluster-controller",
                "--force-conflicts",
                "-f",
                "-",
                input_text=json.dumps(
                    activation_ticket(desired_hash, token, accepted_hash)
                ),
            )
        client.kubectl(
            "apply",
            "--server-side",
            "--field-manager=cnpg-vcluster-controller",
            "--force-conflicts",
            "-f",
            str(manager),
        )
        client.kubectl(
            "rollout",
            "status",
            f"deployment/{CONTROLLER_DEPLOYMENT}",
            "-n",
            CONTROLLER_NAMESPACE,
            f"--timeout={timeout}",
        )
        verify_running_controller(client, image, activation_token=token)
    except Exception as failure:
        if cutover_locked:
            apply_tenant_cutover_lock(config, client)
            raise
        if replacement:
            accepted = client.kubectl(
                "-n",
                CONTROLLER_NAMESPACE,
                "get",
                "configmap/tenant-controller-state",
                "--ignore-not-found=true",
                "-o",
                "json",
            ).stdout.strip()
            accepted_document = json.loads(accepted) if accepted else None
            current_hash = (
                accepted_document.get("data", {}).get("configurationHash")
                if accepted_document
                else None
            )
            if current_hash == desired_hash:
                raise
            if current_hash != accepted_hash:
                raise RuntimeError(
                    "controller acceptance changed during failed replacement; "
                    "refusing to restore an older identity"
                )
            rollback_token = uuid.uuid4().hex
            if accepted_document is None:
                lock = {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {
                        "name": "tenant-controller-state",
                        "namespace": CONTROLLER_NAMESPACE,
                    },
                    "data": {"rollbackToken": rollback_token},
                }
                created = client.kubectl(
                    "create",
                    "-f",
                    "-",
                    input_text=json.dumps(lock),
                    check=False,
                )
                if created.returncode != 0:
                    raise RuntimeError(
                        "controller acceptance changed before first-install rollback"
                    )
            else:
                client.kubectl(
                    "-n",
                    CONTROLLER_NAMESPACE,
                    "patch",
                    "configmap/tenant-controller-state",
                    "--type=merge",
                    "-p",
                    json.dumps({
                        "metadata": {
                            "resourceVersion": accepted_document["metadata"][
                                "resourceVersion"
                            ]
                        },
                        "data": {"rollbackToken": rollback_token},
                    }),
                )
            drain_error = None
            try:
                stop_controller(config, client)
            except RuntimeError as error:
                drain_error = error
            locked = client.json(
                "-n",
                CONTROLLER_NAMESPACE,
                "get",
                "configmap/tenant-controller-state",
            )
            if (
                locked.get("data", {}).get("configurationHash")
                != accepted_hash
                or locked.get("data", {}).get("rollbackToken")
                != rollback_token
            ):
                raise RuntimeError(
                    "controller acceptance changed while acquiring rollback lock"
                )
            for name, document in previous.items():
                if name == "configmap/tenant-controller-state":
                    continue
                if document:
                    value = json.loads(document)
                    value.pop("status", None)
                    metadata = value.get("metadata", {})
                    for key in (
                        "creationTimestamp", "generation", "managedFields",
                        "resourceVersion", "uid",
                    ):
                        metadata.pop(key, None)
                    client.kubectl(
                        "apply",
                        "--server-side",
                        "--field-manager=cnpg-vcluster-controller-rollback",
                        "--force-conflicts",
                        "-f",
                        "-",
                        input_text=json.dumps(value),
                    )
                else:
                    delete_named(config, client, CONTROLLER_NAMESPACE, name)
            delete_named(
                config,
                client,
                CONTROLLER_NAMESPACE,
                "configmap/tenant-controller-activation",
            )
            if accepted_document is not None:
                client.kubectl(
                    "-n",
                    CONTROLLER_NAMESPACE,
                    "patch",
                    "configmap/tenant-controller-state",
                    "--type=merge",
                    "-p",
                    json.dumps({
                        "metadata": {
                            "resourceVersion": locked["metadata"]["resourceVersion"]
                        },
                        "data": {"rollbackToken": None},
                    }),
                )
            if drain_error is not None:
                failure.add_note(
                    f"candidate shutdown failed during rollback: {drain_error}"
                )
        raise
    verify_controller_crd(client)
    verify_controller_image(config, client, image)
    if cutover_locked:
        remove_tenant_cutover_lock(config, client)
    try:
        verify_controller_api(config, client, require_allocation=cutover_locked)
    except Exception:
        if cutover_locked:
            apply_tenant_cutover_lock(config, client)
        raise


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
    stop_controller(config, client)
    require_clean_controller_state(root, client)
    for namespace, resource in (
        ("tenant-system", "configmap/tenant-foundation"),
        ("tenant-system", "configmap/tenant-controller-state"),
        ("tenant-system", "configmap/tenant-controller-activation"),
        ("tenant-system", "lease/tenant-controller.tenancy.cnpg-vcluster.io"),
        (None, "clusterrolebinding/tenant-controller"),
        (None, "clusterrole/tenant-controller-role"),
        ("tenant-system", "serviceaccount/tenant-controller"),
        (None, f"crd/{TENANT_CRD}"),
        (None, "namespace/tenant-system"),
    ):
        delete_named(config, client, namespace, resource)
    shutil.rmtree(
        root / ".runtime" / "rendered" / "controller",
        ignore_errors=True,
    )
