#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

ADMIN_NAME = "tenant-admin"
ADMIN_NAMESPACE = "tenant-system"
ADMIN_IMAGE = "${TENANT_ADMIN_IMAGE}"
ADMIN_CONTAINER_PORT = 8080
ADMIN_SERVICE_PORT = 80
PROVIDERS = ("azure", "local")
ADMIN_ROLE_NAMES = {
    "azure": "tenant-admin-azure",
    "local": "tenant-admin-local",
}
PROVIDER_CATALOGS = {
    "azure": Path("controller/config/azure-management-resources.json"),
    "local": Path("controller/config/management-resources.json"),
}
PROVIDER_EXPLICIT_RULES = {
    "azure": (),
    "local": (("", ("secrets",), ("get",)),),
}

OUTPUT_PATHS = (
    Path("admin/config/deployment/deployment-azure.json.tpl"),
    Path("admin/config/deployment/deployment-local.json.tpl"),
    Path("admin/config/rbac/cluster-role-azure.json"),
    Path("admin/config/rbac/cluster-role-binding-azure.json"),
    Path("admin/config/rbac/cluster-role-binding-local.json"),
    Path("admin/config/rbac/cluster-role-local.json"),
    Path("admin/config/rbac/service-account.json"),
    Path("admin/config/service/service.json"),
)
LEGACY_OUTPUT_PATHS = (
    Path("admin/config/deployment/deployment-azure.yaml.tpl"),
    Path("admin/config/deployment/deployment-local.yaml.tpl"),
    Path("admin/config/rbac/cluster-role-azure.yaml"),
    Path("admin/config/rbac/cluster-role-binding-azure.yaml"),
    Path("admin/config/rbac/cluster-role-binding-local.yaml"),
    Path("admin/config/rbac/cluster-role-local.yaml"),
    Path("admin/config/rbac/service-account.yaml"),
    Path("admin/config/service/service.yaml"),
)


def cluster_role_path(provider: str) -> Path:
    if provider not in PROVIDERS:
        raise ValueError(f"unsupported admin provider: {provider}")
    return Path(f"admin/config/rbac/cluster-role-{provider}.json")


def cluster_role_binding_path(provider: str) -> Path:
    if provider not in PROVIDERS:
        raise ValueError(f"unsupported admin provider: {provider}")
    return Path(f"admin/config/rbac/cluster-role-binding-{provider}.json")


def provider_rules(
    root: Path,
    provider: str,
) -> tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...]:
    if provider not in PROVIDERS:
        raise ValueError(f"unsupported admin provider: {provider}")
    catalog_path = root / PROVIDER_CATALOGS[provider]
    try:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"admin provider catalog is invalid: {catalog_path}"
        ) from exc
    if not isinstance(catalog, list):
        raise RuntimeError(f"admin provider catalog is invalid: {catalog_path}")
    listed: dict[str, set[str]] = {}
    exact_gets: dict[str, set[str]] = {}
    for value in catalog:
        if not isinstance(value, dict):
            raise RuntimeError(
                f"admin provider catalog is invalid: {catalog_path}"
            )
        api_version = value.get("apiVersion")
        kind = value.get("kind")
        plural = value.get("plural")
        namespaced = value.get("namespaced")
        if (
            not isinstance(api_version, str)
            or not isinstance(kind, str)
            or not isinstance(plural, str)
            or not isinstance(namespaced, bool)
        ):
            raise RuntimeError(
                f"admin provider catalog is invalid: {catalog_path}"
            )
        if kind == "Secret":
            continue
        api_group = api_version.split("/", 1)[0] if "/" in api_version else ""
        if not namespaced:
            if (
                kind != "Namespace"
                or api_version != "v1"
                or plural != "namespaces"
                or value.get("namePolicy") != "tenant"
            ):
                raise RuntimeError(
                    "admin RBAC cannot list or infer an unexpected "
                    f"cluster-scoped resource: {api_version} {kind}"
                )
            exact_gets.setdefault(api_group, set()).add(plural)
        else:
            listed.setdefault(api_group, set()).add(plural)
    rules = [
        ("tenancy.cnpg-vcluster.io", ("tenants",), ("get", "list")),
        *PROVIDER_EXPLICIT_RULES[provider],
        *(
            (group, tuple(sorted(resources)), ("get",))
            for group, resources in exact_gets.items()
        ),
        *(
            (group, tuple(sorted(resources)), ("list",))
            for group, resources in listed.items()
        ),
    ]
    return tuple(sorted(rules))


def _render_json(document: dict[str, object]) -> str:
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def _cluster_role(root: Path, provider: str) -> dict[str, object]:
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "ClusterRole",
        "metadata": {"name": ADMIN_ROLE_NAMES[provider]},
        "rules": [
            {
                "apiGroups": [api_group],
                "resources": list(resources),
                "verbs": list(verbs),
            }
            for api_group, resources, verbs in provider_rules(root, provider)
        ],
    }


