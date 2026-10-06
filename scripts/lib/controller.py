from __future__ import annotations

import hashlib
import json
import os
import re
import select
import shutil
import subprocess
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
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
from scripts.lib.database_controller import (
    CATALOG_CRD, build_database_controller_image, catalog_manifests,
    inspect_catalog_inventory, install_database_controller,
    require_absent_legacy_database_crd,
)
from scripts.lib.files import (
    ensure_private_dir, read_private_file, unlink_private_file, write_private_file,
)
from scripts.lib.kube import ManagementClient, wait_for
from scripts.lib.process import run
from scripts.lib.redaction import redact
from scripts.tools import verify_all_inputs

if TYPE_CHECKING:
    from scripts.cache import VerifiedCache


CONTROLLER_NAMESPACE = "tenant-system"
CONTROLLER_DEPLOYMENT = "tenant-controller"
TENANT_CRD = "tenants.tenancy.cnpg-vcluster.io"
TENANT_CUTOVER_POLICY = "tenant-api-cutover-create-lock"
CATALOG_CUTOVER_POLICY = "tenant-database-catalog-cutover-create-lock"
TENANT_API_CUTOVER_READY = True
CATALOG_LIFECYCLE_READY = True
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
                        "apiVersions": ["v1alpha3", "v1alpha4"],
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

def catalog_cutover_lock_documents(
    probe: tuple[str, str] | None = None,
) -> list[dict[str, object]]:
    expression = "false"
    if probe is not None:
        name, uid = probe
        if not re.fullmatch(r"[a-z0-9-]+", name) or not re.fullmatch(r"[a-z0-9-]+", uid):
            raise RuntimeError("invalid probe policy identity")
        expression = (
            "request.userInfo.username == 'system:serviceaccount:tenant-system:tenant-controller'"
            f" && object.metadata.namespace == 'tenant-db-{name}'"
            f" && object.metadata.name == '{name}'"
            f" && object.spec.tenantName == '{name}'"
            f" && object.spec.tenantUID == '{uid}'"
            " && object.spec.closed == false"
            " && object.spec.entries.size() == 0"
            " && object.metadata.finalizers.size() == 1"
            " && object.metadata.finalizers[0] == 'tenancy.cnpg-vcluster.io/database-catalog-finalizer'"
            " && object.metadata.ownerReferences.size() == 1"
            " && object.metadata.ownerReferences[0].kind == 'Tenant'"
            " && object.metadata.ownerReferences[0].apiVersion == 'tenancy.cnpg-vcluster.io/v1alpha4'"
            f" && object.metadata.ownerReferences[0].name == '{name}'"
            f" && object.metadata.ownerReferences[0].uid == '{uid}'"
        )
        expression = f"({expression})"
    return [
        {
            "apiVersion": "admissionregistration.k8s.io/v1",
            "kind": "ValidatingAdmissionPolicy",
            "metadata": {"name": CATALOG_CUTOVER_POLICY},
            "spec": {
                "failurePolicy": "Fail",
                "matchConstraints": {"resourceRules": [{
                    "apiGroups": ["tenancy.cnpg-vcluster.io"],
                    "apiVersions": ["v1alpha1"],
                    "operations": ["CREATE"],
                    "resources": ["tenantdatabasecatalogs"],
                    "scope": "Namespaced",
                }]},
                "validations": [{
                    "expression": expression,
                    "message": "TenantDatabaseCatalog creation is locked during API cutover",
                }],
            },
        },
        {
            "apiVersion": "admissionregistration.k8s.io/v1",
            "kind": "ValidatingAdmissionPolicyBinding",
            "metadata": {"name": CATALOG_CUTOVER_POLICY},
            "spec": {
                "policyName": CATALOG_CUTOVER_POLICY,
                "validationActions": ["Deny"],
            },
        },
    ]


def ensure_catalog_cutover_lock(client: ManagementClient) -> None:
    for document in catalog_cutover_lock_documents():
        client.kubectl(
            "apply", "--server-side",
            "--field-manager=cnpg-vcluster-catalog-cutover",
            "--force-conflicts", "-f", "-",
            input_text=json.dumps(document),
        )
    for resource in (
        f"validatingadmissionpolicy/{CATALOG_CUTOVER_POLICY}",
        f"validatingadmissionpolicybinding/{CATALOG_CUTOVER_POLICY}",
    ):
        if not client.kubectl("get", resource, "-o", "name").stdout.strip():
            raise RuntimeError("catalog cutover CREATE fence is incomplete")


def verify_catalog_cutover_lock(
    client: ManagementClient, *, namespace: str,
    expected_identity: tuple[str, int, str, int] | None = None,
    probe: tuple[str, str] | None = None,
) -> tuple[str, int, str, int]:
    expected_policy, expected_binding = catalog_cutover_lock_documents(probe)
    expected_spec = expected_policy["spec"]

    def current_identity() -> tuple[str, int, str, int] | None:
        policy = client.json(
            "get", f"validatingadmissionpolicy/{CATALOG_CUTOVER_POLICY}",
        )
        binding = client.json(
            "get", f"validatingadmissionpolicybinding/{CATALOG_CUTOVER_POLICY}",
        )
        if not isinstance(policy, dict) or not isinstance(binding, dict):
            raise RuntimeError("catalog cutover CREATE fence contract is malformed")
        policy_spec = policy.get("spec")
        if not isinstance(policy_spec, dict):
            raise RuntimeError("catalog cutover CREATE fence contract is malformed")
        constraints = policy_spec.get("matchConstraints", {})
        policy_meta = policy.get("metadata", {})
        binding_meta = binding.get("metadata", {})
        policy_status = policy.get("status")
        if (
            not isinstance(policy_meta, dict)
            or not isinstance(binding_meta, dict)
            or not isinstance(constraints, dict)
            or policy_meta.get("name") != CATALOG_CUTOVER_POLICY
            or not isinstance(policy_meta.get("uid"), str)
            or not policy_meta["uid"]
            or not isinstance(policy_meta.get("generation"), int)
            or isinstance(policy_meta["generation"], bool)
            or policy_meta["generation"] < 1
            or binding_meta.get("name") != CATALOG_CUTOVER_POLICY
            or not isinstance(binding_meta.get("uid"), str)
            or not binding_meta["uid"]
            or not isinstance(binding_meta.get("generation"), int)
            or isinstance(binding_meta["generation"], bool)
            or binding_meta["generation"] < 1
            or policy_spec.get("failurePolicy") != "Fail"
            or constraints.get("resourceRules") != expected_spec["matchConstraints"]["resourceRules"]
            or set(constraints) - {
                "resourceRules", "excludeResourceRules", "namespaceSelector",
                "objectSelector", "matchPolicy",
            }
            or constraints.get("excludeResourceRules")
            or constraints.get("namespaceSelector")
            or constraints.get("objectSelector")
            or constraints.get("matchPolicy", "Equivalent") != "Equivalent"
            or policy_spec.get("validations") != expected_spec["validations"]
            or policy_spec.get("matchConditions")
            or policy_spec.get("paramKind")
            or binding.get("spec") != expected_binding["spec"]
        ):
            raise RuntimeError("catalog cutover CREATE fence contract is malformed")
        if policy_status is None:
            return None
        if not isinstance(policy_status, dict):
            raise RuntimeError("catalog cutover CREATE fence contract is malformed")
        conditions = policy_status.get("conditions", [])
        type_checking = policy_status.get("typeChecking")
        if not isinstance(conditions, list):
            raise RuntimeError("catalog cutover CREATE fence contract is malformed")
        if (
            policy_status.get("observedGeneration") != policy_meta["generation"]
            or not isinstance(type_checking, dict)
            or any(
                not isinstance(condition, dict)
                or condition.get("observedGeneration") != policy_meta["generation"]
                or condition.get("status") != "True"
                for condition in conditions
            )
        ):
            return None
        if type_checking.get("expressionWarnings", []) != []:
            raise RuntimeError("catalog cutover CREATE fence contract is malformed")
        return (
            policy_meta["uid"], policy_meta["generation"],
            binding_meta["uid"], binding_meta["generation"],
        )

    timeout = parse_duration(
        getattr(client, "config", {}).get("CONDITION_TIMEOUT", "90s")
    )
    identity = wait_for(
        "catalog cutover CREATE fence observation", timeout, 2, current_identity,
    )
    if expected_identity is not None and identity != expected_identity:
        raise RuntimeError("catalog cutover CREATE fence identity changed before release")
    name = f"database-lock-probe-{uuid.uuid4().hex[:8]}"
    document = {
        "apiVersion": "tenancy.cnpg-vcluster.io/v1alpha1",
        "kind": "TenantDatabaseCatalog",
        "metadata": {
            "name": name,
            "namespace": namespace,
        },
        "spec": {"tenantName": name, "tenantUID": "probe", "closed": False},
    }
    for _ in range(5):
        if current_identity() != identity:
            raise RuntimeError("catalog cutover CREATE fence identity changed during probing")
        result = client.kubectl(
            "create", "--dry-run=server", "-f", "-",
            input_text=json.dumps(document), check=False,
        )
        if (result.returncode == 0
            or CATALOG_CUTOVER_POLICY not in result.stderr
            or "TenantDatabaseCatalog creation is locked during API cutover"
            not in result.stderr):
            raise RuntimeError("catalog cutover CREATE fence is not effective")
    if current_identity() != identity:
        raise RuntimeError("catalog cutover CREATE fence identity changed during probing")
    return identity


