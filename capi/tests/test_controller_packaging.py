from __future__ import annotations

import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import MagicMock, Mock, patch

from scripts.lib import controller as packaging
from scripts.lib import database_controller
from scripts.lib.controller_foundation import foundation_payload
from scripts.lib.kube import ManagementClient


ROOT = Path(__file__).resolve().parents[1]
CONFIG = {
    "COMMAND_TIMEOUT": "1s", "CONDITION_TIMEOUT": "1s", "DELETE_TIMEOUT": "1s",
    "KUBERNETES_VERSION": "v1.36.4", "KIND_CLUSTER_NAME": "management",
    "OWNERSHIP_LABEL": "example.io/owned", "LAB_PREFIX": "lab",
}


def response(value="", code=0, error=""):
    return CompletedProcess([], code, json.dumps(value) if isinstance(value, dict) else value, error)


def desired_crd():
    return {
        "apiVersion": "apiextensions.k8s.io/v1",
        "kind": "CustomResourceDefinition",
        "metadata": {"name": packaging.TENANT_CRD},
        "spec": {
            "versions": [{
                "name": "v1alpha4",
                "served": True,
                "storage": True,
                "schema": {"openAPIV3Schema": {"type": "object"}},
            }]
        },
    }


def active_catalog_lock():
    policy, binding = packaging.catalog_cutover_lock_documents()
    policy["metadata"].update({"uid": "policy-uid", "generation": 1})
    policy["status"] = {
        "observedGeneration": 1,
        "typeChecking": {"expressionWarnings": []},
        "conditions": [],
    }
    binding["metadata"].update({"uid": "binding-uid", "generation": 1})
    return policy, binding


