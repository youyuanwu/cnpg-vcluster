from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import Mock, patch

from scripts.lib import controller as packaging
from scripts.lib.controller_foundation import foundation_payload


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
            if document.get("metadata", {}).get("name") == packaging.DATABASE_ACTIVATION_POLICY:
                self.applied.add(
                    document["kind"].lower() + "/" + packaging.DATABASE_ACTIVATION_POLICY
                )
        if args and args[0] == "get" and args[1] in self.applied and not result.stdout:
            return response(args[1])
        return result

    def json(self, *args):
        return json.loads(self.kubectl(*args, "-o", "json").stdout)


class PackagingTests(unittest.TestCase):
    def test_database_activation_lock_is_separate_and_effective(self):
        policy, binding = packaging.database_activation_lock_documents()
        self.assertEqual(policy["metadata"]["name"], packaging.DATABASE_ACTIVATION_POLICY)
        self.assertEqual(policy["spec"]["failurePolicy"], "Fail")
        self.assertEqual(policy["spec"]["matchConstraints"]["resourceRules"][0], {
            "apiGroups": ["tenancy.cnpg-vcluster.io"],
            "apiVersions": ["v1alpha1"],
            "operations": ["CREATE"],
            "resources": ["tenantdatabases"],
            "scope": "Namespaced",
        })
        self.assertEqual(binding["spec"]["validationActions"], ["Deny"])
        calls = []

        def reject(*args, **kwargs):
            calls.append((args, kwargs))
            if args[:2] == ("get", f"validatingadmissionpolicy/{packaging.DATABASE_ACTIVATION_POLICY}"):
                return response(policy)
            if args[:2] == ("get", f"validatingadmissionpolicybinding/{packaging.DATABASE_ACTIVATION_POLICY}"):
                return response(binding)
            if args[:2] == ("create", "--dry-run=server"):
                return response(code=1, error="TenantDatabase creation is locked until both providers are ready")
            return response()

        client = Client(reject)
        packaging.ensure_database_activation_lock(client)
        packaging.verify_database_activation_lock(client, namespace="tenant-system")
        self.assertEqual(len([args for args, _ in calls if args[0] == "create"]), 5)
        def webhook_first(*args, **kwargs):
            result = reject(*args, **kwargs)
            if args[:2] == ("create", "--dry-run=server"):
                return response(code=1, error="Tenant identity is unavailable or not Ready")
            return result
        with self.assertRaisesRegex(RuntimeError, "not effective"):
            packaging.verify_database_activation_lock(
                Client(webhook_first), namespace="tenant-system",
            )
        with self.assertRaisesRegex(RuntimeError, "not effective"):
            packaging.verify_database_activation_lock(
                Client(lambda *args, **kwargs: (
                    reject(*args, **kwargs)
                    if args[:1] == ("get",) else response()
                )), namespace="tenant-system",
            )
        invalid_policy = copy.deepcopy(policy)
        invalid_policy["spec"]["validations"][0]["expression"] = "true"
        with self.assertRaisesRegex(RuntimeError, "contract is malformed"):
            packaging.verify_database_activation_lock(
                Client(lambda *args, **kwargs: (
                    response(invalid_policy)
                    if args[:2] == ("get", f"validatingadmissionpolicy/{packaging.DATABASE_ACTIVATION_POLICY}")
                    else reject(*args, **kwargs)
                )), namespace="tenant-system",
            )
        excluded_policy = copy.deepcopy(policy)
        excluded_policy["spec"]["matchConstraints"]["excludeResourceRules"] = [{
            "apiGroups": ["tenancy.cnpg-vcluster.io"],
            "apiVersions": ["v1alpha1"],
            "operations": ["CREATE"],
            "resources": ["tenantdatabases"],
        }]
        with self.assertRaisesRegex(RuntimeError, "contract is malformed"):
            packaging.verify_database_activation_lock(
                Client(lambda *args, **kwargs: (
                    response(excluded_policy)
                    if args[:2] == ("get", f"validatingadmissionpolicy/{packaging.DATABASE_ACTIVATION_POLICY}")
                    else webhook_first(*args, **kwargs)
                )), namespace="tenant-system",
            )
        selected_policy = copy.deepcopy(policy)
        selected_policy["spec"]["matchConstraints"]["namespaceSelector"] = {
            "matchLabels": {"tenant-database-gates": "excluded"}
        }
        with self.assertRaisesRegex(RuntimeError, "contract is malformed"):
            packaging.verify_database_activation_lock(
                Client(lambda *args, **kwargs: (
                    response(selected_policy)
                    if args[:2] == ("get", f"validatingadmissionpolicy/{packaging.DATABASE_ACTIVATION_POLICY}")
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

    def test_cutover_crd_render_failure_retains_database_activation_lock(self):
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
            packaging.DATABASE_ACTIVATION_POLICY in kwargs.get("input_text", "")
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

    def setUp(self):
        (ROOT / ".runtime").mkdir(exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=ROOT / ".runtime")
        self.addCleanup(self.directory.cleanup)
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


class CurrentControllerPackagingTests(unittest.TestCase):
    def setUp(self):
        cutover = patch.object(packaging, "TENANT_API_CUTOVER_READY", True)
        cutover.start()
        self.addCleanup(cutover.stop)
        for target, value in (
            ("database_admission_image", "admission:image"),
            ("install_database_admission", None),
        ):
            mocked = patch.object(packaging, target, return_value=value)
            mocked.start()
            self.addCleanup(mocked.stop)
        from scripts.lib import database_controller
        build = patch.object(database_controller, "build_admission_image",
                             return_value="admission:image")
        build.start()
        self.addCleanup(build.stop)

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
