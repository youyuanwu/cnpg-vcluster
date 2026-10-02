from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
from collections.abc import Callable
from pathlib import Path

from scripts import generate_admin_resources as admin_resource_generator
from scripts.lib.config import parse_duration, require
from scripts.lib.controller import rust_toolchain
from scripts.lib.files import ensure_private_dir, sha256_file, write_private_file
from scripts.lib.process import run


ADMIN_PACKAGES = (
    "tenant-admin-shared",
    "tenant-admin-server",
    "tenant-admin-web",
)
STATIC_SERVER_FLAGS = ("-C", "target-feature=+crt-static")
SERVER_BUILD_COMMAND = (
    "cargo",
    "rustc",
    "--locked",
    "--offline",
    "--release",
    "-p",
    "tenant-admin-server",
    "--bin",
    "tenant-admin-server",
    "--message-format=json-render-diagnostics",
    "--",
    *STATIC_SERVER_FLAGS,
)
WEB_BUILD_COMMAND = (
    "trunk",
    "build",
    "--release",
    "--locked",
    "--offline",
)
WEB_OUTPUT_PATTERNS = {
    "index": re.compile(r"^index\.html$"),
    "javascript": re.compile(r"^tenant-admin-web-[0-9a-f]{8,64}\.js$"),
    "wasm": re.compile(r"^tenant-admin-web-[0-9a-f]{8,64}_bg\.wasm$"),
    "css": re.compile(r"^style-[0-9a-f]{8,64}\.css$"),
}
PREBUILT_SERVER_ENV = "CAPI_PREBUILT_ADMIN_SERVER"
PREBUILT_WEB_ENV = "CAPI_PREBUILT_ADMIN_WEB"
ADMIN_NAMESPACE = "tenant-system"
ADMIN_NAME = "tenant-admin"
ADMIN_NAMESPACE_LIMIT = 1_000
ADMIN_IDENTITY = (
    f"system:serviceaccount:{ADMIN_NAMESPACE}:{ADMIN_NAME}"
)
_NAMESPACE_NAME = re.compile(
    r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$"
)
_DEFAULT_EFFECTIVE_RESOURCE_PERMISSIONS = frozenset(
    {
        (
            "authorization.k8s.io",
            "selfsubjectaccessreviews",
            "create",
            None,
        ),
        (
            "authorization.k8s.io",
            "selfsubjectrulesreviews",
            "create",
            None,
        ),
        (
            "authentication.k8s.io",
            "selfsubjectreviews",
            "create",
            None,
        ),
    }
)
_DEFAULT_DISCOVERY_URLS = frozenset(
    {
        "/.well-known/openid-configuration",
        "/.well-known/openid-configuration/",
        "/api",
        "/api/*",
        "/apis",
        "/apis/*",
        "/healthz",
        "/livez",
        "/openid/v1/jwks",
        "/openid/v1/jwks/",
        "/openapi",
        "/openapi/*",
        "/readyz",
        "/version",
        "/version/",
    }
)


def _admin_cwd(root: Path) -> Path:
    return root / "admin" / "server"


def fetch_admin_dependencies(
    root: Path,
    config: dict[str, str],
    *,
    offline: bool | None = None,
) -> None:
    cargo, _ = rust_toolchain(root)
    if offline is None:
        offline = os.environ.get("CAPI_OFFLINE_ENFORCED") == "1"
    run(
        [cargo, "fetch", "--locked", *(["--offline"] if offline else [])],
        cwd=_admin_cwd(root),
        timeout=parse_duration(config["COMMAND_TIMEOUT"]) * 8,
    )


def _cargo(root: Path, config: dict[str, str], arguments: list[str]) -> None:
    cargo, _ = rust_toolchain(root)
    run(
        [cargo, *arguments],
        cwd=_admin_cwd(root),
        timeout=parse_duration(config["COMMAND_TIMEOUT"]) * 8,
    )


def generate_admin_resources(*, root: Path, check: bool = True) -> None:
    if not admin_resource_generator.generate(root, check=check):
        raise RuntimeError("generated admin resources are stale")


