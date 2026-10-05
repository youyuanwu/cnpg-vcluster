from __future__ import annotations

import copy
import http.server
import json
import tempfile
import threading
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import Mock, patch

from scripts import admin as admin_cli
from scripts import destroy as destroy_script
from scripts.lib import admin_local
from scripts.lib.kube import ManagementClient


ROOT = Path(__file__).resolve().parents[1]
IMAGE = "cnpg-vcluster/tenant-admin:0123456789abcdef"


def response(stdout: str = "", returncode: int = 0) -> CompletedProcess:
    return CompletedProcess([], returncode, stdout=stdout, stderr="")


def tenant_summary(name: str) -> dict[str, object]:
    return {
        "name": name,
        "provider": "local",
        "classification": "ready",
        "kubernetesVersion": "1.36.4",
        "requestedWorkers": 1,
        "endpoint": "tenant.example",
        "createdAt": "2026-01-01T00:00:00Z",
        "conditions": [],
    }


class FakeClient:
    def __init__(self, *, tenant_names: tuple[str, ...] = ()) -> None:
        template = (
            ROOT / "admin/config/deployment/deployment-local.json.tpl"
        ).read_text(encoding="utf-8")
        self.deployment = json.loads(
            template.replace("${TENANT_ADMIN_IMAGE}", IMAGE)
        )
        self.deployment["metadata"].update(
            {"uid": "deployment-uid", "generation": 7}
        )
        self.deployment["status"] = {
            "readyReplicas": 1,
            "updatedReplicas": 1,
            "availableReplicas": 1,
            "observedGeneration": 7,
        }
        self.pod = {
            "metadata": {"name": "tenant-admin-pod", "uid": "pod-uid"},
            "spec": copy.deepcopy(
                self.deployment["spec"]["template"]["spec"]
            ),
            "status": {
                "conditions": [{"type": "Ready", "status": "True"}]
            },
        }
        self.service_account = json.loads(
            (ROOT / "admin/config/rbac/service-account.json").read_text(
                encoding="utf-8"
            )
        )
        self.role = json.loads(
            (ROOT / "admin/config/rbac/cluster-role-local.json").read_text(
                encoding="utf-8"
            )
        )
        self.controller_role = json.loads(
            (ROOT / "admin/config/rbac/controller-role.json").read_text(
                encoding="utf-8"
            )
        )
        self.binding = json.loads(
            (
                ROOT / "admin/config/rbac/cluster-role-binding-local.json"
            ).read_text(encoding="utf-8")
        )
        self.service = json.loads(
            (ROOT / "admin/config/service/service.json").read_text(
                encoding="utf-8"
            )
        )
        self.service["spec"]["clusterIP"] = "10.96.1.2"
        self.rules_review = {
            "apiVersion": "authorization.k8s.io/v1",
            "kind": "SelfSubjectRulesReview",
            "status": {
                "resourceRules": [
                    *copy.deepcopy(self.role["rules"]),
                    {
                        "apiGroups": ["authorization.k8s.io"],
                        "resources": [
                            "selfsubjectaccessreviews",
                            "selfsubjectrulesreviews",
                        ],
                        "verbs": ["create"],
                    },
                    {
                        "apiGroups": ["authentication.k8s.io"],
                        "resources": ["selfsubjectreviews"],
                        "verbs": ["create"],
                    },
                ],
                "nonResourceRules": [
                    {
                        "verbs": ["get"],
                        "nonResourceURLs": [
                            "/api",
                            "/apis",
                            "/healthz",
                            "/readyz",
                            "/version",
                        ],
                    }
                ],
                "incomplete": False,
            },
        }
        self.namespaces = ("default", "tenant-a", "tenant-system")
        self.rules_reviews = {
            namespace: copy.deepcopy(self.rules_review)
            for namespace in self.namespaces
        }
        self.rules_reviews["tenant-system"]["status"]["resourceRules"].extend(
            copy.deepcopy(self.controller_role["rules"])
        )
        self.tenant_names = tenant_names
        self.failed_proxy_paths: set[str] = set()
        self.delete_on_proxy_failure: set[str] = set()
        self.calls: list[tuple[str, ...]] = []

    def json(self, *arguments: str):
        resource = arguments[-1]
        if resource == "deployment/tenant-admin":
            return copy.deepcopy(self.deployment)
        if "pods" in arguments:
            return {"items": [copy.deepcopy(self.pod)]}
        if resource == "serviceaccount/tenant-admin":
            return copy.deepcopy(self.service_account)
        if resource == "clusterrole/tenant-admin-local":
            return copy.deepcopy(self.role)
        if resource == "clusterrolebinding/tenant-admin":
            return copy.deepcopy(self.binding)
        if resource == "service/tenant-admin":
            return copy.deepcopy(self.service)
        raise AssertionError(arguments)

    def _proxy_response(self, path: str) -> str:
        def topology(name: str) -> dict[str, object]:
            return {
                "tenantName": name,
                "provider": "local",
                "nodes": [
                    {
                        "id": f"tenant:{name}",
                        "kind": "tenant",
                        "provenance": "exact-kubernetes-resource",
                        "label": name,
                        "health": "ready",
                        "resource": None,
                        "attributes": [],
                    },
                    {
                        "id": "database:cluster",
                        "kind": "database",
                        "provenance": "database-logical-representation",
                        "label": "capi-postgres",
                        "health": "ready",
                        "resource": None,
                        "attributes": [],
                    },
                    {
                        "id": "database:instance:capi-postgres-1",
                        "kind": "database",
                        "provenance": "database-logical-representation",
                        "label": "capi-postgres-1",
                        "health": "ready",
                        "resource": None,
                        "attributes": [],
                    },
                ],
                "edges": [
                    {
                        "id": "edge:tenant:database:cluster",
                        "source": f"tenant:{name}",
                        "target": "database:cluster",
                        "kind": "contains",
                        "label": "CNPG Cluster",
                    },
                    {
                        "id": "edge:database:cluster:database:instance",
                        "source": "database:cluster",
                        "target": "database:instance:capi-postgres-1",
                        "kind": "represents",
                        "label": "Primary",
                    },
                ],
            }

        def detail(name: str) -> dict[str, object]:
            return {
                "summary": tenant_summary(name),
                "uid": f"{name}-uid",
                "generation": 1,
                "observedGeneration": 1,
                "specification": {
                    "kubernetesVersion": "1.36.4",
                    "workers": 1,
                    "provider": {"provider": "local"},
                },
                "providerStatus": {
                    "provider": "local",
                    "status": {
                        "allocation": None,
                        "foundationHash": "foundation",
                        "clusterUid": f"{name}-cluster-uid",
                    },
                },
                "lifecycle": [
                    {
                        "stage": stage,
                        "state": "completed",
                        "message": None,
                    }
                    for stage in (
                        "request-accepted",
                        "infrastructure",
                        "control-plane",
                        "workers",
                        "add-ons",
                        "databases",
                        "ready",
                    )
                ],
                "workerCapacity": {
                    "desired": 1,
                    "available": 1,
                    "unavailable": 0,
                    "diagnosticReadyMachines": 1,
                },
                "blockers": [],
                "managementResources": [],
            }

        def database() -> dict[str, object]:
            return {
                "state": "available",
                "observedAt": "2026-09-29T20:00:00Z",
                "freshness": "live",
                "cluster": {
                    "identity": {
                        "apiVersion": "postgresql.cnpg.io/v1",
                        "kind": "Cluster",
                        "namespace": "database",
                        "name": "capi-postgres",
                        "uid": "database-uid",
                        "generation": 1,
                    },
                    "phase": "Cluster in healthy state",
                    "reason": None,
                    "desiredInstances": 1,
                    "observedInstances": 1,
                    "readyInstances": 1,
                    "currentPrimary": "capi-postgres-1",
                    "targetPrimary": "capi-postgres-1",
                    "currentPrimarySince": "2026-09-29T19:59:00Z",
                    "targetPrimaryRequestedAt": None,
                    "currentPrimaryFailingSince": None,
                    "image": "example/postgresql:18",
                    "timeline": 1,
                    "services": {
                        "read": "capi-postgres-r",
                        "write": "capi-postgres-rw",
                    },
                    "topologyAvailable": True,
                    "nodesUsed": 1,
                    "instances": [
                        {
                            "name": "capi-postgres-1",
                            "role": "primary",
                            "status": "healthy",
                            "timeline": 1,
                            "node": "worker-1",
                            "zone": None,
                        }
                    ],
                    "storage": {
                        "total": 1,
                        "healthy": 1,
                        "dangling": 0,
                        "initializing": 0,
                        "resizing": 0,
                        "unusable": 0,
                    },
                    "conditions": [],
                },
            }

        if path.endswith(("/healthz", "/readyz")):
            return ""
        if path.endswith("/api/v1/overview"):
            return json.dumps(
                {
                    "schemaVersion": 7,
                    "data": {
                        "overview": {
                            "providerMode": "local",
                            "creation": {
                                "available": True,
                                "supportedKubernetesVersion": "1.36.4",
                                "reason": None,
                            },
                            "tenants": {
                                "total": len(self.tenant_names),
                                "ready": len(self.tenant_names),
                                "progressing": 0,
                                "degraded": 0,
                                "failed": 0,
                                "deleting": 0,
                            },
                            "components": [],
                        },
                        "tenants": [
                            tenant_summary(name) for name in self.tenant_names
                        ],
                    },
                }
            )
        if path.endswith("/api/v1/tenants"):
            return json.dumps(
                {
                    "schemaVersion": 7,
                    "data": [
                        tenant_summary(name) for name in self.tenant_names
                    ],
                }
            )
        for name in self.tenant_names:
            if path.endswith(f"/api/v1/tenants/{name}/databases"):
                return json.dumps({
                    "schemaVersion": 7,
                    "data": {
                        "tenant": name, "tenantUid": f"{name}-uid",
                        "catalogUid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                        "resourceVersion": "42", "closed": False,
                        "capabilityAvailable": True, "databases": [],
                    },
                })
            if path.endswith(f"/api/v1/tenants/{name}/topology"):
                return json.dumps(
                    {
                        "schemaVersion": 7,
                        "data": topology(name),
                    }
                )
            if path.endswith(f"/api/v1/tenants/{name}"):
                return json.dumps(
                    {
                        "schemaVersion": 7,
                        "data": {
                            "observedAt": "2026-09-29T20:00:00Z",
                            "sections": {
                                "resources": {"state": "available"},
                                "databases": {"state": "available"},
                            },
                            "identity": {
                                "uid": f"{name}-uid",
                                "generation": 1,
                                "observedGeneration": 1,
                            },
                            "detail": detail(name),
                            "database": database(),
                            "topology": topology(name),
                        },
                    }
                )
        raise AssertionError(path)

    def kubectl(self, *arguments: str, check: bool = True, **kwargs):
        self.calls.append(arguments)
        if arguments[:2] == ("get", "--raw"):
            path = arguments[2]
            if path.startswith("/api/v1/namespaces?"):
                return response(json.dumps({
                    "apiVersion": "v1",
                    "kind": "NamespaceList",
                    "metadata": {"continue": ""},
                    "items": [
                        {"metadata": {"name": namespace}}
                        for namespace in self.namespaces
                    ],
                }))
            if any(path.endswith(suffix) for suffix in self.failed_proxy_paths):
                for name in self.delete_on_proxy_failure:
                    if f"/tenants/{name}" in path:
                        self.tenant_names = tuple(
                            tenant
                            for tenant in self.tenant_names
                            if tenant != name
                        )
                return response(returncode=1)
            return response(self._proxy_response(path))
        if arguments[:2] == ("create", "--raw"):
            request = json.loads(kwargs["input_text"])
            if arguments[2].endswith("/api/v1/tenants"):
                name = request["name"]
                self.tenant_names = tuple(sorted((*self.tenant_names, name)))
                return response(json.dumps({
                    "schemaVersion": 7,
                    "data": {
                        "identity": {
                            "name": name,
                            "uid": f"{name}-uid",
                            "generation": 1,
                        },
                        "provider": "local",
                        "kubernetesVersion": "1.36.4",
                    },
                }))
            tenant_name = arguments[2].split("/tenants/", 1)[1].split("/", 1)[0]
            return response(json.dumps({
                "schemaVersion": 7,
                "data": {
                    "tenant": tenant_name,
                    "cluster": "capi-postgres",
                    "instance": request["instance"],
                    "database": request["database"],
                    "executedAt": "2026-09-29T22:40:00Z",
                    "durationMs": 7,
                    "truncated": False,
                    "results": [{
                        "columns": ["value"],
                        "rows": [["1"]],
                        "affectedRows": 1,
                        "truncated": False,
                    }],
                },
            }))
        if arguments[:2] == ("delete", "--raw"):
            request = json.loads(kwargs["input_text"])
            name = arguments[2].rsplit("/", 1)[-1]
            self.tenant_names = tuple(
                tenant for tenant in self.tenant_names if tenant != name
            )
            return response(json.dumps({
                "schemaVersion": 7,
                "data": {
                    "identity": {
                        "name": name,
                        "uid": request["uid"],
                        "generation": 1,
                    },
                    "state": "accepted",
                },
            }))
        if arguments[0] == "create" and "-f" in arguments:
            request = json.loads(kwargs["input_text"])
            namespace = request["spec"]["namespace"]
            return response(json.dumps(self.rules_reviews[namespace]))
        return response()

    def request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, object],
    ):
        self.calls.append(
            ("raw-json", method, path, json.dumps(payload, sort_keys=True))
        )
        if method == "POST" and path.endswith("/api/v1/tenants"):
            name = payload["name"]
            self.tenant_names = tuple(sorted((*self.tenant_names, name)))
            return response(json.dumps({
                "schemaVersion": 7,
                "data": {
                    "identity": {
                        "name": name,
                        "uid": f"{name}-uid",
                        "generation": 1,
                    },
                    "provider": "local",
                    "kubernetesVersion": "1.36.4",
                },
            }))
        if method == "DELETE":
            name = path.rsplit("/", 1)[-1]
            self.tenant_names = tuple(
                tenant for tenant in self.tenant_names if tenant != name
            )
            return response(json.dumps({
                "schemaVersion": 7,
                "data": {
                    "identity": {
                        "name": name,
                        "uid": payload["uid"],
                        "generation": 1,
                    },
                    "state": "accepted",
                },
            }))
        tenant_name = path.split("/tenants/", 1)[1].split("/", 1)[0]
        return response(json.dumps({
            "schemaVersion": 7,
            "data": {
                "tenant": tenant_name,
                "cluster": "capi-postgres",
                "instance": payload["instance"],
                "database": payload["database"],
                "executedAt": "2026-09-29T22:40:00Z",
                "durationMs": 7,
                "truncated": False,
                "results": [{
                    "columns": ["value"],
                    "rows": [["1"]],
                    "affectedRows": 1,
                    "truncated": False,
                }],
            },
        }))