def verify_catalog_release_deployment(
        client: ManagementClient, *, name: str, image: str, service_account: str,
        provider: str | None = None, probe: tuple[str, str] | None = None,
) -> None:
        if not image or not service_account:
            raise RuntimeError(f"{name} release image or service account is not specified")
        deployment = client.json(
            "-n", CONTROLLER_NAMESPACE, "get", f"deployment/{name}",
        )
        meta = deployment.get("metadata", {})
        spec = deployment.get("spec", {})
        status = deployment.get("status", {})
        template = spec.get("template", {}).get("spec", {})
        selector = spec.get("selector", {}).get("matchLabels", {})
        if (
            not isinstance(meta, dict) or not isinstance(spec, dict)
            or not isinstance(status, dict) or not isinstance(template, dict)
            or not isinstance(selector, dict) or not selector
            or not isinstance(meta.get("uid"), str) or not meta["uid"]
            or not isinstance(meta.get("generation"), int)
            or isinstance(meta["generation"], bool)
            or status.get("observedGeneration") != meta["generation"]
            or spec.get("replicas") != 1
            or spec.get("strategy", {}).get("type") != "Recreate"
            or status.get("replicas") != 1
            or status.get("readyReplicas") != 1
            or status.get("updatedReplicas") != 1
            or status.get("availableReplicas") != 1
            or template.get("serviceAccountName") != service_account
            or not any(
                isinstance(container, dict)
                and container.get("name") == "manager"
                and container.get("image") == image
                and (
                    provider is None or (
                        isinstance(container.get("args"), list)
                        and container["args"].count(f"--provider={provider}") == 1
                        and container.get("args", []).count(f"--controller-image={image}") == 1
                        and all(
                            not isinstance(arg, str)
                            or not arg.startswith(("--provider=", "--controller-image="))
                            or arg in (f"--provider={provider}", f"--controller-image={image}")
                            for arg in container["args"]
                        )
                    )
                )
                for container in template.get("containers", [])
            )
        ):
            raise RuntimeError(f"{name} release rollout is not exact and Ready")
        pods = client.json(
            "-n", CONTROLLER_NAMESPACE, "get", "pods",
            "-l", ",".join(f"{key}={value}" for key, value in sorted(selector.items())),
        ).get("items")
        if not isinstance(pods, list) or len(pods) != 1:
            raise RuntimeError(f"{name} release rollout has no exact Ready Pod")
        pod = pods[0]
        pod_meta = pod.get("metadata", {})
        pod_spec = pod.get("spec", {})
        if (
            not isinstance(pod_meta, dict) or not isinstance(pod_spec, dict)
            or pod_meta.get("deletionTimestamp")
            or pod_spec.get("serviceAccountName") != service_account
            or not isinstance(pod_meta.get("uid"), str) or not pod_meta["uid"]
            or not all(pod_meta.get("labels", {}).get(k) == v for k, v in selector.items())
            or not any(
                isinstance(container, dict)
                and container.get("name") == "manager"
                and container.get("image") == image
                and (
                    provider is None or (
                        isinstance(container.get("args"), list)
                        and container["args"].count(f"--provider={provider}") == 1
                        and container.get("args", []).count(f"--controller-image={image}") == 1
                        and all(
                            not isinstance(arg, str)
                            or not arg.startswith(("--provider=", "--controller-image="))
                            or arg in (f"--provider={provider}", f"--controller-image={image}")
                            for arg in container["args"]
                        )
                    )
                )
                for container in pod_spec.get("containers", [])
            )
            or not any(
                condition.get("type") == "Ready" and condition.get("status") == "True"
                for condition in pod.get("status", {}).get("conditions", [])
            )
        ):
            raise RuntimeError(f"{name} release rollout has no exact Ready Pod")
        owners = pod_meta.get("ownerReferences", [])
        if (
            not isinstance(owners, list) or len(owners) != 1
            or owners[0].get("kind") != "ReplicaSet"
            or owners[0].get("controller") is not True
            or not isinstance(owners[0].get("uid"), str)
            or not owners[0]["uid"]
            or not isinstance(owners[0].get("name"), str)
            or not owners[0]["name"]
        ):
            raise RuntimeError(f"{name} release Pod is not owned by an exact ReplicaSet")
        replica_set = client.json(
            "-n", CONTROLLER_NAMESPACE, "get", f"replicaset/{owners[0]['name']}",
        )
        replica_meta = replica_set.get("metadata", {})
        if (
            replica_meta.get("uid") != owners[0]["uid"]
            or not any(
                owner.get("kind") == "Deployment"
                and owner.get("controller") is True
                and owner.get("name") == name
                and owner.get("uid") == meta["uid"]
                for owner in replica_meta.get("ownerReferences", [])
            )
            or replica_set.get("status", {}).get("readyReplicas") != 1
            or replica_set.get("spec", {}).get("replicas") != 1
        ):
            raise RuntimeError(f"{name} release ReplicaSet owner is not exact and Ready")
        if name == "database-controller":
            pod_name = pod_meta.get("name")
            if not isinstance(pod_name, str) or not pod_name:
                raise RuntimeError("database-controller Ready Pod name is absent")
            if probe is None:
                raise RuntimeError("database-controller readiness requires an exact catalog probe")

            def receipt_ready() -> bool:
                try:
                    _verify_catalog_observation(client, pod_name, pod_meta["uid"], *probe)
                except RuntimeError as exc:
                    if str(exc).startswith(
                        "database-controller exact probe observation is unavailable:"
                    ):
                        return False
                    raise
                return True

            wait_for("database-controller exact probe receipt", 90, 2, receipt_ready)


def _verify_catalog_observation(
        client: ManagementClient, pod_name: str, pod_uid: str,
        name: str, tenant_uid: str,
) -> None:
        namespace = f"tenant-db-{name}"
        catalog = client.json("-n", namespace, "get", f"tenantdatabasecatalog/{name}")
        metadata = catalog.get("metadata", {})
        observer = catalog.get("status", {}).get("observer", {})
        catalog_uid = metadata.get("uid")
        resource_version = metadata.get("resourceVersion")
        if (
            metadata.get("namespace") != namespace or metadata.get("name") != name
            or catalog.get("spec", {}).get("tenantUID") != tenant_uid
            or catalog.get("spec", {}).get("entries") != {}
            or catalog.get("status", {}).get("entries", {}) != {}
            or not isinstance(catalog_uid, str) or not catalog_uid
            or not isinstance(resource_version, str) or not resource_version
            or not isinstance(observer, dict)
            or observer.get("catalogUID") != catalog_uid
            or observer.get("observedGeneration") != metadata.get("generation")
            or observer.get("podUID") != pod_uid
            or not isinstance(observer.get("instanceId"), str)
            or not observer["instanceId"]
            or not isinstance(observer.get("observedResourceVersion"), str)
            or not observer["observedResourceVersion"]
        ):
            raise RuntimeError("database-controller has not observed the exact probe")
        from urllib.parse import urlencode
        query = urlencode({
            "namespace": namespace, "name": name,
            "catalogUID": catalog_uid, "resourceVersion": resource_version,
        })
        result = client.kubectl(
            "get", "--request-timeout=0", "--raw",
            f"/api/v1/namespaces/{CONTROLLER_NAMESPACE}/pods/{pod_name}:8082/proxy/observation?{query}",
            check=False,
        )
        if result.returncode:
            raise RuntimeError(
                "database-controller exact probe observation is unavailable: "
                + redact(result.stderr)[:512]
            )
        try:
            receipt = json.loads(result.stdout)
        except ValueError as exc:
            raise RuntimeError("database-controller exact probe receipt is invalid") from exc
        if (
            not isinstance(receipt, dict)
            or receipt.get("resourceVersion") != resource_version
            or receipt.get("observer") != observer
        ):
            raise RuntimeError("database-controller exact probe receipt changed")