def admin_role_name(provider: str) -> str:
    try:
        return admin_resource_generator.ADMIN_ROLE_NAMES[provider]
    except KeyError as exc:
        raise ValueError(f"unsupported admin provider: {provider}") from exc


def admin_rbac_resource_paths(root: Path, provider: str) -> tuple[Path, ...]:
    return (
        root / "admin/config/rbac/service-account.json",
        root / admin_resource_generator.cluster_role_path(provider),
        root / admin_resource_generator.cluster_role_binding_path(provider),
        root / admin_resource_generator.controller_role_path(),
        root / admin_resource_generator.controller_role_binding_path(),
        root / "admin/config/service/service.json",
    )


def admin_rules_review_request(namespace: str) -> str:
    if not _NAMESPACE_NAME.fullmatch(namespace):
        raise RuntimeError("Tenant Admin RBAC review namespace is invalid")
    return json.dumps(
        {
            "apiVersion": "authorization.k8s.io/v1",
            "kind": "SelfSubjectRulesReview",
            "spec": {"namespace": namespace},
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def admin_review_namespaces(payload: object) -> tuple[str, ...]:
    if not isinstance(payload, dict):
        raise RuntimeError("Tenant Admin Namespace inventory is invalid")
    metadata = payload.get("metadata")
    items = payload.get("items")
    if (
        payload.get("apiVersion") != "v1"
        or payload.get("kind") != "NamespaceList"
        or not isinstance(metadata, dict)
        or not isinstance(items, list)
        or len(items) > ADMIN_NAMESPACE_LIMIT
        or metadata.get("continue", "") != ""
    ):
        raise RuntimeError(
            "Tenant Admin Namespace inventory is malformed or oversized"
        )
    names = []
    for value in items:
        item_metadata = value.get("metadata") if isinstance(value, dict) else None
        name = (
            item_metadata.get("name")
            if isinstance(item_metadata, dict)
            else None
        )
        if not isinstance(name, str) or not _NAMESPACE_NAME.fullmatch(name):
            raise RuntimeError("Tenant Admin Namespace inventory is invalid")
        names.append(name)
    if len(set(names)) != len(names) or ADMIN_NAMESPACE not in names:
        raise RuntimeError("Tenant Admin Namespace inventory is invalid")
    return tuple(sorted(names))


def _admin_resource_permission_atoms(
    rules: object,
    description: str,
) -> set[tuple[str, str, str, str | None]]:
    if not isinstance(rules, list):
        raise RuntimeError(f"Tenant Admin {description} rules are invalid")
    atoms = set()
    for value in rules:
        if not isinstance(value, dict) or not set(value).issubset(
            {"apiGroups", "resources", "verbs", "resourceNames"}
        ):
            raise RuntimeError(f"Tenant Admin {description} rules are invalid")
        api_groups = value.get("apiGroups")
        resources = value.get("resources")
        verbs = value.get("verbs")
        resource_names = value.get("resourceNames", [])
        if (
            not isinstance(api_groups, list)
            or not api_groups
            or not isinstance(resources, list)
            or not resources
            or not isinstance(verbs, list)
            or not verbs
            or not isinstance(resource_names, list)
            or not all(
                isinstance(item, str)
                for item in api_groups
            )
            or not all(
                isinstance(item, str) and item
                for item in (*resources, *verbs, *resource_names)
            )
            or "*" in api_groups
            or "*" in resources
            or "*" in verbs
            or any("/" in resource for resource in resources)
        ):
            raise RuntimeError(
                f"Tenant Admin {description} rules are not exact resources"
            )
        names: tuple[str | None, ...] = tuple(resource_names) or (None,)
        atoms.update(
            (api_group, resource, verb, resource_name)
            for api_group in api_groups
            for resource in resources
            for verb in verbs
            for resource_name in names
        )
    return atoms


def validate_admin_effective_rules(
    root: Path,
    provider: str,
    review: object,
    namespace: str,
    fetch_credential_resource: Callable[[str, str], object] | None = None,
) -> None:
    if not isinstance(review, dict):
        raise RuntimeError("Tenant Admin effective RBAC review is invalid")
    status = review.get("status")
    if (
        review.get("apiVersion") != "authorization.k8s.io/v1"
        or review.get("kind") != "SelfSubjectRulesReview"
        or not isinstance(status, dict)
    ):
        raise RuntimeError("Tenant Admin effective RBAC review is invalid")
    incomplete = status.get("incomplete", False)
    evaluation_error = status.get("evaluationError", "")
    if (
        not isinstance(incomplete, bool)
        or incomplete
        or not isinstance(evaluation_error, str)
        or evaluation_error
    ):
        raise RuntimeError("Tenant Admin effective RBAC review is incomplete")
    role_path = root / admin_resource_generator.cluster_role_path(provider)
    try:
        role = json.loads(role_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Tenant Admin generated ClusterRole is invalid: {role_path}"
        ) from exc
    if not isinstance(role, dict):
        raise RuntimeError(
            f"Tenant Admin generated ClusterRole is invalid: {role_path}"
        )
    expected = _admin_resource_permission_atoms(
        role.get("rules"),
        "generated ClusterRole",
    )
    if namespace == ADMIN_NAMESPACE:
        controller_role_path = root / admin_resource_generator.controller_role_path()
        try:
            controller_role = json.loads(
                controller_role_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Tenant Admin generated Role is invalid: {controller_role_path}"
            ) from exc
        if not isinstance(controller_role, dict):
            raise RuntimeError(
                f"Tenant Admin generated Role is invalid: {controller_role_path}"
            )
        expected.update(
            _admin_resource_permission_atoms(
                controller_role.get("rules"),
                "generated Role",
            )
        )
    for group, resource, verb, resource_name in expected:
        tenant_mutation = (
            group == "tenancy.cnpg-vcluster.io"
            and resource == "tenants"
            and verb in {"create", "delete"}
            and resource_name is None
        )
        controller_read = (
            group == "apps"
            and resource == "deployments"
            and verb == "get"
            and resource_name in {"tenant-controller", "database-controller"}
        )
        catalog_intent = (
            group == "tenancy.cnpg-vcluster.io"
            and resource == "tenantdatabasecatalogs"
            and verb in {"get", "update"}
            and resource_name is None
        )
        credential_read = (
            group == "rbac.authorization.k8s.io"
            and resource in {"roles", "rolebindings"}
            and verb == "get"
            and resource_name == "tenant-database-credentials"
        )
        cutover_read = (
            group == "admissionregistration.k8s.io"
            and resource in {
                "validatingadmissionpolicies",
                "validatingadmissionpolicybindings",
            }
            and verb == "get"
            and resource_name == "tenant-database-catalog-cutover-create-lock"
        )
        ordinary_read = verb in {"get", "list"} and resource_name is None
        if not (tenant_mutation or controller_read or catalog_intent or credential_read or cutover_read or ordinary_read):
            raise RuntimeError(
                "Tenant Admin generated ClusterRole exceeds lifecycle authority"
            )
    actual = _admin_resource_permission_atoms(
        status.get("resourceRules"),
        "effective RBAC",
    )
    secret_rules = {atom for atom in actual if atom[1] == "secrets"}
    if secret_rules:
        secret_name = (
            f"{namespace}-admin-kubeconfig"
            if provider == "azure"
            else f"{namespace}-kubeconfig"
        )
        expected_secret = {("", "secrets", "get", secret_name)}
        if (
            namespace == ADMIN_NAMESPACE
            or secret_rules != expected_secret
            or fetch_credential_resource is None
        ):
            raise RuntimeError("Tenant Admin effective RBAC includes unbound Secret access")
        try:
            namespace_object = fetch_credential_resource(namespace, f"namespace/{namespace}")
            role = fetch_credential_resource(
                namespace, "role/tenant-database-credentials"
            )
            binding = fetch_credential_resource(
                namespace, "rolebinding/tenant-database-credentials"
            )
            validate_admin_credential_scope(
                provider, namespace, namespace_object, role, binding
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise RuntimeError(
                "Tenant Admin effective RBAC credential identity is invalid"
            ) from exc
        expected.update(expected_secret)
    missing = expected - actual
    extra = actual - expected - _DEFAULT_EFFECTIVE_RESOURCE_PERMISSIONS
    if missing or extra:
        raise RuntimeError(
            "Tenant Admin effective RBAC does not match the generated contract"
        )
    non_resource_rules = status.get("nonResourceRules", [])
    if not isinstance(non_resource_rules, list):
        raise RuntimeError("Tenant Admin non-resource RBAC rules are invalid")
    for value in non_resource_rules:
        if (
            not isinstance(value, dict)
            or set(value) != {"nonResourceURLs", "verbs"}
            or not isinstance(value.get("nonResourceURLs"), list)
            or not isinstance(value.get("verbs"), list)
            or not value["nonResourceURLs"]
            or value["verbs"] != ["get"]
            or not all(
                isinstance(url, str) and url in _DEFAULT_DISCOVERY_URLS
                for url in value["nonResourceURLs"]
            )
        ):
            raise RuntimeError(
                "Tenant Admin effective non-resource RBAC is broader than discovery"
            )


def validate_admin_credential_scope(
    provider: str,
    namespace: str,
    namespace_object: object,
    role: object,
    binding: object,
) -> None:
    if not all(isinstance(value, dict) for value in (namespace_object, role, binding)):
        raise RuntimeError("Tenant Admin effective RBAC credential identity is invalid")
    namespace_meta = namespace_object.get("metadata")
    if not isinstance(namespace_meta, dict):
        raise RuntimeError("Tenant Admin effective RBAC credential identity is invalid")
    annotations = namespace_meta.get("annotations")
    tenant_uid = (
        annotations.get("tenancy.cnpg-vcluster.io/tenant-uid")
        if isinstance(annotations, dict)
        else None
    )
    if (
        namespace_object.get("kind") != "Namespace"
        or namespace_meta.get("name") != namespace
        or not isinstance(namespace_meta.get("uid"), str)
        or not namespace_meta["uid"]
        or not isinstance(tenant_uid, str)
        or not tenant_uid
        or annotations.get("tenancy.cnpg-vcluster.io/tenant") != namespace
        or namespace_meta.get("deletionTimestamp")
        or namespace_meta.get("ownerReferences")
    ):
        raise RuntimeError("Tenant Admin effective RBAC credential identity is invalid")
    for resource, kind in ((role, "Role"), (binding, "RoleBinding")):
        meta = resource.get("metadata")
        if (
            resource.get("kind") != kind
            or not isinstance(meta, dict)
            or meta.get("name") != "tenant-database-credentials"
            or meta.get("namespace") != namespace
            or not isinstance(meta.get("uid"), str)
            or not meta["uid"]
            or meta.get("deletionTimestamp")
            or meta.get("ownerReferences")
            or not isinstance(meta.get("labels"), dict)
            or meta["labels"].get("tenancy.cnpg-vcluster.io/tenant-uid")
            != tenant_uid
        ):
            raise RuntimeError("Tenant Admin effective RBAC credential identity is invalid")
    if (
        role.get("rules")
        != [{
            "apiGroups": [""],
            "resources": ["secrets"],
            "resourceNames": [
                f"{namespace}-admin-kubeconfig"
                if provider == "azure"
                else f"{namespace}-kubeconfig"
            ],
            "verbs": ["get"],
        }]
        or binding.get("roleRef")
        != {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "Role",
            "name": "tenant-database-credentials",
        }
        or binding.get("subjects")
        != [
            {"kind": "ServiceAccount", "name": name, "namespace": ADMIN_NAMESPACE}
            for name in ("tenant-admin", "database-controller")
        ]
    ):
        raise RuntimeError("Tenant Admin effective RBAC credential identity is invalid")


def test_admin(root: Path, config: dict[str, str]) -> None:
    fetch_admin_dependencies(root, config)
    packages = [
        value
        for package in ADMIN_PACKAGES
        for value in ("-p", package)
    ]
    _cargo(
        root,
        config,
        [
            "test",
            "--locked",
            "--offline",
            *packages,
            "--all-targets",
            "--all-features",
        ],
    )


def vet_admin(root: Path, config: dict[str, str]) -> None:
    fetch_admin_dependencies(root, config)
    _cargo(root, config, ["fmt", "--all", "--check"])
    packages = [
        value
        for package in ADMIN_PACKAGES
        for value in ("-p", package)
    ]
    _cargo(
        root,
        config,
        [
            "clippy",
            "--locked",
            "--offline",
            *packages,
            "--all-targets",
            "--all-features",
            "--",
            "-D",
            "warnings",
        ],
    )


def trunk_identity(root: Path, config: dict[str, str]) -> tuple[Path, dict[str, str]]:
    require(config, "TRUNK_VERSION", "TRUNK_URL", "TRUNK_SHA256")
    binary = root / ".tools" / "bin" / "trunk"
    try:
        details = binary.lstat()
    except FileNotFoundError as exc:
        raise RuntimeError(
            "verified Trunk is missing; run `just cache` then `just tools`"
        ) from exc
    if (
        stat.S_ISLNK(details.st_mode)
        or not stat.S_ISREG(details.st_mode)
        or details.st_uid != os.getuid()
        or details.st_mode & 0o022
    ):
        raise RuntimeError("verified Trunk must be an owned, non-writable regular file")
    version = run([str(binary), "--version"], timeout=30).stdout.strip()
    expected = f"trunk {config['TRUNK_VERSION']}"
    if version != expected:
        raise RuntimeError(
            f"installed Trunk identity mismatch: {version!r}, expected {expected!r}"
        )
    return binary, {
        "version": version,
        "url": config["TRUNK_URL"],
        "archiveSha256": config["TRUNK_SHA256"],
        "binarySha256": sha256_file(binary),
    }


def wasm_bindgen_identity(
    root: Path,
    config: dict[str, str],
) -> tuple[Path, dict[str, str]]:
    require(
        config,
        "WASM_BINDGEN_VERSION",
        "WASM_BINDGEN_URL",
        "WASM_BINDGEN_SHA256",
    )
    binary = root / ".tools" / "bin" / "wasm-bindgen"
    try:
        details = binary.lstat()
    except FileNotFoundError as exc:
        raise RuntimeError(
            "verified wasm-bindgen is missing; run `just cache` then `just tools`"
        ) from exc
    if (
        stat.S_ISLNK(details.st_mode)
        or not stat.S_ISREG(details.st_mode)
        or details.st_uid != os.getuid()
        or details.st_mode & 0o022
    ):
        raise RuntimeError(
            "verified wasm-bindgen must be an owned, non-writable regular file"
        )
    version = run([str(binary), "--version"], timeout=30).stdout.strip()
    expected = f"wasm-bindgen {config['WASM_BINDGEN_VERSION']}"
    if version != expected:
        raise RuntimeError(
            f"installed wasm-bindgen identity mismatch: "
            f"{version!r}, expected {expected!r}"
        )
    return binary, {
        "version": version,
        "url": config["WASM_BINDGEN_URL"],
        "archiveSha256": config["WASM_BINDGEN_SHA256"],
        "binarySha256": sha256_file(binary),
    }


def _prepare_admin_build(root: Path, config: dict[str, str]) -> None:
    fetch_admin_dependencies(root, config, offline=True)
    generate_admin_resources(root=root, check=True)


def _private_output_tree(directory: Path) -> None:
    if directory.is_symlink() or not directory.is_dir():
        raise RuntimeError(f"admin output directory is invalid: {directory}")
    directory.chmod(0o700)
    for path in directory.rglob("*"):
        details = path.lstat()
        if stat.S_ISLNK(details.st_mode):
            raise RuntimeError(f"admin output contains a symlink: {path}")
        if path.is_dir():
            path.chmod(0o700)
        elif path.is_file():
            path.chmod(0o600)
        else:
            raise RuntimeError(f"admin output contains a special file: {path}")


def validate_web_output(directory: Path) -> tuple[Path, ...]:
    if directory.is_symlink() or not directory.is_dir():
        raise RuntimeError(f"admin web output is missing: {directory}")
    observed: dict[str, list[Path]] = {
        output_type: [] for output_type in WEB_OUTPUT_PATTERNS
    }
    for path in sorted(directory.iterdir()):
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"unexpected admin web output: {path.name}")
        matches = [
            output_type
            for output_type, pattern in WEB_OUTPUT_PATTERNS.items()
            if pattern.fullmatch(path.name)
        ]
        if len(matches) != 1:
            raise RuntimeError(f"unexpected admin web output: {path.name}")
        observed[matches[0]].append(path)
    invalid = {
        output_type: [path.name for path in paths]
        for output_type, paths in observed.items()
        if len(paths) != 1
    }
    if invalid:
        raise RuntimeError(f"admin web output inventory is incomplete: {invalid}")
    return tuple(
        observed[output_type][0]
        for output_type in ("index", "javascript", "wasm", "css")
    )


def _owned_artifact(path: Path, description: str, *, directory: bool) -> None:
    try:
        details = path.lstat()
    except FileNotFoundError as exc:
        raise RuntimeError(f"configured prebuilt {description} is missing") from exc
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if (
        stat.S_ISLNK(details.st_mode)
        or not expected_type(details.st_mode)
        or details.st_uid != os.getuid()
        or details.st_mode & 0o022
    ):
        raise RuntimeError(
            f"configured prebuilt {description} must be an owned, "
            "non-writable regular "
            f"{'directory' if directory else 'file'}"
        )


def _prebuilt_admin_inputs(root: Path) -> tuple[Path, Path] | None:
    configured_server = os.environ.get(PREBUILT_SERVER_ENV)
    configured_web = os.environ.get(PREBUILT_WEB_ENV)
    if not configured_server and not configured_web:
        return None
    if not configured_server or not configured_web:
        raise RuntimeError(
            f"{PREBUILT_SERVER_ENV} and {PREBUILT_WEB_ENV} must be set together"
        )
    artifact_root = (root / ".tools" / "artifacts").resolve()
    server = Path(configured_server).resolve()
    web = Path(configured_web).resolve()
    if (
        not server.is_relative_to(artifact_root)
        or not web.is_relative_to(artifact_root)
        or server.name != "tenant-admin"
        or web.name != "web"
        or server.parent != web.parent
    ):
        raise RuntimeError(
            "configured prebuilt admin paths must be sibling tenant-admin and "
            "web artifacts below .tools/artifacts"
        )
    _owned_artifact(server, "admin server", directory=False)
    _owned_artifact(web, "admin web directory", directory=True)
    for path in web.rglob("*"):
        _owned_artifact(
            path,
            f"admin web artifact {path.relative_to(web)}",
            directory=path.is_dir(),
        )
    verify_static_admin_server(server)
    validate_web_output(web)
    return server, web


def _materialize_prebuilt_admin(
    root: Path,
    server: Path,
    web: Path,
) -> tuple[Path, Path]:
    output_root = root / ".runtime" / "rendered" / "admin"
    output_server = output_root / "tenant-admin"
    output_web = output_root / "web"
    ensure_private_dir(output_root)
    output_server.unlink(missing_ok=True)
    _remove_private_build_directory(output_web)
    shutil.copy2(server, output_server)
    output_server.chmod(0o700)
    shutil.copytree(web, output_web)
    _private_output_tree(output_web)
    verify_static_admin_server(output_server)
    validate_web_output(output_web)
    return output_server, output_web


def _remove_private_build_directory(path: Path) -> None:
    if not os.path.lexists(path):
        return
    details = path.lstat()
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
        raise RuntimeError(f"admin build directory is not a regular directory: {path}")
    shutil.rmtree(path)


def _build_admin_web(root: Path, config: dict[str, str]) -> Path:
    trunk, _ = trunk_identity(root, config)
    wasm_bindgen, _ = wasm_bindgen_identity(root, config)
    output = root / ".runtime" / "rendered" / "admin" / "web"
    _remove_private_build_directory(output)
    ensure_private_dir(output.parent)
    command = [str(trunk), *WEB_BUILD_COMMAND[1:]]
    environment = dict(os.environ)
    environment["PATH"] = (
        f"{wasm_bindgen.parent}{os.pathsep}{environment.get('PATH', '')}"
    )
    try:
        run(
            command,
            cwd=root / "admin" / "web",
            env=environment,
            timeout=parse_duration(config["COMMAND_TIMEOUT"]) * 8,
        )
    except BaseException:
        _remove_private_build_directory(output)
        raise
    _private_output_tree(output)
    validate_web_output(output)
    return output


def verify_static_admin_server(binary: Path) -> None:
    with binary.open("rb") as stream:
        if stream.read(4) != b"\x7fELF":
            raise RuntimeError("tenant admin server is not an ELF executable")
    headers = run(["readelf", "-lW", str(binary)], timeout=30).stdout
    dynamic = run(["readelf", "-dW", str(binary)], timeout=30).stdout
    if re.search(r"\bINTERP\b", headers) or re.search(r"\bNEEDED\b", dynamic):
        raise RuntimeError(
            "tenant admin server must be static: ELF INTERP/NEEDED found"
        )


def _build_admin_server(root: Path, config: dict[str, str]) -> Path:
    output = root / ".runtime" / "rendered" / "admin" / "tenant-admin"
    ensure_private_dir(output.parent)
    output.unlink(missing_ok=True)
    cargo, _ = rust_toolchain(root)
    result = run(
        [cargo, *SERVER_BUILD_COMMAND[1:]],
        cwd=_admin_cwd(root),
        timeout=parse_duration(config["COMMAND_TIMEOUT"]) * 8,
    )
    executables = [
        Path(message["executable"])
        for line in result.stdout.splitlines()
        for message in [json.loads(line)]
        if (
            message.get("reason") == "compiler-artifact"
            and message.get("target", {}).get("name") == "tenant-admin-server"
            and isinstance(message.get("executable"), str)
        )
    ]
    if len(executables) != 1 or not executables[0].is_file():
        raise RuntimeError(
            "Cargo did not report exactly one tenant-admin-server executable"
        )
    verify_static_admin_server(executables[0])
    shutil.copy2(executables[0], output)
    output.chmod(0o700)
    verify_static_admin_server(output)
    return output


def build_admin(root: Path, config: dict[str, str]) -> tuple[Path, Path]:
    _prepare_admin_build(root, config)
    prebuilt = _prebuilt_admin_inputs(root)
    if prebuilt is not None:
        return _materialize_prebuilt_admin(root, *prebuilt)
    web = _build_admin_web(root, config)
    server = _build_admin_server(root, config)
    return server, web


def _package_fingerprint(server: Path, web: Path) -> dict[str, str]:
    verify_static_admin_server(server)
    outputs = validate_web_output(web)
    return {
        "tenant-admin": sha256_file(server),
        **{
            f"web/{path.name}": sha256_file(path)
            for path in outputs
        },
    }


def verify_reproducible_admin_build(
    root: Path,
    config: dict[str, str],
) -> tuple[Path, Path]:
    if _prebuilt_admin_inputs(root) is not None:
        raise RuntimeError(
            "admin reproducibility check requires a source build, not prebuilt inputs"
        )
    first_server, first_web = build_admin(root, config)
    first = _package_fingerprint(first_server, first_web)
    second_server, second_web = build_admin(root, config)
    second = _package_fingerprint(second_server, second_web)
    if first != second:
        raise RuntimeError(
            f"admin build output is not reproducible: first={first}, second={second}"
        )
    return second_server, second_web


def _source_tree_inputs(directory: Path, description: str) -> list[Path]:
    try:
        details = directory.lstat()
    except FileNotFoundError as exc:
        raise RuntimeError(f"{description} source directory is missing") from exc
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
        raise RuntimeError(f"{description} source directory is invalid: {directory}")
    inputs = []
    for path in directory.rglob("*"):
        details = path.lstat()
        if stat.S_ISLNK(details.st_mode):
            raise RuntimeError(f"{description} source input is a symlink: {path}")
        if stat.S_ISREG(details.st_mode):
            inputs.append(path)
        elif not stat.S_ISDIR(details.st_mode):
            raise RuntimeError(
                f"{description} source input is a special file: {path}"
            )
    return inputs


def admin_source_digest(root: Path, config: dict[str, str]) -> str:
    admin = root / "admin"
    controller = root / "controller"
    repository = root.parent
    admin_inputs = _source_tree_inputs(admin, "admin")
    try:
        controller_details = controller.lstat()
    except FileNotFoundError as exc:
        raise RuntimeError("controller source directory is missing") from exc
    if (
        stat.S_ISLNK(controller_details.st_mode)
        or not stat.S_ISDIR(controller_details.st_mode)
    ):
        raise RuntimeError(f"controller source directory is invalid: {controller}")
    controller_inputs = _source_tree_inputs(controller / "src", "controller")
    runtime = root / "database-runtime"
    runtime_inputs = _source_tree_inputs(runtime / "src", "database runtime")
    inputs = [
        *admin_inputs,
        *controller_inputs,
        *runtime_inputs,
        controller / "Cargo.toml",
        runtime / "Cargo.toml",
        root / "scripts" / "generate_admin_resources.py",
        repository / "Cargo.toml",
        repository / "Cargo.lock",
        repository / "rust-toolchain.toml",
    ]
    controller_build = controller / "build.rs"
    if os.path.lexists(controller_build):
        inputs.append(controller_build)
    digest = hashlib.sha256()
    for path in sorted(set(inputs)):
        if path.is_symlink():
            raise RuntimeError(f"admin source input is a symlink: {path}")
        relative = path.relative_to(
            root if path.is_relative_to(root) else repository
        ).as_posix()
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    _, compiler = rust_toolchain(root)
    _, trunk = trunk_identity(root, config)
    _, wasm_bindgen = wasm_bindgen_identity(root, config)
    digest.update(
        json.dumps(
            {
                "compiler": compiler,
                "trunk": trunk,
                "wasmBindgen": wasm_bindgen,
                "serverCommand": SERVER_BUILD_COMMAND,
                "webCommand": WEB_BUILD_COMMAND,
            },
            sort_keys=True,
        ).encode()
    )
    return digest.hexdigest()


def admin_image(root: Path, config: dict[str, str]) -> str:
    repository = config.get("TENANT_ADMIN_IMAGE_REPOSITORY", "")
    if (
        not repository
        or "@" in repository
        or ":" in repository.rsplit("/", 1)[-1]
        or not re.fullmatch(r"[a-z0-9][a-z0-9._/-]*", repository)
    ):
        raise RuntimeError("TENANT_ADMIN_IMAGE_REPOSITORY is invalid")
    return f"{repository}:{admin_source_digest(root, config)[:16]}"


def render_azure_admin_deployment(root: Path, image: str) -> Path:
    if not re.fullmatch(r"[^@\s]+@sha256:[0-9a-f]{64}", image):
        raise RuntimeError("Azure admin image must be an immutable digest reference")
    template = (
        root / "admin" / "config" / "deployment" / "deployment-azure.json.tpl"
    ).read_text(encoding="utf-8")
    placeholder = "${TENANT_ADMIN_IMAGE}"
    if template.count(placeholder) != 1:
        raise RuntimeError(
            "Azure admin Deployment template must contain exactly one image placeholder"
        )
    destination = (
        root / ".runtime" / "rendered" / "azure-admin" / "deployment.json"
    )
    write_private_file(destination, template.replace(placeholder, image))
    return destination


def build_admin_image(
    root: Path,
    config: dict[str, str],
    image: str | None = None,
) -> str:
    server, web = build_admin(root, config)
    verify_static_admin_server(server)
    validate_web_output(web)
    build_root = root / ".runtime" / "rendered" / "admin-build"
    _remove_private_build_directory(build_root)
    ensure_private_dir(build_root)
    try:
        shutil.copy2(root / "admin" / "Dockerfile", build_root / "Dockerfile")
        shutil.copy2(server, build_root / "tenant-admin")
        shutil.copytree(web, build_root / "web")
        selected_image = image or admin_image(root, config)
        if (
            "@" in selected_image
            or not re.fullmatch(
                r"[^@\s]+:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}",
                selected_image,
            )
        ):
            raise RuntimeError("admin build image must be a valid tagged reference")
        run(
            [
                "docker",
                "build",
                "--pull=false",
                "-t",
                selected_image,
                str(build_root),
            ],
            timeout=parse_duration(config["COMMAND_TIMEOUT"]) * 8,
        )
        return selected_image
    finally:
        _remove_private_build_directory(build_root)