class AdminLocalTests(unittest.TestCase):
    def test_management_json_request_sends_post_and_delete_bodies(self):
        received = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def handle_request(self):
                size = int(self.headers.get("content-length", "0"))
                received.append((
                    self.command,
                    self.path,
                    self.headers.get("Authorization"),
                    self.headers.get("Content-Type"),
                    json.loads(self.rfile.read(size)),
                ))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')

            do_POST = handle_request
            do_DELETE = handle_request

            def log_message(self, *_args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = ManagementClient.__new__(ManagementClient)
        client.timeout = 2
        client.kubectl = Mock(return_value=response(json.dumps({
            "clusters": [{
                "cluster": {
                    "server": f"http://127.0.0.1:{server.server_port}",
                }
            }],
            "users": [{"user": {"token": "test-token"}}],
        })))
        try:
            self.assertEqual(
                0,
                client.request_json("POST", "/create", {"name": "tenant-a"})
                .returncode,
            )
            self.assertEqual(
                0,
                client.request_json("DELETE", "/delete", {"uid": "uid-a"})
                .returncode,
            )
        finally:
            server.shutdown()
            thread.join()
            server.server_close()
        self.assertEqual([
            (
                "POST",
                "/create",
                "Bearer test-token",
                "application/json",
                {"name": "tenant-a"},
            ),
            (
                "DELETE",
                "/delete",
                "Bearer test-token",
                "application/json",
                {"uid": "uid-a"},
            ),
        ], received)
        self.assertIn("--flatten", client.kubectl.call_args.args)

    def test_admin_lifecycle_helpers_validate_create_and_delete_contracts(self):
        client = FakeClient()
        created = admin_local.create_tenant_via_admin(
            client,
            "tenant-new",
            workers=2,
        )
        self.assertEqual(created["identity"]["uid"], "tenant-new-uid")
        self.assertIn(
            ("raw-json", "POST", f"{admin_local.ADMIN_SERVICE_PROXY}/api/v1/tenants",
             '{"name": "tenant-new", "workers": 2}'),
            client.calls,
        )
        self.assertIn("tenant-new", client.tenant_names)
        deleted = admin_local.delete_tenant_via_admin(
            client,
            "tenant-new",
            "tenant-new-uid",
        )
        self.assertEqual(deleted["state"], "accepted")
        self.assertNotIn("tenant-new", client.tenant_names)

    def setUp(self) -> None:
        (ROOT / ".runtime").mkdir(exist_ok=True)

    def test_render_is_exact_private_and_rejects_unsafe_images(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".runtime") as temporary:
            root = Path(temporary)
            template = root / "admin/config/deployment/deployment-local.json.tpl"
            template.parent.mkdir(parents=True)
            template.write_text(
                (
                    ROOT
                    / "admin/config/deployment/deployment-local.json.tpl"
                ).read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            rendered = admin_local.render_local_admin_deployment(root, IMAGE)
            self.assertEqual(0o600, rendered.stat().st_mode & 0o777)
            self.assertEqual(
                IMAGE,
                json.loads(rendered.read_text(encoding="utf-8"))["spec"][
                    "template"
                ]["spec"]["containers"][0]["image"],
            )
            for unsafe in (
                "example/admin:tag\nkind: Secret",
                "${TENANT_ADMIN_IMAGE}",
                "Example/Admin:tag",
                "example/admin",
            ):
                with self.subTest(unsafe=unsafe), self.assertRaisesRegex(
                    RuntimeError, "unsafe"
                ):
                    admin_local.render_local_admin_deployment(root, unsafe)

    def test_install_builds_loads_applies_rolls_out_and_verifies_exact_uid(
        self,
    ) -> None:
        client = FakeClient()
        rendered = ROOT / ".runtime/rendered/admin/deployment-local.json"
        with (
            patch.object(
                admin_local, "build_admin_image", return_value=IMAGE
            ) as build,
            patch.object(admin_local, "_load_admin_image") as load,
            patch.object(
                admin_local,
                "render_local_admin_deployment",
                return_value=rendered,
            ),
            patch.object(admin_local, "_apply") as apply,
            patch.object(
                admin_local,
                "verify_local_admin",
                return_value={"healthy": True},
            ) as verify,
        ):
            result = admin_local.reconcile_local_admin(
                ROOT, {"CONDITION_TIMEOUT": "1s"}, client
            )
        self.assertEqual({"healthy": True}, result)
        expected_config = {"CONDITION_TIMEOUT": "1s"}
        build.assert_called_once_with(ROOT, expected_config)
        load.assert_called_once_with(ROOT, expected_config, IMAGE)
        self.assertEqual(
            [
                ROOT / "admin/config/rbac/service-account.json",
                ROOT / "admin/config/rbac/cluster-role-local.json",
                ROOT / "admin/config/rbac/cluster-role-binding-local.json",
                ROOT / "admin/config/rbac/controller-role.json",
                ROOT / "admin/config/rbac/controller-role-binding.json",
                ROOT / "admin/config/service/service.json",
                rendered,
            ],
            [call.args[1] for call in apply.call_args_list],
        )
        self.assertIn(
            (
                "rollout",
                "status",
                "deployment/tenant-admin",
                "-n",
                "tenant-system",
                "--timeout=1s",
            ),
            client.calls,
        )
        self.assertEqual(
            "deployment-uid",
            verify.call_args.kwargs["expected_deployment_uid"],
        )

    def test_live_contract_rejects_wrong_provider_image_uid_service_and_rbac(
        self,
    ) -> None:
        client = FakeClient()
        self.assertEqual(
            "deployment-uid",
            admin_local._verify_deployment_contract(
                client.deployment, IMAGE, require_ready=True
            ),
        )
        status = admin_local.verify_local_admin(
            ROOT,
            client,
            IMAGE,
            expected_deployment_uid="deployment-uid",
            expected_tenant_names=(),
        )
        self.assertTrue(status["healthy"])
        wrong = copy.deepcopy(client.deployment)
        wrong["spec"]["template"]["spec"]["containers"][0]["env"][0][
            "value"
        ] = "azure"
        with self.assertRaisesRegex(RuntimeError, "Deployment contract"):
            admin_local._verify_deployment_contract(
                wrong, IMAGE, require_ready=True
            )
        with self.assertRaisesRegex(RuntimeError, "Deployment contract"):
            admin_local._verify_deployment_contract(
                client.deployment,
                "cnpg-vcluster/tenant-admin:ffffffffffffffff",
                require_ready=True,
            )
        with self.assertRaisesRegex(RuntimeError, "UID changed"):
            admin_local.verify_local_admin(
                ROOT,
                client,
                IMAGE,
                expected_deployment_uid="foreign-uid",
            )
        service = copy.deepcopy(client.service)
        service["spec"]["ports"][0]["port"] = 81
        with self.assertRaisesRegex(RuntimeError, "Service contract"):
            admin_local._verify_service(service)
        role = copy.deepcopy(client.role)
        role["rules"][0]["resources"].append("secrets")
        with self.assertRaisesRegex(RuntimeError, "read-only RBAC"):
            admin_local._verify_role(ROOT, role)
        catalog_rule_index = next(
            index
            for index, rule in enumerate(client.role["rules"])
            if rule["resources"] == ["tenantdatabasecatalogs"]
        )
        for verbs in (
            ["watch"],
            ["create"],
            ["patch"],
            ["delete"],
        ):
            role = copy.deepcopy(client.role)
            role["rules"][catalog_rule_index]["verbs"] = verbs
            with self.subTest(catalog_verbs=verbs), self.assertRaisesRegex(
                RuntimeError,
                "read-only RBAC",
            ):
                admin_local._verify_role(ROOT, role)
        review_calls = [
            arguments
            for arguments in client.calls
            if arguments[0] == "create" and "-f" in arguments
        ]
        self.assertEqual(len(client.namespaces), len(review_calls))
        self.assertTrue(
            all(
                "--as=system:serviceaccount:tenant-system:tenant-admin"
                in arguments
                for arguments in review_calls
            )
        )
        self.assertIn(
            (
                "get",
                "--raw",
                "/api/v1/namespaces?limit=1001",
            ),
            client.calls,
        )

    def test_effective_rbac_rejects_additive_and_missing_permissions(self) -> None:
        additions = (
            (
                "create-pods",
                {
                    "apiGroups": [""],
                    "resources": ["pods"],
                    "verbs": ["create"],
                },
            ),
            (
                "patch-deployments",
                {
                    "apiGroups": ["apps"],
                    "resources": ["deployments"],
                    "verbs": ["patch"],
                },
            ),
            (
                "group-contributed-right",
                {
                    "apiGroups": [""],
                    "resources": ["configmaps"],
                    "verbs": ["list"],
                },
            ),
            (
                "secret-watch",
                {
                    "apiGroups": [""],
                    "resources": ["secrets"],
                    "verbs": ["watch"],
                },
            ),
            (
                "secret-mutation",
                {
                    "apiGroups": [""],
                    "resources": ["secrets"],
                    "verbs": ["create", "update", "patch", "delete"],
                },
            ),
            (
                "catalog-patch",
                {
                    "apiGroups": ["tenancy.cnpg-vcluster.io"],
                    "resources": ["tenantdatabasecatalogs"],
                    "verbs": ["patch"],
                },
            ),
            (
                "catalog-status-write",
                {
                    "apiGroups": ["tenancy.cnpg-vcluster.io"],
                    "resources": ["tenantdatabasecatalogs/status"],
                    "verbs": ["update"],
                },
            ),
            (
                "direct-disk-delete",
                {
                    "apiGroups": ["resources.azure.com"],
                    "resources": ["disks"],
                    "verbs": ["delete"],
                },
            ),
            (
                "cutover-policy-write",
                {
                    "apiGroups": ["admissionregistration.k8s.io"],
                    "resources": ["validatingadmissionpolicybindings"],
                    "verbs": ["update"],
                },
            ),
            (
                "wildcard-api-group",
                {
                    "apiGroups": ["*"],
                    "resources": ["secrets"],
                    "verbs": ["get"],
                },
            ),
            (
                "wildcard-resource",
                {
                    "apiGroups": [""],
                    "resources": ["*"],
                    "verbs": ["get"],
                },
            ),
            (
                "wildcard-verb",
                {
                    "apiGroups": [""],
                    "resources": ["secrets"],
                    "verbs": ["*"],
                },
            ),
            (
                "subresource",
                {
                    "apiGroups": [""],
                    "resources": ["secrets/status"],
                    "verbs": ["get"],
                },
            ),
        )
        for index, (name, rule) in enumerate(additions):
            client = FakeClient()
            namespace = client.namespaces[index % len(client.namespaces)]
            client.rules_reviews[namespace]["status"]["resourceRules"].append(
                rule
            )
            with self.subTest(name=name), self.assertRaisesRegex(
                RuntimeError,
                "effective RBAC",
            ):
                admin_local.verify_local_admin(ROOT, client, IMAGE)

        client = FakeClient()
        client.rules_reviews["tenant-system"]["status"]["resourceRules"].pop(0)
        with self.assertRaisesRegex(RuntimeError, "effective RBAC"):
            admin_local.verify_local_admin(ROOT, client, IMAGE)

        for namespaces in (
            ("default", "default", "tenant-system"),
            ("default", "Invalid", "tenant-system"),
            tuple(f"ns-{index}" for index in range(1_001))
            + ("tenant-system",),
        ):
            client = FakeClient()
            client.namespaces = namespaces
            with self.subTest(namespace_count=len(namespaces)), (
                self.assertRaisesRegex(RuntimeError, "Namespace inventory")
            ):
                admin_local.verify_local_admin(ROOT, client, IMAGE)

        for field, value in (
            ("incomplete", True),
            ("evaluationError", "authorizer unavailable"),
        ):
            client = FakeClient()
            client.rules_reviews["tenant-system"]["status"][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(
                RuntimeError,
                "incomplete",
            ):
                admin_local.verify_local_admin(ROOT, client, IMAGE)

        client = FakeClient()
        client.rules_reviews["tenant-system"]["status"]["nonResourceRules"][0][
            "nonResourceURLs"
        ].append("/metrics")
        with self.assertRaisesRegex(RuntimeError, "non-resource RBAC"):
            admin_local.verify_local_admin(ROOT, client, IMAGE)

        for namespace in FakeClient().namespaces:
            client = FakeClient()
            client.rules_reviews[namespace]["status"]["resourceRules"].append({
                "apiGroups": [""],
                "resources": ["secrets"],
                "verbs": ["list"],
            })
            with self.subTest(namespace=namespace), self.assertRaisesRegex(
                RuntimeError,
                "effective RBAC",
            ):
                admin_local.verify_local_admin(ROOT, client, IMAGE)

    def test_api_verification_accepts_empty_and_typed_populated_responses(
        self,
    ) -> None:
        empty_client = FakeClient()
        empty = admin_local.verify_admin_api(
            empty_client, expected_tenant_names=()
        )
        self.assertEqual(0, empty["tenantCount"])
        self.assertEqual(
            [
                "healthz",
                "readyz",
                "api/v1/overview",
                "api/v1/tenants",
            ],
            [
                arguments[2].rsplit("/proxy/", 1)[-1]
                for arguments in empty_client.calls
                if arguments[:2] == ("get", "--raw")
            ],
        )
        populated_client = FakeClient(tenant_names=("tenant-a",))
        populated = admin_local.verify_admin_api(
            populated_client,
            expected_tenant_names=("tenant-a",),
        )
        self.assertEqual(["tenant-a"], populated["tenantNames"])
        self.assertEqual(
            [
                "healthz",
                "readyz",
                "api/v1/overview",
                "api/v1/tenants",
                "api/v1/tenants/tenant-a",
                "api/v1/tenants/tenant-a/databases",
                "api/v1/tenants/tenant-a/topology",
            ],
            [
                arguments[2].rsplit("/proxy/", 1)[-1]
                for arguments in populated_client.calls
                if arguments[:2] == ("get", "--raw")
            ],
        )
        queried = FakeClient(tenant_names=("tenant-a",))
        result = admin_local.verify_admin_api(
            queried,
            expected_tenant_names=("tenant-a",),
            require_available_databases=True,
            verify_database_queries=True,
        )
        self.assertEqual(["tenant-a"], result["tenantNames"])
        query_calls = [
            arguments
            for arguments in queried.calls
            if arguments[:2] == ("raw-json", "POST")
        ]
        self.assertEqual([], query_calls)
        from tests.test_catalog_lifecycle import FIRST, entry

        observed = entry("alpha", FIRST)
        with_entries = FakeClient(tenant_names=("tenant-a",))
        original_proxy = with_entries._proxy_response
        original_mutation = with_entries.request_json

        def populated_response(path: str) -> str:
            response = original_proxy(path)
            if path.endswith("/api/v1/tenants/tenant-a/databases"):
                body = json.loads(response)
                body["data"]["databases"] = [observed]
                return json.dumps(body)
            if path.endswith("/api/v1/tenants/tenant-a/topology"):
                body = json.loads(response)
                body["data"]["nodes"].append({
                    "id": f"database:{FIRST}", "kind": "database",
                    "provenance": "database-logical-representation",
                    "label": "alpha", "health": "ready",
                    "resource": None, "attributes": [],
                })
                return json.dumps(body)
            return response

        def query_response(method, path, payload):
            if path.endswith(f"/databases/{FIRST}/query"):
                with_entries.calls.append(
                    ("raw-json", method, path, json.dumps(payload, sort_keys=True))
                )
                return response(json.dumps({
                    "schemaVersion": 7,
                    "data": {
                        "catalogUid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                        "logicalUid": FIRST, "instance": "pg-alpha-1",
                        "instanceUid": "pod-alpha-1",
                        "executedAt": "2026-10-01T00:00:00Z",
                        "durationMs": 2, "truncated": False,
                        "results": [{
                            "columns": ["value"], "rows": [["1"]],
                            "affectedRows": 1, "truncated": False,
                        }],
                    },
                }))
            return original_mutation(method, path, payload)

        with (
            patch.object(with_entries, "_proxy_response", side_effect=populated_response),
            patch.object(with_entries, "request_json", side_effect=query_response),
        ):
            admin_local.verify_admin_api(
                with_entries, expected_tenant_names=("tenant-a",),
                verify_database_queries=True,
            )
        self.assertEqual(
            "SELECT 1 AS value",
            json.loads([
                call[3] for call in with_entries.calls
                if call[:2] == ("raw-json", "POST")
            ][0])["sql"],
        )
        transitioning = FakeClient(tenant_names=("tenant-a",))
        original_transition = transitioning._proxy_response

        def transition_response(path: str) -> str:
            if path.endswith("/api/v1/tenants"):
                summary = tenant_summary("tenant-a")
                summary["classification"] = "progressing"
                return json.dumps({"schemaVersion": 7, "data": [summary]})
            if path.endswith("/api/v1/tenants/tenant-a/topology"):
                topology = json.loads(original_transition(path))["data"]
                topology["nodes"][0]["health"] = "progressing"
                return json.dumps({"schemaVersion": 7, "data": topology})
            return original_transition(path)

        with patch.object(
            transitioning,
            "_proxy_response",
            side_effect=transition_response,
        ):
            transitioned = admin_local.verify_admin_api(
                transitioning,
                expected_tenant_names=("tenant-a",),
            )
        self.assertEqual(["tenant-a"], transitioned["tenantNames"])

        unavailable = FakeClient(tenant_names=("tenant-a",))
        original_unavailable = unavailable._proxy_response

        def unavailable_response(path: str) -> str:
            response = original_unavailable(path)
            if path.endswith("/api/v1/tenants/tenant-a/databases"):
                envelope = json.loads(response)
                envelope["data"]["capabilityAvailable"] = False
                return json.dumps(envelope)
            if path.endswith("/api/v1/tenants/tenant-a"):
                envelope = json.loads(response)
                envelope["data"]["database"] = {
                    "state": "unavailable",
                    "observedAt": "2026-09-29T20:00:00Z",
                    "freshness": "live",
                    "reason": "tenant-api-unavailable",
                    "message": "Tenant API database read failed",
                    "retryable": True,
                }
                topology = envelope["data"]["topology"]
                topology["nodes"] = [
                    node
                    for node in topology["nodes"]
                    if not node["id"].startswith("database:")
                ]
                topology["nodes"].append({
                    "id": "database:unavailable",
                    "kind": "database",
                    "provenance": "synthetic-summary",
                    "label": "Databases unavailable",
                    "health": "degraded",
                    "resource": None,
                    "attributes": [],
                })
                topology["edges"] = [
                    edge
                    for edge in topology["edges"]
                    if not edge["source"].startswith("database:")
                    and not edge["target"].startswith("database:")
                ]
                return json.dumps(envelope)
            return response

        with patch.object(
            unavailable,
            "_proxy_response",
            side_effect=unavailable_response,
        ):
            partial = admin_local.verify_admin_api(
                unavailable,
                expected_tenant_names=("tenant-a",),
            )
            self.assertEqual(["tenant-a"], partial["tenantNames"])
            with self.assertRaisesRegex(RuntimeError, "catalog capability is unavailable"):
                admin_local.verify_admin_api(
                    unavailable,
                    expected_tenant_names=("tenant-a",),
                    require_available_databases=True,
                )

        malformed = FakeClient()
        original = malformed._proxy_response

        def malformed_response(path: str) -> str:
            if path.endswith("/api/v1/overview"):
                return '{"schemaVersion":7,"data":[]}'
            return original(path)

        with patch.object(
            malformed,
            "_proxy_response",
            side_effect=malformed_response,
        ), self.assertRaisesRegex(RuntimeError, "overview data"):
            admin_local.verify_admin_api(malformed)

    def test_api_verification_rejects_malformed_schema_v6_snapshot_fields(
        self,
    ) -> None:
        def mutate_timestamp(body):
            body["data"]["observedAt"] = "2026-01-01"

        def mutate_missing_seconds(body):
            body["data"]["observedAt"] = "2026-01-01T00:00Z"

        def mutate_comma_fraction(body):
            body["data"]["observedAt"] = "2026-01-01T00:00:00,5Z"

        def mutate_compact_offset(body):
            body["data"]["observedAt"] = "2026-01-01T00:00:00+0000"

        def mutate_section(body):
            body["data"]["sections"]["resources"] = {
                "state": "unavailable",
                "retryable": True,
            }

        def mutate_lifecycle(body):
            body["data"]["detail"]["lifecycle"][0]["stage"] = "ready"

        def mutate_capacity(body):
            body["data"]["detail"]["workerCapacity"]["unavailable"] = None

        def mutate_blocker(body):
            body["data"]["detail"]["blockers"] = [{
                "code": "worker",
                "message": "worker unavailable",
                "conditionType": "WorkersReady",
                "targetNodeId": "missing-node",
            }]

        def mutate_edge(body):
            body["data"]["topology"]["edges"][0]["target"] = "missing-node"

        def mutate_database_timestamp(body):
            body["data"]["database"]["observedAt"] = "2026-01-01"

        def mutate_nested_timestamp(body):
            body["data"]["database"]["cluster"]["currentPrimarySince"] = (
                "2026-01-01T00:00:00"
            )

        def mutate_specification(body):
            body["data"]["detail"]["specification"] = {}

        def mutate_provider_status(body):
            body["data"]["detail"]["providerStatus"] = {}

        def mutate_created_at(body):
            body["data"]["detail"]["summary"]["createdAt"] = "not-a-time"

        def mutate_provider_allocation(body):
            body["data"]["detail"]["providerStatus"]["status"]["allocation"] = 7

        def mutate_management_resource(body):
            body["data"]["detail"]["managementResources"] = [{"identity": {}}]

        def mutate_node_resource(body):
            body["data"]["topology"]["nodes"][0]["resource"] = {}

        def mutate_attribute(body):
            body["data"]["topology"]["nodes"][0]["attributes"] = [
                {"label": "", "value": "bad"}
            ]

        for name, mutator in (
            ("timestamp", mutate_timestamp),
            ("missing-seconds", mutate_missing_seconds),
            ("comma-fraction", mutate_comma_fraction),
            ("compact-offset", mutate_compact_offset),
            ("section", mutate_section),
            ("lifecycle", mutate_lifecycle),
            ("capacity", mutate_capacity),
            ("blocker", mutate_blocker),
            ("edge", mutate_edge),
            ("database-timestamp", mutate_database_timestamp),
            ("nested-timestamp", mutate_nested_timestamp),
            ("specification", mutate_specification),
            ("provider-status", mutate_provider_status),
            ("created-at", mutate_created_at),
            ("provider-allocation", mutate_provider_allocation),
            ("management-resource", mutate_management_resource),
            ("node-resource", mutate_node_resource),
            ("attribute", mutate_attribute),
        ):
            with self.subTest(name=name):
                client = FakeClient(tenant_names=("tenant-a",))
                original = client._proxy_response

                def malformed(path: str, mutate=mutator) -> str:
                    response = original(path)
                    if path.endswith("/api/v1/tenants/tenant-a"):
                        body = json.loads(response)
                        mutate(body)
                        return json.dumps(body)
                    return response

                with patch.object(
                    client,
                    "_proxy_response",
                    side_effect=malformed,
                ), self.assertRaises(RuntimeError):
                    admin_local.verify_admin_api(
                        client,
                        expected_tenant_names=("tenant-a",),
                    )

    def test_api_verification_handles_tenant_deletion_race(self) -> None:
        for suffix in (
            "/api/v1/tenants/tenant-a",
            "/api/v1/tenants/tenant-a/topology",
        ):
            client = FakeClient(tenant_names=("tenant-a",))
            client.failed_proxy_paths.add(suffix)
            client.delete_on_proxy_failure.add("tenant-a")
            with self.subTest(suffix=suffix):
                result = admin_local.verify_admin_api(client)
                self.assertEqual(["tenant-a"], result["tenantNames"])
                overview_calls = [
                    arguments
                    for arguments in client.calls
                    if arguments[:2] == ("get", "--raw")
                    and arguments[2].endswith("/api/v1/overview")
                ]
                self.assertEqual(2, len(overview_calls))

        client = FakeClient(tenant_names=("tenant-a",))
        client.failed_proxy_paths.add("/api/v1/tenants/tenant-a")
        with self.assertRaisesRegex(RuntimeError, "API is unavailable"):
            admin_local.verify_admin_api(client)
        overview_calls = [
            arguments
            for arguments in client.calls
            if arguments[:2] == ("get", "--raw")
            and arguments[2].endswith("/api/v1/overview")
        ]
        self.assertEqual(2, len(overview_calls))

    def test_status_and_port_forward_use_explicit_management_identity(
        self,
    ) -> None:
        config = {"KUBECTL_REQUEST_TIMEOUT": "5s"}
        status_client = FakeClient()
        with (
            patch.object(admin_local, "require_management_ownership") as owned,
            patch.object(admin_local, "validate_management_kubeconfig") as valid,
            patch.object(
                admin_local, "ManagementClient", return_value=status_client
            ),
            patch.object(
                admin_local,
                "verify_local_admin",
                return_value={"healthy": True},
            ) as verify,
        ):
            self.assertEqual(
                {"healthy": True},
                admin_local.collect_local_admin_status(ROOT, config),
            )
        owned.assert_called_once_with(ROOT, config)
        valid.assert_called_once_with(ROOT, config)
        verify.assert_called_once_with(ROOT, status_client, IMAGE)

        client = type(
            "Client",
            (),
            {
                "kubectl_path": Path("/verified/kubectl"),
                "kubeconfig": Path("/repo/.runtime/management/kubeconfig"),
                "context": "kind-management",
            },
        )()
        completed = response(returncode=9)
        with (
            patch.object(admin_local, "require_management_ownership") as owned,
            patch.object(admin_local, "validate_management_kubeconfig") as valid,
            patch.object(admin_local, "ManagementClient", return_value=client),
            patch.object(
                admin_local.subprocess, "run", return_value=completed
            ) as execute,
        ):
            self.assertEqual(
                9, admin_local.admin_port_forward(ROOT, config)
            )
        owned.assert_called_once_with(ROOT, config)
        valid.assert_called_once_with(ROOT, config)
        execute.assert_called_once_with(
            [
                "/verified/kubectl",
                "--kubeconfig",
                "/repo/.runtime/management/kubeconfig",
                "--context",
                "kind-management",
                "--request-timeout",
                "5s",
                "-n",
                "tenant-system",
                "port-forward",
                "service/tenant-admin",
                "8080:80",
            ],
            check=False,
        )
        justfile = (ROOT / "Justfile").read_text(encoding="utf-8")
        self.assertIn("admin-status:\n    @python3 scripts/admin.py status", justfile)
        self.assertIn(
            "admin-port-forward:\n    @python3 scripts/admin.py port-forward",
            justfile,
        )

    def test_admin_cli_status_and_port_forward_commands(self) -> None:
        with (
            patch.object(admin_cli, "load_configuration", return_value={}),
            patch.object(
                admin_cli,
                "collect_local_admin_status",
                return_value={"healthy": True},
            ) as status,
            patch.object(
                admin_cli, "admin_port_forward", return_value=7
            ) as forward,
            patch("builtins.print"),
        ):
            self.assertEqual(0, admin_cli.main(["status"]))
            self.assertEqual(7, admin_cli.main(["port-forward"]))
        status.assert_called_once_with(admin_cli.ROOT, {})
        forward.assert_called_once_with(admin_cli.ROOT, {})

    def test_teardown_deletes_exact_resources_and_rendered_admin_outputs(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".runtime") as temporary:
            root = Path(temporary)
            rendered = root / ".runtime/rendered/admin"
            rendered.mkdir(parents=True)
            (rendered / "deployment-local.json").write_text(
                "fixture\n", encoding="utf-8"
            )
            resources = iter((None, None, None, None, None))
            deleted = []
            with (
                patch.object(
                    admin_local,
                    "_optional_resource",
                    side_effect=lambda *_: next(resources),
                ),
                patch.object(
                    admin_local,
                    "delete_named",
                    side_effect=lambda *args: deleted.append(args[2:]),
                ),
            ):
                admin_local.delete_local_admin(
                    root, {"DELETE_TIMEOUT": "1s"}, object()
                )
            self.assertEqual(
                [
                    ("tenant-system", "deployment/tenant-admin"),
                    ("tenant-system", "service/tenant-admin"),
                    (None, "clusterrolebinding/tenant-admin"),
                    (None, "clusterrole/tenant-admin-local"),
                    ("tenant-system", "serviceaccount/tenant-admin"),
                ],
                deleted,
            )
            self.assertFalse(rendered.exists())

    def test_management_teardown_removes_admin_before_controller(self) -> None:
        events = []

        class Client:
            def helm(self, *arguments, **_kwargs):
                events.append(("helm", arguments))
                return response()

            def kubectl(self, *arguments, **_kwargs):
                events.append(("kubectl", arguments))
                return response()

        config = {
            "MANAGEMENT_NAMESPACE": "kamaji-system",
            "DELETE_TIMEOUT": "1s",
        }
        with (
            patch.object(
                destroy_script,
                "delete_local_admin",
                side_effect=lambda *_: events.append("admin"),
            ),
            patch.object(
                destroy_script,
                "delete_controller",
                side_effect=lambda *_: events.append("controller"),
            ),
            patch.object(
                destroy_script,
                "delete_providers",
                side_effect=lambda *_: events.append("providers"),
            ),
            patch.object(destroy_script, "reconcile_network", return_value={}),
            patch.object(
                destroy_script,
                "_render_metallb_pool",
                return_value=Path("pool.yaml"),
            ),
            patch.object(
                destroy_script,
                "_render_metallb",
                return_value=Path("metallb.yaml"),
            ),
            patch.object(
                destroy_script,
                "_render_cert_manager",
                return_value=Path("cert-manager.yaml"),
            ),
        ):
            destroy_script._delete_kubernetes_stack(
                ROOT, config, Client()
            )
        self.assertLess(events.index("admin"), events.index("controller"))
        self.assertLess(events.index("controller"), events.index("providers"))


if __name__ == "__main__":
    unittest.main()