def verify_catalog_release_rbac(
        client: ManagementClient, *, namespace: str = CONTROLLER_NAMESPACE,
) -> None:
        rules = (
            ("tenant-controller", "create", "tenantdatabasecatalogs.tenancy.cnpg-vcluster.io", True),
            ("tenant-controller", "get", "tenantdatabasecatalogs.tenancy.cnpg-vcluster.io", True),
            ("tenant-controller", "update", "tenants/status.tenancy.cnpg-vcluster.io", False),
            ("tenant-controller", "create", "namespaces", False),
            ("tenant-controller", "delete", "roles.rbac.authorization.k8s.io", True),
            ("tenant-controller", "delete", "rolebindings.rbac.authorization.k8s.io", True),
            ("database-controller", "get", "tenantdatabasecatalogs.tenancy.cnpg-vcluster.io", True),
            ("database-controller", "list", "tenantdatabasecatalogs.tenancy.cnpg-vcluster.io", False),
            ("database-controller", "watch", "tenantdatabasecatalogs.tenancy.cnpg-vcluster.io", False),
            ("database-controller", "update", "tenantdatabasecatalogs/status.tenancy.cnpg-vcluster.io", True),
            ("database-controller", "get", "tenants.tenancy.cnpg-vcluster.io", False),
        )
        for account, verb, resource, namespaced in rules:
            arguments = (
                "auth", "can-i", verb, resource,
                f"--as=system:serviceaccount:{CONTROLLER_NAMESPACE}:{account}",
                *(("--namespace=" + namespace,) if namespaced else ()),
                *(("--all-namespaces=true",)
                  if verb in ("list", "watch") and not namespaced else ()),
            )
            result = client.kubectl(*arguments, check=False)
            if result.returncode or result.stdout.strip() != "yes":
                raise RuntimeError(f"{account} effective RBAC cannot {verb} {resource}")


def verify_catalog_release_capability(
        client: ManagementClient, *, provider: str,
        probe: tuple[str, str] | None = None,
) -> None:
        tenants = client.json("get", "tenants.tenancy.cnpg-vcluster.io")
        items = tenants.get("items") if isinstance(tenants, dict) else None
        if not isinstance(items, list) or not items:
            raise RuntimeError("database provider capability has no live Tenant evidence")
        if probe is not None and (
            len(items) != 1
            or items[0].get("metadata", {}).get("name") != probe[0]
            or items[0].get("metadata", {}).get("uid") != probe[1]
            or tenants.get("metadata", {}).get("continue", "") != ""
        ):
            raise RuntimeError("database provider capability has a foreign Tenant")
        matched = False
        for tenant in items:
            metadata = tenant.get("metadata", {})
            spec = tenant.get("spec", {})
            status = tenant.get("status", {})
            if not isinstance(spec, dict) or not isinstance(status, dict):
                raise RuntimeError("database provider capability inventory is malformed")
            if spec.get("provider", {}).get("type") != provider:
                continue
            if probe is not None and (
                metadata.get("name") != probe[0] or metadata.get("uid") != probe[1]
            ):
                continue
            matched = True
            capability = status.get("databaseCapability", {})
            if (
                not isinstance(metadata, dict) or metadata.get("deletionTimestamp")
                or not isinstance(capability, dict)
                or capability.get("available") is not True
                or not isinstance(capability.get("namespace"), str)
                or not capability["namespace"]
                or not isinstance(capability.get("namespaceUID"), str)
                or not capability["namespaceUID"]
                or not isinstance(capability.get("catalogUID"), str)
                or not capability["catalogUID"]
                or not isinstance(metadata.get("uid"), str)
                or not metadata["uid"]
            ):
                raise RuntimeError("database provider capability is not Ready")
            namespace = client.json("get", f"namespace/{capability['namespace']}")
            catalog = client.json(
                "-n", capability["namespace"], "get",
                f"tenantdatabasecatalog/{metadata['name']}",
            )
            if (
                namespace.get("metadata", {}).get("uid") != capability["namespaceUID"]
                or catalog.get("metadata", {}).get("uid") != capability["catalogUID"]
                or catalog.get("spec", {}).get("tenantUID") != metadata["uid"]
                or catalog.get("spec", {}).get("tenantName") != metadata["name"]
                or catalog.get("spec", {}).get("closed") is not False
                or catalog.get("spec", {}).get("entries", {}) != {}
                or catalog.get("status", {}).get("entries", {}) != {}
                or catalog.get("metadata", {}).get("namespace") != capability["namespace"]
                or catalog.get("metadata", {}).get("name") != metadata["name"]
                or "tenancy.cnpg-vcluster.io/database-catalog-finalizer" not in (
                    catalog.get("metadata", {}).get("finalizers") or []
                )
                or capability["namespace"] != f"tenant-db-{metadata['name']}"
                or namespace.get("metadata", {}).get("name") != capability["namespace"]
                or namespace.get("metadata", {}).get("ownerReferences")
                or namespace.get("metadata", {}).get("labels", {}).get(
                    "tenancy.cnpg-vcluster.io/tenant-uid"
                ) != metadata["uid"]
                or not isinstance(catalog.get("metadata", {}).get("ownerReferences"), list)
                or len(catalog["metadata"]["ownerReferences"]) != 1
                or any(
                    catalog["metadata"]["ownerReferences"][0].get(key) != value
                    for key, value in {
                        "apiVersion": "tenancy.cnpg-vcluster.io/v1alpha4",
                        "kind": "Tenant", "name": metadata["name"], "uid": metadata["uid"],
                    }.items()
                )
            ):
                raise RuntimeError("database provider capability catalog identity is not exact")
            if provider == "azure":
                storage_name = f"tenant-db-storage-{metadata['name']}"
                storage_uid = capability.get("storageNamespaceUID")
                if not isinstance(storage_uid, str) or not storage_uid:
                    raise RuntimeError("Azure database storage namespace UID is absent")
                storage = client.json("get", f"namespace/{storage_name}")
                if (
                    storage.get("metadata", {}).get("uid") != storage_uid
                    or storage.get("metadata", {}).get("name") != storage_name
                    or storage.get("metadata", {}).get("ownerReferences")
                    or storage.get("metadata", {}).get("labels", {}).get(
                        "tenancy.cnpg-vcluster.io/tenant-uid"
                    ) != metadata["uid"]
                ):
                    raise RuntimeError("Azure database storage namespace identity is not exact")
            verify_catalog_release_rbac(
                client, namespace=capability["namespace"],
            )
            if probe is not None:
                response = client.kubectl(
                    "get", "--raw=/apis/tenancy.cnpg-vcluster.io/v1alpha1/tenantdatabasecatalogs",
                    check=False,
                )
                try:
                    listing = json.loads(response.stdout)
                except (ValueError, TypeError) as exc:
                    raise RuntimeError("database provider capability catalog inventory is invalid") from exc
                entries = listing.get("items") if isinstance(listing, dict) else None
                if (
                    response.returncode != 0 or not isinstance(listing, dict)
                    or listing.get("kind") != "TenantDatabaseCatalogList"
                    or listing.get("apiVersion") != "tenancy.cnpg-vcluster.io/v1alpha1"
                    or listing.get("metadata", {}).get("continue", "") != ""
                    or not isinstance(entries, list) or len(entries) != 1
                    or entries[0].get("metadata", {}).get("uid") != capability["catalogUID"]
                    or entries[0].get("metadata", {}).get("namespace") != capability["namespace"]
                    or entries[0].get("metadata", {}).get("name") != metadata["name"]
                ):
                    raise RuntimeError("database provider capability has a foreign catalog")
        if not matched:
            raise RuntimeError(f"database provider capability for {provider} is unavailable")


def verify_catalog_release_probe(
        client: ManagementClient, *, namespace: str,
) -> None:
        name = f"db-release-probe-{uuid.uuid4().hex[:10]}"
        reference = f"tenantdatabasecatalog/{name}"

        def require_absent() -> None:
            result = client.kubectl(
                "-n", namespace, "get", reference,
                "--ignore-not-found=true", "-o", "name", check=False,
            )
            if result.returncode or result.stdout.strip():
                raise RuntimeError("catalog release probe was persisted or cannot be inspected")

        require_absent()
        document = {
            "apiVersion": "tenancy.cnpg-vcluster.io/v1alpha1",
            "kind": "TenantDatabaseCatalog",
            "metadata": {"name": name, "namespace": namespace},
            "spec": {
                "tenantName": name, "tenantUID": "release-probe",
                "closed": False, "entries": {},
            },
        }
        result = client.kubectl(
            "create", "--dry-run=server", "-f", "-",
            input_text=json.dumps(document), check=False,
        )
        require_absent()
        if (
            result.returncode == 0 or CATALOG_CUTOVER_POLICY not in result.stderr
            or "TenantDatabaseCatalog creation is locked during API cutover"
            not in result.stderr
        ):
            raise RuntimeError(
                "catalog release probe did not receive the owned policy denial: "
                + redact(result.stderr)[:512]
            )


def verify_catalog_release_gates(
        client: ManagementClient, *, provider: str,
        tenant_image: str, database_image: str,
) -> tuple[str, int, str, int]:
        identity = verify_catalog_cutover_lock(client, namespace=CONTROLLER_NAMESPACE)
        verify_catalog_release_deployment(
            client, name=CONTROLLER_DEPLOYMENT, image=tenant_image,
            service_account="tenant-controller", provider=provider,
        )
        verify_catalog_release_rbac(client)
        verify_catalog_release_probe(client, namespace=CONTROLLER_NAMESPACE)
        verify_catalog_cutover_lock(
            client, namespace=CONTROLLER_NAMESPACE, expected_identity=identity,
        )
        return identity