class Client:
    def __init__(self, handler=None):
        self.calls = []
        self.applied = set()
        self.handler = handler or (lambda *_args, **_kwargs: response())

    def kubectl(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        result = self.handler(*args, **kwargs)
        if kwargs.get("check", True) and result.returncode:
            raise RuntimeError(result.stderr)
        if args and args[0] == "apply" and result.returncode == 0:
            document = json.loads(kwargs.get("input_text", "{}"))
            if document.get("metadata", {}).get("name") == packaging.CATALOG_CUTOVER_POLICY:
                self.applied.add(
                    document["kind"].lower() + "/" + packaging.CATALOG_CUTOVER_POLICY
                )
        if args and args[0] == "get" and args[1] in self.applied and not result.stdout:
            return response(args[1])
        return result

    def json(self, *args):
        return json.loads(self.kubectl(*args, "-o", "json").stdout)


class PackagingTests(unittest.TestCase):
    def test_database_image_entrypoint_imports_outside_repository(self):
        entrypoint = ROOT / "scripts/build_database_controller.py"
        result = subprocess.run(
            [sys.executable, "-I", "-c",
             f"import runpy; runpy.run_path({str(entrypoint)!r}, run_name='import-check')"],
            cwd=ROOT.parent, capture_output=True, text=True, timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_database_controller_build_uses_offline_static_binary_and_bounded_context(self):
        binary = ROOT / "database-controller" / "Dockerfile"
        result = response(json.dumps({
            "reason": "compiler-artifact",
            "target": {"name": "manager", "kind": ["bin"]},
            "executable": str(binary),
        }) + "\n")
        manager_modes = []

        def command(args, **_kwargs):
            if args[:2] == ["docker", "build"]:
                manager_modes.append(
                    (Path(args[-1]) / "manager").stat().st_mode & 0o777
                )
                return response()
            return result

        with (
            patch("scripts.lib.controller.rust_toolchain",
                  return_value=("cargo", "rustc identity")),
            patch("scripts.lib.controller.fetch_controller_dependencies") as fetch,
            patch("scripts.lib.controller.verify_static_manager") as verify,
            patch.object(database_controller, "run", side_effect=command) as run,
        ):
            image = database_controller.build_database_controller_image(
                ROOT, {**CONFIG, "TENANT_CONTROLLER_IMAGE_REPOSITORY": "local/tenant"},
            )
        self.assertRegex(image, r"^local/tenant-database:[0-9a-f]{16}$")
        fetch.assert_called_once()
        verify.assert_called_once_with(binary)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn("--offline", commands[0])
        self.assertIn("--locked", commands[0])
        self.assertIn("tenant-database-controller", commands[0])
        self.assertEqual(commands[1][:3], ["docker", "build", "--pull=false"])
        self.assertEqual(commands[1][4], image)
        self.assertEqual(manager_modes, [0o755])
        self.assertFalse((ROOT / ".runtime/rendered/database-controller-build").exists())

    def test_prebuilt_database_controller_requires_verified_artifact_boundary(self):
        binary = ROOT / "database-controller" / "Dockerfile"
        with (
            patch.dict("os.environ", {
                "CAPI_PREBUILT_DATABASE_CONTROLLER_BINARY": str(binary),
            }),
            patch("scripts.lib.controller.rust_toolchain",
                  return_value=("cargo", "rustc identity")),
            patch("scripts.lib.controller.fetch_controller_dependencies") as fetch,
            patch.object(database_controller, "run") as build,
            self.assertRaisesRegex(RuntimeError, "inside .tools/artifacts"),
        ):
            database_controller.build_database_controller_image(
                ROOT, {**CONFIG, "TENANT_CONTROLLER_IMAGE_REPOSITORY": "local/tenant"},
            )
        fetch.assert_not_called()
        build.assert_not_called()

    def test_database_deployment_renders_verified_image_and_rolls_out(self):
        image = "registry.example/db@sha256:" + "a" * 64
        path = database_controller.render_database_controller(ROOT, image)
        try:
            content = path.read_text()
            self.assertIn("image: " + image, content)
            self.assertNotIn(database_controller.DATABASE_IMAGE_PLACEHOLDER, content)
            client = Client()
            database_controller.install_database_controller(ROOT, client, image)
            self.assertTrue(any(args[:1] == ("apply",) for args, _ in client.calls))
            self.assertFalse(any("rollout" in args for args, _ in client.calls))
        finally:
            path.unlink(missing_ok=True)
        with self.assertRaisesRegex(RuntimeError, "built"):
            database_controller.render_database_controller(ROOT, "")

    def test_database_deployment_selects_local_runtime_only_for_local_installer(self):
        image = "registry.example/db@sha256:" + "a" * 64
        rendered = []
        try:
            local = ManagementClient(ROOT, CONFIG)
            with patch.object(
                local, "kubectl",
                side_effect=lambda *args, **_kwargs: rendered.append(
                    Path(args[-1]).read_text()
                ),
            ):
                database_controller.install_database_controller(ROOT, local, image)
            self.assertEqual(len(rendered), 1)
            self.assertIn("path: /var/run/docker.sock", rendered[0])
            self.assertIn("path: /var/lib/docker/volumes", rendered[0])
            kind_config = (ROOT / "config/kind.yaml").read_text()
            self.assertIn(
                "hostPath: /var/lib/docker/volumes\n"
                "        containerPath: /var/lib/docker/volumes",
                kind_config,
            )
            self.assertIn("runAsUser: 0", rendered[0])
            self.assertIn("- CHOWN", rendered[0])
            self.assertIn("- DAC_OVERRIDE", rendered[0])
            self.assertIn("image: " + image, rendered[0])

            azure = Client(
                lambda *args, **_kwargs: (
                    rendered.append(Path(args[-1]).read_text()) or response()
                    if args[0] == "apply" else response()
                )
            )
            database_controller.install_database_controller(
                ROOT, azure, image, azure=True,
                azure_identity_client_id="11111111-1111-4111-8111-111111111111",
            )
            self.assertEqual(len(rendered), 2)
            self.assertNotIn("docker.sock", rendered[1])
            self.assertNotIn("docker/volumes", rendered[1])
            self.assertIn("runAsNonRoot: true", rendered[1])
            self.assertIn("azure.workload.identity/use: 'true'", rendered[1])
            self.assertIn("11111111-1111-4111-8111-111111111111", rendered[1])
            self.assertIn("image: " + image, rendered[1])
            self.assertTrue(any(
                args[:3] == ("-n", "tenant-system", "annotate")
                and "azure.workload.identity/client-id=11111111-1111-4111-8111-111111111111"
                in args for args, _ in azure.calls
            ))
            with self.assertRaisesRegex(RuntimeError, "dedicated Azure disk identity"):
                database_controller.render_database_controller(ROOT, image, azure=True)
        finally:
            (ROOT / ".runtime/rendered/database-controller/controller.yaml").unlink(
                missing_ok=True
            )

    def test_database_catalog_role_only_grants_management_side_of_tenant_access(self):
        local = (ROOT / "database-controller/config/rbac/controller-cluster-role.yaml").read_text()
        azure = (ROOT / "database-controller/config/rbac/controller-cluster-role-azure.yaml").read_text()
        for role in (local, azure):
            self.assertIn("kamajicontrolplanes", role)
            self.assertIn("clusters", role)
            self.assertNotIn("  - secrets\n", role)
            self.assertIn("tenantdatabasecatalogs/status", role)
            self.assertNotIn("tenantdatabases\n", role)
            self.assertNotIn("resourcequotas", role)
            for tenant_resource in (
                "persistentvolumes", "persistentvolumeclaims", "clusters.postgresql.cnpg.io",
            ):
                self.assertNotIn(tenant_resource, role)
        self.assertIn("tenant-foundation", local)
        self.assertNotIn("tenant-foundation", azure)
        self.assertIn("tenant-azure-provider", azure)
        self.assertIn("disks", azure)
        self.assertNotIn("disks", local)
        local_inventory = json.loads(
            (ROOT / "database-controller/config/management-resources.json").read_text()
        )
        azure_inventory = json.loads(
            (ROOT / "database-controller/config/azure-management-resources.json").read_text()
        )
        self.assertEqual(
            {entry["resource"] for entry in local_inventory},
            {"tenantdatabasecatalogs", "tenants", "namespaces",
             "clusters", "kamajicontrolplanes", "secrets", "configmaps"},
        )
        self.assertEqual(
            {entry["resource"] for entry in azure_inventory},
            {"tenantdatabasecatalogs", "tenants", "namespaces",
             "clusters", "kamajicontrolplanes", "secrets", "disks"},
        )

    def test_azure_tenant_controller_has_read_only_disk_drain_visibility(self):
        role = (ROOT / "controller/config/rbac/role-azure.yaml").read_text()
        self.assertIn(
            "- apiGroups:\n"
            "  - compute.azure.com\n"
            "  resources:\n"
            "  - disks\n"
            "  verbs:\n"
            "  - get\n"
            "  - list\n",
            role,
        )

    def test_azure_identity_failure_does_not_roll_out_and_retry_recovers(self):
        image = "registry.example/db@sha256:" + "a" * 64
        client_id = "11111111-1111-4111-8111-111111111111"
        blocked = Client(
            lambda *args, **_kwargs: response(
                code=1, error="dedicated identity unavailable",
            ) if args[:3] == ("-n", "tenant-system", "annotate") else response()
        )
        try:
            with self.assertRaisesRegex(RuntimeError, "dedicated identity unavailable"):
                database_controller.install_database_controller(
                    ROOT, blocked, image, azure=True,
                    azure_identity_client_id=client_id,
                )
            self.assertFalse(any(args[0] == "apply" for args, _ in blocked.calls))
            recovered = Client()
            database_controller.install_database_controller(
                ROOT, recovered, image, azure=True,
                azure_identity_client_id=client_id,
            )
            self.assertEqual(
                [args[0] for args, _ in recovered.calls],
                ["-n", "apply"],
            )
            for invalid in ("", "not-a-client-id", "11111111111141118111111111111111"):
                with self.subTest(invalid=invalid), self.assertRaisesRegex(
                    RuntimeError, "dedicated Azure disk identity"
                ):
                    database_controller.render_database_controller(
                        ROOT, image, azure=True, azure_identity_client_id=invalid,
                    )
        finally:
            (ROOT / ".runtime/rendered/database-controller/controller.yaml").unlink(
                missing_ok=True
            )

    def test_catalog_release_retains_admin_intent_only_permissions(self):
        self.assertTrue(packaging.CATALOG_LIFECYCLE_READY)
        for role in ("cluster-role-local.json", "cluster-role-azure.json"):
            payload = json.loads((ROOT / "admin/config/rbac" / role).read_text())
            self.assertFalse(any(
                "tenantdatabasecatalogs" in rule.get("resources", [])
                and any(verb in rule.get("verbs", []) for verb in ("create", "patch", "delete"))
                for rule in payload["rules"]
            ))
            self.assertTrue(any(
                rule.get("resources") == ["tenantdatabasecatalogs"]
                and rule.get("verbs") == ["get", "update"]
                for rule in payload["rules"]
            ))

    def test_catalog_probe_exception_is_single_identity_and_service_account(self):
        policy = packaging.catalog_cutover_lock_documents(
            ("catalog-bootstrap-probe", "uid-123"),
        )[0]
        expression = policy["spec"]["validations"][0]["expression"]
        for fragment in (
            "request.userInfo.username == 'system:serviceaccount:tenant-system:tenant-controller'",
            "object.metadata.namespace == 'tenant-db-catalog-bootstrap-probe'",
            "object.spec.tenantUID == 'uid-123'",
            "object.spec.entries.size() == 0",
            "object.metadata.ownerReferences.size() == 1",
        ):
            self.assertIn(fragment, expression)
        self.assertEqual(policy["spec"]["failurePolicy"], "Fail")
        self.assertEqual(
            packaging.catalog_cutover_lock_documents()[0]["spec"]["validations"],
            [{"expression": "false",
              "message": "TenantDatabaseCatalog creation is locked during API cutover"}],
        )
        with self.assertRaisesRegex(RuntimeError, "invalid probe"):
            packaging.catalog_cutover_lock_documents(("other", "uid' || true"))

    def test_probe_deletion_uses_api_uid_precondition_and_stops_proxy(self):
        proxy = Mock()
        proxy.stdout = io.StringIO("Starting to serve on 127.0.0.1:18888\n")
        proxy.poll.return_value = None
        client = Mock(
            kubeconfig=Path("management.kubeconfig"), kubectl_path=Path("kubectl"),
            context="kind-management",
        )
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value = (
            io.BytesIO(b'{"kind":"Status","status":"Success"}')
        )
        with (
            patch.object(packaging.subprocess, "Popen", return_value=proxy) as start_proxy,
            patch.object(packaging.select, "select",
                         return_value=([proxy.stdout], [], [])),
            patch.object(packaging.urllib.request, "build_opener",
                         return_value=opener),
        ):
            packaging._delete_probe_tenant_uid(CONFIG, client, "probe", "uid-123")
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "DELETE")
        self.assertIn("--context", start_proxy.call_args.args[0])
        self.assertIn("kind-management", start_proxy.call_args.args[0])
        self.assertEqual(json.loads(request.data)["preconditions"], {"uid": "uid-123"})
        self.assertEqual(
            request.full_url,
            "http://127.0.0.1:18888/apis/tenancy.cnpg-vcluster.io/v1alpha4/tenants/probe",
        )
        proxy.terminate.assert_called_once()

    def test_probe_exception_rejects_nonmatching_uid_as_controller_service_account(self):
        def client(allow_foreign=False):
            def handler(*args, **kwargs):
                if args[:2] == (
                    "get",
                    "namespace/tenant-db-catalog-bootstrap-probe",
                ):
                    return response({"metadata": {
                        "name": "tenant-db-catalog-bootstrap-probe",
                        "uid": "namespace-uid",
                        "labels": {
                            "tenancy.cnpg-vcluster.io/tenant-uid": "uid-123",
                        },
                    }})
                if "create" in args:
                    document = json.loads(kwargs["input_text"])
                    if document["spec"]["tenantUID"] == "uid-123" or allow_foreign:
                        return response()
                    return response(code=1, error=(
                        f"{packaging.CATALOG_CUTOVER_POLICY}: "
                        "TenantDatabaseCatalog creation is locked during API cutover"
                    ))
                return response()
            return Client(handler)

        accepted = client()
        packaging._verify_probe_exception(accepted, "catalog-bootstrap-probe", "uid-123")
        creations = [(args, kwargs) for args, kwargs in accepted.calls if "create" in args]
        self.assertEqual(len(creations), 2)
        self.assertTrue(all(
            "--as=system:serviceaccount:tenant-system:tenant-controller" in args
            and "--dry-run=server" in args
            for args, _ in creations
        ))
        with self.assertRaisesRegex(RuntimeError, "foreign catalog"):
            packaging._verify_probe_exception(
                client(allow_foreign=True), "catalog-bootstrap-probe", "uid-123",
            )

    def test_probe_exception_waits_for_namespace_and_policy_propagation(self):
        namespace_attempts = 0
        attempts = 0

        def handler(*args, **kwargs):
            nonlocal attempts, namespace_attempts
            if args[:2] == (
                "get",
                "namespace/tenant-db-catalog-bootstrap-probe",
            ):
                namespace_attempts += 1
                if namespace_attempts == 1:
                    return response()
                return response({"metadata": {
                    "name": "tenant-db-catalog-bootstrap-probe",
                    "uid": "namespace-uid",
                    "labels": {
                        "tenancy.cnpg-vcluster.io/tenant-uid": "uid-123",
                    },
                }})
            if "create" not in args:
                return response()
            document = json.loads(kwargs["input_text"])
            if document["spec"]["tenantUID"] == "uid-123":
                attempts += 1
                if attempts == 1:
                    return response(code=1, error=(
                        f"{packaging.CATALOG_CUTOVER_POLICY}: "
                        "TenantDatabaseCatalog creation is locked during API cutover"
                    ))
                return response()
            return response(code=1, error=(
                f"{packaging.CATALOG_CUTOVER_POLICY}: "
                "TenantDatabaseCatalog creation is locked during API cutover"
            ))

        client = Client(handler)
        with patch.object(
            packaging,
            "wait_for",
            side_effect=lambda _description, _timeout, _interval, predicate: (
                predicate() or predicate()
            ),
        ):
            packaging._verify_probe_exception(
                client,
                "catalog-bootstrap-probe",
                "uid-123",
                timeout=1,
            )
        self.assertEqual(namespace_attempts, 2)
        self.assertEqual(attempts, 2)
        self.assertEqual(len([
            args for args, _ in client.calls if "create" in args
        ]), 3)

    def test_observer_receipt_is_bound_to_live_catalog_and_pod(self):
        observer = {
            "catalogUID": "catalog-uid", "observedGeneration": 1,
            "observedResourceVersion": "8", "podUID": "pod-uid",
            "instanceId": "instance-uid",
        }
        catalog = {
            "metadata": {
                "name": "probe", "namespace": "tenant-db-probe",
                "uid": "catalog-uid", "resourceVersion": "9", "generation": 1,
            },
            "spec": {"tenantUID": "tenant-uid", "entries": {}},
            "status": {"observer": observer, "entries": {}},
        }
        receipt = {"observer": observer, "resourceVersion": "9"}

        def client(catalog_value, receipt_value):
            return Client(lambda *args, **kwargs: (
                response(catalog_value) if args[:2] == ("-n", "tenant-db-probe")
                else response(receipt_value) if args[:3] == (
                    "get", "--request-timeout=0", "--raw",
                )
                else response()
            ))

        accepted = client(catalog, receipt)
        packaging._verify_catalog_observation(
            accepted, "pod-name", "pod-uid", "probe", "tenant-uid",
        )
        self.assertTrue(any(
            "/pods/pod-name:8082/proxy/observation?" in " ".join(args)
            and args[:3] == ("get", "--request-timeout=0", "--raw")
            and "catalogUID=catalog-uid" in " ".join(args)
            and "resourceVersion=9" in " ".join(args)
            for args, _ in accepted.calls
        ))
        for mutation in (
            lambda c, r: c["status"]["observer"].update(podUID="previous-pod"),
            lambda c, r: c["metadata"].update(resourceVersion=""),
            lambda c, r: c["metadata"].update(generation=2),
            lambda c, r: c["spec"].update(tenantUID="foreign"),
            lambda c, r: r.update(resourceVersion="old"),
        ):
            changed_catalog, changed_receipt = copy.deepcopy((catalog, receipt))
            mutation(changed_catalog, changed_receipt)
            with self.assertRaisesRegex(RuntimeError, "obser|receipt"):
                packaging._verify_catalog_observation(
                    client(changed_catalog, changed_receipt),
                    "pod-name", "pod-uid", "probe", "tenant-uid",
                )

    def test_probe_cleanup_requires_exact_credential_role_absence(self):
        namespace = "tenant-db-probe"
        for occupied in ("role/tenant-database-credentials",
                         "rolebinding/tenant-database-credentials"):
            client = Client(lambda *args, **kwargs: (
                response({"metadata": {"name": occupied.split("/")[1]}})
                if occupied in args else response()
            ))
            with self.assertRaisesRegex(RuntimeError, "identity persists"):
                packaging._verify_probe_cleanup(
                    client, "probe", namespace, "tenant-db-storage-probe",
                )
            self.assertTrue(any(args[:4] == ("-n", "probe", "get", occupied)
                                for args, _ in client.calls))
            self.assertFalse(any(args[:2] == ("delete", occupied)
                                 for args, _ in client.calls))
        management = Client(lambda *args, **kwargs: (
            response({"metadata": {"name": "probe"}})
            if args[:2] == ("get", "namespace/probe") else response()
        ))
        with self.assertRaisesRegex(RuntimeError, "identity persists"):
            packaging._verify_probe_cleanup(
                management, "probe", namespace, "tenant-db-storage-probe",
            )

    def test_probe_rejects_existing_tenant_without_unlock_or_cleanup(self):
        client = Client(lambda *args, **kwargs: (
            response({"metadata": {}, "items": [{"metadata": {"name": "foreign"}}]})
            if args[:2] == ("get", "tenants.tenancy.cnpg-vcluster.io")
            else response()
        ))
        with self.assertRaisesRegex(RuntimeError, "empty Tenant inventory"):
            packaging.run_catalog_lifecycle_probe(
                CONFIG, client, provider="local", database_image="db:image",
            )
        self.assertFalse(any(args[0] in ("delete", "create") for args, _ in client.calls))

    def test_probe_releases_tenant_first_then_restores_catalog_fence_after_uid_cleanup(self):
        state = {"created": False, "deleted": False}
        name = "catalog-bootstrap-probe"
        namespace = f"tenant-db-{name}"
        tenant = {
            "metadata": {
                "name": name, "uid": "uid-123",
                "annotations": {"tenancy.cnpg-vcluster.io/catalog-bootstrap": "installer-owned"},
            },
            "status": {
                "catalogCreateIntent": {
                    "name": name, "namespace": namespace, "tenantUID": "uid-123",
                },
                "databaseCapability": {"available": True},
            },
        }

        def handler(*args, **kwargs):
            if args[:2] == ("get", "tenants.tenancy.cnpg-vcluster.io"):
                return response({"metadata": {}, "items": [tenant] if state["created"] and not state["deleted"] else []})
            if args[:2] == ("get", f"tenant/{name}"):
                return response(tenant) if state["created"] and not state["deleted"] else response()
            if args[:2] == ("get", f"namespace/{namespace}"):
                return response()
            if args[:1] == ("-n",):
                return response()
            if args[:2] == ("create", "-f"):
                state["created"] = True
                tenant["metadata"]["annotations"].update(
                    json.loads(kwargs["input_text"])["metadata"]["annotations"]
                )
                tenant["spec"] = json.loads(kwargs["input_text"])["spec"]
                return response(tenant)
            if args[:2] == ("wait", "--for=delete"):
                self.assertTrue(state["deleted"])
            return response()

        client = Client(handler)
        prepared = []

        def delete_uid(_config, _client, deleted_name, uid):
            self.assertEqual((deleted_name, uid), (name, "uid-123"))
            state["deleted"] = True

        with (
            patch.object(database_controller, "inspect_catalog_inventory") as inventory,
            patch.object(packaging, "inspect_catalog_inventory") as cleanup_inventory,
            patch.object(packaging, "remove_tenant_cutover_lock") as unlock,
            patch.object(packaging, "verify_catalog_cutover_lock",
                         return_value=("p", 1, "b", 1)) as fence,
            patch.object(packaging, "_verify_probe_exception",
                         side_effect=[RuntimeError("interrupted"), None]) as exception,
            patch.object(packaging, "verify_catalog_release_capability") as capability,
            patch.object(packaging, "_empty_database_namespace") as empty,
            patch.object(packaging, "verify_catalog_release_deployment") as observer,
            patch.object(packaging, "_delete_probe_tenant_uid", side_effect=delete_uid),
            patch.object(packaging, "ensure_catalog_cutover_lock") as restore,
        ):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                packaging.run_catalog_lifecycle_probe(
                    CONFIG, client, provider="local", database_image="db:image",
                    prepare_capability=prepared.append,
                )
            self.assertTrue(self.probe_path.is_file())
            self.assertTrue(state["created"])
            packaging.run_catalog_lifecycle_probe(
                CONFIG, client, provider="local", database_image="db:image",
                prepare_capability=prepared.append,
            )
        self.assertEqual(unlock.call_count, 2)
        self.assertEqual(exception.call_count, 2)
        exception.assert_called_with(client, name, "uid-123", timeout=1)
        self.assertEqual(len([args for args, _ in client.calls if args[:2] == ("create", "-f")]), 1)
        observer.assert_called_once_with(
            client, name="database-controller", image="db:image",
            service_account="database-controller", probe=(name, "uid-123"),
        )
        self.assertTrue(any(args[:4] == (
            "-n", "tenant-system", "rollout", "status",
        ) for args, _ in client.calls))
        capability.assert_called_once_with(
            client, provider="local", probe=(name, "uid-123"),
        )
        self.assertEqual(prepared, [name])
        self.assertEqual(empty.call_count, 2)
        inventory.assert_called_once_with(client)
        cleanup_inventory.assert_called_once_with(client)
        restore.assert_called_once_with(client)
        self.assertEqual(fence.call_count, 4)
        self.assertFalse(self.probe_path.exists())
        self.assertFalse(any(args[0] == "delete" for args, _ in client.calls))

    def test_ambiguous_probe_create_restores_both_fences_without_deleting(self):
        client = Client(lambda *args, **kwargs: (
            response({"metadata": {}, "items": []})
            if args[:2] == ("get", "tenants.tenancy.cnpg-vcluster.io")
            else response()
        ))
        with (
            patch.object(packaging, "CATALOG_LIFECYCLE_READY", True),
            patch.object(packaging, "tenant_cutover_lock_present", return_value=True),
            patch.object(packaging, "verify_release_tenant_cutover_lock"),
            patch.object(packaging, "verify_catalog_release_gates",
                         return_value=("p", 1, "b", 1)),
            patch.object(packaging, "verify_catalog_cutover_lock"),
            patch.object(database_controller, "inspect_catalog_inventory"),
            patch.object(packaging, "remove_tenant_cutover_lock") as release_tenant,
            patch.object(packaging, "apply_tenant_cutover_lock") as restore_tenant,
            patch.object(packaging, "ensure_catalog_cutover_lock") as restore_catalog,
            self.assertRaisesRegex(RuntimeError, "outcome is unknown"),
        ):
            packaging.release_catalog_and_tenant_cutover_locks(
                CONFIG, client, provider="local", tenant_image="tenant:image",
                database_image="db:image",
            )
        release_tenant.assert_called_once()
        restore_tenant.assert_called_once()
        restore_catalog.assert_called_once()
        self.assertFalse(any(args[0] == "delete" for args, _ in client.calls))
        self.assertTrue(self.probe_path.is_file())
        with (
            patch.object(database_controller, "inspect_catalog_inventory"),
            patch.object(packaging, "remove_tenant_cutover_lock") as unlock,
            patch.object(packaging, "verify_release_tenant_cutover_lock"),
            patch.object(packaging, "verify_catalog_cutover_lock"),
            self.assertRaisesRegex(RuntimeError, "recorded management checkout"),
        ):
            packaging.run_catalog_lifecycle_probe(
                CONFIG, client, provider="local", database_image="db:image",
            )
        unlock.assert_not_called()
        self.assertEqual(len([
            args for args, _ in client.calls if args[:2] == ("create", "-f")
        ]), 1)

    def test_unknown_local_create_recovery_restarts_only_recorded_container(self):
        from scripts.lib import management

        name = "catalog-bootstrap-probe"
        path = self.root / ".runtime/management/catalog-bootstrap-probe.json"
        record = {
            "schema": 1, "name": name, "provider": "local",
            "token": "a" * 32, "uid": None, "issued": True, "deleting": False,
        }
        packaging.write_private_file(path, json.dumps(record))
        identity = Mock(identifier="recorded-container-id")
        client = Client(lambda *args, **kwargs: (
            response("ok") if args[:2] == ("get", "--raw=/readyz") else response()
        ))
        client.root = self.root
        client.kubeconfig = path.with_name("kubeconfig")
        events = []
        with (
            patch.object(packaging, "_probe_record_path", return_value=path),
            patch.object(packaging, "apply_tenant_cutover_lock",
                         side_effect=lambda *_: events.append("fence")),
            patch.object(packaging, "ensure_catalog_cutover_lock",
                         side_effect=lambda *_: events.append("catalog-fence")),
            patch.object(packaging, "verify_release_tenant_cutover_lock",
                         side_effect=lambda *_: events.append("tenant-denied")),
            patch.object(packaging, "verify_catalog_cutover_lock",
                         side_effect=lambda *_a, **_k: events.append("catalog-denied")),
            patch.object(management, "require_management_ownership",
                         return_value=identity) as ownership,
            patch.object(management, "validate_management_kubeconfig") as kubeconfig,
            patch.object(packaging, "run",
                         side_effect=lambda *_a, **_k: events.append("restart") or response()
                         ) as restart,
            patch.object(packaging, "wait_for",
                         side_effect=lambda _desc, _timeout, _interval, predicate:
                         predicate() or (_ for _ in ()).throw(RuntimeError("API not ready"))),
            patch.object(packaging, "_database_controller_restarted", return_value=True),
            patch.object(packaging, "_verify_probe_cleanup",
                         side_effect=lambda *_: events.append("cleanup")),
        ):
            packaging._recover_unknown_local_probe(CONFIG, client, record)
        self.assertFalse(path.exists())
        self.assertEqual(
            events,
            ["fence", "catalog-fence", "tenant-denied", "catalog-denied",
             "restart", "tenant-denied", "catalog-denied", "cleanup",
             "tenant-denied", "catalog-denied"],
        )
        restart.assert_called_once_with(
            ["docker", "restart", "recorded-container-id"], timeout=61,
        )
        self.assertEqual(ownership.call_count, 2)
        identity.require_exact.assert_called_once_with(identity)
        kubeconfig.assert_called_once_with(self.root, CONFIG)
        self.assertTrue(any(
            args[:4] == ("-n", "tenant-system", "rollout", "status")
            for args, _ in client.calls
        ))

    def test_unknown_local_create_faults_keep_record_and_never_delete_foreign_tenant(self):
        from scripts.lib import management

        name = "catalog-bootstrap-probe"
        path = self.root / ".runtime/management/catalog-bootstrap-probe.json"
        client = Client(lambda *args, **kwargs: response())
        client.root = self.root
        client.kubeconfig = path.with_name("kubeconfig")
        record = {
            "schema": 1, "name": name, "provider": "local",
            "token": "a" * 32, "uid": None, "issued": True, "deleting": False,
        }
        identity = Mock(identifier="exact-container")
        for failed_step in ("fence", "ownership", "kubeconfig", "restart",
                            "readiness", "inspection", "cleanup"):
            with self.subTest(failed_step=failed_step):
                packaging.write_private_file(path, json.dumps(record))

                def fail(step):
                    if step == failed_step:
                        raise RuntimeError(f"{step} failure")

                def inspect(*args, **kwargs):
                    fail("inspection")
                    return None

                with (
                    patch.object(packaging, "_probe_record_path", return_value=path),
                    patch.object(packaging, "apply_tenant_cutover_lock",
                                 side_effect=lambda *_: fail("fence")),
                    patch.object(packaging, "ensure_catalog_cutover_lock"),
                    patch.object(packaging, "verify_release_tenant_cutover_lock"),
                    patch.object(packaging, "verify_catalog_cutover_lock"),
                    patch.object(management, "require_management_ownership",
                                 side_effect=lambda *_: fail("ownership") or identity),
                    patch.object(management, "validate_management_kubeconfig",
                                 side_effect=lambda *_: fail("kubeconfig")),
                    patch.object(packaging, "run",
                                 side_effect=lambda *_a, **_k: fail("restart") or response()),
                    patch.object(packaging, "wait_for",
                                 side_effect=lambda *_a: fail("readiness") or True),
                    patch.object(packaging, "_database_controller_restarted", return_value=True),
                    patch.object(packaging, "_probe_resource", side_effect=inspect),
                    patch.object(packaging, "_verify_probe_cleanup",
                                 side_effect=lambda *_: fail("cleanup")),
                    patch.object(packaging, "_delete_probe_tenant_uid") as delete,
                    self.assertRaisesRegex(RuntimeError, f"{failed_step} failure"),
                ):
                    packaging._recover_unknown_local_probe(CONFIG, client, record.copy())
                self.assertTrue(path.exists())
                delete.assert_not_called()

        tenant = {
            "metadata": {"name": name, "uid": "tenant-uid", "annotations": {
                "tenancy.cnpg-vcluster.io/catalog-bootstrap": "installer-owned",
                "tenancy.cnpg-vcluster.io/catalog-bootstrap-token": record["token"],
            }},
            "spec": {"kubernetesVersion": "1.36.4", "workers": 1,
                     "provider": {"type": "local"}},
        }
        for foreign in ({**tenant, "spec": {**tenant["spec"], "workers": 2}},
                        {**tenant, "metadata": {**tenant["metadata"],
                         "annotations": {"tenancy.cnpg-vcluster.io/catalog-bootstrap-token": "bad"}}}):
            packaging.write_private_file(path, json.dumps(record))
            with (
                patch.object(packaging, "_probe_record_path", return_value=path),
                patch.object(packaging, "apply_tenant_cutover_lock"),
                patch.object(packaging, "ensure_catalog_cutover_lock"),
                patch.object(packaging, "verify_release_tenant_cutover_lock"),
                patch.object(packaging, "verify_catalog_cutover_lock"),
                patch.object(management, "require_management_ownership",
                             return_value=identity),
                patch.object(management, "validate_management_kubeconfig"),
                patch.object(packaging, "run"),
                patch.object(packaging, "wait_for", return_value=True),
                patch.object(packaging, "_database_controller_restarted", return_value=True),
                patch.object(packaging, "_probe_resource", return_value=foreign),
                patch.object(packaging, "_delete_probe_tenant_uid") as delete,
                self.assertRaisesRegex(RuntimeError, "another Tenant"),
            ):
                packaging._recover_unknown_local_probe(CONFIG, client, record.copy())
            self.assertTrue(path.exists())
            delete.assert_not_called()

    def test_unknown_managed_create_does_not_restart_or_clear_record(self):
        record = {
            "schema": 1, "name": "catalog-bootstrap-probe", "provider": "azure",
            "token": "a" * 32, "uid": None, "issued": True, "deleting": False,
        }
        packaging.write_private_file(self.probe_path, json.dumps(record))
        client = Client()
        with (
            patch.object(packaging, "run") as restart,
            self.assertRaisesRegex(RuntimeError, "unknown on managed API"),
        ):
            packaging.run_catalog_lifecycle_probe(
                CONFIG, client, provider="azure", database_image="db:image",
            )
        restart.assert_not_called()
        self.assertTrue(self.probe_path.exists())
        self.assertEqual(client.calls, [])

    def test_delayed_unknown_create_is_uid_deleted_only_after_restart(self):
        from scripts.lib import management

        name = "catalog-bootstrap-probe"
        path = self.root / ".runtime/management/catalog-bootstrap-probe.json"
        record = {
            "schema": 1, "name": name, "provider": "local",
            "token": "a" * 32, "uid": None, "issued": True, "deleting": False,
        }
        packaging.write_private_file(path, json.dumps(record))
        identity = Mock(identifier="recorded-container-id")
        tenant = {
            "metadata": {"name": name, "uid": "recorded-tenant-uid", "annotations": {
                "tenancy.cnpg-vcluster.io/catalog-bootstrap": "installer-owned",
                "tenancy.cnpg-vcluster.io/catalog-bootstrap-token": record["token"],
            }},
            "spec": {"kubernetesVersion": "1.36.4", "workers": 1,
                     "provider": {"type": "local"}},
        }
        state = {"restarted": False, "deleted": False, "checked": False}
        client = Client(lambda *args, **_kwargs: response())
        client.root = self.root
        client.kubeconfig = path.with_name("kubeconfig")

        def restart(*_args, **_kwargs):
            state["restarted"] = True
            return response()

        def exact_get(*_args, **_kwargs):
            self.assertTrue(state["restarted"])
            return tenant

        def delete(_config, _client, deleted_name, uid):
            self.assertTrue(state["restarted"])
            self.assertEqual((deleted_name, uid), (name, "recorded-tenant-uid"))
            self.assertEqual(json.loads(packaging.read_private_file(path))["uid"], uid)
            self.assertTrue(json.loads(packaging.read_private_file(path))["deleting"])
            state["deleted"] = True

        def cleanup(*_args):
            self.assertTrue(state["deleted"])
            state["checked"] = True

        with (
            patch.object(packaging, "_probe_record_path", return_value=path),
            patch.object(packaging, "apply_tenant_cutover_lock"),
            patch.object(packaging, "ensure_catalog_cutover_lock"),
            patch.object(packaging, "verify_release_tenant_cutover_lock"),
            patch.object(packaging, "verify_catalog_cutover_lock"),
            patch.object(management, "require_management_ownership",
                         return_value=identity),
            patch.object(management, "validate_management_kubeconfig"),
            patch.object(packaging, "run", side_effect=restart),
            patch.object(packaging, "wait_for", return_value=True),
            patch.object(packaging, "_database_controller_restarted", return_value=True),
            patch.object(packaging, "_probe_resource", side_effect=exact_get),
            patch.object(packaging, "_delete_probe_tenant_uid", side_effect=delete),
            patch.object(packaging, "_verify_probe_cleanup", side_effect=cleanup),
        ):
            packaging._recover_unknown_local_probe(CONFIG, client, record)
        self.assertTrue(state["checked"])
        self.assertFalse(path.exists())

    def test_database_recovery_readiness_requires_current_healthy_leader(self):
        pod_uid = "recorded-pod-uid"
        deployment = {
            "metadata": {"generation": 1},
            "spec": {"replicas": 1},
            "status": {"observedGeneration": 1, "updatedReplicas": 1},
        }
        pod = {
            "metadata": {"uid": pod_uid, "name": "db-pod"},
            "status": {"phase": "Running", "containerStatuses": [
                {"state": {"running": {"startedAt": "now"}}},
            ]},
        }
        lease = {"spec": {
            "holderIdentity": f"{pod_uid}-instance",
            "renewTime": datetime.now(timezone.utc).isoformat(),
            "leaseDurationSeconds": 30,
        }}
        objects = {
            "deployment/database-controller": deployment,
            "pods": {"items": [pod]},
            "lease/database-controller.tenancy.cnpg-vcluster.io": lease,
        }

        class RecoveryClient:
            def json(self, *args):
                return objects[next(value for value in args if value in objects)]

            def kubectl(self, *args, **kwargs):
                return response("ok")

        client = RecoveryClient()
        restarted_at = datetime.now(timezone.utc) - timedelta(seconds=2)
        with patch.object(client, "kubectl", return_value=response("ok")) as health:
            self.assertTrue(packaging._database_controller_restarted(client, since=restarted_at))
            self.assertIn("/pods/db-pod:8082/proxy/healthz", health.call_args.args[1])
            for change in (
                lambda: lease["spec"].update(holderIdentity="other-pod-instance"),
                lambda: lease["spec"].update(renewTime=(
                    datetime.now(timezone.utc) - timedelta(seconds=40)).isoformat()),
                lambda: lease["spec"].update(renewTime=(
                    restarted_at - timedelta(seconds=1)).isoformat()),
                lambda: pod["metadata"].update(deletionTimestamp="now"),
            ):
                original = copy.deepcopy((lease, pod))
                change()
                self.assertFalse(packaging._database_controller_restarted(
                    client, since=restarted_at,
                ))
                lease.clear()
                lease.update(original[0])
                pod.clear()
                pod.update(original[1])
            health.return_value = response(code=1, error="unhealthy")
            self.assertFalse(packaging._database_controller_restarted(
                client, since=restarted_at,
            ))

    def test_restart_after_probe_deletion_proves_absence_before_releasing_record(self):
        record = {
            "schema": 1, "name": "catalog-bootstrap-probe", "provider": "local",
            "token": "a" * 32, "uid": "uid-123", "issued": True,
            "deleting": True,
        }
        packaging.write_private_file(self.probe_path, json.dumps(record))
        client = Client(lambda *args, **kwargs: (
            response({"metadata": {}, "items": []})
            if args[:2] == ("get", "tenants.tenancy.cnpg-vcluster.io")
            else response()
        ))
        with (
            patch.object(packaging, "inspect_catalog_inventory"),
            patch.object(packaging, "ensure_catalog_cutover_lock") as restore,
            patch.object(packaging, "verify_catalog_cutover_lock") as verify,
            patch.object(packaging, "remove_tenant_cutover_lock") as unlock,
        ):
            packaging.run_catalog_lifecycle_probe(
                CONFIG, client, provider="local", database_image="db:image",
            )
        self.assertFalse(self.probe_path.exists())
        restore.assert_called_once_with(client)
        verify.assert_called_once_with(client, namespace="tenant-system")
        unlock.assert_called_once_with(CONFIG, client)
        self.assertFalse(any(args[:1] in (("create",), ("delete",))
                             for args, _ in client.calls))
        for occupied in ("role/tenant-database-credentials",
                         "rolebinding/tenant-database-credentials"):
            packaging.write_private_file(self.probe_path, json.dumps(record))
            blocked = Client(lambda *args, **kwargs: (
                response({"metadata": {}, "items": []})
                if args[:2] == ("get", "tenants.tenancy.cnpg-vcluster.io")
                else response({"metadata": {"name": occupied.split("/")[1]}})
                if occupied in args else response()
            ))
            with self.assertRaisesRegex(RuntimeError, "identity persists"):
                packaging.run_catalog_lifecycle_probe(
                    CONFIG, blocked, provider="local", database_image="db:image",
                )
            self.assertTrue(self.probe_path.exists())

    def test_release_wrapper_unlocks_tenant_after_interrupted_probe_cleanup(self):
        record = {
            "schema": 1, "name": "catalog-bootstrap-probe", "provider": "local",
            "token": "a" * 32, "uid": "uid-123", "issued": True,
            "deleting": True,
        }
        packaging.write_private_file(self.probe_path, json.dumps(record))
        client = Client(lambda *args, **kwargs: (
            response({"metadata": {}, "items": []})
            if args[:2] == ("get", "tenants.tenancy.cnpg-vcluster.io")
            else response()
        ))
        steps = []
        with (
            patch.object(packaging, "CATALOG_LIFECYCLE_READY", True),
            patch.object(packaging, "tenant_cutover_lock_present", return_value=True),
            patch.object(packaging, "apply_tenant_cutover_lock"),
            patch.object(packaging, "ensure_catalog_cutover_lock"),
            patch.object(packaging, "verify_release_tenant_cutover_lock"),
            patch.object(packaging, "verify_catalog_release_gates",
                         return_value=("policy", 1, "binding", 1)),
            patch.object(packaging, "verify_catalog_cutover_lock"),
            patch.object(packaging, "inspect_catalog_inventory"),
            patch.object(packaging, "remove_tenant_cutover_lock",
                         side_effect=lambda *_args: steps.append("tenant")),
            patch.object(packaging, "_record_catalog_activation",
                         side_effect=lambda *_args: steps.append("record")),
            patch.object(packaging, "remove_catalog_cutover_lock",
                         side_effect=lambda *_args: steps.append("catalog")),
        ):
            packaging.release_catalog_and_tenant_cutover_locks(
                CONFIG, client, provider="local", tenant_image="tenant:image",
                database_image="db:image",
            )
        self.assertEqual(steps, ["tenant", "record", "catalog"])
        self.assertFalse(self.probe_path.exists())

    def test_uninstall_requires_empty_catalog_before_database_controller_cleanup(self):
        client = Client()
        with (
            patch.object(packaging, "stop_controller"),
            patch.object(packaging, "require_clean_controller_state"),
            patch.object(packaging, "inspect_catalog_inventory",
                         side_effect=RuntimeError("retained catalog")),
            self.assertRaisesRegex(RuntimeError, "retained catalog"),
        ):
            packaging.delete_controller(ROOT, CONFIG, client)
        self.assertFalse(any(
            "database-controller" in " ".join(args)
            for args, _ in client.calls
        ))
        client = Client()
        with (
            patch.object(packaging, "stop_controller"),
            patch.object(packaging, "require_clean_controller_state"),
            patch.object(packaging, "inspect_catalog_inventory"),
        ):
            packaging.delete_controller(ROOT, CONFIG, client)
        resources = [
            argument
            for args, _ in client.calls if "delete" in args
            for argument in args if "database-controller" in argument
        ]
        self.assertEqual(resources[:4], [
            "deployment/database-controller",
            "clusterrolebinding/database-controller",
            "clusterrole/database-controller",
            "serviceaccount/database-controller",
        ])

    def test_release_probe_is_empty_unique_dry_run_and_never_cleans_catalog(self):
        names = set()
        calls = []

        def handler(*args, **kwargs):
            calls.append((args, kwargs))
            if args[:2] == ("create", "--dry-run=server"):
                document = json.loads(kwargs["input_text"])
                self.assertEqual(document["spec"]["entries"], {})
                self.assertEqual(document["metadata"]["namespace"], "tenant-system")
                self.assertLessEqual(len(document["metadata"]["name"]), 30)
                self.assertEqual(document["spec"]["tenantName"], document["metadata"]["name"])
                self.assertNotIn(document["metadata"]["name"], names)
                names.add(document["metadata"]["name"])
                return response(code=1, error=(
                    f"{packaging.CATALOG_CUTOVER_POLICY}: "
                    "TenantDatabaseCatalog creation is locked during API cutover"
                ))
            return response()

        client = Client(handler)
        packaging.verify_catalog_release_probe(client, namespace="tenant-system")
        packaging.verify_catalog_release_probe(client, namespace="tenant-system")
        self.assertEqual(len(names), 2)
        self.assertEqual(
            len([args for args, _ in calls if "--ignore-not-found=true" in args]), 4,
        )
        self.assertFalse(any(args[0] == "delete" for args, _ in calls))
        for failure in (
            response(),
            response(code=1, error="rejected by webhook"),
        ):
            with self.subTest(failure=failure), self.assertRaisesRegex(
                RuntimeError, "owned policy denial",
            ):
                packaging.verify_catalog_release_probe(
                    Client(lambda *args, **kwargs: (
                        failure if args[:2] == ("create", "--dry-run=server")
                        else response()
                    )), namespace="tenant-system",
                )
        persisted = Client(lambda *args, **kwargs: (
            response("tenantdatabasecatalog/database-release-probe")
            if "--ignore-not-found=true" in args else handler(*args, **kwargs)
        ))
        with self.assertRaisesRegex(RuntimeError, "persisted"):
            packaging.verify_catalog_release_probe(
                persisted, namespace="tenant-system",
            )
        self.assertFalse(any(args[0] == "delete" for args, _ in persisted.calls))

    def test_release_rollout_requires_exact_image_service_account_and_ready_pod(self):
        def deployment(name):
            return {
                "metadata": {"uid": name + "-uid", "generation": 2},
                "spec": {
                    "replicas": 1, "strategy": {"type": "Recreate"},
                    "selector": {"matchLabels": {"app": name}},
                    "template": {"spec": {
                        "serviceAccountName": name,
                        "containers": [{
                            "name": "manager", "image": name + ":image",
                            "args": ["--provider=local", "--controller-image=" + name + ":image"],
                        }],
                    }},
                },
                "status": {
                    "observedGeneration": 2, "replicas": 1,
                    "readyReplicas": 1, "updatedReplicas": 1, "availableReplicas": 1,
                },
            }

        def pod(name):
            return {
                "metadata": {
                    "uid": name + "-pod", "name": name + "-pod",
                    "labels": {"app": name},
                    "ownerReferences": [{
                        "kind": "ReplicaSet", "name": name + "-rs",
                        "uid": name + "-rs-uid", "controller": True,
                    }],
                },
                "spec": {
                    "serviceAccountName": name,
                    "containers": [{
                        "name": "manager", "image": name + ":image",
                        "args": ["--provider=local", "--controller-image=" + name + ":image"],
                    }],
                },
                "status": {"conditions": [{"type": "Ready", "status": "True"}]},
            }

        def check(name, observed, running, *, image=None):
            client = Client(lambda *args, **kwargs: (
                response("ok") if args[:2] == ("get", "--raw") else
                response(observed) if args[3] == f"deployment/{name}"
                else response({
                    "metadata": {
                        "uid": name + "-rs-uid",
                        "ownerReferences": [{
                            "kind": "Deployment", "name": name,
                            "uid": name + "-uid", "controller": True,
                        }],
                    },
                    "spec": {"replicas": 1}, "status": {"readyReplicas": 1},
                }) if args[3] == f"replicaset/{name}-rs"
                else response({"items": running})
            ))
            with patch.object(packaging, "_verify_catalog_observation") as receipt:
                packaging.verify_catalog_release_deployment(
                    client, name=name, image=image or name + ":image",
                    service_account=name,
                    probe=("bootstrap", "tenant-uid") if name == "database-controller" else None,
                )
                if name == "database-controller":
                    receipt.assert_called_once_with(
                        client, name + "-pod", name + "-pod", "bootstrap", "tenant-uid",
                    )

        for name in ("tenant-controller", "database-controller"):
            with self.subTest(name=name):
                check(name, deployment(name), [pod(name)])
                for mutation in (
                    lambda d: d["status"].update(observedGeneration=1),
                    lambda d: d["status"].update(readyReplicas=0),
                    lambda d: d["spec"]["template"]["spec"].update(
                        serviceAccountName="wrong",
                    ),
                ):
                    changed = deployment(name)
                    mutation(changed)
                    with self.assertRaisesRegex(RuntimeError, "rollout"):
                        check(name, changed, [pod(name)])
                with self.assertRaisesRegex(RuntimeError, "rollout"):
                    check(name, deployment(name), [pod(name)], image="stale:image")
                with self.assertRaisesRegex(RuntimeError, "Ready Pod"):
                    check(name, deployment(name), [])
                if name == "tenant-controller":
                    with self.assertRaisesRegex(RuntimeError, "rollout"):
                        packaging.verify_catalog_release_deployment(
                            Client(lambda *args, **kwargs: response(deployment(name))),
                            name=name, image=name + ":image",
                            service_account=name, provider="azure",
                        )

    def test_release_effective_rbac_fails_closed(self):
        client = Client(lambda *args, **_kwargs: response("yes\n"))
        packaging.verify_catalog_release_rbac(client)
        self.assertTrue(all(args[:2] == ("auth", "can-i") for args, _ in client.calls))
        self.assertTrue(any("database-controller" in " ".join(args)
                            for args, _ in client.calls))
        for result in (response("no\n"), response("", 1, "forbidden")):
            with self.assertRaisesRegex(RuntimeError, "effective RBAC"):
                packaging.verify_catalog_release_rbac(
                    Client(lambda *_args, **_kwargs: result),
                )

    def test_release_capability_requires_live_exact_catalog(self):
        tenant = {
            "metadata": {"name": "example", "uid": "tenant-uid"},
            "spec": {"provider": {"type": "local"}},
            "status": {"databaseCapability": {
                "available": True, "namespace": "tenant-db-example",
                "namespaceUID": "ns-uid", "catalogUID": "catalog-uid",
            }},
        }
        catalog = {
            "metadata": {
                "uid": "catalog-uid", "name": "example",
                "namespace": "tenant-db-example",
                "finalizers": ["tenancy.cnpg-vcluster.io/database-catalog-finalizer"],
                "ownerReferences": [{
                    "apiVersion": "tenancy.cnpg-vcluster.io/v1alpha4",
                    "kind": "Tenant", "name": "example", "uid": "tenant-uid",
                }],
            },
            "spec": {
                "tenantUID": "tenant-uid", "tenantName": "example",
                "closed": False, "entries": {},
            },
        }

        def client(tenants, value=catalog):
            return Client(lambda *args, **kwargs: (
                response("yes") if args[:2] == ("auth", "can-i")
                else response({
                    "apiVersion": "tenancy.cnpg-vcluster.io/v1alpha1",
                    "kind": "TenantDatabaseCatalogList",
                    "metadata": {"continue": ""},
                    "items": [value],
                }) if args[:2] == (
                    "get", "--raw=/apis/tenancy.cnpg-vcluster.io/v1alpha1/tenantdatabasecatalogs"
                )
                else
                response({"items": tenants}) if args[:2] == (
                    "get", "tenants.tenancy.cnpg-vcluster.io"
                ) else response({"metadata": {
                    "uid": "ns-uid", "name": "tenant-db-example",
                    "labels": {"tenancy.cnpg-vcluster.io/tenant-uid": "tenant-uid"},
                }}) if args[:2] == (
                    "get", "namespace/tenant-db-example"
                ) else response({"metadata": {
                    "uid": "storage-uid", "name": "tenant-db-storage-example",
                    "labels": {"tenancy.cnpg-vcluster.io/tenant-uid": "tenant-uid"},
                }}) if args[:2] == (
                    "get", "namespace/tenant-db-storage-example"
                ) else response(value)
            ))

        packaging.verify_catalog_release_capability(
            client([tenant]), provider="local",
        )
        packaging.verify_catalog_release_capability(
            client([tenant]), provider="local", probe=("example", "tenant-uid"),
        )
        with self.assertRaisesRegex(RuntimeError, "foreign Tenant"):
            packaging.verify_catalog_release_capability(
                client([tenant, tenant]), provider="local", probe=("example", "tenant-uid"),
            )
        azure_tenant = copy.deepcopy(tenant)
        azure_tenant["spec"]["provider"]["type"] = "azure"
        azure_tenant["status"]["databaseCapability"]["storageNamespaceUID"] = "storage-uid"
        packaging.verify_catalog_release_capability(
            client([azure_tenant]), provider="azure",
        )
        azure_tenant["status"]["databaseCapability"]["storageNamespaceUID"] = "foreign"
        with self.assertRaisesRegex(RuntimeError, "storage namespace identity"):
            packaging.verify_catalog_release_capability(
                client([azure_tenant]), provider="azure",
            )
        for tenants in ([], [{**tenant, "status": {}}], [
            {**tenant, "metadata": {**tenant["metadata"], "deletionTimestamp": "now"}}
        ]):
            with self.assertRaisesRegex(RuntimeError, "capability"):
                packaging.verify_catalog_release_capability(
                    client(tenants), provider="local",
                )
        changed = copy.deepcopy(catalog)
        changed["spec"]["entries"] = {"unexpected": {}}
        with self.assertRaisesRegex(RuntimeError, "identity is not exact"):
            packaging.verify_catalog_release_capability(
                client([tenant], changed), provider="local",
            )
        with self.assertRaisesRegex(RuntimeError, "azure is unavailable"):
            packaging.verify_catalog_release_capability(
                client([tenant]), provider="azure",
            )

    def test_release_rechecks_identity_and_refences_interruption(self):
        with (
            patch.object(packaging, "CATALOG_LIFECYCLE_READY", False),
            self.assertRaisesRegex(RuntimeError, "not approved"),
        ):
            packaging.release_catalog_and_tenant_cutover_locks(
                CONFIG, Client(), provider="local",
                tenant_image="tenant:image", database_image="database:image",
            )
        calls = []
        client = Client(lambda *args, **kwargs: (
            calls.append(args) or response("fence")
            if args[:1] == ("get",) else response()
        ))
        with (
            patch.object(packaging, "CATALOG_LIFECYCLE_READY", True),
            patch.object(packaging, "verify_catalog_release_gates",
                         return_value=("policy", 1, "binding", 1)) as gates,
            patch.object(packaging, "run_catalog_lifecycle_probe") as bootstrap,
            patch.object(packaging, "verify_catalog_cutover_lock") as verify,
            patch.object(packaging, "_record_catalog_activation"),
            patch.object(packaging, "verify_release_tenant_cutover_lock") as tenant_verify,
            patch.object(packaging, "remove_catalog_cutover_lock",
                         side_effect=RuntimeError("interrupted")) as remove,
            patch.object(packaging, "apply_tenant_cutover_lock") as tenant_lock,
            patch.object(packaging, "ensure_catalog_cutover_lock") as catalog_lock,
        ):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                packaging.release_catalog_and_tenant_cutover_locks(
                    CONFIG, client, provider="local",
                    tenant_image="tenant:image", database_image="database:image",
                )
            gates.assert_called_once_with(
                client, provider="local",
                tenant_image="tenant:image", database_image="database:image",
            )
            verify.assert_any_call(
                client, namespace="tenant-system",
                expected_identity=("policy", 1, "binding", 1),
            )
            self.assertEqual(tenant_verify.call_count, 2)
            remove.assert_called_once()
            bootstrap.assert_called_once_with(
                CONFIG, client, provider="local", database_image="database:image",
                prepare_capability=None,
            )
            tenant_lock.assert_called_once()
            catalog_lock.assert_called_once()
        self.assertFalse(any(args[0] == "delete" for args in calls))

    def test_approved_release_only_deletes_cutover_policies(self):
        client = Client()
        prepare = Mock()
        with (
            patch.object(packaging, "CATALOG_LIFECYCLE_READY", True),
            patch.object(packaging, "tenant_cutover_lock_present", return_value=True),
            patch.object(packaging, "verify_release_tenant_cutover_lock") as tenant_fence,
            patch.object(packaging, "verify_catalog_release_gates",
                         return_value=("p", 1, "b", 1)),
            patch.object(packaging, "run_catalog_lifecycle_probe") as bootstrap,
            patch.object(packaging, "verify_catalog_cutover_lock") as catalog_fence,
            patch.object(packaging, "_record_catalog_activation") as record,
        ):
            packaging.release_catalog_and_tenant_cutover_locks(
                CONFIG, client, provider="local",
                tenant_image="tenant:image", database_image="database:image",
                prepare_capability=prepare,
            )
            self.assertEqual(tenant_fence.call_count, 2)
            bootstrap.assert_called_once_with(
                CONFIG, client, provider="local", database_image="database:image",
                prepare_capability=prepare,
            )
            self.assertEqual(catalog_fence.call_count, 2)
            catalog_fence.assert_any_call(
                client, namespace="tenant-system", expected_identity=("p", 1, "b", 1),
            )
            record.assert_called_once_with(client)
        self.assertEqual(
            [args[1] for args, _ in client.calls if args[0] == "delete"],
            [
                *packaging.catalog_cutover_lock_cleanup_refs(),
            ],
        )
        self.assertFalse(any(args[0] == "create" for args, _ in client.calls))

    def test_completed_activation_reinstall_preserves_live_tenants_and_open_fences(self):
        crd = {
            "metadata": {"uid": "tenant-crd-uid"},
            "spec": {"versions": [{"name": "v1alpha4", "served": True, "storage": True}]},
            "status": {"storedVersions": ["v1alpha4"]},
        }
        identity = {
            f"crd/{packaging.TENANT_CRD}": crd,
            "crd/tenantdatabasecatalogs.tenancy.cnpg-vcluster.io":
                {"metadata": {"uid": "catalog-crd-uid"}},
            "namespace/kube-system": {"metadata": {"uid": "cluster-uid"}},
        }

        def handler(*args, **kwargs):
            if args[:2] == ("get", "tenants.tenancy.cnpg-vcluster.io"):
                return response({"items": [{"metadata": {"uid": "live-tenant"}}]})
            if args[:1] == ("get",):
                return response(identity.get(args[1], ""))
            raise AssertionError(f"reinstall mutated cutover state: {args}")

        client = Client(handler)
        packaging._record_catalog_activation(client)
        self.assertFalse(packaging.prepare_tenant_api_cutover(ROOT, CONFIG, client))
        self.assertTrue(all(args[0] == "get" for args, _ in client.calls))
        identity["namespace/kube-system"] = {"metadata": {"uid": "new-cluster"}}
        with self.assertRaisesRegex(RuntimeError, "another management cluster"):
            packaging.prepare_tenant_api_cutover(ROOT, CONFIG, client)
        self.assertTrue(all(args[0] == "get" for args, _ in client.calls))

    def test_release_gates_fail_before_unlock_on_rollout_rbac_or_capability(self):
        for stage in ("rollout", "rbac", "probe"):
            with self.subTest(stage=stage):
                client = Client()
                with (
                    patch.object(packaging, "verify_catalog_cutover_lock",
                                 return_value=("policy", 1, "binding", 1)) as fence,
                    patch.object(packaging, "verify_catalog_release_deployment",
                                 side_effect=(RuntimeError("rollout unavailable")
                                              if stage == "rollout" else None)),
                    patch.object(packaging, "verify_catalog_release_rbac",
                                 side_effect=(RuntimeError("rbac unavailable")
                                              if stage == "rbac" else None)),
                    patch.object(packaging, "verify_catalog_release_probe",
                                 side_effect=(RuntimeError("probe unavailable")
                                              if stage == "probe" else None)),
                    self.assertRaisesRegex(RuntimeError, "unavailable"),
                ):
                    packaging.verify_catalog_release_gates(
                        client, provider="local",
                        tenant_image="tenant:image", database_image="database:image",
                    )
                fence.assert_called_once()
                self.assertFalse(client.calls)

    def test_release_gate_revalidates_policy_identity_after_all_checks(self):
        client = Client()
        with (
            patch.object(packaging, "verify_catalog_cutover_lock",
                         side_effect=[
                             ("policy", 1, "binding", 1),
                             RuntimeError("identity changed"),
                         ]) as fence,
            patch.object(packaging, "verify_catalog_release_deployment"),
            patch.object(packaging, "verify_catalog_release_rbac"),
            patch.object(packaging, "verify_catalog_release_probe") as probe,
            self.assertRaisesRegex(RuntimeError, "identity changed"),
        ):
            packaging.verify_catalog_release_gates(
                client, provider="azure",
                tenant_image="tenant:image", database_image="database:image",
            )
        probe.assert_called_once_with(client, namespace="tenant-system")
        self.assertEqual(
            fence.call_args_list[-1].kwargs["expected_identity"],
            ("policy", 1, "binding", 1),
        )

    def test_tenant_release_fence_requires_current_exact_policy_and_denial(self):
        policy, binding = packaging.tenant_cutover_lock_documents()
        for doc in (policy, binding):
            doc["metadata"].update({"uid": doc["kind"], "generation": 2})
        policy["status"] = {
            "observedGeneration": 2, "typeChecking": {},
            "conditions": [{"status": "True", "observedGeneration": 2}],
        }

        def handler(*args, **kwargs):
            if args[:2] == ("get", f"validatingadmissionpolicy/{packaging.TENANT_CUTOVER_POLICY}"):
                return response(policy)
            if args[:2] == ("get", f"validatingadmissionpolicybinding/{packaging.TENANT_CUTOVER_POLICY}"):
                return response(binding)
            if args[:2] == ("create", "--dry-run=server"):
                return response(code=1, error=(
                    f"{packaging.TENANT_CUTOVER_POLICY}: "
                    "Tenant creation is locked during API cutover"
                ))
            raise AssertionError(args)

        client = Client(handler)
        packaging.verify_release_tenant_cutover_lock(client)
        self.assertEqual(
            len([args for args, _ in client.calls if args[:1] == ("create",)]), 5,
        )
        for args, kwargs in client.calls:
            if args[:1] == ("create",):
                self.assertLessEqual(
                    len(json.loads(kwargs["input_text"])["metadata"]["name"]), 30,
                )
        for mutation in (
            lambda p, b: p["status"].update(observedGeneration=1),
            lambda p, b: b["spec"].update(validationActions=["Warn"]),
            lambda p, b: b["metadata"].update(uid=""),
        ):
            changed_policy, changed_binding = copy.deepcopy((policy, binding))
            mutation(changed_policy, changed_binding)
            with self.assertRaisesRegex(RuntimeError, "contract is malformed"):
                packaging.verify_release_tenant_cutover_lock(
                    Client(lambda *args, **kwargs: (
                        response(changed_policy)
                        if args[:2] == ("get", f"validatingadmissionpolicy/{packaging.TENANT_CUTOVER_POLICY}")
                        else response(changed_binding)
                        if args[:2] == ("get", f"validatingadmissionpolicybinding/{packaging.TENANT_CUTOVER_POLICY}")
                        else handler(*args, **kwargs)
                    )),
                )
        with self.assertRaisesRegex(RuntimeError, "not effective"):
            packaging.verify_release_tenant_cutover_lock(
                Client(lambda *args, **kwargs: (
                    response(code=1, error="unrelated: Tenant creation is locked during API cutover")
                    if args[:2] == ("create", "--dry-run=server")
                    else handler(*args, **kwargs)
                )),
            )

    def test_catalog_cutover_lock_is_separate_and_effective(self):
        policy, binding = active_catalog_lock()
        self.assertEqual(policy["metadata"]["name"], packaging.CATALOG_CUTOVER_POLICY)
        self.assertEqual(policy["spec"]["failurePolicy"], "Fail")
        self.assertEqual(policy["spec"]["matchConstraints"]["resourceRules"][0], {
            "apiGroups": ["tenancy.cnpg-vcluster.io"],
            "apiVersions": ["v1alpha1"],
            "operations": ["CREATE"],
            "resources": ["tenantdatabasecatalogs"],
            "scope": "Namespaced",
        })
        self.assertEqual(binding["spec"]["validationActions"], ["Deny"])
        self.assertNotEqual(
            packaging.catalog_cutover_lock_cleanup_refs(),
            packaging.tenant_cutover_lock_cleanup_refs(),
        )
        calls = []

        def reject(*args, **kwargs):
            calls.append((args, kwargs))
            if args[:2] == ("get", f"validatingadmissionpolicy/{packaging.CATALOG_CUTOVER_POLICY}"):
                return response(policy)
            if args[:2] == ("get", f"validatingadmissionpolicybinding/{packaging.CATALOG_CUTOVER_POLICY}"):
                return response(binding)
            if args[:2] == ("create", "--dry-run=server"):
                return response(code=1, error=f"{packaging.CATALOG_CUTOVER_POLICY}: TenantDatabaseCatalog creation is locked during API cutover")
            return response()

        client = Client(reject)
        packaging.ensure_catalog_cutover_lock(client)
        packaging.verify_catalog_cutover_lock(client, namespace="tenant-system")
        self.assertEqual(len([args for args, _ in calls if args[0] == "create"]), 5)
        def webhook_first(*args, **kwargs):
            result = reject(*args, **kwargs)
            if args[:2] == ("create", "--dry-run=server"):
                return response(code=1, error="Tenant identity is unavailable or not Ready")
            return result
        with self.assertRaisesRegex(RuntimeError, "not effective"):
            packaging.verify_catalog_cutover_lock(
                Client(webhook_first), namespace="tenant-system",
            )
        with self.assertRaisesRegex(RuntimeError, "not effective"):
            packaging.verify_catalog_cutover_lock(
                Client(lambda *args, **kwargs: (
                    reject(*args, **kwargs)
                    if args[:1] == ("get",) else response()
                )), namespace="tenant-system",
            )
        invalid_policy = copy.deepcopy(policy)
        invalid_policy["spec"]["validations"][0]["expression"] = "true"
        with self.assertRaisesRegex(RuntimeError, "contract is malformed"):
            packaging.verify_catalog_cutover_lock(
                Client(lambda *args, **kwargs: (
                    response(invalid_policy)
                    if args[:2] == ("get", f"validatingadmissionpolicy/{packaging.CATALOG_CUTOVER_POLICY}")
                    else reject(*args, **kwargs)
                )), namespace="tenant-system",
            )
        for mutation in (
            lambda p, b: p.pop("status"),
            lambda p, b: p.update(spec=[]),
            lambda p, b: p["status"].update(observedGeneration=0),
            lambda p, b: p["status"]["typeChecking"].update(
                expressionWarnings="invalid",
            ),
            lambda p, b: p["status"]["typeChecking"].update(
                expressionWarnings=[{"warning": "invalid expression"}],
            ),
            lambda p, b: b["metadata"].update(name="replacement"),
            lambda p, b: b["metadata"].update(generation=0),
            lambda p, b: b["metadata"].pop("uid"),
            lambda p, b: b["spec"].update(validationActions=["Warn"]),
            lambda p, b: b["spec"].update(policyName="different-policy"),
            lambda p, b: b["spec"].update(matchResources={"namespaceSelector": {}}),
            lambda p, b: p["status"].update(conditions=[{
                "type": "Ready", "status": "True", "observedGeneration": 0,
            }]),
            lambda p, b: p["spec"]["matchConstraints"].update(matchPolicy="Exact"),
        ):
            changed_policy, changed_binding = active_catalog_lock()
            mutation(changed_policy, changed_binding)
            with self.assertRaisesRegex(RuntimeError, "contract is malformed"):
                packaging.verify_catalog_cutover_lock(
                    Client(lambda *args, **kwargs: (
                        response(changed_policy) if args[:2] == (
                            "get", f"validatingadmissionpolicy/{packaging.CATALOG_CUTOVER_POLICY}"
                        ) else response(changed_binding) if args[:2] == (
                            "get", f"validatingadmissionpolicybinding/{packaging.CATALOG_CUTOVER_POLICY}"
                        ) else reject(*args, **kwargs)
                    )), namespace="tenant-system",
                )
        changed_policy, changed_binding = active_catalog_lock()
        changed_binding["metadata"]["uid"] = "replaced"
        policy_reads = 0

        def replaced(*args, **kwargs):
            nonlocal policy_reads
            if args[:2] == ("get", f"validatingadmissionpolicy/{packaging.CATALOG_CUTOVER_POLICY}"):
                policy_reads += 1
            if args[:2] == ("get", f"validatingadmissionpolicybinding/{packaging.CATALOG_CUTOVER_POLICY}") and policy_reads > 1:
                return response(changed_binding)
            return reject(*args, **kwargs)

        with self.assertRaisesRegex(RuntimeError, "identity changed"):
            packaging.verify_catalog_cutover_lock(Client(replaced), namespace="tenant-system")
        probe_count = 0

        def denial_lost(*args, **kwargs):
            nonlocal probe_count
            result = reject(*args, **kwargs)
            if args[:2] == ("create", "--dry-run=server"):
                probe_count += 1
                if probe_count == 3:
                    return response(code=1, error="denied by unrelated admission policy")
            return result

        with self.assertRaisesRegex(RuntimeError, "not effective"):
            packaging.verify_catalog_cutover_lock(
                Client(denial_lost), namespace="tenant-system",
            )
        self.assertEqual(probe_count, 3)
        excluded_policy = copy.deepcopy(policy)
        excluded_policy["spec"]["matchConstraints"]["excludeResourceRules"] = [{
            "apiGroups": ["tenancy.cnpg-vcluster.io"],
            "apiVersions": ["v1alpha1"],
            "operations": ["CREATE"],
            "resources": ["tenantdatabasecatalogs"],
        }]
        with self.assertRaisesRegex(RuntimeError, "contract is malformed"):
            packaging.verify_catalog_cutover_lock(
                Client(lambda *args, **kwargs: (
                    response(excluded_policy)
                    if args[:2] == ("get", f"validatingadmissionpolicy/{packaging.CATALOG_CUTOVER_POLICY}")
                    else webhook_first(*args, **kwargs)
                )), namespace="tenant-system",
            )
        selected_policy = copy.deepcopy(policy)
        selected_policy["spec"]["matchConstraints"]["namespaceSelector"] = {
            "matchLabels": {"tenant-database-gates": "excluded"}
        }
        with self.assertRaisesRegex(RuntimeError, "contract is malformed"):
            packaging.verify_catalog_cutover_lock(
                Client(lambda *args, **kwargs: (
                    response(selected_policy)
                    if args[:2] == ("get", f"validatingadmissionpolicy/{packaging.CATALOG_CUTOVER_POLICY}")
                    else webhook_first(*args, **kwargs)
                )), namespace="tenant-system",
            )

    def test_cutover_lock_denies_create_for_old_and_new_api_generations(self):
        policy, binding = packaging.tenant_cutover_lock_documents()
        self.assertEqual(policy["metadata"]["name"], packaging.TENANT_CUTOVER_POLICY)
        rule = policy["spec"]["matchConstraints"]["resourceRules"][0]
        self.assertEqual(rule["apiVersions"], ["v1alpha3", "v1alpha4"])
        self.assertEqual(rule["operations"], ["CREATE"])
        self.assertEqual(policy["spec"]["failurePolicy"], "Fail")
        self.assertEqual(binding["spec"]["policyName"], packaging.TENANT_CUTOVER_POLICY)
        self.assertEqual(binding["spec"]["validationActions"], ["Deny"])
        self.assertEqual(
            packaging.tenant_cutover_lock_cleanup_refs(),
            (
                f"validatingadmissionpolicybinding/{packaging.TENANT_CUTOVER_POLICY}",
                f"validatingadmissionpolicy/{packaging.TENANT_CUTOVER_POLICY}",
            ),
        )

    def test_partial_catalog_policy_removal_is_refenced_before_cutover_retry(self):
        refs = (
            f"validatingadmissionpolicy/{packaging.CATALOG_CUTOVER_POLICY}",
            f"validatingadmissionpolicybinding/{packaging.CATALOG_CUTOVER_POLICY}",
        )
        for survivor in refs:
            client = Client(lambda *args, **kwargs: (
                response(args[1]) if args[:2] == ("get", survivor)
                else response()
            ))
            events = []
            with (
                patch.object(packaging, "ensure_catalog_cutover_lock",
                             side_effect=lambda *_: events.append("restore")),
                patch.object(packaging, "verify_catalog_cutover_lock",
                             side_effect=lambda *_a, **_k: events.append("deny")),
            ):
                self.assertTrue(packaging._catalog_lock_present(client))
            self.assertEqual(events, ["restore", "deny"])
            with (
                patch.object(packaging, "ensure_catalog_cutover_lock",
                             side_effect=RuntimeError("cannot restore")),
                self.assertRaisesRegex(RuntimeError, "cannot restore"),
            ):
                packaging._catalog_lock_present(client)

    def test_cutover_preflight_requires_empty_tenant_and_provider_inventory(self):
        packaging.require_empty_tenant_cutover({"items": []}, [])
        with self.assertRaisesRegex(RuntimeError, "retained Tenants"):
            packaging.require_empty_tenant_cutover({"items": [{}]}, [])
        with self.assertRaisesRegex(RuntimeError, "provider residue"):
            packaging.require_empty_tenant_cutover(
                {"items": []}, ["AzureCluster/tenant-a"]
            )
        with self.assertRaisesRegex(RuntimeError, "inventory is invalid"):
            packaging.require_empty_tenant_cutover({}, [])
        packaging.require_tenant_cutover_double_check(
            {"items": []}, {"items": []}, []
        )
        with self.assertRaisesRegex(RuntimeError, "retained Tenants"):
            packaging.require_tenant_cutover_double_check(
                {"items": []}, {"items": [{}]}, []
            )

    def test_cutover_generation_detection_and_phase_two_activation_ready(self):
        for version in ("v1alpha3", "v1alpha4"):
            crd = {
                "spec": {
                    "versions": [
                        {"name": version, "served": True, "storage": True}
                    ]
                },
                "status": {"storedVersions": [version]},
            }
            self.assertEqual(packaging.tenant_api_generation(crd), version)
            self.assertEqual(packaging.tenant_api_cutover_state(crd), version)
        transition = packaging.tenant_crd_transition_document(
            {
                "spec": {"versions": [{
                    "name": "v1alpha3",
                    "served": True,
                    "storage": True,
                }]},
                "status": {"storedVersions": ["v1alpha3"]},
            },
            desired_crd(),
        )
        transition["status"] = {"storedVersions": ["v1alpha3"]}
        self.assertEqual(
            "transitioning",
            packaging.tenant_api_cutover_state(transition),
        )
        self.assertFalse(transition["spec"]["versions"][0]["served"])
        self.assertFalse(transition["spec"]["versions"][0]["storage"])
        packaging.require_tenant_api_cutover_ready()

    def test_cutover_locks_checks_stops_and_transitions_old_crd(self):
        old = {
            "spec": {
                "versions": [
                    {"name": "v1alpha3", "served": True, "storage": True}
                ]
            },
            "status": {"storedVersions": ["v1alpha3"]},
        }

        def handle(*args, **_kwargs):
            if args[:2] == ("get", f"crd/{packaging.TENANT_CRD}"):
                return response(old)
            return response()

        client = Client(handle)
        with (
            patch.object(packaging, "require_clean_controller_state") as clean,
            patch.object(packaging, "stop_controller") as stop,
            patch.object(packaging, "verify_tenant_cutover_lock") as fence,
            patch.object(packaging, "desired_tenant_crd", return_value=desired_crd()),
        ):
            self.assertTrue(
                packaging.prepare_tenant_api_cutover(Path("."), CONFIG, client)
            )
        self.assertEqual(clean.call_count, 4)
        stop.assert_called_once()
        self.assertEqual(2, fence.call_count)
        self.assertFalse(any(
            args[:2] == ("delete", f"crd/{packaging.TENANT_CRD}")
            for args, _ in client.calls
        ))
        self.assertTrue(any(
            args[:2] == ("patch", f"crd/{packaging.TENANT_CRD}")
            and "--subresource=status" in args
            for args, _ in client.calls
        ))

    def test_cutover_second_check_failure_retains_lock_and_old_crd(self):
        old = {
            "spec": {
                "versions": [
                    {"name": "v1alpha3", "served": True, "storage": True}
                ]
            },
            "status": {"storedVersions": ["v1alpha3"]},
        }
        client = Client(
            lambda *args, **_kwargs: response(old)
            if args[:2] == ("get", f"crd/{packaging.TENANT_CRD}")
            else response()
        )
        with (
            patch.object(
                packaging,
                "require_clean_controller_state",
                side_effect=[None, RuntimeError("race")],
            ),
            patch.object(packaging, "stop_controller"),
            patch.object(packaging, "verify_tenant_cutover_lock"),
            patch.object(packaging, "desired_tenant_crd", return_value=desired_crd()),
            self.assertRaisesRegex(RuntimeError, "race"),
        ):
            packaging.prepare_tenant_api_cutover(Path("."), CONFIG, client)
        self.assertFalse(
            any(
                args[:2] == ("delete", f"crd/{packaging.TENANT_CRD}")
                for args, _ in client.calls
            )
        )
        for resource in packaging.tenant_cutover_lock_cleanup_refs():
            self.assertFalse(any(args[:2] == ("delete", resource) for args, _ in client.calls))
        self.assertTrue(any(
            args[:4]
            == ("-n", packaging.CONTROLLER_NAMESPACE, "scale",
                f"deployment/{packaging.CONTROLLER_DEPLOYMENT}")
            and "--replicas=1" in args
            for args, _ in client.calls
        ))

    def test_cutover_crd_render_failure_retains_catalog_cutover_lock(self):
        old = {
            "spec": {"versions": [{
                "name": "v1alpha3", "served": True, "storage": True,
            }]},
            "status": {"storedVersions": ["v1alpha3"]},
        }
        client = Client(
            lambda *args, **_kwargs: response(old)
            if args[:2] == ("get", f"crd/{packaging.TENANT_CRD}")
            else response()
        )
        with (
            patch.object(
                packaging,
                "desired_tenant_crd",
                side_effect=RuntimeError("render failed"),
            ),
            self.assertRaisesRegex(RuntimeError, "render failed"),
        ):
            packaging.prepare_tenant_api_cutover(Path("."), CONFIG, client)
        self.assertTrue(any(
            args and args[0] == "apply" and
            packaging.CATALOG_CUTOVER_POLICY in kwargs.get("input_text", "")
            for args, kwargs in client.calls
        ))
        self.assertFalse(any(
            args and args[0] == "apply" and
            packaging.TENANT_CUTOVER_POLICY in kwargs.get("input_text", "")
            for args, kwargs in client.calls
        ))

    def test_cutover_restore_failure_retains_lock(self):
        old = {
            "spec": {"versions": [{
                "name": "v1alpha3", "served": True, "storage": True,
            }]},
            "status": {"storedVersions": ["v1alpha3"]},
        }
        client = Client(
            lambda *args, **_kwargs: response(old)
            if args[:2] == ("get", f"crd/{packaging.TENANT_CRD}")
            else response()
        )
        with (
            patch.object(packaging, "desired_tenant_crd", return_value=desired_crd()),
            patch.object(packaging, "require_clean_controller_state",
                         side_effect=[None, RuntimeError("late Tenant")]),
            patch.object(packaging, "stop_controller"),
            patch.object(
                packaging,
                "restore_controller",
                side_effect=RuntimeError("restore failed"),
            ),
            patch.object(packaging, "remove_tenant_cutover_lock") as remove,
            self.assertRaisesRegex(RuntimeError, "restore failed"),
        ):
            packaging.prepare_tenant_api_cutover(Path("."), CONFIG, client)
        remove.assert_not_called()

    def test_cutover_preserves_existing_lock_without_reapply(self):
        old = {
            "spec": {"versions": [{
                "name": "v1alpha3", "served": True, "storage": True,
            }]},
            "status": {"storedVersions": ["v1alpha3"]},
        }
        refs = set(packaging.tenant_cutover_lock_cleanup_refs())

        def handle(*args, **_kwargs):
            if args[:2] == ("get", f"crd/{packaging.TENANT_CRD}"):
                return response(old)
            if args and args[0] == "get" and args[1] in refs:
                return response(args[1])
            return response()

        with (
            patch.object(packaging, "desired_tenant_crd", return_value=desired_crd()),
            patch.object(packaging, "apply_tenant_cutover_lock") as apply_lock,
            patch.object(packaging, "verify_tenant_cutover_lock"),
            patch.object(
                packaging,
                "require_clean_controller_state",
                side_effect=RuntimeError("not clean"),
            ),
            patch.object(packaging, "restore_controller"),
            patch.object(
                packaging,
                "remove_tenant_cutover_lock",
            ) as remove_lock,
            self.assertRaisesRegex(RuntimeError, "not clean"),
        ):
            packaging.prepare_tenant_api_cutover(Path("."), CONFIG, Client(handle))
        apply_lock.assert_not_called()
        remove_lock.assert_not_called()

    def test_partial_cutover_lock_application_retains_policy_for_retry(self):
        apply_count = 0

        def handle(*args, **_kwargs):
            nonlocal apply_count
            if args and args[0] == "apply":
                apply_count += 1
                if apply_count == 2:
                    return response(code=1, error="binding rejected")
            return response()

        client = Client(handle)
        with self.assertRaises(RuntimeError):
            packaging.apply_tenant_cutover_lock(CONFIG, client)
        for resource in packaging.tenant_cutover_lock_cleanup_refs():
            self.assertFalse(any(args[:2] == ("delete", resource) for args, _ in client.calls))
        self.assertEqual(apply_count, 2)

    def test_cutover_rerun_adopts_existing_lock_after_crd_deletion(self):
        refs = set(packaging.tenant_cutover_lock_cleanup_refs())

        def handle(*args, **_kwargs):
            if args[:2] == ("get", f"crd/{packaging.TENANT_CRD}"):
                return response()
            if args and args[0] == "get" and args[1] in refs:
                return response(args[1])
            return response()

        self.assertTrue(
            packaging.prepare_tenant_api_cutover(
                Path("."), CONFIG, Client(handle)
            )
        )

    def test_cutover_lock_probe_requires_admission_denial(self):
        client = Client(
            lambda *_args, **_kwargs: response(
                code=1,
                error="Tenant creation is locked during API cutover",
            )
        )
        packaging.verify_tenant_cutover_lock(client, "v1alpha3")
        self.assertEqual(5, len(client.calls))
        for args, kwargs in client.calls:
            probe = json.loads(kwargs["input_text"])
            self.assertLessEqual(len(probe["metadata"]["name"]), 30)
            self.assertEqual("Tenant", probe["kind"])

    def setUp(self):
        (ROOT / ".runtime").mkdir(exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=ROOT / ".runtime")
        self.addCleanup(self.directory.cleanup)
        legacy = patch.object(packaging, "require_absent_legacy_database_crd")
        legacy.start()
        self.addCleanup(legacy.stop)
        self.probe_path = Path(self.directory.name) / "probe.json"
        record_path = patch.object(
            packaging, "_probe_record_path", return_value=self.probe_path,
        )
        record_path.start()
        self.addCleanup(record_path.stop)
        self.repository = Path(self.directory.name)
        self.root = self.repository / "capi"
        self.root.mkdir(mode=0o700)
        (self.root / "controller").mkdir(mode=0o700)

    def toolchain(self, *_args):
        return ("/installed/cargo", "rustc 1.96\ncargo 1.96")

    def test_cargo_commands_use_process_environment_defaults(self):
        with (
            patch.object(packaging, "rust_toolchain", side_effect=self.toolchain),
            patch.object(packaging, "run") as run,
        ):
            packaging._cargo(self.root, CONFIG, ["metadata", "--no-deps"])
        self.assertEqual(
            run.call_args.args[0],
            ["/installed/cargo", "metadata", "--no-deps"],
        )
        self.assertNotIn("env", run.call_args.kwargs)

    def test_system_compiler_floor_and_missing_tools_never_install(self):
        for version, valid in (("1.88.9", False), ("1.89.0", True), ("1.96.0", True)):
            with (
                self.subTest(version=version),
                patch.object(packaging.shutil, "which", side_effect=lambda name: f"/installed/{name}"),
                patch.object(packaging, "run", side_effect=[
                    response(f"rustc {version} (system)\nhost: x86_64-unknown-linux-gnu"),
                    response("cargo 1.96.0"),
                ]) as run,
            ):
                if valid:
                    cargo, identity = packaging.rust_toolchain(self.root)
                    self.assertEqual(cargo, "/installed/cargo")
                    self.assertIn(version, identity)
                else:
                    with self.assertRaisesRegex(RuntimeError, ">= 1.89"):
                        packaging.rust_toolchain(self.root)
                self.assertEqual(len(run.call_args_list), 2)
                self.assertTrue(all(call.args[0][1:] == ["--version", "--verbose"]
                                    for call in run.call_args_list))
                self.assertTrue(all("env" not in call.kwargs for call in run.call_args_list))
        with patch.object(packaging.shutil, "which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "rustc >= 1.89"):
                packaging.rust_toolchain(self.root)

    def test_fetch_is_locked_and_enforced_offline_cannot_download(self):
        for offline in ("0", "1"):
            with (
                self.subTest(offline=offline),
                patch.dict(os.environ, {"CAPI_OFFLINE_ENFORCED": offline}),
                patch.object(packaging, "rust_toolchain", side_effect=self.toolchain),
                patch.object(packaging, "run") as run,
            ):
                packaging.fetch_controller_dependencies(self.root, CONFIG)
                command = run.call_args.args[0]
                self.assertEqual(command[:3], ["/installed/cargo", "fetch", "--locked"])
                self.assertEqual("--offline" in command, offline == "1")
                self.assertNotIn("env", run.call_args.kwargs)

    def test_default_target_offline_static_final_manager_build(self):
        binary = self.root / "shared-target" / "release" / "manager"

        def build(command, **kwargs):
            self.assertEqual(command, [
                "/installed/cargo", "rustc", "--locked", "--offline", "--release",
                "--bin", "manager", "--message-format=json-render-diagnostics",
                "--", "-C", "target-feature=+crt-static",
            ])
            self.assertNotIn("env", kwargs)
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"\x7fELFstatic")
            return response(
                json.dumps({
                    "reason": "compiler-artifact",
                    "target": {"name": "manager"},
                    "executable": str(binary),
                })
            )

        with (
            patch.object(packaging, "fetch_controller_dependencies") as fetch,
            patch.object(packaging, "rust_toolchain", side_effect=self.toolchain),
            patch.object(packaging, "run", side_effect=build),
            patch.object(packaging, "verify_static_manager") as verify,
        ):
            output = packaging.build_controller_binary(self.root, CONFIG)
        fetch.assert_called_once_with(self.root, CONFIG)
        verify.assert_called_once_with(binary)
        self.assertEqual(output.read_bytes(), b"\x7fELFstatic")
        self.assertEqual(output.stat().st_mode & 0o777, 0o700)

    def test_prebuilt_manager_is_verified_cleanup_safe_and_never_falls_back(self):
        source = self.root / ".tools/artifacts/commit/manager"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"\x7fELFprebuilt")
        source.chmod(0o600)
        with (
            patch.dict(
                os.environ,
                {"CAPI_PREBUILT_CONTROLLER_BINARY": str(source)},
            ),
            patch.object(packaging, "verify_static_manager") as verify,
            patch.object(packaging, "fetch_controller_dependencies") as fetch,
            patch.object(packaging, "run") as run,
        ):
            output = packaging.build_controller_binary(self.root, CONFIG)
        verify.assert_called_once_with(source.resolve())
        fetch.assert_not_called()
        run.assert_not_called()
        self.assertTrue(source.exists())
        self.assertEqual(output.read_bytes(), source.read_bytes())
        self.assertEqual(output.stat().st_mode & 0o777, 0o700)
        outside = self.root / "outside-manager"
        outside.write_bytes(b"\x7fELF")
        with patch.dict(
            os.environ,
            {"CAPI_PREBUILT_CONTROLLER_BINARY": str(outside)},
        ), self.assertRaisesRegex(RuntimeError, "outside"):
            packaging.build_controller_binary(self.root, CONFIG)

    def test_static_checks_fail_closed_on_non_elf_interp_and_needed(self):
        binary = self.root / "manager"
        binary.write_bytes(b"not ELF")
        with self.assertRaisesRegex(RuntimeError, "not an ELF"):
            packaging.verify_static_manager(binary)
        binary.write_bytes(b"\x7fELF")
        for headers, dynamic in (("INTERP", ""), ("", "(NEEDED) libc.so.6"), ("LOAD", "No dynamic section")):
            with self.subTest(headers=headers, dynamic=dynamic), patch.object(
                packaging, "run", side_effect=[response(headers), response(dynamic)],
            ):
                if "INTERP" in headers or "NEEDED" in dynamic:
                    with self.assertRaisesRegex(RuntimeError, "must be static"):
                        packaging.verify_static_manager(binary)
                else:
                    packaging.verify_static_manager(binary)

    def test_generate_check_is_read_only_and_rust_gates_use_locked_offline(self):
        with (
            patch.object(packaging, "fetch_controller_dependencies"),
            patch.object(packaging, "_cargo") as cargo,
        ):
            packaging.generate_controller(self.root, CONFIG, check=True)
            packaging.test_controller(self.root, CONFIG)
            packaging.vet_controller(self.root, CONFIG)
        self.assertEqual(cargo.call_args_list[0].args[2], [
            "run", "--locked", "--offline", "--bin", "generate", "--", "--check",
        ])
        for call in (cargo.call_args_list[1], cargo.call_args_list[3]):
            self.assertIn("--locked", call.args[2])
            self.assertIn("--offline", call.args[2])
            self.assertIn("--all-targets", call.args[2])
        self.assertEqual(cargo.call_args_list[2].args[2], ["fmt", "--all", "--check"])

    def test_source_identity_tracks_rust_compiler_flags_assets_not_go_or_target(self):
        files = {
            "../Cargo.toml": "[workspace]", "../Cargo.lock": "lock",
            "../rust-toolchain.toml": '[toolchain]\nchannel = "stable"',
            "controller/Cargo.toml": "[package]",
            "controller/src/lib.rs": "source", "controller/Dockerfile": "FROM scratch",
            "database-runtime/Cargo.toml": "[package]",
            "database-runtime/src/lib.rs": "shared source",
            "controller/config/manager/manager.yaml.tpl": "manager",
            "controller/config/crd/bases/tenant.yaml": "crd",
            "controller/config/rbac/role.yaml": "role",
            "controller/config/rbac/role-azure.yaml": "azure-role",
            ".tools/inputs/calico.yaml": "calico", ".tools/inputs/cnpg.yaml": "cnpg",
        }
        for name, content in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        with patch.object(packaging, "rust_toolchain", side_effect=self.toolchain):
            original = packaging.controller_source_digest(self.root, CONFIG)
            for name, content in files.items():
                with self.subTest(name=name):
                    (self.root / name).write_text(content + "changed")
                    self.assertNotEqual(original, packaging.controller_source_digest(self.root, CONFIG))
                    (self.root / name).write_text(content)
            (self.root / "controller/unrelated.txt").write_text("ignored")
            self.assertEqual(original, packaging.controller_source_digest(self.root, {**CONFIG, "UNRELATED": "99"}))
            with patch.object(packaging, "STATIC_MANAGER_FLAGS", ("changed",)):
                self.assertNotEqual(original, packaging.controller_source_digest(self.root, CONFIG))
        with patch.object(packaging, "rust_toolchain", return_value=("cargo", "different compiler")):
            self.assertNotEqual(original, packaging.controller_source_digest(self.root, CONFIG))

    def test_foundation_wires_the_schema_three_producer(self):
        self.assertIs(packaging._foundation_payload, foundation_payload)

    def test_manager_template_has_no_webhook_or_tls_and_single_recreate(self):
        text = (ROOT / "controller/config/manager/manager.yaml.tpl").read_text()
        for absent in ("webhook", "tls", "9443", "secretName"):
            self.assertNotIn(absent, text)
        for present in ("replicas: 1", "type: Recreate", "--leader-elect=true",
                        "--provider=local", "/healthz", "/readyz",
                        "/var/run/docker.sock"):
            self.assertIn(present, text)
        dockerfile = (ROOT / "controller/Dockerfile").read_text()
        self.assertIn("FROM scratch", dockerfile)
        self.assertIn("COPY assets /assets", dockerfile)

    def test_azure_image_and_manager_exclude_local_only_dependencies(self):
        dockerfile = ROOT / "controller/Dockerfile.azure"
        self.assertEqual(
            dockerfile.read_text(),
            "FROM scratch\nCOPY manager /manager\nENTRYPOINT [\"/manager\"]\n",
        )
        image = (
            "registry.example/tenant-controller@sha256:"
            + "1" * 64
        )
        rendered = packaging.render_azure_controller_manager(
            ROOT,
            "v1.32.13",
            image,
            "2" * 64,
        )
        content = rendered.read_text()
        self.assertIn("--provider=azure", content)
        self.assertIn(f"image: {image}", content)
        self.assertIn("--supported-kubernetes-version=1.32.13", content)
        self.assertIn(
            "tenancy.cnpg-vcluster.io/allocation-sha256: " + "2" * 64,
            content,
        )
        for absent in (
            "docker.sock",
            "tenant-foundation",
            "activation-token",
            "calico",
            "cnpg.yaml",
            "imagePullPolicy: Never",
        ):
            self.assertNotIn(absent, content.lower())

    def test_azure_image_build_context_contains_only_static_manager(self):
        binary = self.root / ".runtime/rendered/controller/manager"
        binary.parent.mkdir(parents=True)
        (self.root / ".runtime").chmod(0o700)
        (self.root / ".runtime/rendered").chmod(0o700)
        binary.parent.chmod(0o700)
        binary.write_bytes(b"\x7fELFmanager")
        azure_dockerfile = self.root / "controller/Dockerfile.azure"
        azure_dockerfile.write_text(
            "FROM scratch\nCOPY manager /manager\nENTRYPOINT [\"/manager\"]\n"
        )

        def build(command, **_kwargs):
            context = Path(command[-1])
            self.assertEqual(
                sorted(path.name for path in context.iterdir()),
                ["Dockerfile", "manager"],
            )
            return response()

        with (
            patch.object(packaging, "generate_controller"),
            patch.object(packaging, "build_controller_binary", return_value=binary),
            patch.object(packaging, "verify_static_manager"),
            patch.object(packaging, "run", side_effect=build) as run,
        ):
            result = packaging.build_azure_controller_image(
                self.root,
                {"COMMAND_TIMEOUT": "1s"},
                "registry.example/tenant-controller:v1alpha2",
            )
        self.assertEqual(result, "registry.example/tenant-controller:v1alpha2")
        self.assertEqual(
            run.call_args.args[0][:5],
            [
                "docker",
                "build",
                "--pull=false",
                "-t",
                "registry.example/tenant-controller:v1alpha2",
            ],
        )