def _deployment(provider: str) -> dict[str, object]:
    if provider not in PROVIDERS:
        raise ValueError(f"unsupported admin provider: {provider}")
    image_pull_policy = "Never" if provider == "local" else "IfNotPresent"
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": ADMIN_NAME,
            "namespace": ADMIN_NAMESPACE,
        },
        "spec": {
            "replicas": 1,
            "revisionHistoryLimit": 2,
            "progressDeadlineSeconds": 300,
            "strategy": {"type": "Recreate"},
            "selector": {
                "matchLabels": {"app.kubernetes.io/name": ADMIN_NAME}
            },
            "template": {
                "metadata": {
                    "labels": {"app.kubernetes.io/name": ADMIN_NAME}
                },
                "spec": {
                    "serviceAccountName": ADMIN_NAME,
                    "automountServiceAccountToken": True,
                    "enableServiceLinks": False,
                    "terminationGracePeriodSeconds": 30,
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 65532,
                        "runAsGroup": 65532,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            "name": "admin",
                            "image": ADMIN_IMAGE,
                            "imagePullPolicy": image_pull_policy,
                            "env": [
                                {
                                    "name": "TENANT_ADMIN_PROVIDER",
                                    "value": provider,
                                }
                            ],
                            "ports": [
                                {
                                    "name": "http",
                                    "containerPort": ADMIN_CONTAINER_PORT,
                                    "protocol": "TCP",
                                }
                            ],
                            "livenessProbe": {
                                "httpGet": {
                                    "path": "/healthz",
                                    "port": "http",
                                },
                                "initialDelaySeconds": 5,
                                "periodSeconds": 10,
                                "timeoutSeconds": 2,
                                "failureThreshold": 3,
                            },
                            "readinessProbe": {
                                "httpGet": {
                                    "path": "/readyz",
                                    "port": "http",
                                },
                                "periodSeconds": 5,
                                "timeoutSeconds": 2,
                                "failureThreshold": 3,
                            },
                            "securityContext": {
                                "runAsNonRoot": True,
                                "privileged": False,
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "resources": {
                                "requests": {
                                    "cpu": "25m",
                                    "memory": "32Mi",
                                },
                                "limits": {
                                    "cpu": "250m",
                                    "memory": "128Mi",
                                },
                            },
                        }
                    ],
                },
            },
        },
    }


def generated_documents(root: Path = ROOT) -> dict[Path, str]:
    documents = {
        Path("admin/config/deployment/deployment-azure.json.tpl"): _render_json(
            _deployment("azure")
        ),
        Path("admin/config/deployment/deployment-local.json.tpl"): _render_json(
            _deployment("local")
        ),
        Path("admin/config/rbac/service-account.json"): _render_json(
            {
                "apiVersion": "v1",
                "kind": "ServiceAccount",
                "metadata": {
                    "name": ADMIN_NAME,
                    "namespace": ADMIN_NAMESPACE,
                },
                "automountServiceAccountToken": False,
            }
        ),
        Path("admin/config/service/service.json"): _render_json(
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {
                    "name": ADMIN_NAME,
                    "namespace": ADMIN_NAMESPACE,
                },
                "spec": {
                    "type": "ClusterIP",
                    "selector": {
                        "app.kubernetes.io/name": ADMIN_NAME,
                    },
                    "ports": [
                        {
                            "name": "http",
                            "port": ADMIN_SERVICE_PORT,
                            "targetPort": ADMIN_CONTAINER_PORT,
                            "protocol": "TCP",
                        }
                    ],
                },
            }
        ),
    }
    for provider in PROVIDERS:
        documents[cluster_role_path(provider)] = _render_json(
            _cluster_role(root, provider)
        )
        documents[cluster_role_binding_path(provider)] = _render_json(
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "ClusterRoleBinding",
                "metadata": {"name": ADMIN_NAME},
                "roleRef": {
                    "apiGroup": "rbac.authorization.k8s.io",
                    "kind": "ClusterRole",
                    "name": ADMIN_ROLE_NAMES[provider],
                },
                "subjects": [
                    {
                        "kind": "ServiceAccount",
                        "name": ADMIN_NAME,
                        "namespace": ADMIN_NAMESPACE,
                    }
                ],
            }
        )
    return documents


def generate(root: Path, *, check: bool) -> bool:
    documents = generated_documents(root)
    if tuple(sorted(documents)) != OUTPUT_PATHS:
        raise RuntimeError("admin resource output set does not match OUTPUT_PATHS")

    valid = True
    for relative_path in LEGACY_OUTPUT_PATHS:
        path = root / relative_path
        if check:
            if path.exists():
                print(
                    f"legacy generated admin resource remains: {path}",
                    file=sys.stderr,
                )
                valid = False
        elif path.exists():
            path.unlink()
    for relative_path in OUTPUT_PATHS:
        path = root / relative_path
        expected = documents[relative_path]
        if check:
            if not path.is_file() or path.read_text(encoding="utf-8") != expected:
                print(f"generated admin resource is stale: {path}", file=sys.stderr)
                valid = False
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(expected, encoding="utf-8")
    return valid


def main(arguments: list[str]) -> int:
    if arguments not in ([], ["--check"]):
        print("usage: generate_admin_resources.py [--check]", file=sys.stderr)
        return 2
    return 0 if generate(ROOT, check=arguments == ["--check"]) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