def verify_release_tenant_cutover_lock(client: ManagementClient) -> None:
    expected_policy, expected_binding = tenant_cutover_lock_documents()

    def identity() -> tuple[str, int, str, int]:
        policy = client.json(
            "get", f"validatingadmissionpolicy/{TENANT_CUTOVER_POLICY}",
        )
        binding = client.json(
            "get", f"validatingadmissionpolicybinding/{TENANT_CUTOVER_POLICY}",
        )
        policy_meta = policy.get("metadata", {})
        binding_meta = binding.get("metadata", {})
        status = policy.get("status", {})
        conditions = status.get("conditions", [])
        policy_spec = policy.get("spec", {})
        constraints = policy_spec.get("matchConstraints", {})
        if (
            not isinstance(policy_meta, dict)
            or not isinstance(binding_meta, dict)
            or not isinstance(status, dict)
            or not isinstance(policy_spec, dict)
            or not isinstance(constraints, dict)
            or policy_meta.get("name") != TENANT_CUTOVER_POLICY
            or binding_meta.get("name") != TENANT_CUTOVER_POLICY
            or any(
                not isinstance(meta.get("uid"), str) or not meta["uid"]
                or not isinstance(meta.get("generation"), int)
                or isinstance(meta["generation"], bool)
                or meta["generation"] < 1
                for meta in (policy_meta, binding_meta)
            )
            or policy_spec.get("failurePolicy") != "Fail"
            or set(policy_spec) - {
                "failurePolicy", "matchConstraints", "validations",
                "matchConditions", "paramKind",
            }
            or constraints.get("resourceRules") != expected_policy["spec"]["matchConstraints"]["resourceRules"]
            or set(constraints) - {
                "resourceRules", "excludeResourceRules", "namespaceSelector",
                "objectSelector", "matchPolicy",
            }
            or constraints.get("excludeResourceRules")
            or constraints.get("namespaceSelector")
            or constraints.get("objectSelector")
            or constraints.get("matchPolicy", "Equivalent") != "Equivalent"
            or policy_spec.get("validations") != expected_policy["spec"]["validations"]
            or policy_spec.get("matchConditions")
            or policy_spec.get("paramKind")
            or binding.get("spec") != expected_binding["spec"]
            or status.get("observedGeneration") != policy_meta["generation"]
            or not isinstance(status.get("typeChecking"), dict)
            or status["typeChecking"].get("expressionWarnings", []) != []
            or not isinstance(conditions, list)
            or any(
                not isinstance(condition, dict)
                or condition.get("observedGeneration") != policy_meta["generation"]
                or condition.get("status") != "True"
                for condition in conditions
            )
        ):
            raise RuntimeError("Tenant cutover release fence contract is malformed")
        return (
            policy_meta["uid"], policy_meta["generation"],
            binding_meta["uid"], binding_meta["generation"],
        )

    observed = identity()
    document = {
        "apiVersion": "tenancy.cnpg-vcluster.io/v1alpha4",
        "kind": "Tenant",
        "metadata": {"name": f"release-lock-probe-{uuid.uuid4().hex[:10]}"},
        "spec": {
            "kubernetesVersion": "1.36.4", "workers": 1,
            "provider": {"type": "local"},
        },
    }
    for _ in range(5):
        if identity() != observed:
            raise RuntimeError("Tenant cutover release fence identity changed")
        result = client.kubectl(
            "create", "--dry-run=server", "-f", "-",
            input_text=json.dumps(document), check=False,
        )
        if (
            result.returncode == 0
            or TENANT_CUTOVER_POLICY not in result.stderr
            or "Tenant creation is locked during API cutover" not in result.stderr
        ):
            raise RuntimeError("Tenant cutover release fence is not effective")
    if identity() != observed:
        raise RuntimeError("Tenant cutover release fence identity changed")


def release_catalog_and_tenant_cutover_locks(
        config: dict[str, str], client: ManagementClient, *,
        provider: str, tenant_image: str, database_image: str,
        prepare_capability: Callable[[str], None] | None = None,
) -> None:
        if not CATALOG_LIFECYCLE_READY:
            raise RuntimeError("catalog lifecycle release is not approved")
        try:
            if _probe_record_path(client).exists() or (
                _catalog_lock_present(client) and not tenant_cutover_lock_present(client)
            ):
                apply_tenant_cutover_lock(config, client)
                ensure_catalog_cutover_lock(client)
            if not tenant_cutover_lock_present(client):
                raise RuntimeError("Tenant cutover lock is absent before catalog release")
            verify_release_tenant_cutover_lock(client)
            identity = verify_catalog_release_gates(
                client, provider=provider, tenant_image=tenant_image,
                database_image=database_image,
            )
            verify_release_tenant_cutover_lock(client)
            verify_catalog_cutover_lock(
                client, namespace=CONTROLLER_NAMESPACE, expected_identity=identity,
            )
            run_catalog_lifecycle_probe(
                config, client, provider=provider, database_image=database_image,
                prepare_capability=prepare_capability,
            )
            verify_catalog_cutover_lock(client, namespace=CONTROLLER_NAMESPACE)
            _record_catalog_activation(client)
            remove_catalog_cutover_lock(config, client)
        except Exception as failure:
            def restore_catalog_fence() -> None:
                ensure_catalog_cutover_lock(client)
                verify_catalog_cutover_lock(client, namespace=CONTROLLER_NAMESPACE)
            for restore in (
                lambda: apply_tenant_cutover_lock(config, client),
                restore_catalog_fence,
            ):
                try:
                    restore()
                except Exception as error:
                    failure.add_note(f"cutover fence restoration failed: {error}")
            raise


def _activation_record_path(client: ManagementClient) -> Path:
    return _probe_record_path(client).with_name("catalog-activation.json")


def _activation_identity(client: ManagementClient) -> dict[str, str]:
    resources = (
        ("clusterUID", "namespace/kube-system"),
        ("tenantCRDUID", f"crd/{TENANT_CRD}"),
        ("catalogCRDUID", "crd/tenantdatabasecatalogs.tenancy.cnpg-vcluster.io"),
    )
    identity = {}
    for key, reference in resources:
        resource = client.json("get", reference)
        uid = resource.get("metadata", {}).get("uid") if isinstance(resource, dict) else None
        if not isinstance(uid, str) or not re.fullmatch(r"[a-zA-Z0-9-]+", uid):
            raise RuntimeError(f"catalog activation identity is unavailable: {reference}")
        identity[key] = uid
    return identity


def _record_catalog_activation(client: ManagementClient) -> None:
    identity = _activation_identity(client)
    write_private_file(_activation_record_path(client), json.dumps({
        "schema": 1, **identity,
    }))


def _catalog_activation_complete(client: ManagementClient) -> bool:
    path = _activation_record_path(client)
    if not path.exists():
        return False
    try:
        record = json.loads(read_private_file(path))
    except ValueError as exc:
        raise RuntimeError("catalog activation record is malformed") from exc
    if (
        not isinstance(record, dict)
        or set(record) != {"schema", "clusterUID", "tenantCRDUID", "catalogCRDUID"}
        or record["schema"] != 1
    ):
        raise RuntimeError("catalog activation record is invalid")
    if record != {"schema": 1, **_activation_identity(client)}:
        raise RuntimeError("catalog activation belongs to another management cluster")
    return True


def _catalog_lock_present(client: ManagementClient) -> bool:
    refs = (
        f"validatingadmissionpolicy/{CATALOG_CUTOVER_POLICY}",
        f"validatingadmissionpolicybinding/{CATALOG_CUTOVER_POLICY}",
    )
    present = [
        bool(client.kubectl(
            "get", reference, "--ignore-not-found=true", "-o", "name",
        ).stdout.strip())
        for reference in refs
    ]
    if present[0] != present[1]:
        ensure_catalog_cutover_lock(client)
        verify_catalog_cutover_lock(client, namespace=CONTROLLER_NAMESPACE)
        return True
    return present[0]


def _probe_record_path(client: ManagementClient) -> Path:
    kubeconfig = getattr(client, "kubeconfig", None)
    if not isinstance(kubeconfig, Path):
        raise RuntimeError("bootstrap recovery requires a verified kubeconfig location")
    return kubeconfig.parent / "catalog-bootstrap-probe.json"


def _probe_record(client: ManagementClient) -> dict[str, object] | None:
    path = _probe_record_path(client)
    if not path.exists():
        return None
    try:
        record = json.loads(read_private_file(path))
    except ValueError as exc:
        raise RuntimeError("bootstrap recovery record is malformed") from exc
    if (
        not isinstance(record, dict) or record.get("schema") != 1
        or record.get("name") != "catalog-bootstrap-probe"
        or not isinstance(record.get("token"), str)
        or not re.fullmatch(r"[0-9a-f]{32}", record["token"])
        or record.get("provider") not in ("local", "azure")
        or record.get("issued") not in (True, False)
        or (record.get("uid") is not None and (
            not isinstance(record["uid"], str)
            or not re.fullmatch(r"[a-zA-Z0-9-]+", record["uid"])
        ))
    ):
        raise RuntimeError("bootstrap recovery record is invalid")
    return record