class CatalogInstallerTests(unittest.TestCase):
    @staticmethod
    def catalog_crd():
        return {
            "spec": {"versions": [{
                "name": "v1alpha1", "served": True, "storage": True,
                "subresources": {"status": {}},
            }]},
            "status": {"storedVersions": ["v1alpha1"]},
        }

    def test_catalog_installer_requires_discovery_and_policy_denial(self):
        local = database_controller.catalog_manifests(ROOT)
        azure = database_controller.catalog_manifests(ROOT, azure=True)
        self.assertTrue(all(path.is_file() for path in (*local, *azure)))
        self.assertIn(
            "tenancy.cnpg-vcluster.io_tenantdatabasecatalogs.yaml",
            [path.name for path in local],
        )
        self.assertNotIn("controller-cluster-role-azure.yaml", [path.name for path in local])
        self.assertNotIn("controller-cluster-role.yaml", [path.name for path in azure])
        self.assertFalse(any("admission" in str(path) or "gates" in str(path)
                             for path in (*local, *azure)))
        policy, binding = active_catalog_lock()

        def handler(*args, **_kwargs):
            if args[:2] == ("get", f"crd/{database_controller.CATALOG_CRD}"):
                return response(self.catalog_crd())
            if args[:2] == ("get", f"validatingadmissionpolicy/{packaging.CATALOG_CUTOVER_POLICY}"):
                return response(policy)
            if args[:2] == ("get", f"validatingadmissionpolicybinding/{packaging.CATALOG_CUTOVER_POLICY}"):
                return response(binding)
            if args == ("get", "--raw=/apis/tenancy.cnpg-vcluster.io/v1alpha1"):
                return response({"resources": [{
                    "name": "tenantdatabasecatalogs",
                    "kind": "TenantDatabaseCatalog",
                    "namespaced": True,
                }]})
            if args[:2] == ("create", "--dry-run=server"):
                return response(code=1, error=(
                    f"{packaging.CATALOG_CUTOVER_POLICY}: "
                    "TenantDatabaseCatalog creation is locked during API cutover"
                ))
            return response()

        with patch.object(packaging, "require_absent_legacy_database_crd") as preflight:
            client = Client(handler)
            packaging.install_database_catalog(ROOT, CONFIG, client)
            preflight.assert_called_once_with(client)
            self.assertEqual(
                len([args for args, _ in client.calls if args[:2] == ("create", "--dry-run=server")]),
                25,
            )
            self.assertFalse(any("database-admission" in str(args) for args, _ in client.calls))

            with self.assertRaisesRegex(RuntimeError, "endpoint is not served"):
                packaging.install_database_catalog(
                    ROOT, CONFIG, Client(lambda *args, **kw: (
                        response({"resources": []})
                        if args == ("get", "--raw=/apis/tenancy.cnpg-vcluster.io/v1alpha1")
                        else handler(*args, **kw)
                    )),
                )
            self.assertEqual(preflight.call_count, 2)

            with self.assertRaisesRegex(RuntimeError, "not effective"):
                packaging.install_database_catalog(
                    ROOT, CONFIG, Client(lambda *args, **kw: (
                        response(code=1, error="schema validation rejected")
                        if args[:2] == ("create", "--dry-run=server")
                        else handler(*args, **kw)
                    )),
                )
            self.assertEqual(preflight.call_count, 3)

            probes = 0
            def lagging(*args, **kw):
                nonlocal probes
                if args[:2] == ("create", "--dry-run=server"):
                    probes += 1
                    if probes == 8:
                        return response()
                return handler(*args, **kw)

            with self.assertRaisesRegex(RuntimeError, "not effective"):
                packaging.install_database_catalog(ROOT, CONFIG, Client(lagging))
            self.assertEqual(preflight.call_count, 4)

    def test_catalog_reinstall_does_not_recreate_verified_released_fence(self):
        def handler(*args, **kwargs):
            if args[:2] == ("get", f"crd/{database_controller.CATALOG_CRD}"):
                return response(self.catalog_crd())
            if args == ("get", "--raw=/apis/tenancy.cnpg-vcluster.io/v1alpha1"):
                return response({"resources": [{
                    "name": "tenantdatabasecatalogs",
                    "kind": "TenantDatabaseCatalog", "namespaced": True,
                }]})
            return response()

        client = Client(handler)
        with (
            patch.object(packaging, "_catalog_activation_complete", return_value=True),
            patch.object(packaging, "_catalog_lock_present", return_value=False),
            patch.object(packaging, "tenant_cutover_lock_present", return_value=False),
        ):
            packaging.install_database_catalog(
                ROOT, CONFIG, client, cutover_locked=False,
            )
            self.assertFalse(any(
                args[:2] == ("create", "--dry-run=server")
                or kwargs.get("input_text", "").find(packaging.CATALOG_CUTOVER_POLICY) >= 0
                for args, kwargs in client.calls
            ))
            with (
                patch.object(packaging, "_catalog_lock_present", return_value=True),
                self.assertRaisesRegex(RuntimeError, "no longer open"),
            ):
                packaging.install_database_catalog(
                    ROOT, CONFIG, Client(handler), cutover_locked=False,
                )

    def test_legacy_crd_blocks_before_catalog_install_and_is_never_deleted(self):
        client = Client(lambda *args, **_kwargs: (
            response(f"crd/{database_controller.LEGACY_CRD}")
            if args[:2] == ("get", f"crd/{database_controller.LEGACY_CRD}")
            else response()
        ))
        with (
            patch.object(packaging, "require_absent_legacy_database_crd",
                         database_controller.require_absent_legacy_database_crd),
            self.assertRaisesRegex(RuntimeError, "Separately prove"),
        ):
            packaging.install_database_catalog(ROOT, CONFIG, client)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0][0][:2], (
            "get", f"crd/{database_controller.LEGACY_CRD}",
        ))

    def test_legacy_crd_blocks_tenant_cutover_before_policy_mutation(self):
        client = Client(lambda *args, **_kwargs: (
            response(f"crd/{database_controller.LEGACY_CRD}")
            if args[:2] == ("get", f"crd/{database_controller.LEGACY_CRD}")
            else response()
        ))
        with (
            patch.object(packaging, "require_absent_legacy_database_crd",
                         database_controller.require_absent_legacy_database_crd),
            self.assertRaisesRegex(RuntimeError, "clean-install-only cutover"),
        ):
            packaging.prepare_tenant_api_cutover(ROOT, CONFIG, client)
        self.assertEqual(len(client.calls), 1)
        self.assertFalse(any(args[0] in ("apply", "patch", "delete")
                             for args, _ in client.calls))

    def test_fresh_cutover_retains_tenant_create_lock_until_catalog_lifecycle(self):
        client = Client()
        with patch.object(packaging, "require_absent_legacy_database_crd",
                          database_controller.require_absent_legacy_database_crd):
            self.assertTrue(packaging.prepare_tenant_api_cutover(ROOT, CONFIG, client))
        applied = [
            json.loads(kwargs["input_text"])["metadata"]["name"]
            for args, kwargs in client.calls if args[:1] == ("apply",)
        ]
        self.assertIn(packaging.TENANT_CUTOVER_POLICY, applied)
        self.assertIn(packaging.CATALOG_CUTOVER_POLICY, applied)
        self.assertFalse(any(args[:1] == ("delete",) for args, _ in client.calls))

    def test_absent_legacy_crd_allows_catalog_preflight_and_lookup_errors_block(self):
        absent = Client()
        database_controller.require_absent_legacy_database_crd(absent)
        self.assertEqual(len(absent.calls), 1)
        for error in ("forbidden", "connection refused"):
            client = Client(lambda *_args, **_kwargs: response(code=1, error=error))
            with self.subTest(error=error), self.assertRaisesRegex(
                RuntimeError, "failed to inspect CRD",
            ):
                database_controller.require_absent_legacy_database_crd(client)
            self.assertEqual(len(client.calls), 1)

    def test_absent_legacy_crd_permits_catalog_install(self):
        policy, binding = active_catalog_lock()

        def handler(*args, **_kwargs):
            if args[:2] == ("get", f"crd/{database_controller.CATALOG_CRD}"):
                return response(self.catalog_crd())
            if args[:2] == ("get", f"validatingadmissionpolicy/{packaging.CATALOG_CUTOVER_POLICY}"):
                return response(policy)
            if args[:2] == ("get", f"validatingadmissionpolicybinding/{packaging.CATALOG_CUTOVER_POLICY}"):
                return response(binding)
            if args == ("get", "--raw=/apis/tenancy.cnpg-vcluster.io/v1alpha1"):
                return response({"resources": [{
                    "name": "tenantdatabasecatalogs", "kind": "TenantDatabaseCatalog",
                    "namespaced": True,
                }]})
            if args[:2] == ("create", "--dry-run=server"):
                return response(code=1, error=(
                    f"{packaging.CATALOG_CUTOVER_POLICY}: "
                    "TenantDatabaseCatalog creation is locked during API cutover"
                ))
            return response()

        client = Client(handler)
        with patch.object(packaging, "require_absent_legacy_database_crd",
                          database_controller.require_absent_legacy_database_crd):
            packaging.install_database_catalog(ROOT, CONFIG, client)
        self.assertEqual(
            client.calls[0][0][:2],
            ("get", f"crd/{database_controller.LEGACY_CRD}"),
        )
        self.assertTrue(any(
            args[:1] == ("apply",)
            and "tenancy.cnpg-vcluster.io_tenantdatabasecatalogs.yaml" in str(args)
            for args, _ in client.calls
        ))
        self.assertFalse(any(
            args[:2] == ("delete", f"crd/{database_controller.LEGACY_CRD}")
            for args, _ in client.calls
        ))

    def test_clean_install_no_legacy_mutation_in_tracked_installer_sources(self):
        sources = [
            ROOT / "scripts/lib/database_controller.py",
            ROOT / "scripts/lib/controller.py",
            ROOT / "scripts/lib/azure/foundation.py",
        ]
        for source in sources:
            with self.subTest(source=source):
                text = source.read_text()
                self.assertNotIn("retire_legacy_database_api", text)
                self.assertNotIn("fence_legacy_database_api", text)
        self.assertFalse(
            (ROOT / "database-controller/config/crd/bases/"
             "tenancy.cnpg-vcluster.io_tenantdatabases.yaml").exists()
        )

    def test_catalog_inventory_never_treats_accepted_entries_as_empty(self):
        def handler(*args, **_kwargs):
            if args[:2] == ("get", f"crd/{database_controller.CATALOG_CRD}"):
                return response("crd/" + database_controller.CATALOG_CRD)
            if args == ("get", "--raw=/apis/tenancy.cnpg-vcluster.io/v1alpha1/tenantdatabasecatalogs"):
                return response({
                    "apiVersion": "tenancy.cnpg-vcluster.io/v1alpha1",
                    "kind": "TenantDatabaseCatalogList",
                    "metadata": {"continue": ""},
                    "items": [{"metadata": {"name": "accepted"}}],
                })
            return response()

        with self.assertRaisesRegex(RuntimeError, "retained TenantDatabaseCatalogs"):
            database_controller.inspect_catalog_inventory(Client(handler))
        with self.assertRaisesRegex(RuntimeError, "inventory is invalid"):
            database_controller.inspect_catalog_inventory(Client(
                lambda *args, **kwargs: (
                    response({
                        "apiVersion": "tenancy.cnpg-vcluster.io/v1alpha1",
                        "kind": "TenantDatabaseCatalogList",
                        "metadata": {"continue": "more"},
                        "items": [],
                    }) if args[:2] == (
                        "get", "--raw=/apis/tenancy.cnpg-vcluster.io/v1alpha1/tenantdatabasecatalogs"
                    ) else handler(*args, **kwargs)
                )
            ))


