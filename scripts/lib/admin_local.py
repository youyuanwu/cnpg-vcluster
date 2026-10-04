from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from scripts.lib.admin import (
    ADMIN_IDENTITY,
    ADMIN_NAMESPACE_LIMIT,
    admin_rbac_resource_paths,
    admin_review_namespaces,
    admin_role_name,
    admin_rules_review_request,
    build_admin_image,
    validate_admin_effective_rules,
)
from scripts.lib.config import parse_duration
from scripts.lib.catalog_lifecycle import CatalogClient, ready_entries
from scripts.lib.controller_state import delete_named
from scripts.lib.files import write_private_file
from scripts.lib.kube import ManagementClient
from scripts.lib.management import (
    require_management_ownership,
    validate_management_kubeconfig,
)
from scripts.lib.process import run


ADMIN_NAME = "tenant-admin"
ADMIN_NAMESPACE = "tenant-system"
ADMIN_FIELD_MANAGER = "cnpg-vcluster-admin"
ADMIN_LABEL = "app.kubernetes.io/name=tenant-admin"
ADMIN_SERVICE_PROXY = (
    "/api/v1/namespaces/tenant-system/services/http:tenant-admin:80/proxy"
)
ADMIN_API_SCHEMA_VERSION = 6
ADMIN_IMAGE_PATTERN = re.compile(
    r"[a-z0-9](?:[a-z0-9._/-]*[a-z0-9])?:"
    r"[A-Za-z0-9_][A-Za-z0-9._-]{0,127}"
)
ADMIN_CLASSIFICATIONS = {
    "ready",
    "progressing",
    "degraded",
    "failed",
    "deleting",
    "ownership-invalid",
}
ADMIN_DATABASE_UNAVAILABLE_REASONS = {
    "pending",
    "management-resource-missing",
    "management-inventory-unavailable",
    "tenant-access-invalid",
    "tenant-api-unavailable",
    "cluster-missing",
    "malformed",
}
ADMIN_DATABASE_INSTANCE_ROLES = {"primary", "standby", "unknown"}
ADMIN_CONDITION_STATUSES = {"True", "False", "Unknown"}
ADMIN_TOPOLOGY_NODE_KINDS = {
    "tenant",
    "control-plane",
    "worker-pool",
    "machine",
    "node",
    "provider-resource",
    "add-on",
    "database",
}
ADMIN_TOPOLOGY_HEALTH = {
    "ready",
    "progressing",
    "degraded",
    "failed",
    "deleting",
    "unknown",
}
ADMIN_TOPOLOGY_PROVENANCE = {
    "exact-kubernetes-resource",
    "database-logical-representation",
    "external-provider-representation",
    "recorded-resource-representation",
    "synthetic-summary",
}
ADMIN_TOPOLOGY_EDGE_KINDS = {
    "owns",
    "contains",
    "manages",
    "provides",
    "represents",
    "depends-on",
}
ADMIN_LIFECYCLE_STAGES = (
    "request-accepted",
    "infrastructure",
    "control-plane",
    "workers",
    "add-ons",
    "databases",
    "ready",
)
ADMIN_LIFECYCLE_STATES = {
    "completed",
    "current",
    "blocked",
    "pending",
    "not-applicable",
    "unknown",
}


def render_local_admin_deployment(root: Path, image: str) -> Path:
    if not ADMIN_IMAGE_PATTERN.fullmatch(image):
        raise RuntimeError("Tenant Admin image reference is unsafe")
    source = root / "admin/config/deployment/deployment-local.json.tpl"
    template = source.read_text(encoding="utf-8")
    placeholder = "${TENANT_ADMIN_IMAGE}"
    if template.count(placeholder) != 1:
        raise RuntimeError("local Tenant Admin template image placeholder is invalid")
    rendered = template.replace(placeholder, image)
    if placeholder in rendered:
        raise RuntimeError("local Tenant Admin image substitution was incomplete")
    try:
        deployment = json.loads(rendered)
    except json.JSONDecodeError as exc:
        raise RuntimeError("rendered local Tenant Admin Deployment is invalid") from exc
    try:
        rendered_image = deployment["spec"]["template"]["spec"]["containers"][0][
            "image"
        ]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("rendered local Tenant Admin Deployment is incomplete") from exc
    if rendered_image != image:
        raise RuntimeError("rendered local Tenant Admin image does not match")
    destination = (
        root / ".runtime/rendered/admin/deployment-local.json"
    )
    write_private_file(destination, rendered)
    return destination


def _admin_resource_paths(root: Path) -> tuple[Path, ...]:
    return admin_rbac_resource_paths(root, "local")


def _apply(client: ManagementClient, path: Path) -> None:
    client.kubectl(
        "apply",
        "--server-side",
        f"--field-manager={ADMIN_FIELD_MANAGER}",
        "--force-conflicts",
        "-f",
        str(path),
    )


def _load_admin_image(root: Path, config: dict[str, str], image: str) -> None:
    run(
        [
            str(root / ".tools/bin/kind"),
            "load",
            "docker-image",
            image,
            "--name",
            config["KIND_CLUSTER_NAME"],
        ],
        timeout=parse_duration(config["COMMAND_TIMEOUT"]) * 4,
    )