def _probe_resource(
    client: ManagementClient, reference: str, *, namespace: str | None = None,
) -> dict[str, object] | None:
    arguments = ("-n", namespace) if namespace else ()
    result = client.kubectl(
        *arguments, "get", reference, "--ignore-not-found=true", "-o", "json",
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"cannot inspect bootstrap identity {reference}: {result.stderr}")
    if not result.stdout.strip():
        return None
    try:
        payload = json.loads(result.stdout)
    except ValueError as exc:
        raise RuntimeError(f"bootstrap identity {reference} is invalid") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("metadata"), dict):
        raise RuntimeError(f"bootstrap identity {reference} is invalid")
    return payload


def _empty_database_namespace(client: ManagementClient, namespace: str) -> None:
    for resource in (
        "persistentvolumeclaims", "pods", "statefulsets.apps", "deployments.apps",
    ):
        arguments = ("-n", namespace)
        listing = client.json(*arguments, "get", resource)
        if (
            not isinstance(listing, dict)
            or not isinstance(listing.get("items"), list)
            or listing.get("metadata", {}).get("continue", "") != ""
            or listing["items"]
        ):
            raise RuntimeError(f"bootstrap database namespace has data or unknown inventory: {resource}")
    pv = client.json("get", "persistentvolumes")
    if (
        not isinstance(pv, dict) or not isinstance(pv.get("items"), list)
        or pv.get("metadata", {}).get("continue", "") != ""
        or any(
            item.get("spec", {}).get("claimRef", {}).get("namespace") == namespace
            for item in pv["items"]
        )
    ):
        raise RuntimeError("bootstrap namespace has persistent volume data or unknown inventory")
    crd = client.kubectl(
        "get", "crd/clusters.postgresql.cnpg.io",
        "--ignore-not-found=true", "-o", "name", check=False,
    )
    if crd.returncode or (crd.stdout.strip() not in ("", "customresourcedefinition.apiextensions.k8s.io/clusters.postgresql.cnpg.io")):
        raise RuntimeError("CNPG Cluster API discovery is uncertain")
    if crd.stdout.strip():
        clusters = client.json("-n", namespace, "get", "clusters.postgresql.cnpg.io")
        if (
            not isinstance(clusters, dict) or clusters.get("items") != []
            or clusters.get("metadata", {}).get("continue", "") != ""
        ):
            raise RuntimeError("bootstrap namespace has CNPG clusters or unknown inventory")