class CurrentControllerPackagingTests(unittest.TestCase):
    def setUp(self):
        cutover = patch.object(packaging, "TENANT_API_CUTOVER_READY", True)
        cutover.start()
        self.addCleanup(cutover.stop)
        for target, value in (
            ("install_database_catalog", None),
            ("build_database_controller_image", "database:image"),
            ("install_database_controller", None),
        ):
            mocked = patch.object(packaging, target, return_value=value)
            mocked.start()
            self.addCleanup(mocked.stop)

    @staticmethod
    def foundation(image="rust:image"):
        raw = {"schema": 3, "controllerImage": image}
        return {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "tenant-foundation", "namespace": "tenant-system"},
            "data": {
                "foundation.json": json.dumps(raw, sort_keys=True),
                "foundation.sha256": packaging._foundation_checksum(raw),
            },
        }

    def test_catalog_install_failure_keeps_tenant_creation_fenced(self):
        client = Client()
        with (
            patch.object(packaging, "build_controller_image", return_value="rust:image"),
            patch.object(packaging, "_foundation_payload", return_value=self.foundation()),
            patch.object(packaging, "run"),
            patch.object(packaging, "prepare_tenant_api_cutover", return_value=True),
            patch.object(packaging, "verify_controller_crd"),
            patch.object(packaging, "install_database_catalog",
                         side_effect=RuntimeError("catalog fence is not effective")),
            patch.object(packaging, "remove_tenant_cutover_lock") as unlock,
            self.assertRaisesRegex(RuntimeError, "catalog fence is not effective"),
        ):
            packaging.reconcile_controller(Path("."), CONFIG, client, {}, Mock(), None)
        unlock.assert_not_called()

    def test_database_controller_rollout_failure_keeps_tenant_creation_fenced(self):
        client = Client()
        with (
            patch.object(packaging, "build_controller_image", return_value="rust:image"),
            patch.object(packaging, "_foundation_payload", return_value=self.foundation()),
            patch.object(packaging, "run"),
            patch.object(packaging, "prepare_tenant_api_cutover", return_value=True),
            patch.object(packaging, "verify_controller_crd"),
            patch.object(packaging, "install_database_controller",
                         side_effect=RuntimeError("database rollout unavailable")),
            patch.object(packaging, "remove_tenant_cutover_lock") as unlock,
            self.assertRaisesRegex(RuntimeError, "database rollout unavailable"),
        ):
            packaging.reconcile_controller(Path("."), CONFIG, client, {}, Mock(), None)
        unlock.assert_not_called()

    def test_crd_requires_exact_served_and_stored_version_and_status(self):
        valid = {
            "spec": {"versions": [{
                "name": "v1alpha4", "served": True, "storage": True, "subresources": {"status": {}},
            }]},
            "status": {"storedVersions": ["v1alpha4"]},
        }
        packaging.verify_controller_crd(Client(lambda *_a, **_k: response(valid)))
        for change in (
            lambda crd: crd["status"].update(storedVersions=["v1alpha3", "v1alpha4"]),
            lambda crd: crd["status"].update(storedVersions=[]),
            lambda crd: crd["spec"]["versions"].append({"name": "v1alpha1"}),
            lambda crd: crd["spec"]["versions"][0].update(served=False),
            lambda crd: crd["spec"]["versions"][0].update(subresources={}),
            lambda crd: crd["spec"].update(conversion={"strategy": "Webhook"}),
        ):
            invalid = copy.deepcopy(valid)
            change(invalid)
            with self.subTest(change=change), self.assertRaisesRegex(RuntimeError, "only v1alpha4"):
                packaging.verify_controller_crd(Client(lambda *_a, **_k: response(invalid)))

    def test_pre_acceptance_failure_restores_previous_controller(self):
        desired = self.foundation()
        old = self.foundation("old:image")
        old_state = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": "tenant-controller-state",
                "namespace": "tenant-system",
                "resourceVersion": "1",
            },
            "data": {"configurationHash": "old-hash"},
        }
        old_deployment = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "tenant-controller", "namespace": "tenant-system"},
            "spec": {},
        }
        events = []

        current_state = copy.deepcopy(old_state)

        def handle(*args, **kwargs):
            if "get" in args and "configmap/tenant-foundation" in args:
                return response(old)
            if "get" in args and "configmap/tenant-controller-state" in args:
                return response(current_state)
            if "get" in args and "deployment/tenant-controller" in args:
                return response(old_deployment)
            if "patch" in args and "configmap/tenant-controller-state" in args:
                patch_value = json.loads(args[args.index("-p") + 1])
                current_state["data"].update(patch_value["data"])
                current_state["metadata"]["resourceVersion"] = "2"
                return response(current_state)
            if kwargs.get("input_text"):
                events.append(("apply", kwargs["input_text"]))
            return response()

        client = Client(handle)
        with (
            patch.object(packaging, "build_controller_image", return_value="rust:image"),
            patch.object(packaging, "prepare_tenant_api_cutover", return_value=False),
            patch.object(packaging, "_foundation_payload", return_value=desired),
            patch.object(packaging, "run"),
            patch.object(packaging, "verify_controller_crd"),
            patch.object(packaging, "stop_controller", side_effect=lambda *_a: events.append("stop")),
            patch.object(
                packaging,
                "require_clean_controller_state",
                side_effect=[None, RuntimeError("second inventory failed")],
            ),
            patch.object(
                packaging,
                "render_controller_manager",
                side_effect=lambda *_a, **_k: Path("manager.yaml"),
            ),
            self.assertRaisesRegex(RuntimeError, "second inventory failed"),
        ):
            packaging.reconcile_controller(Path("."), CONFIG, client, {}, Mock(), None)
        self.assertFalse(packaging.install_database_catalog.call_args.kwargs["cutover_locked"])
        self.assertIn("stop", events)
        self.assertTrue(
            any(
                "old:image" in item[1]
                for item in events
                if isinstance(item, tuple) and item[0] == "apply"
            )
        )

    def test_first_install_rollback_lock_handles_shutdown_failure(self):
        desired = self.foundation()
        state = None
        deleted = []

        def handle(*args, **kwargs):
            nonlocal state
            if "get" in args and "configmap/tenant-controller-state" in args:
                return response(state) if state is not None else response()
            if args[:2] == ("create", "-f"):
                if state is not None:
                    return response(code=1, error="AlreadyExists")
                state = json.loads(kwargs["input_text"])
                state["metadata"].update(
                    uid="rollback-state", resourceVersion="1"
                )
                return response(state)
            return response()

        def remove(_config, _client, _namespace, resource):
            nonlocal state
            deleted.append(resource)
            if resource == "configmap/tenant-controller-state":
                state = None

        client = Client(handle)
        with (
            patch.object(packaging, "build_controller_image", return_value="rust:image"),
            patch.object(packaging, "prepare_tenant_api_cutover", return_value=False),
            patch.object(packaging, "_foundation_payload", return_value=desired),
            patch.object(packaging, "run"),
            patch.object(packaging, "verify_controller_crd"),
            patch.object(
                packaging,
                "stop_controller",
                side_effect=[None, RuntimeError("Pod wait failed")],
            ),
            patch.object(
                packaging,
                "require_clean_controller_state",
                side_effect=[None, RuntimeError("second inventory failed")],
            ),
            patch.object(packaging, "delete_named", side_effect=remove),
            self.assertRaisesRegex(RuntimeError, "second inventory failed"),
        ):
            packaging.reconcile_controller(
                Path("."), CONFIG, client, {}, Mock(), None
            )
        self.assertIsNotNone(state)
        self.assertIn("rollbackToken", state["data"])
        self.assertNotIn("configmap/tenant-controller-state", deleted)
        self.assertNotEqual(
            client.kubectl(
                "create",
                "-f",
                "-",
                input_text=json.dumps({
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {"name": "tenant-controller-state"},
                    "data": {"configurationHash": desired["data"]["foundation.sha256"]},
                }),
                check=False,
            ).returncode,
            0,
        )

    def test_first_install_rollback_loses_to_concurrent_acceptance(self):
        desired = self.foundation()
        deleted = []

        def handle(*args, **kwargs):
            if "get" in args and "configmap/tenant-controller-state" in args:
                return response()
            if args[:2] == ("create", "-f"):
                return response(code=1, error="AlreadyExists")
            return response()

        with (
            patch.object(packaging, "build_controller_image", return_value="rust:image"),
            patch.object(packaging, "prepare_tenant_api_cutover", return_value=False),
            patch.object(packaging, "_foundation_payload", return_value=desired),
            patch.object(packaging, "run"),
            patch.object(packaging, "verify_controller_crd"),
            patch.object(packaging, "stop_controller"),
            patch.object(
                packaging,
                "require_clean_controller_state",
                side_effect=[None, RuntimeError("second inventory failed")],
            ),
            patch.object(
                packaging,
                "delete_named",
                side_effect=lambda *_args: deleted.append(_args[-1]),
            ),
            self.assertRaisesRegex(RuntimeError, "acceptance changed"),
        ):
            packaging.reconcile_controller(
                Path("."), CONFIG, Client(handle), {}, Mock(), None
            )
        self.assertEqual(deleted, [])

    def test_post_acceptance_failure_never_restores_previous_controller(self):
        desired = self.foundation()
        desired_hash = desired["data"]["foundation.sha256"]
        old_state = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": "tenant-controller-state",
                "namespace": "tenant-system",
                "resourceVersion": "1",
            },
            "data": {"configurationHash": "old-hash"},
        }
        for accepted_after, error in (
            (desired_hash, "candidate failed after acceptance"),
            ("third-hash", "acceptance changed"),
        ):
            state_reads = 0
            rollback_applies = []

            def handle(*args, **kwargs):
                nonlocal state_reads
                if "get" in args and "configmap/tenant-controller-state" in args:
                    state_reads += 1
                    if state_reads == 1:
                        return response(old_state)
                    return response({"data": {"configurationHash": accepted_after}})
                if "get" in args:
                    return response()
                if kwargs.get("input_text") and "rollback" in " ".join(args):
                    rollback_applies.append(kwargs["input_text"])
                return response()

            with (
                self.subTest(accepted_after=accepted_after),
                patch.object(packaging, "build_controller_image", return_value="rust:image"),
                patch.object(packaging, "prepare_tenant_api_cutover", return_value=False),
                patch.object(packaging, "_foundation_payload", return_value=desired),
                patch.object(packaging, "run"),
                patch.object(packaging, "verify_controller_crd"),
                patch.object(packaging, "stop_controller"),
                patch.object(packaging, "require_clean_controller_state"),
                patch.object(
                    packaging,
                    "render_controller_manager",
                    return_value=Path("manager.yaml"),
                ),
                patch.object(
                    packaging,
                    "verify_running_controller",
                    side_effect=RuntimeError("candidate failed after acceptance"),
                ),
                self.assertRaisesRegex(RuntimeError, error),
            ):
                packaging.reconcile_controller(
                    Path("."), CONFIG, Client(handle), {}, Mock(), None
                )
            self.assertEqual(rollback_applies, [])

    def test_uninstall_keeps_controller_alive_until_ordinary_tenant_delete_finishes(self):
        events = []
        client = Client(lambda *args, **kwargs: events.append(args) or response())
        with ExitStack() as stack:
            for name in ("stop_controller", "require_clean_controller_state", "delete_named"):
                stack.enter_context(patch.object(packaging, name, side_effect=lambda *a, _n=name: events.append(_n)))
            packaging.delete_controller(Path("nonexistent"), CONFIG, client)
        delete = next(item for item in events if isinstance(item, tuple) and "delete" in item)
        self.assertIn("--wait=true", delete)
        self.assertLess(events.index(delete), events.index("stop_controller"))
        self.assertLess(events.index("stop_controller"), events.index("require_clean_controller_state"))

    def test_uninstall_failed_tenant_deletion_never_stops_manager_or_removes_crd(self):
        client = Client(lambda *args, **kwargs: response(code=1, error="finalizer blocked") if "delete" in args else response())
        with (
            patch.object(packaging, "stop_controller") as stop,
            self.assertRaisesRegex(RuntimeError, "finalizer blocked"),
        ):
            packaging.delete_controller(Path("nonexistent"), CONFIG, client)
        stop.assert_not_called()

    def test_scratch_probe_uses_in_cluster_dns_and_always_cleans_job(self):
        client = Client()
        with patch.object(packaging, "delete_named") as delete:
            packaging.verify_controller_image(CONFIG, client, "rust:image")
        job = json.loads(client.calls[0][1]["input_text"])
        container = job["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container["args"], ["--probe-in-cluster"])
        self.assertEqual(container["image"], "rust:image")
        self.assertEqual(container["env"][0]["value"], "kubernetes.default.svc")
        delete.assert_called_once()

    def test_api_gate_checks_cel_unknown_fields_and_status_using_server_dry_runs(self):
        spec = {
            "kubernetesVersion": "1.36.4",
            "workers": 1,
            "provider": {"type": "local"},
        }

        def handle(*args, **kwargs):
            if "get" in args:
                return response(code=1, error="NotFound")
            if "delete" in args:
                return response()
            if "create" in args:
                value = json.loads(kwargs["input_text"])
                incoming = value["spec"]
                provider = incoming["provider"]
                if value["metadata"]["name"] == "invalid.name":
                    return response(code=1, error="Invalid: Tenant name")
                if incoming["workers"] == 0:
                    return response(code=1, error="Invalid workers")
                if "databases" in provider and "--validate=strict" in args:
                    return response(code=1, error="unknown field")
                if incoming["kubernetesVersion"] == "bad":
                    return response(code=1, error="Invalid kubernetesVersion")
                if provider["type"] == "azure":
                    if provider != {"type": "azure"}:
                        return response(code=1, error="unknown field")
                    return response({**value, "spec": incoming})
                if "unexpected" in incoming and "--validate=strict" in args:
                    return response(code=1, error="unknown field")
                value.pop("status", None)
                value["spec"] = spec
                return response(value, error="unknown field" if "--validate=warn" in args else "")
            patch_data = json.loads(args[args.index("-p") + 1])
            if "--subresource=status" in args:
                return response({"spec": spec, "status": patch_data["status"]})
            if "spec" in patch_data:
                return response(code=1, error="Tenant spec is immutable")
            return response({"spec": spec})

        client = Client(handle)
        packaging.verify_controller_api(CONFIG, client)
        creates = [args for args, _ in client.calls if "create" in args]
        self.assertEqual(len([args for args in creates if "--dry-run=server" not in args]), 1)
        for mode in ("warn", "ignore", "strict"):
            self.assertTrue(any(f"--validate={mode}" in args for args in creates))
        azure_dry_runs = [
            json.loads(kwargs["input_text"])["spec"]["provider"]
            for args, kwargs in client.calls
            if "create" in args
            and "--dry-run=server" in args
            and json.loads(kwargs["input_text"])["spec"]["provider"]["type"] == "azure"
        ]
        self.assertEqual(len(azure_dry_runs), 4)
        self.assertIn({"type": "azure"}, azure_dry_runs)
        self.assertTrue(any("databases" in provider for provider in azure_dry_runs))
        self.assertTrue(any("podCIDR" in provider for provider in azure_dry_runs))
        self.assertTrue(any("serviceCIDR" in provider for provider in azure_dry_runs))
        patches = [args for args, _ in client.calls if "patch" in args]
        self.assertEqual(len(patches), 5)
        self.assertTrue(all("--dry-run=server" in args for args in patches))
        self.assertTrue(any("--subresource=status" in args for args in patches))
        self.assertTrue(any("delete" in args and "--wait=true" in args for args, _ in client.calls))

    def test_api_gate_does_not_accept_transport_errors_as_expected_validation(self):
        client = Client(lambda *_a, **_k: response(code=1, error="Forbidden"))
        with self.assertRaisesRegex(RuntimeError, "dry-run failed"):
            packaging.verify_controller_api(CONFIG, client)
        self.assertFalse(any("delete" in args for args, _ in client.calls))

    def test_deployment_pod_activation_image_and_live_health_are_verified(self):
        manager = {
            "name": "manager", "image": "rust:image",
            "args": [
                "--leader-elect=true", "--activation-token=token-a",
                "--controller-image=rust:image",
            ],
        }
        deployment = {
            "metadata": {"generation": 3},
            "spec": {
                "replicas": 1, "strategy": {"type": "Recreate"},
                "template": {"spec": {"containers": [copy.deepcopy(manager)]}},
            },
            "status": {"observedGeneration": 3, "readyReplicas": 1, "updatedReplicas": 1},
        }
        pods = {"items": [{
            "metadata": {"name": "manager-one"},
            "spec": {"containers": [copy.deepcopy(manager)]},
            "status": {"conditions": [{"type": "Ready", "status": "True"}]},
        }]}

        def handle(*args, **kwargs):
            if "deployment/tenant-controller" in args:
                return response(deployment)
            if "pods" in args:
                return response(pods)
            return response("ok")

        client = Client(handle)
        packaging.verify_running_controller(client, "rust:image", activation_token="token-a")
        health = [args for args, _ in client.calls if "--raw" in args]
        self.assertEqual(len(health), 2)
        self.assertTrue(health[0][-1].endswith("/proxy/healthz"))
        self.assertTrue(health[1][-1].endswith("/proxy/readyz"))
        manager = pods["items"][0]["spec"]["containers"][0]
        manager["image"] = "old:image"
        with self.assertRaisesRegex(RuntimeError, "image or runtime"):
            packaging.verify_running_controller(
                client, "rust:image", activation_token="token-a"
            )