def _required_mapping(value: object, description: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise RuntimeError(f"Tenant Admin {description} is not an object")
    return value


def _required_list(value: object, description: str) -> list[object]:
    if not isinstance(value, list):
        raise RuntimeError(f"Tenant Admin {description} is not a list")
    return value


def _required_string(value: object, description: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"Tenant Admin {description} is missing")
    return value


def _container(document: dict[str, object]) -> dict[str, object]:
    try:
        containers = document["spec"]["template"]["spec"]["containers"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError("Tenant Admin Deployment container is missing") from exc
    if not isinstance(containers, list) or len(containers) != 1:
        raise RuntimeError("Tenant Admin Deployment must have exactly one container")
    return _required_mapping(containers[0], "Deployment container")


def _expected_rules(root: Path) -> list[dict[str, object]]:
    document = json.loads(
        (
            root / "admin/config/rbac/cluster-role-local.json"
        ).read_text(encoding="utf-8")
    )
    return document["rules"]


def _normalized_rules(value: object) -> list[tuple[tuple[str, ...], ...]]:
    rules = _required_list(value, "ClusterRole rules")
    normalized = []
    for rule_value in rules:
        rule = _required_mapping(rule_value, "ClusterRole rule")
        if not set(rule).issubset(
            {"apiGroups", "resources", "verbs", "resourceNames"}
        ):
            raise RuntimeError("Tenant Admin ClusterRole rule has unexpected fields")
        raw_groups = _required_list(rule["apiGroups"], "RBAC API groups")
        raw_resources = _required_list(rule["resources"], "RBAC resources")
        raw_verbs = _required_list(rule["verbs"], "RBAC verbs")
        raw_names = rule.get("resourceNames", [])
        if not isinstance(raw_names, list):
            raise RuntimeError("Tenant Admin ClusterRole resourceNames are invalid")
        if not all(
            isinstance(value, str)
            for value in (*raw_groups, *raw_resources, *raw_verbs, *raw_names)
        ):
            raise RuntimeError("Tenant Admin ClusterRole values are invalid")
        groups = tuple(sorted(raw_groups))
        resources = tuple(sorted(raw_resources))
        verbs = tuple(sorted(raw_verbs))
        names = tuple(sorted(raw_names))
        tenant_mutation = (
            groups == ("tenancy.cnpg-vcluster.io",)
            and resources == ("tenants",)
            and set(verbs) == {"create", "delete", "get", "list"}
            and not names
        )
        controller_read = (
            groups == ("apps",)
            and resources == ("deployments",)
            and verbs == ("get",)
            and names in (("tenant-controller",), ("database-controller",))
        )
        catalog_intent = (
            groups == ("tenancy.cnpg-vcluster.io",)
            and resources == ("tenantdatabasecatalogs",)
            and verbs == ("get", "update")
            and not names
        )
        credential_policy_read = (
            groups == ("rbac.authorization.k8s.io",)
            and resources == ("rolebindings", "roles")
            and verbs == ("get",)
            and names == ("tenant-database-credentials",)
        )
        cutover_read = (
            groups == ("admissionregistration.k8s.io",)
            and resources == (
                "validatingadmissionpolicies",
                "validatingadmissionpolicybindings",
            )
            and verbs == ("get",)
            and names == ("tenant-database-catalog-cutover-create-lock",)
        )
        ordinary_read = (
            set(verbs).issubset({"get", "list"}) and not names
        )
        if (
            "*" in groups
            or "*" in resources
            or "*" in verbs
            or (
                "secrets" in resources
                and (
                    groups != ("",)
                    or resources != ("secrets",)
                    or verbs != ("get",)
                )
            )
            or any("/" in resource for resource in resources)
            or not verbs
            or not (tenant_mutation or controller_read or catalog_intent or credential_policy_read or cutover_read or ordinary_read)
        ):
            raise RuntimeError("Tenant Admin ClusterRole is not exact read-only RBAC")
        normalized.append((groups, resources, verbs, names))
    return sorted(normalized)


def _verify_service_account(service_account: dict[str, object]) -> None:
    metadata = _required_mapping(
        service_account.get("metadata"), "ServiceAccount metadata"
    )
    if (
        metadata.get("name") != ADMIN_NAME
        or metadata.get("namespace") != ADMIN_NAMESPACE
        or service_account.get("automountServiceAccountToken") is not False
    ):
        raise RuntimeError("Tenant Admin ServiceAccount contract does not match")


def _verify_role(root: Path, role: dict[str, object]) -> None:
    metadata = _required_mapping(role.get("metadata"), "ClusterRole metadata")
    if (
        metadata.get("name") != admin_role_name("local")
        or _normalized_rules(role.get("rules"))
        != _normalized_rules(_expected_rules(root))
    ):
        raise RuntimeError("Tenant Admin ClusterRole contract does not match")


def _verify_binding(root: Path, binding: dict[str, object]) -> None:
    metadata = _required_mapping(
        binding.get("metadata"), "ClusterRoleBinding metadata"
    )
    expected = json.loads(
        (
            root / "admin/config/rbac/cluster-role-binding-local.json"
        ).read_text(encoding="utf-8")
    )
    if (
        metadata.get("name") != ADMIN_NAME
        or binding.get("roleRef") != expected.get("roleRef")
        or binding.get("subjects") != expected.get("subjects")
    ):
        raise RuntimeError("Tenant Admin ClusterRoleBinding contract does not match")


def _verify_service(service: dict[str, object]) -> None:
    metadata = _required_mapping(service.get("metadata"), "Service metadata")
    service_spec = _required_mapping(service.get("spec"), "Service spec")
    if (
        metadata.get("name") != ADMIN_NAME
        or metadata.get("namespace") != ADMIN_NAMESPACE
        or service_spec.get("type") != "ClusterIP"
        or service_spec.get("selector")
        != {"app.kubernetes.io/name": ADMIN_NAME}
        or service_spec.get("ports")
        != [
            {
                "name": "http",
                "port": 80,
                "protocol": "TCP",
                "targetPort": 8080,
            }
        ]
        or not isinstance(service_spec.get("clusterIP"), str)
        or not service_spec["clusterIP"]
    ):
        raise RuntimeError("Tenant Admin Service contract does not match")


def _verify_static_resources(
    root: Path,
    service_account: dict[str, object],
    role: dict[str, object],
    binding: dict[str, object],
    service: dict[str, object],
) -> None:
    _verify_service_account(service_account)
    _verify_role(root, role)
    _verify_binding(root, binding)
    _verify_service(service)


def _verify_deployment_contract(
    deployment: dict[str, object],
    image: str,
    *,
    require_ready: bool,
) -> str:
    metadata = _required_mapping(deployment.get("metadata"), "Deployment metadata")
    uid = _required_string(metadata.get("uid"), "Deployment UID")
    spec = _required_mapping(deployment.get("spec"), "Deployment spec")
    template = _required_mapping(spec.get("template"), "Pod template")
    pod = _required_mapping(template.get("spec"), "Pod template spec")
    container = _container(deployment)
    if (
        metadata.get("name") != ADMIN_NAME
        or metadata.get("namespace") != ADMIN_NAMESPACE
        or spec.get("replicas") != 1
        or spec.get("strategy") != {"type": "Recreate"}
        or pod.get("serviceAccountName") != ADMIN_NAME
        or pod.get("automountServiceAccountToken") is not True
        or pod.get("enableServiceLinks") is not False
        or pod.get("terminationGracePeriodSeconds") != 30
        or pod.get("volumes") not in (None, [])
        or pod.get("securityContext")
        != {
            "runAsNonRoot": True,
            "runAsUser": 65532,
            "runAsGroup": 65532,
            "seccompProfile": {"type": "RuntimeDefault"},
        }
        or container.get("name") != "admin"
        or container.get("image") != image
        or container.get("imagePullPolicy") != "Never"
        or container.get("env")
        != [{"name": "TENANT_ADMIN_PROVIDER", "value": "local"}]
        or container.get("ports")
        != [{"name": "http", "containerPort": 8080, "protocol": "TCP"}]
        or container.get("securityContext")
        != {
            "runAsNonRoot": True,
            "privileged": False,
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
        }
    ):
        raise RuntimeError("Tenant Admin Deployment contract does not match")
    for name, path in (("livenessProbe", "/healthz"), ("readinessProbe", "/readyz")):
        probe = _required_mapping(container.get(name), f"{name}")
        http_get = _required_mapping(probe.get("httpGet"), f"{name} HTTP request")
        if http_get.get("path") != path or http_get.get("port") != "http":
            raise RuntimeError(f"Tenant Admin {name} contract does not match")
    if require_ready:
        status = _required_mapping(deployment.get("status"), "Deployment status")
        if (
            status.get("readyReplicas") != 1
            or status.get("updatedReplicas") != 1
            or status.get("availableReplicas") != 1
            or status.get("observedGeneration") != metadata.get("generation")
        ):
            raise RuntimeError("Tenant Admin Deployment is not exactly ready")
    return uid


def _verify_effective_rbac(root: Path, client: ManagementClient) -> None:
    inventory = client.kubectl(
        "get",
        "--raw",
        f"/api/v1/namespaces?limit={ADMIN_NAMESPACE_LIMIT + 1}",
        check=False,
    )
    if inventory.returncode != 0:
        raise RuntimeError("Tenant Admin Namespace inventory failed")
    try:
        namespaces = admin_review_namespaces(json.loads(inventory.stdout))
    except json.JSONDecodeError as exc:
        raise RuntimeError("Tenant Admin Namespace inventory is invalid") from exc
    for namespace in namespaces:
        response = client.kubectl(
            "create",
            "--validate=false",
            "-f",
            "-",
            "-o",
            "json",
            f"--as={ADMIN_IDENTITY}",
            input_text=admin_rules_review_request(namespace),
            check=False,
        )
        if response.returncode != 0:
            raise RuntimeError(
                f"Tenant Admin effective RBAC review failed in {namespace}"
            )
        try:
            review = json.loads(response.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Tenant Admin effective RBAC review is invalid in {namespace}"
            ) from exc
        validate_admin_effective_rules(
            root, "local", review, namespace,
            lambda scope, resource: client.json(
                "get", "-n", scope, resource
            ) if not resource.startswith("namespace/") else client.json("get", resource),
        )


def _service_proxy_response(client: ManagementClient, path: str):
    return client.kubectl(
        "get",
        "--raw",
        f"{ADMIN_SERVICE_PROXY}/{path.lstrip('/')}",
        check=False,
    )


def _service_proxy(client: ManagementClient, path: str) -> str:
    response = _service_proxy_response(client, path)
    if response.returncode != 0:
        raise RuntimeError(f"Tenant Admin API is unavailable: /{path}")
    return response.stdout


def _service_proxy_post(
    client: ManagementClient,
    path: str,
    payload: dict[str, object],
):
    return client.request_json(
        "POST",
        f"{ADMIN_SERVICE_PROXY}/{path.lstrip('/')}",
        payload,
    )


def _service_proxy_delete(
    client: ManagementClient,
    path: str,
    payload: dict[str, object],
):
    return client.request_json(
        "DELETE",
        f"{ADMIN_SERVICE_PROXY}/{path.lstrip('/')}",
        payload,
    )


def _envelope(raw: str, description: str) -> object:
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Tenant Admin {description} response is not JSON") from exc
    if (
        not isinstance(envelope, dict)
        or set(envelope) != {"schemaVersion", "data"}
        or envelope.get("schemaVersion") != ADMIN_API_SCHEMA_VERSION
    ):
        raise RuntimeError(f"Tenant Admin {description} response envelope is invalid")
    return envelope["data"]


def create_tenant_via_admin(
    client: ManagementClient,
    name: str,
    *,
    workers: int,
) -> dict[str, object]:
    response = _service_proxy_post(
        client,
        "api/v1/tenants",
        {"name": name, "workers": workers},
    )
    if response.returncode != 0:
        raise RuntimeError(
            f"Tenant Admin create API is unavailable: {redact(response.stderr)}"
        )
    created = _required_mapping(
        _envelope(response.stdout, "Tenant create"),
        "Tenant create data",
    )
    identity = _required_mapping(created.get("identity"), "created Tenant identity")
    if (
        set(created) != {"identity", "provider", "kubernetesVersion"}
        or set(identity) != {"name", "uid", "generation"}
        or identity.get("name") != name
        or not isinstance(identity.get("uid"), str)
        or not identity["uid"]
        or not _is_integer(identity.get("generation"))
        or created.get("provider") != "local"
        or not isinstance(created.get("kubernetesVersion"), str)
        or not created["kubernetesVersion"]
    ):
        raise RuntimeError("Tenant Admin create response is invalid")
    if any(
        token in response.stdout.lower()
        for token in ("password", "clientsecret", "kubeconfig", "pgpass")
    ):
        raise RuntimeError("Tenant Admin create response contains credential material")
    return created


def delete_tenant_via_admin(
    client: ManagementClient,
    name: str,
    uid: str,
) -> dict[str, object]:
    response = _service_proxy_delete(
        client,
        f"api/v1/tenants/{name}",
        {"uid": uid, "confirmation": name},
    )
    if response.returncode != 0:
        raise RuntimeError("Tenant Admin delete API is unavailable")
    deleted = _required_mapping(
        _envelope(response.stdout, "Tenant delete"),
        "Tenant delete data",
    )
    identity = _required_mapping(deleted.get("identity"), "deleted Tenant identity")
    if (
        set(deleted) != {"identity", "state"}
        or set(identity) != {"name", "uid", "generation"}
        or identity.get("name") != name
        or identity.get("uid") != uid
        or (
            identity.get("generation") is not None
            and not _is_integer(identity.get("generation"))
        )
        or deleted.get("state") not in {"accepted", "completed"}
    ):
        raise RuntimeError("Tenant Admin delete response is invalid")
    return deleted


def _validate_database_query(
    client: ManagementClient,
    tenant_name: str,
    database: dict[str, object],
) -> None:
    cluster = _required_mapping(database.get("cluster"), "database cluster")
    instances = _required_list(cluster.get("instances"), "database instances")
    current_primary = cluster.get("currentPrimary")
    names = [
        _required_string(
            _required_mapping(instance, "database instance").get("name"),
            "database instance name",
        )
        for instance in instances
    ]
    instance = (
        current_primary
        if isinstance(current_primary, str) and current_primary in names
        else names[0] if names else None
    )
    if instance is None:
        raise RuntimeError("Tenant Admin database query instance is unavailable")
    response = _service_proxy_post(
        client,
        f"api/v1/tenants/{tenant_name}/database/query",
        {
            "instance": instance,
            "database": "postgres",
            "sql": "SELECT 1 AS value;",
        },
    )
    if response.returncode != 0:
        raise RuntimeError("Tenant Admin database query API is unavailable")
    query = _required_mapping(
        _envelope(response.stdout, "database query"),
        "database query data",
    )
    results = _required_list(query.get("results"), "database query results")
    if (
        set(query)
        != {
            "tenant",
            "cluster",
            "instance",
            "database",
            "executedAt",
            "durationMs",
            "truncated",
            "results",
        }
        or query.get("tenant") != tenant_name
        or query.get("cluster") != "capi-postgres"
        or query.get("instance") != instance
        or query.get("database") != "postgres"
        or not isinstance(query.get("executedAt"), str)
        or not query["executedAt"]
        or not _is_integer(query.get("durationMs"))
        or query["durationMs"] < 0
        or query.get("truncated") is not False
        or len(results) != 1
    ):
        raise RuntimeError("Tenant Admin database query response is invalid")
    result = _required_mapping(results[0], "database query result")
    if result != {
        "columns": ["value"],
        "rows": [["1"]],
        "affectedRows": 1,
        "truncated": False,
    }:
        raise RuntimeError("Tenant Admin database query result is invalid")


def _validate_summary(value: object) -> str:
    summary = _required_mapping(value, "Tenant summary")
    expected = {
        "name",
        "provider",
        "classification",
        "kubernetesVersion",
        "requestedWorkers",
        "endpoint",
        "createdAt",
        "conditions",
    }
    conditions = summary.get("conditions")
    if (
        set(summary) != expected
        or summary.get("provider") not in {"local", "azure", "unknown"}
        or summary.get("classification") not in ADMIN_CLASSIFICATIONS
        or not isinstance(summary.get("kubernetesVersion"), str)
        or not summary["kubernetesVersion"]
        or not _is_integer(summary.get("requestedWorkers"))
        or (
            summary.get("endpoint") is not None
            and not isinstance(summary.get("endpoint"), str)
        )
        or (
            summary.get("createdAt") is not None
            and not isinstance(summary.get("createdAt"), str)
        )
        or not isinstance(conditions, list)
    ):
        raise RuntimeError("Tenant Admin Tenant summary response is invalid")
    for condition_value in conditions:
        condition = _required_mapping(condition_value, "Tenant condition")
        if (
            set(condition)
            != {
                "type",
                "status",
                "reason",
                "message",
                "observedGeneration",
                "lastTransitionTime",
            }
            or not isinstance(condition.get("type"), str)
            or condition.get("status") not in ADMIN_CONDITION_STATUSES
            or any(
                condition.get(key) is not None
                and not isinstance(condition.get(key), str)
                for key in ("reason", "message")
            )
            or not _is_optional_timestamp(condition.get("lastTransitionTime"))
            or (
                condition.get("observedGeneration") is not None
                and not _is_integer(condition.get("observedGeneration"))
            )
        ):
            raise RuntimeError("Tenant Admin Tenant condition response is invalid")
    return _required_string(summary.get("name"), "Tenant summary name")


def _is_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_optional_string(value: object) -> bool:
    return value is None or isinstance(value, str)


def _is_optional_timestamp(value: object) -> bool:
    return value is None or _valid_timestamp(value)


def _validate_database_observation(
    value: object,
    *,
    provider: str,
    require_available: bool,
) -> tuple[str, int]:
    observation = _required_mapping(value, "database observation")
    state = observation.get("state")
    if state == "available":
        if (
            set(observation)
            != {"state", "observedAt", "freshness", "cluster"}
            or not _valid_timestamp(observation.get("observedAt"))
            or observation.get("freshness") != "live"
            or provider != "local"
        ):
            raise RuntimeError("Tenant Admin database observation is invalid")
        cluster = _required_mapping(
            observation.get("cluster"), "database cluster observation"
        )
        if set(cluster) != {
            "identity",
            "phase",
            "reason",
            "desiredInstances",
            "observedInstances",
            "readyInstances",
            "currentPrimary",
            "targetPrimary",
            "currentPrimarySince",
            "targetPrimaryRequestedAt",
            "currentPrimaryFailingSince",
            "image",
            "timeline",
            "services",
            "topologyAvailable",
            "nodesUsed",
            "instances",
            "storage",
            "conditions",
        }:
            raise RuntimeError("Tenant Admin database cluster response is invalid")
        identity = _required_mapping(
            cluster.get("identity"), "database cluster identity"
        )
        services = _required_mapping(
            cluster.get("services"), "database services"
        )
        storage = _required_mapping(cluster.get("storage"), "database storage")
        instances = _required_list(
            cluster.get("instances"), "database instances"
        )
        conditions = _required_list(
            cluster.get("conditions"), "database conditions"
        )
        if (
            set(identity)
            != {
                "apiVersion",
                "kind",
                "namespace",
                "name",
                "uid",
                "generation",
            }
            or identity.get("apiVersion") != "postgresql.cnpg.io/v1"
            or identity.get("kind") != "Cluster"
            or identity.get("namespace") != "database"
            or identity.get("name") != "capi-postgres"
            or not _is_optional_string(identity.get("uid"))
            or not _is_integer(identity.get("generation"))
            or set(services) != {"read", "write"}
            or not all(_is_optional_string(services.get(key)) for key in services)
            or set(storage)
            != {
                "total",
                "healthy",
                "dangling",
                "initializing",
                "resizing",
                "unusable",
            }
            or not all(_is_integer(item) and item >= 0 for item in storage.values())
            or not all(
                _is_integer(cluster.get(key)) and cluster[key] >= 0
                for key in (
                    "desiredInstances",
                    "observedInstances",
                    "readyInstances",
                )
            )
            or not all(
                _is_optional_string(cluster.get(key))
                for key in ("phase", "reason", "currentPrimary", "targetPrimary", "image")
            )
            or not all(
                _is_optional_timestamp(cluster.get(key))
                for key in (
                    "currentPrimarySince",
                    "targetPrimaryRequestedAt",
                    "currentPrimaryFailingSince",
                )
            )
            or (
                cluster.get("timeline") is not None
                and not _is_integer(cluster.get("timeline"))
            )
            or not isinstance(cluster.get("topologyAvailable"), bool)
            or (
                cluster.get("nodesUsed") is not None
                and not _is_integer(cluster.get("nodesUsed"))
            )
        ):
            raise RuntimeError("Tenant Admin database cluster response is invalid")
        primary_count = 0
        names = set()
        for value in instances:
            instance = _required_mapping(value, "database instance")
            if (
                set(instance)
                != {"name", "role", "status", "timeline", "node", "zone"}
                or not isinstance(instance.get("name"), str)
                or not instance["name"]
                or instance["name"] in names
                or instance.get("role") not in ADMIN_DATABASE_INSTANCE_ROLES
                or not _is_optional_string(instance.get("status"))
                or (
                    instance.get("timeline") is not None
                    and not _is_integer(instance.get("timeline"))
                )
                or not _is_optional_string(instance.get("node"))
                or not _is_optional_string(instance.get("zone"))
            ):
                raise RuntimeError("Tenant Admin database instance response is invalid")
            names.add(instance["name"])
            primary_count += instance["role"] == "primary"
        if primary_count > 1:
            raise RuntimeError("Tenant Admin database primary response is invalid")
        for value in conditions:
            condition = _required_mapping(value, "database condition")
            if (
                set(condition)
                != {
                    "type",
                    "status",
                    "reason",
                    "message",
                    "observedGeneration",
                    "lastTransitionTime",
                }
                or not isinstance(condition.get("type"), str)
                or condition.get("status") not in ADMIN_CONDITION_STATUSES
                or not _is_optional_string(condition.get("reason"))
                or not _is_optional_string(condition.get("message"))
                or (
                    condition.get("observedGeneration") is not None
                    and not _is_integer(condition.get("observedGeneration"))
                )
                or not _is_optional_timestamp(condition.get("lastTransitionTime"))
            ):
                raise RuntimeError("Tenant Admin database condition response is invalid")
        return state, len(instances)
    if state == "unavailable":
        if (
            set(observation)
            != {
                "state",
                "observedAt",
                "freshness",
                "reason",
                "message",
                "retryable",
            }
            or require_available
            or not _valid_timestamp(observation.get("observedAt"))
            or observation.get("freshness") != "live"
            or observation.get("reason")
            not in ADMIN_DATABASE_UNAVAILABLE_REASONS
            or not isinstance(observation.get("message"), str)
            or not observation["message"]
            or not isinstance(observation.get("retryable"), bool)
        ):
            raise RuntimeError("Tenant Admin unavailable database response is invalid")
        return state, 0
    if state == "not-applicable":
        if (
            set(observation)
            != {"state", "observedAt", "freshness", "reason"}
            or provider != "azure"
            or not _valid_timestamp(observation.get("observedAt"))
            or observation.get("freshness") != "live"
            or observation.get("reason") != "provider-unsupported"
        ):
            raise RuntimeError("Tenant Admin database applicability response is invalid")
        return state, 0
    raise RuntimeError("Tenant Admin database observation is invalid")


def _validate_topology(topology: dict[str, object], name: str) -> None:
    nodes = topology.get("nodes")
    edges = topology.get("edges")
    if (
        set(topology) != {"tenantName", "provider", "nodes", "edges"}
        or topology.get("tenantName") != name
        or topology.get("provider") not in {"local", "azure", "unknown"}
        or not isinstance(nodes, list)
        or not isinstance(edges, list)
        or not nodes
    ):
        raise RuntimeError("Tenant Admin Tenant topology response is invalid")
    node_ids: set[str] = set()
    for value in nodes:
        node = _required_mapping(value, "topology node")
        if (
            set(node)
            != {
                "id",
                "kind",
                "provenance",
                "label",
                "health",
                "resource",
                "attributes",
            }
            or not isinstance(node.get("id"), str)
            or not node["id"]
            or node.get("kind") not in ADMIN_TOPOLOGY_NODE_KINDS
            or node.get("provenance") not in ADMIN_TOPOLOGY_PROVENANCE
            or not isinstance(node.get("label"), str)
            or node.get("health") not in ADMIN_TOPOLOGY_HEALTH
            or (
                node.get("resource") is not None
                and not isinstance(node.get("resource"), dict)
            )
            or not isinstance(node.get("attributes"), list)
        ):
            raise RuntimeError("Tenant Admin topology node response is invalid")
        if node["id"] in node_ids:
            raise RuntimeError("Tenant Admin topology node identity is duplicated")
        node_ids.add(node["id"])
    for value in edges:
        edge = _required_mapping(value, "topology edge")
        if (
            set(edge) != {"id", "source", "target", "kind", "label"}
            or any(
                not isinstance(edge.get(key), str) or not edge[key]
                for key in ("id", "source", "target")
            )
            or edge.get("kind") not in ADMIN_TOPOLOGY_EDGE_KINDS
            or (
                edge.get("label") is not None
                and not isinstance(edge.get("label"), str)
            )
        ):
            raise RuntimeError("Tenant Admin topology edge response is invalid")
        if edge["source"] not in node_ids or edge["target"] not in node_ids:
            raise RuntimeError("Tenant Admin topology edge endpoint is missing")


def _valid_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return "T" in value and parsed.tzinfo is not None


def _validate_section(value: object, name: str) -> None:
    section = _required_mapping(value, f"{name} section")
    state = section.get("state")
    if state == "available":
        if set(section) != {"state"}:
            raise RuntimeError(f"Tenant Admin {name} section is invalid")
        return
    if (
        state != "unavailable"
        or set(section) != {"state", "code", "message", "retryable"}
        or not isinstance(section.get("code"), str)
        or not section["code"]
        or not isinstance(section.get("message"), str)
        or not section["message"]
        or not isinstance(section.get("retryable"), bool)
    ):
        raise RuntimeError(f"Tenant Admin {name} section is invalid")


def _validate_lifecycle(value: object) -> None:
    if not isinstance(value, list) or len(value) != len(ADMIN_LIFECYCLE_STAGES):
        raise RuntimeError("Tenant Admin lifecycle is invalid")
    for expected, item in zip(ADMIN_LIFECYCLE_STAGES, value, strict=True):
        stage = _required_mapping(item, "lifecycle stage")
        if (
            set(stage) != {"stage", "state", "message"}
            or stage.get("stage") != expected
            or stage.get("state") not in ADMIN_LIFECYCLE_STATES
            or (
                stage.get("message") is not None
                and not isinstance(stage.get("message"), str)
            )
        ):
            raise RuntimeError("Tenant Admin lifecycle stage is invalid")


def _validate_worker_capacity(value: object) -> None:
    capacity = _required_mapping(value, "worker capacity")
    if set(capacity) != {
        "desired",
        "available",
        "unavailable",
        "diagnosticReadyMachines",
    } or not _is_integer(capacity.get("desired")) or capacity["desired"] < 0:
        raise RuntimeError("Tenant Admin worker capacity is invalid")
    for key in ("available", "unavailable", "diagnosticReadyMachines"):
        observed = capacity.get(key)
        if observed is not None and (not _is_integer(observed) or observed < 0):
            raise RuntimeError("Tenant Admin worker capacity is invalid")
    if (
        capacity["available"] is None
        and capacity["unavailable"] is not None
    ) or (
        capacity["available"] is not None
        and capacity["unavailable"] is None
    ):
        raise RuntimeError("Tenant Admin worker capacity is incomplete")


def _validate_blockers(value: object, node_ids: set[str]) -> None:
    if not isinstance(value, list):
        raise RuntimeError("Tenant Admin blockers are invalid")
    for item in value:
        blocker = _required_mapping(item, "Tenant blocker")
        if (
            set(blocker)
            != {"code", "message", "conditionType", "targetNodeId"}
            or not isinstance(blocker.get("code"), str)
            or not blocker["code"]
            or not isinstance(blocker.get("message"), str)
            or not blocker["message"]
            or not _is_optional_string(blocker.get("conditionType"))
            or not _is_optional_string(blocker.get("targetNodeId"))
            or (
                blocker.get("targetNodeId") is not None
                and blocker["targetNodeId"] not in node_ids
            )
        ):
            raise RuntimeError("Tenant Admin blocker is invalid")


def _local_overview(
    client: ManagementClient,
) -> tuple[dict[str, object], tuple[str, ...]]:
    overview_snapshot = _required_mapping(
        _envelope(_service_proxy(client, "api/v1/overview"), "overview"),
        "overview data",
    )
    if set(overview_snapshot) != {"overview", "tenants"}:
        raise RuntimeError("Tenant Admin overview snapshot is invalid")
    overview = _required_mapping(
        overview_snapshot.get("overview"), "overview summary"
    )
    if (
        set(overview) != {"providerMode", "creation", "tenants", "components"}
        or overview.get("providerMode") != "local"
    ):
        raise RuntimeError("Tenant Admin overview response is invalid")
    creation = _required_mapping(
        overview.get("creation"), "overview creation capability"
    )
    if (
        set(creation)
        != {"available", "supportedKubernetesVersion", "reason"}
        or not isinstance(creation.get("available"), bool)
        or (
            creation["available"]
            and (
                not isinstance(creation.get("supportedKubernetesVersion"), str)
                or not creation["supportedKubernetesVersion"]
                or creation.get("reason") is not None
            )
        )
    ):
        raise RuntimeError("Tenant Admin creation capability is invalid")
    counts = _required_mapping(overview.get("tenants"), "overview counts")
    if set(counts) != {
        "total",
        "ready",
        "progressing",
        "degraded",
        "failed",
        "deleting",
    } or not all(_is_integer(value) for value in counts.values()):
        raise RuntimeError("Tenant Admin overview counts are invalid")
    summaries = _required_list(
        overview_snapshot.get("tenants"), "overview Tenant list"
    )
    names = tuple(_validate_summary(summary) for summary in summaries)
    if names != tuple(sorted(names)) or counts["total"] != len(names):
        raise RuntimeError("Tenant Admin overview and Tenant list disagree")
    return counts, names


def verify_admin_api(
    client: ManagementClient,
    *,
    expected_tenant_names: tuple[str, ...] | None = None,
    require_available_databases: bool = False,
    verify_database_queries: bool = False,
) -> dict[str, object]:
    for path in ("healthz", "readyz"):
        if _service_proxy(client, path):
            raise RuntimeError(f"Tenant Admin {path} response body must be empty")
    counts, names = _local_overview(client)
    if expected_tenant_names is not None and names != tuple(
        sorted(expected_tenant_names)
    ):
        raise RuntimeError("Tenant Admin Tenant list does not match expected identity")
    listed = _required_list(
        _envelope(_service_proxy(client, "api/v1/tenants"), "Tenant list"),
        "Tenant list data",
    )
    listed_names = tuple(_validate_summary(summary) for summary in listed)
    if listed_names != tuple(sorted(listed_names)):
        raise RuntimeError("Tenant Admin Tenant list is not sorted")
    for name in names:
        snapshot_response = _service_proxy_response(
            client,
            f"api/v1/tenants/{name}",
        )
        if snapshot_response.returncode != 0:
            _, refreshed_names = _local_overview(client)
            if name not in refreshed_names:
                continue
            raise RuntimeError(
                f"Tenant Admin API is unavailable: /api/v1/tenants/{name}"
            )
        snapshot = _required_mapping(
            _envelope(
                snapshot_response.stdout,
                f"Tenant {name} snapshot",
            ),
            "Tenant snapshot data",
        )
        if set(snapshot) != {
            "observedAt",
            "sections",
            "identity",
            "detail",
            "database",
            "topology",
        } or not _valid_timestamp(snapshot.get("observedAt")):
            raise RuntimeError("Tenant Admin Tenant snapshot is invalid")
        sections = _required_mapping(
            snapshot.get("sections"), "Tenant snapshot sections"
        )
        if set(sections) != {"resources", "databases"}:
            raise RuntimeError("Tenant Admin Tenant snapshot sections are invalid")
        _validate_section(sections.get("resources"), "resources")
        _validate_section(sections.get("databases"), "databases")
        identity = _required_mapping(
            snapshot.get("identity"), "Tenant snapshot identity"
        )
        detail = _required_mapping(snapshot.get("detail"), "Tenant detail data")
        if set(detail) != {
            "summary",
            "uid",
            "generation",
            "observedGeneration",
            "specification",
            "providerStatus",
            "lifecycle",
            "workerCapacity",
            "blockers",
            "managementResources",
        } or _validate_summary(detail.get("summary")) != name or (
            not _is_integer(detail.get("generation"))
            or (
                detail.get("observedGeneration") is not None
                and not _is_integer(detail.get("observedGeneration"))
            )
            or not isinstance(detail.get("specification"), dict)
            or not isinstance(detail.get("providerStatus"), dict)
            or not isinstance(detail.get("managementResources"), list)
        ):
            raise RuntimeError("Tenant Admin Tenant detail response is invalid")
        _validate_lifecycle(detail.get("lifecycle"))
        _validate_worker_capacity(detail.get("workerCapacity"))
        summary = _required_mapping(detail.get("summary"), "Tenant detail summary")
        _validate_database_observation(
            snapshot.get("database"),
            provider=summary["provider"],
            require_available=False,
        )
        detail_uid = _required_string(detail.get("uid"), "Tenant detail UID")
        if (
            identity
            != {
                "uid": detail_uid,
                "generation": detail["generation"],
                "observedGeneration": detail["observedGeneration"],
            }
        ):
            raise RuntimeError("Tenant Admin Tenant snapshot identity changed")
        snapshot_topology = _required_mapping(
            snapshot.get("topology"), "Tenant topology data"
        )
        _validate_topology(snapshot_topology, name)
        _validate_blockers(
            detail.get("blockers"),
            {
                _required_mapping(node, "topology node")["id"]
                for node in snapshot_topology["nodes"]
            },
        )
        catalog_client = CatalogClient(
            lambda path: _service_proxy(client, path),
            lambda method, path, body: client.request_json(
                method, f"{ADMIN_SERVICE_PROXY}/{path}", body,
            ),
            name, detail_uid,
        )
        catalog = catalog_client.read()
        if require_available_databases and not catalog["capabilityAvailable"]:
            raise RuntimeError("Tenant Admin catalog capability is unavailable")
        if verify_database_queries:
            entries = ready_entries(
                catalog,
                {entry["name"] for entry in catalog["databases"]},
                "local",
            )
            for entry in entries.values():
                catalog_client.probe(catalog["catalogUid"], entry)
        topology_response = _service_proxy_response(
            client,
            f"api/v1/tenants/{name}/topology",
        )
        if topology_response.returncode != 0:
            _, refreshed_names = _local_overview(client)
            if name not in refreshed_names:
                continue
            raise RuntimeError(
                "Tenant Admin API is unavailable: "
                f"/api/v1/tenants/{name}/topology"
            )
        topology = _required_mapping(
            _envelope(
                topology_response.stdout,
                f"Tenant {name} topology",
            ),
            "Tenant topology data",
        )
        _validate_topology(topology, name)
        catalog_nodes = {
            f"database:{entry['logicalUid']}"
            for entry in catalog["databases"]
        }
        if not catalog_nodes <= {
            node.get("id") for node in topology["nodes"] if isinstance(node, dict)
        }:
            raise RuntimeError("Tenant Admin catalog topology is incomplete")
    return {
        "schemaVersion": ADMIN_API_SCHEMA_VERSION,
        "tenantCount": len(names),
        "tenantNames": list(names),
        "counts": counts,
    }


def verify_local_admin(
    root: Path,
    client: ManagementClient,
    image: str,
    *,
    expected_deployment_uid: str | None = None,
    expected_tenant_names: tuple[str, ...] | None = None,
) -> dict[str, object]:
    deployment = client.json(
        "-n", ADMIN_NAMESPACE, "get", f"deployment/{ADMIN_NAME}"
    )
    uid = _verify_deployment_contract(deployment, image, require_ready=True)
    if expected_deployment_uid is not None and uid != expected_deployment_uid:
        raise RuntimeError("Tenant Admin Deployment UID changed during installation")
    pods = client.json(
        "-n",
        ADMIN_NAMESPACE,
        "get",
        "pods",
        "-l",
        ADMIN_LABEL,
    )
    items = _required_list(pods.get("items"), "Pod inventory")
    active = [
        item
        for item in items
        if isinstance(item, dict)
        and isinstance(item.get("metadata"), dict)
        and not item["metadata"].get("deletionTimestamp")
    ]
    if len(active) != 1:
        raise RuntimeError("Tenant Admin must have exactly one active Pod")
    pod = _required_mapping(active[0], "Pod")
    pod_metadata = _required_mapping(pod.get("metadata"), "Pod metadata")
    pod_uid = _required_string(pod_metadata.get("uid"), "Pod UID")
    if pod_metadata.get("deletionTimestamp"):
        raise RuntimeError("Tenant Admin Pod is terminating")
    pod_container = _required_mapping(
        _required_list(
            _required_mapping(pod.get("spec"), "Pod spec").get("containers"),
            "Pod containers",
        )[0],
        "Pod container",
    )
    if (
        pod_container.get("image") != image
        or pod_container.get("env")
        != [{"name": "TENANT_ADMIN_PROVIDER", "value": "local"}]
        or not any(
            condition.get("type") == "Ready"
            and condition.get("status") == "True"
            for condition in _required_list(
                _required_mapping(pod.get("status"), "Pod status").get("conditions"),
                "Pod conditions",
            )
            if isinstance(condition, dict)
        )
    ):
        raise RuntimeError("Tenant Admin Pod identity or readiness does not match")
    service_account = client.json(
        "-n", ADMIN_NAMESPACE, "get", f"serviceaccount/{ADMIN_NAME}"
    )
    role = client.json("get", f"clusterrole/{admin_role_name('local')}")
    binding = client.json("get", f"clusterrolebinding/{ADMIN_NAME}")
    service = client.json(
        "-n", ADMIN_NAMESPACE, "get", f"service/{ADMIN_NAME}"
    )
    _verify_static_resources(root, service_account, role, binding, service)
    _verify_effective_rbac(root, client)
    api = verify_admin_api(
        client,
        expected_tenant_names=expected_tenant_names,
    )
    service_spec = _required_mapping(service.get("spec"), "Service spec")
    return {
        "schemaVersion": ADMIN_API_SCHEMA_VERSION,
        "healthy": True,
        "deployment": {
            "name": ADMIN_NAME,
            "namespace": ADMIN_NAMESPACE,
            "uid": uid,
            "image": image,
            "provider": "local",
        },
        "pod": {
            "name": _required_string(pod_metadata.get("name"), "Pod name"),
            "uid": pod_uid,
        },
        "service": {
            "name": ADMIN_NAME,
            "clusterIP": service_spec["clusterIP"],
            "ports": service_spec["ports"],
        },
        "api": api,
    }


def reconcile_local_admin(
    root: Path,
    config: dict[str, str],
    client: ManagementClient,
) -> dict[str, object]:
    image = build_admin_image(root, config)
    _load_admin_image(root, config, image)
    for path in _admin_resource_paths(root):
        _apply(client, path)
    deployment = render_local_admin_deployment(root, image)
    _apply(client, deployment)
    client.kubectl(
        "rollout",
        "status",
        f"deployment/{ADMIN_NAME}",
        "-n",
        ADMIN_NAMESPACE,
        f"--timeout={config['CONDITION_TIMEOUT']}",
    )
    installed = client.json(
        "-n", ADMIN_NAMESPACE, "get", f"deployment/{ADMIN_NAME}"
    )
    uid = _required_string(
        _required_mapping(installed.get("metadata"), "Deployment metadata").get(
            "uid"
        ),
        "Deployment UID",
    )
    return verify_local_admin(
        root,
        client,
        image,
        expected_deployment_uid=uid,
    )


def _optional_resource(
    client: ManagementClient,
    namespace: str | None,
    resource: str,
) -> dict[str, object] | None:
    scope = ("-n", namespace) if namespace else ()
    response = client.kubectl(
        *scope,
        "get",
        resource,
        "--ignore-not-found=true",
        "-o",
        "json",
        check=False,
    )
    if response.returncode != 0:
        raise RuntimeError(f"failed to inspect Tenant Admin resource {resource}")
    if not response.stdout.strip():
        return None
    try:
        document = json.loads(response.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Tenant Admin resource {resource} is invalid") from exc
    return _required_mapping(document, resource)


def delete_local_admin(
    root: Path,
    config: dict[str, str],
    client: ManagementClient,
) -> None:
    deployment = _optional_resource(
        client, ADMIN_NAMESPACE, f"deployment/{ADMIN_NAME}"
    )
    service_account = _optional_resource(
        client, ADMIN_NAMESPACE, f"serviceaccount/{ADMIN_NAME}"
    )
    role = _optional_resource(
        client,
        None,
        f"clusterrole/{admin_role_name('local')}",
    )
    binding = _optional_resource(
        client, None, f"clusterrolebinding/{ADMIN_NAME}"
    )
    service = _optional_resource(
        client, ADMIN_NAMESPACE, f"service/{ADMIN_NAME}"
    )
    if service_account is not None:
        _verify_service_account(service_account)
    if role is not None:
        _verify_role(root, role)
    if binding is not None:
        _verify_binding(root, binding)
    if service is not None:
        _verify_service(service)
    if deployment is not None:
        container = _container(deployment)
        image = _required_string(container.get("image"), "Deployment image")
        _verify_deployment_contract(deployment, image, require_ready=False)
    for namespace, resource in (
        (ADMIN_NAMESPACE, f"deployment/{ADMIN_NAME}"),
        (ADMIN_NAMESPACE, f"service/{ADMIN_NAME}"),
        (None, f"clusterrolebinding/{ADMIN_NAME}"),
        (None, f"clusterrole/{admin_role_name('local')}"),
        (ADMIN_NAMESPACE, f"serviceaccount/{ADMIN_NAME}"),
    ):
        delete_named(config, client, namespace, resource)
    shutil.rmtree(root / ".runtime/rendered/admin", ignore_errors=True)


def collect_local_admin_status(
    root: Path,
    config: dict[str, str],
) -> dict[str, object]:
    require_management_ownership(root, config)
    validate_management_kubeconfig(root, config)
    client = ManagementClient(root, config)
    deployment = client.json(
        "-n", ADMIN_NAMESPACE, "get", f"deployment/{ADMIN_NAME}"
    )
    image = _required_string(_container(deployment).get("image"), "Deployment image")
    return verify_local_admin(root, client, image)


def admin_port_forward(root: Path, config: dict[str, str]) -> int:
    require_management_ownership(root, config)
    validate_management_kubeconfig(root, config)
    client = ManagementClient(root, config)
    command = [
        str(client.kubectl_path),
        "--kubeconfig",
        str(client.kubeconfig),
        "--context",
        client.context,
        "--request-timeout",
        config["KUBECTL_REQUEST_TIMEOUT"],
        "-n",
        ADMIN_NAMESPACE,
        "port-forward",
        f"service/{ADMIN_NAME}",
        "8080:80",
    ]
    try:
        return subprocess.run(command, check=False).returncode
    except OSError as exc:
        raise RuntimeError("failed to start Tenant Admin port-forward") from exc