def _delete_probe_tenant_uid(
    config: dict[str, str], client: ManagementClient, name: str, uid: str,
) -> None:
    kubeconfig = getattr(client, "kubeconfig", None)
    kubectl = getattr(client, "kubectl_path", None)
    if not isinstance(kubeconfig, Path) or not isinstance(kubectl, Path):
        raise RuntimeError("UID-preconditioned bootstrap deletion needs a verified kubeconfig")
    context = getattr(client, "context", None)
    process = subprocess.Popen(
        [str(kubectl), "--kubeconfig", str(kubeconfig),
         *(["--context", context] if isinstance(context, str) and context else []),
         "proxy", "--address=127.0.0.1", "--port=0",
         "--accept-hosts=^127\\.0\\.0\\.1$"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        if process.stdout is None or not select.select(
            [process.stdout], [], [], min(parse_duration(config["COMMAND_TIMEOUT"]), 30),
        )[0]:
            raise RuntimeError("bootstrap API proxy did not become ready")
        started = process.stdout.readline().strip()
        match = re.fullmatch(r"Starting to serve on 127\.0\.0\.1:(\d+)", started)
        if not match or process.poll() is not None:
            raise RuntimeError("bootstrap API proxy did not become ready")
        request = urllib.request.Request(
            f"http://127.0.0.1:{match[1]}/apis/tenancy.cnpg-vcluster.io/v1alpha4/tenants/{name}",
            data=json.dumps({
                "apiVersion": "meta.k8s.io/v1", "kind": "DeleteOptions",
                "preconditions": {"uid": uid},
            }).encode(),
            headers={"Content-Type": "application/json"},
            method="DELETE",
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=parse_duration(config["COMMAND_TIMEOUT"])) as response:
            outcome = json.load(response)
        if not isinstance(outcome, dict) or outcome.get("status") == "Failure":
            raise RuntimeError("UID-preconditioned bootstrap deletion was not accepted")
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise RuntimeError("UID-preconditioned bootstrap deletion failed") from exc
    finally:
        process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()


def _verify_probe_exception(
    client: ManagementClient, name: str, uid: str, *, timeout: int = 90,
) -> None:
    namespace = f"tenant-db-{name}"

    def namespace_ready() -> bool:
        current = _probe_resource(client, f"namespace/{namespace}")
        if current is None:
            return False
        metadata = current.get("metadata", {})
        if (
            metadata.get("name") != namespace
            or not isinstance(metadata.get("uid"), str)
            or not metadata["uid"]
            or metadata.get("labels", {}).get(
                "tenancy.cnpg-vcluster.io/tenant-uid"
            ) != uid
            or metadata.get("deletionTimestamp")
            or metadata.get("ownerReferences")
        ):
            raise RuntimeError("bootstrap database namespace identity is occupied")
        return True

    wait_for(
        "UID-bound bootstrap database namespace",
        timeout,
        2,
        namespace_ready,
    )
    existing = _probe_resource(client, f"tenantdatabasecatalog/{name}", namespace=namespace)
    if existing is not None and (
        existing.get("metadata", {}).get("name") != name
        or existing.get("metadata", {}).get("namespace") != namespace
        or not isinstance(existing.get("metadata", {}).get("uid"), str)
        or not existing["metadata"]["uid"]
        or existing.get("spec", {}).get("tenantUID") != uid
        or existing.get("spec", {}).get("tenantName") != name
        or existing.get("spec", {}).get("closed") is not False
        or existing.get("spec", {}).get("entries", {}) != {}
        or existing.get("status", {}).get("entries", {}) != {}
        or existing.get("metadata", {}).get("finalizers") != [
            "tenancy.cnpg-vcluster.io/database-catalog-finalizer"
        ]
        or len(existing.get("metadata", {}).get("ownerReferences", [])) != 1
        or any(
            existing["metadata"]["ownerReferences"][0].get(key) != value
            for key, value in {
                "apiVersion": "tenancy.cnpg-vcluster.io/v1alpha4",
                "kind": "Tenant", "name": name, "uid": uid,
            }.items()
        )
    ):
        raise RuntimeError("bootstrap catalog identity is occupied")
    document = {
        "apiVersion": "tenancy.cnpg-vcluster.io/v1alpha1",
        "kind": "TenantDatabaseCatalog",
        "metadata": {
            "namespace": namespace, "name": name,
            "finalizers": ["tenancy.cnpg-vcluster.io/database-catalog-finalizer"],
            "ownerReferences": [{
                "apiVersion": "tenancy.cnpg-vcluster.io/v1alpha4",
                "kind": "Tenant", "name": name, "uid": uid,
            }],
        },
        "spec": {"tenantName": name, "tenantUID": uid, "closed": False, "entries": {}},
    }
    actor = f"--as=system:serviceaccount:{CONTROLLER_NAMESPACE}:tenant-controller"
    if existing is None:
        def exact_exception_ready() -> bool:
            result = client.kubectl(
                actor, "create", "--dry-run=server", "-f", "-",
                input_text=json.dumps(document), check=False,
            )
            if result.returncode == 0:
                return True
            if (
                CATALOG_CUTOVER_POLICY in result.stderr
                and "TenantDatabaseCatalog creation is locked during API cutover"
                in result.stderr
            ):
                return False
            raise RuntimeError(
                "UID-bound bootstrap policy exception was rejected: "
                + redact(result.stderr)[:512]
            )

        wait_for(
            "UID-bound bootstrap policy exception propagation",
            timeout,
            2,
            exact_exception_ready,
        )
    foreign = {
        **document,
        "spec": {**document["spec"], "tenantUID": "foreign-uid"},
    }
    result = client.kubectl(
            actor, "create", "--dry-run=server", "-f", "-",
            input_text=json.dumps(foreign), check=False,
    )
    if (
        result.returncode == 0 or CATALOG_CUTOVER_POLICY not in result.stderr
        or "TenantDatabaseCatalog creation is locked during API cutover" not in result.stderr
    ):
        raise RuntimeError("UID-bound bootstrap policy permits a foreign catalog")


def _recover_unknown_local_probe(
    config: dict[str, str], client: ManagementClient,
    record: dict[str, object],
) -> None:
    from scripts.lib.management import (
        require_management_ownership, validate_management_kubeconfig,
    )

    if record["provider"] != "local" or record["uid"] is not None or not record["issued"]:
        raise RuntimeError("unknown bootstrap CREATE recovery is local-only")
    name = "catalog-bootstrap-probe"
    namespace = f"tenant-db-{name}"
    storage_namespace = f"tenant-db-storage-{name}"
    apply_tenant_cutover_lock(config, client)
    ensure_catalog_cutover_lock(client)
    verify_release_tenant_cutover_lock(client)
    verify_catalog_cutover_lock(client, namespace=CONTROLLER_NAMESPACE)

    root = getattr(client, "root", None)
    if not isinstance(root, Path) or _probe_record_path(client) != (
        root / ".runtime" / "management" / "catalog-bootstrap-probe.json"
    ):
        raise RuntimeError("bootstrap recovery requires the recorded management checkout")
    identity = require_management_ownership(root, config)
    validate_management_kubeconfig(root, config)
    restarted_at = datetime.now(timezone.utc)
    run(
        ["docker", "restart", identity.identifier],
        timeout=parse_duration(config["COMMAND_TIMEOUT"]) + 60,
    )
    require_management_ownership(root, config).require_exact(identity)
    wait_for(
        "restarted management API", parse_duration(config["CONDITION_TIMEOUT"]),
        2,
        lambda: client.kubectl("get", "--raw=/readyz", check=False).returncode == 0,
    )
    client.kubectl(
        "-n", CONTROLLER_NAMESPACE, "rollout", "status",
        f"deployment/{CONTROLLER_DEPLOYMENT}",
        f"--timeout={config['CONDITION_TIMEOUT']}",
    )
    wait_for(
        "restarted database-controller leader and health",
        parse_duration(config["CONDITION_TIMEOUT"]), 2,
        lambda: _database_controller_restarted(client, since=restarted_at),
    )
    verify_release_tenant_cutover_lock(client)
    verify_catalog_cutover_lock(client, namespace=CONTROLLER_NAMESPACE)

    current = _probe_resource(client, f"tenant/{name}")
    if current is not None:
        metadata = current["metadata"]
        uid = metadata.get("uid")
        if (
            metadata.get("name") != name
            or not isinstance(uid, str) or not re.fullmatch(r"[a-zA-Z0-9-]+", uid)
            or metadata.get("annotations", {}).get(
                "tenancy.cnpg-vcluster.io/catalog-bootstrap"
            ) != "installer-owned"
            or metadata.get("annotations", {}).get(
                "tenancy.cnpg-vcluster.io/catalog-bootstrap-token"
            ) != record["token"]
            or current.get("spec") != {
                "kubernetesVersion": config["KUBERNETES_VERSION"].removeprefix("v"),
                "workers": 1, "provider": {"type": "local"},
            }
        ):
            raise RuntimeError("unknown bootstrap CREATE belongs to another Tenant")
        record["uid"] = uid
        record["deleting"] = True
        write_private_file(_probe_record_path(client), json.dumps(record))
        _delete_probe_tenant_uid(config, client, name, uid)
        client.kubectl(
            "wait", "--for=delete", f"tenant/{name}",
            f"--timeout={config['DELETE_TIMEOUT']}",
            timeout=parse_duration(config["DELETE_TIMEOUT"]) + 60,
        )
    _verify_probe_cleanup(client, name, namespace, storage_namespace)
    verify_release_tenant_cutover_lock(client)
    verify_catalog_cutover_lock(client, namespace=CONTROLLER_NAMESPACE)
    unlink_private_file(_probe_record_path(client))


def _database_controller_restarted(
    client: ManagementClient, *, since: datetime,
) -> bool:
    deployment = client.json(
        "-n", CONTROLLER_NAMESPACE, "get", "deployment/database-controller",
    )
    metadata = deployment.get("metadata", {})
    spec = deployment.get("spec", {})
    status = deployment.get("status", {})
    if (
        not isinstance(metadata, dict) or not isinstance(spec, dict)
        or not isinstance(status, dict)
        or spec.get("replicas") != 1
        or status.get("observedGeneration") != metadata.get("generation")
        or status.get("updatedReplicas") != 1
    ):
        return False
    pods = client.json(
        "-n", CONTROLLER_NAMESPACE, "get", "pods", "-l", "app=database-controller",
    )
    items = pods.get("items")
    if not isinstance(items, list) or len(items) != 1:
        return False
    pod = items[0]
    pod_meta = pod.get("metadata", {})
    pod_status = pod.get("status", {})
    containers = pod_status.get("containerStatuses") if isinstance(pod_status, dict) else None
    if (
        not isinstance(pod_meta, dict) or not isinstance(pod_status, dict)
        or not isinstance(pod_meta.get("uid"), str)
        or not isinstance(pod_meta.get("name"), str)
        or pod_meta.get("deletionTimestamp")
        or pod_status.get("phase") != "Running"
        or not isinstance(containers, list) or len(containers) != 1
        or not isinstance(containers[0], dict)
        or not isinstance(containers[0].get("state", {}).get("running"), dict)
    ):
        return False
    lease = client.json(
        "-n", "default", "get",
        "lease/database-controller.tenancy.cnpg-vcluster.io",
    ).get("spec", {})
    if not isinstance(lease, dict):
        return False
    holder = lease.get("holderIdentity")
    renewed = lease.get("renewTime")
    duration = lease.get("leaseDurationSeconds")
    if (
        not isinstance(holder, str)
        or not holder.startswith(f"{pod_meta['uid']}-")
        or not isinstance(renewed, str)
        or type(duration) is not int or duration <= 0
    ):
        return False
    try:
        observed = datetime.fromisoformat(renewed.replace("Z", "+00:00"))
    except ValueError:
        return False
    if observed.tzinfo is None:
        return False
    age = (datetime.now(timezone.utc) - observed).total_seconds()
    if not 0 <= age < duration or observed < since:
        return False
    return client.kubectl(
        "get", f"--raw=/api/v1/namespaces/{CONTROLLER_NAMESPACE}/pods/"
        f"{pod_meta['name']}:8082/proxy/healthz", check=False,
    ).returncode == 0


def run_catalog_lifecycle_probe(
    config: dict[str, str], client: ManagementClient, *,
    provider: str, database_image: str,
    prepare_capability: Callable[[str], None] | None = None,
) -> None:
    from scripts.lib.database_controller import inspect_catalog_inventory

    name = "catalog-bootstrap-probe"
    namespace = f"tenant-db-{name}"
    storage_namespace = f"tenant-db-storage-{name}"
    if provider not in ("local", "azure"):
        raise RuntimeError("unknown bootstrap provider")
    path = _probe_record_path(client)
    record = _probe_record(client)
    if record is not None and record["provider"] != provider:
        raise RuntimeError("bootstrap provider differs from recovery record")
    if record is not None and record["issued"] and record["uid"] is None:
        if provider != "local":
            raise RuntimeError("bootstrap Tenant CREATE outcome is unknown on managed API")
        _recover_unknown_local_probe(config, client, record)
        record = None
    inventory = client.json("get", "tenants.tenancy.cnpg-vcluster.io")
    current = _probe_resource(client, f"tenant/{name}") if record else None
    if (
        not isinstance(inventory, dict)
        or not isinstance(inventory.get("items"), list)
        or inventory.get("metadata", {}).get("continue", "") != ""
        or (record is None and inventory["items"])
        or (record is not None and (
            len(inventory["items"]) > 1
            or (len(inventory["items"]) == 1 and (
                current is None or inventory["items"][0].get("metadata", {}).get("uid")
                != current["metadata"].get("uid")
            ))
        ))
    ):
        raise RuntimeError("bootstrap requires an empty Tenant inventory")
    if record is None:
        inspect_catalog_inventory(client)
        if _probe_resource(client, f"namespace/{namespace}") is not None:
            raise RuntimeError("bootstrap namespace identity is occupied")
        if _probe_resource(client, f"namespace/{storage_namespace}") is not None:
            raise RuntimeError("bootstrap storage namespace identity is occupied")
        record = {
            "schema": 1, "name": name, "provider": provider,
            "token": uuid.uuid4().hex, "uid": None, "issued": False,
            "deleting": False,
        }
        write_private_file(path, json.dumps(record))
    document = {
        "apiVersion": "tenancy.cnpg-vcluster.io/v1alpha4",
        "kind": "Tenant",
        "metadata": {"name": name, "annotations": {
            "tenancy.cnpg-vcluster.io/catalog-bootstrap": "installer-owned",
            "tenancy.cnpg-vcluster.io/catalog-bootstrap-token": record["token"],
        }},
        "spec": {
            "kubernetesVersion": config[
                "KUBERNETES_VERSION" if provider == "local"
                else "AZURE_SUPPORTED_TENANT_KUBERNETES_VERSION"
            ].removeprefix("v"),
            "workers": 1, "provider": {"type": provider},
        },
    }
    if current is None and record["issued"] and record["uid"] is None:
        raise RuntimeError("bootstrap Tenant CREATE outcome is unknown; await exact identity")
    if current is None and record["uid"] is not None and not record["deleting"]:
        raise RuntimeError("bootstrap Tenant disappeared before UID-bound cleanup")
    unlocked = False
    if current is None and not record["issued"]:
        remove_tenant_cutover_lock(config, client)
        unlocked = True

        def create_admitted() -> bool:
            result = client.kubectl(
                "create", "--dry-run=server", "-f", "-",
                input_text=json.dumps(document), check=False,
            )
            if result.returncode == 0:
                return True
            if (
                TENANT_CUTOVER_POLICY in result.stderr
                and "Tenant creation is locked during API cutover" in result.stderr
            ):
                return False
            raise RuntimeError("bootstrap Tenant CREATE dry-run failed: "
                               + redact(result.stderr)[:512])

        wait_for(
            "bootstrap Tenant CREATE admission propagation",
            parse_duration(config["CONDITION_TIMEOUT"]), 2, create_admitted,
        )
        record["issued"] = True
        write_private_file(path, json.dumps(record))
        try:
            created = client.kubectl("create", "-f", "-", input_text=json.dumps(document))
            current = json.loads(created.stdout)
        except (RuntimeError, ValueError, KeyError, TypeError) as exc:
            current = _probe_resource(client, f"tenant/{name}")
            if current is None:
                raise RuntimeError(
                    "bootstrap Tenant creation outcome is unknown; do not delete it"
                ) from exc
    if current is not None and (
        current.get("metadata", {}).get("name") != name
        or current.get("metadata", {}).get("annotations", {}).get(
            "tenancy.cnpg-vcluster.io/catalog-bootstrap-token"
        ) != record["token"]
        or current.get("metadata", {}).get("annotations", {}).get(
            "tenancy.cnpg-vcluster.io/catalog-bootstrap"
        ) != "installer-owned"
        or current.get("spec") != document["spec"]
        or not isinstance(current.get("metadata", {}).get("uid"), str)
        or not re.fullmatch(r"[a-zA-Z0-9-]+", current["metadata"]["uid"])
        or (record["uid"] is not None and record["uid"] != current["metadata"]["uid"])
    ):
        raise RuntimeError("bootstrap Tenant identity is occupied or changed")
    if current is not None and record["uid"] is None:
        record["uid"] = current["metadata"]["uid"]
        write_private_file(path, json.dumps(record))
    uid = record["uid"]
    if record["deleting"]:
        if current is not None:
            _delete_probe_tenant_uid(config, client, name, uid)
            client.kubectl(
                "wait", "--for=delete", f"tenant/{name}",
                f"--timeout={config['DELETE_TIMEOUT']}",
                timeout=parse_duration(config["DELETE_TIMEOUT"]) + 60,
            )
        _verify_probe_cleanup(client, name, namespace, storage_namespace)
        ensure_catalog_cutover_lock(client)
        verify_catalog_cutover_lock(client, namespace=CONTROLLER_NAMESPACE)
        remove_tenant_cutover_lock(config, client)
        unlink_private_file(path)
        return
    if current is not None and not unlocked:
        remove_tenant_cutover_lock(config, client)
    probe = (name, uid)

    def intent_ready() -> bool:
        current = _probe_resource(client, f"tenant/{name}")
        if (
            current is None or current["metadata"].get("uid") != uid
            or current["metadata"].get("annotations", {}).get(
                "tenancy.cnpg-vcluster.io/catalog-bootstrap"
            ) != "installer-owned"
            or current["metadata"].get("annotations", {}).get(
                "tenancy.cnpg-vcluster.io/catalog-bootstrap-token"
            ) != record["token"]
            or current["metadata"].get("deletionTimestamp")
        ):
            raise RuntimeError("bootstrap Tenant identity changed")
        intent = current.get("status", {}).get("catalogCreateIntent", {})
        return intent == {"namespace": namespace, "name": name, "tenantUID": uid}

    wait_for(
        "bootstrap catalog creation intent", parse_duration(config["CONDITION_TIMEOUT"]),
        2, intent_ready,
    )
    for document in catalog_cutover_lock_documents(probe):
        client.kubectl(
            "apply", "--server-side", "--field-manager=cnpg-vcluster-catalog-cutover",
            "--force-conflicts", "-f", "-", input_text=json.dumps(document),
        )
    identity = verify_catalog_cutover_lock(
        client, namespace=CONTROLLER_NAMESPACE, probe=probe,
    )
    _verify_probe_exception(
        client,
        name,
        uid,
        timeout=parse_duration(config["CONDITION_TIMEOUT"]),
    )
    if prepare_capability is not None:
        prepare_capability(name)

    def capability_ready() -> bool:
        intent_ready()
        current = _probe_resource(client, f"tenant/{name}")
        if current.get("status", {}).get("databaseCapability", {}).get("available") is not True:
            return False
        verify_catalog_release_capability(client, provider=provider, probe=probe)
        _empty_database_namespace(client, namespace)
        if provider == "azure":
            _empty_database_namespace(client, storage_namespace)
        return True

    wait_for(
        "bootstrap database capability", parse_duration(config["CONDITION_TIMEOUT"]),
        2, capability_ready,
    )
    client.kubectl(
        "-n", CONTROLLER_NAMESPACE, "rollout", "status",
        "deployment/database-controller",
        f"--timeout={config['CONDITION_TIMEOUT']}",
    )
    verify_catalog_release_deployment(
        client, name="database-controller", image=database_image,
        service_account="database-controller", probe=probe,
    )
    verify_catalog_cutover_lock(
        client, namespace=CONTROLLER_NAMESPACE, expected_identity=identity, probe=probe,
    )
    _empty_database_namespace(client, namespace)
    if provider == "azure":
        _empty_database_namespace(client, storage_namespace)
    current = _probe_resource(client, f"tenant/{name}")
    if current is None or current["metadata"].get("uid") != uid:
        raise RuntimeError("bootstrap Tenant identity changed before cleanup")
    record["deleting"] = True
    write_private_file(path, json.dumps(record))
    _delete_probe_tenant_uid(config, client, name, uid)
    client.kubectl(
        "wait", "--for=delete", f"tenant/{name}",
        f"--timeout={config['DELETE_TIMEOUT']}",
        timeout=parse_duration(config["DELETE_TIMEOUT"]) + 60,
    )
    _verify_probe_cleanup(client, name, namespace, storage_namespace)
    ensure_catalog_cutover_lock(client)
    verify_catalog_cutover_lock(client, namespace=CONTROLLER_NAMESPACE)
    unlink_private_file(path)


def _verify_probe_cleanup(
        client: ManagementClient, name: str, namespace: str,
        storage_namespace: str,
) -> None:
    for reference, scope in (
        (f"tenant/{name}", None),
        (f"tenantdatabasecatalog/{name}", namespace),
        (f"namespace/{namespace}", None),
        (f"namespace/{storage_namespace}", None),
        (f"namespace/{name}", None),
        ("role/tenant-database-credentials", name),
        ("rolebinding/tenant-database-credentials", name),
    ):
        if _probe_resource(client, reference, namespace=scope) is not None:
            raise RuntimeError("bootstrap identity persists after Tenant deletion")
    inspect_catalog_inventory(client)
    inventory = client.json("get", "tenants.tenancy.cnpg-vcluster.io")
    if inventory.get("items") != [] or inventory.get("metadata", {}).get("continue", "") != "":
        raise RuntimeError("bootstrap Tenant inventory is not empty after deletion")


def catalog_cutover_lock_cleanup_refs() -> tuple[str, str]:
    return (
        f"validatingadmissionpolicybinding/{CATALOG_CUTOVER_POLICY}",
        f"validatingadmissionpolicy/{CATALOG_CUTOVER_POLICY}",
    )


def remove_catalog_cutover_lock(
    config: dict[str, str], client: ManagementClient,
) -> None:
    for resource in catalog_cutover_lock_cleanup_refs():
        client.kubectl(
            "delete", resource, "--ignore-not-found=true",
            "--wait=true", f"--timeout={config['DELETE_TIMEOUT']}",
        )


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
        name not in {"v1alpha3", "v1alpha4"}
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
    if names == {"v1alpha3", "v1alpha4"} and stored in (
        ["v1alpha3"],
        ["v1alpha3", "v1alpha4"],
        ["v1alpha4"],
    ):
        return "transitioning"
    return tenant_api_generation(crd)


def tenant_crd_transition_document(
    observed: dict[str, object],
    desired: dict[str, object],
) -> dict[str, object]:
    if tenant_api_generation(observed) != "v1alpha3":
        raise RuntimeError("Tenant CRD transition source is invalid")
    if tenant_api_generation({
        **desired,
        "status": {"storedVersions": ["v1alpha4"]},
    }) != "v1alpha4":
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
            "Tenant API v1alpha4 activation is blocked until provider lifecycle contracts are complete"
        )


def apply_tenant_cutover_lock(
    config: dict[str, str],
    client: ManagementClient,
) -> None:
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
        "metadata": {"name": f"cutover-lock-probe-{uuid.uuid4().hex[:10]}"},
        "spec": {
            "kubernetesVersion": "1.36.4",
            "workers": 1,
            "provider": (
                {"type": "local", "databases": 1}
                if generation == "v1alpha3" else {"type": "local"}
            ),
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
            raise RuntimeError(
                "Tenant cutover create lock is not effective"
                f" for {generation}: {redact(response.stderr)[:512]}"
            )


def remove_tenant_cutover_lock(config: dict[str, str], client: ManagementClient) -> None:
    for resource in tenant_cutover_lock_cleanup_refs():
        client.kubectl(
            "delete",
            resource,
            "--ignore-not-found=true",
            "--wait=true",
            f"--timeout={config['DELETE_TIMEOUT']}",
        )


def restore_controller(
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
        raise RuntimeError("old Tenant controller cannot be restored")
    client.kubectl(
        "-n",
        CONTROLLER_NAMESPACE,
        "scale",
        f"deployment/{CONTROLLER_DEPLOYMENT}",
        "--replicas=1",
    )
    client.kubectl(
        "-n",
        CONTROLLER_NAMESPACE,
        "rollout",
        "status",
        f"deployment/{CONTROLLER_DEPLOYMENT}",
        f"--timeout={config['CONDITION_TIMEOUT']}",
    )


def prepare_tenant_api_cutover(
    root: Path,
    config: dict[str, str],
    client: ManagementClient,
) -> bool:
    require_absent_legacy_database_crd(client)
    observed = client.kubectl(
        "get",
        f"crd/{TENANT_CRD}",
        "--ignore-not-found=true",
        "-o",
        "json",
    ).stdout.strip()
    if observed and tenant_api_cutover_state(json.loads(observed)) == "v1alpha4":
        if (
            not tenant_cutover_lock_present(client)
            and not _catalog_lock_present(client)
            and _catalog_activation_complete(client)
        ):
            return False
    ensure_catalog_cutover_lock(client)
    if not observed:
        if not tenant_cutover_lock_present(client):
            apply_tenant_cutover_lock(config, client)
        return True
    current = json.loads(observed)
    generation = tenant_api_cutover_state(current)
    if generation == "v1alpha4":
        if not tenant_cutover_lock_present(client):
            apply_tenant_cutover_lock(config, client)
        return True
    desired = desired_tenant_crd(root, client)
    transition = generation == "transitioning"
    transition_document = (
        None
        if transition
        else tenant_crd_transition_document(current, desired)
    )
    if transition:
        if not tenant_cutover_lock_present(client):
            raise RuntimeError("Tenant CRD transition is missing its create lock")
    else:
        if not tenant_cutover_lock_present(client):
            apply_tenant_cutover_lock(config, client)
    try:
        verify_tenant_cutover_lock(
            client,
            "v1alpha4" if transition else generation,
        )
        if not transition:
            require_clean_controller_state(root, client)
        stop_controller(config, client)
        require_clean_controller_state(root, client)
        if not transition:
            transition = True
            client.kubectl(
                "apply",
                "--server-side",
                "--field-manager=cnpg-vcluster-tenant-api-cutover",
                "--force-conflicts",
                "-f",
                "-",
                input_text=json.dumps(transition_document),
            )
        verify_tenant_cutover_lock(client, "v1alpha4")
        require_clean_controller_state(root, client)
        client.kubectl(
            "patch",
            f"crd/{TENANT_CRD}",
            "--subresource=status",
            "--type=merge",
            "-p",
            json.dumps({"status": {"storedVersions": ["v1alpha4"]}}),
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
            restore_controller(config, client)
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
    repository = root
    inputs = [
        *controller.joinpath("src").rglob("*.rs"),
        *root.joinpath("database-runtime", "src").rglob("*.rs"),
        root / "database-runtime" / "Cargo.toml",
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
    allocation_sha256: str,
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
        .replace("${TENANT_ALLOCATION_SHA256}", allocation_sha256)
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
        len(versions) != 1 or versions[0].get("name") != "v1alpha4"
        or versions[0].get("served") is not True or versions[0].get("storage") is not True
        or crd.get("status", {}).get("storedVersions") != ["v1alpha4"]
        or "status" not in versions[0].get("subresources", {})
        or crd["spec"].get("conversion", {}).get("strategy", "None") != "None"
    ):
        raise RuntimeError("Tenant CRD must serve and store only v1alpha4 with a status subresource")


def install_database_catalog(
    root: Path, config: dict[str, str], client: ManagementClient,
    *, azure: bool = False, cutover_locked: bool = True,
) -> None:
    require_absent_legacy_database_crd(client)
    if cutover_locked:
        ensure_catalog_cutover_lock(client)
    elif (
        not _catalog_activation_complete(client)
        or _catalog_lock_present(client)
        or tenant_cutover_lock_present(client)
    ):
        raise RuntimeError("completed catalog activation is no longer open")
    for path in catalog_manifests(root, azure=azure):
        client.kubectl(
            "apply", "--server-side",
            "--field-manager=cnpg-vcluster-database-catalog",
            "--force-conflicts", "-f", str(path),
        )
    timeout = config["CONDITION_TIMEOUT"]
    client.kubectl(
        "wait", "--for=condition=Established", f"crd/{CATALOG_CRD}",
        f"--timeout={timeout}",
    )
    crd = client.json("get", f"crd/{CATALOG_CRD}")
    versions = crd.get("spec", {}).get("versions", [])
    if (
        len(versions) != 1
        or versions[0].get("name") != "v1alpha1"
        or versions[0].get("served") is not True
        or versions[0].get("storage") is not True
        or crd.get("status", {}).get("storedVersions") != ["v1alpha1"]
        or "status" not in versions[0].get("subresources", {})
    ):
        raise RuntimeError("TenantDatabaseCatalog CRD must serve only v1alpha1 with status")
    for _ in range(5):
        discovery = client.kubectl(
            "get", "--raw=/apis/tenancy.cnpg-vcluster.io/v1alpha1", check=False,
        )
        try:
            resources = json.loads(discovery.stdout)["resources"]
        except (ValueError, KeyError, TypeError) as exc:
            raise RuntimeError("TenantDatabaseCatalog endpoint is not served") from exc
        if (discovery.returncode != 0 or not isinstance(resources, list)
            or not any(isinstance(item, dict)
                       and item.get("name") == "tenantdatabasecatalogs"
                       and item.get("kind") == "TenantDatabaseCatalog"
                       and item.get("namespaced") is True for item in resources)):
            raise RuntimeError("TenantDatabaseCatalog endpoint is not served")
        if cutover_locked:
            verify_catalog_cutover_lock(client, namespace=CONTROLLER_NAMESPACE)
        elif (
            not _catalog_activation_complete(client)
            or _catalog_lock_present(client)
            or tenant_cutover_lock_present(client)
        ):
            raise RuntimeError("completed catalog activation changed during install")


def verify_controller_api(
    config: dict[str, str],
    client: ManagementClient,
    *,
    require_allocation: bool = False,
) -> None:
    name = f"contract-probe-{uuid.uuid4().hex[:12]}"
    probe = {
        "apiVersion": "tenancy.cnpg-vcluster.io/v1alpha4",
        "kind": "Tenant",
        "metadata": {"name": name},
        "spec": {
            "kubernetesVersion": config["KUBERNETES_VERSION"].removeprefix("v"),
            "workers": 1,
            "provider": {"type": "local"},
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
        ("kubernetesVersion", {**probe["spec"], "kubernetesVersion": "bad"}),
    )
    for field, invalid_spec in invalid_specs:
        create_dry({**probe, "spec": invalid_spec}, rejected=field)
    create_dry(
        {**probe, "spec": {
            **probe["spec"], "provider": {"type": "local", "databases": 1},
        }},
        rejected="unknown field",
    )
    create_dry({**probe, "metadata": {"name": "invalid.name"}}, rejected="Tenant name")
    azure_spec = {
        "kubernetesVersion": probe["spec"]["kubernetesVersion"],
        "workers": 3,
        "provider": {"type": "azure"},
    }
    create_dry({**probe, "spec": azure_spec}, expected_spec=azure_spec)
    for invalid_provider, rejected in (
        ({"type": "azure", "databases": 1}, "unknown field"),
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
            {"provider": {"type": "azure"}},
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
    database_image = build_database_controller_image(root, config)
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
    run(
        [
            str(root / ".tools" / "bin" / "kind"),
            "load", "docker-image", database_image, "--name", config["KIND_CLUSTER_NAME"],
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
        root / "controller" / "config" / "rbac" / "allocation-role.yaml",
        root / "controller" / "config" / "rbac" / "allocation-binding.yaml",
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
    install_database_catalog(root, config, client, cutover_locked=cutover_locked)
    install_database_controller(root, client, database_image)
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
            if not tenant_cutover_lock_present(client):
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
        verify_catalog_cutover_lock(client, namespace=CONTROLLER_NAMESPACE)
        verify_tenant_cutover_lock(client, "v1alpha4")
        if CATALOG_LIFECYCLE_READY:
            release_catalog_and_tenant_cutover_locks(
                config, client, provider="local", tenant_image=image,
                database_image=database_image,
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
    stop_controller(config, client)
    require_clean_controller_state(root, client)
    inspect_catalog_inventory(client)
    for namespace, resource in (
        (CONTROLLER_NAMESPACE, "deployment/database-controller"),
        (None, "clusterrolebinding/database-controller"),
        (None, "clusterrole/database-controller"),
        (CONTROLLER_NAMESPACE, "serviceaccount/database-controller"),
        (None, f"crd/{CATALOG_CRD}"),
    ):
        delete_named(config, client, namespace, resource)
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
