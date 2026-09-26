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
from scripts.lib import controller_cutover as cutover
from scripts.lib.controller_foundation import foundation_payload


ROOT = Path(__file__).resolve().parents[1]
CONFIG = {
    "COMMAND_TIMEOUT": "1s", "CONDITION_TIMEOUT": "1s", "DELETE_TIMEOUT": "1s",
    "KUBERNETES_VERSION": "v1.36.4", "KIND_CLUSTER_NAME": "management",
    "OWNERSHIP_LABEL": "example.io/owned", "LAB_PREFIX": "lab",
}


def response(value="", code=0, error=""):
    return CompletedProcess([], code, json.dumps(value) if isinstance(value, dict) else value, error)


class Client:
    def __init__(self, handler=None):
        self.calls = []
        self.handler = handler or (lambda *_args, **_kwargs: response())

    def kubectl(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        result = self.handler(*args, **kwargs)
        if kwargs.get("check", True) and result.returncode:
            raise RuntimeError(result.stderr)
        return result

    def json(self, *args):
        return json.loads(self.kubectl(*args, "-o", "json").stdout)


class PackagingTests(unittest.TestCase):
    def setUp(self):
        (ROOT / ".runtime").mkdir(exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=ROOT / ".runtime")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        packaging.cargo_environment(self.root)

    def toolchain(self, *_args):
        return ("/installed/cargo", packaging.cargo_environment(self.root), "rustc 1.96\ncargo 1.96")

    def test_environment_is_local_offline_and_not_global_static_flags(self):
        with patch.dict(os.environ, {
            "CARGO_HOME": "/foreign", "CARGO_TARGET_DIR": "/foreign",
            "RUSTFLAGS": "-C target-feature=+crt-static", "RUSTC_WRAPPER": "untrusted",
            "CARGO_ENCODED_RUSTFLAGS": "foreign", "CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_RUSTFLAGS": "foreign",
            "GOCACHE": "/foreign", "GOMODCACHE": "/foreign",
            "KUBEBUILDER_ASSETS": "/foreign",
        }):
            env = packaging.cargo_environment(self.root)
        self.assertEqual(env["CARGO_HOME"], str(self.root / ".tools/cargo-home"))
        self.assertEqual(env["CARGO_TARGET_DIR"], str(self.root / ".tools/cargo-target"))
        self.assertEqual(env["CARGO_NET_OFFLINE"], "true")
        self.assertEqual(env["RUSTUP_AUTO_INSTALL"], "0")
        self.assertNotIn("RUSTFLAGS", env)
        self.assertNotIn("CARGO_ENCODED_RUSTFLAGS", env)
        self.assertNotIn("RUSTC_WRAPPER", env)
        for legacy in ("GOCACHE", "GOMODCACHE", "KUBEBUILDER_ASSETS"):
            self.assertNotIn(legacy, env)
        self.assertEqual(Path(env["TMPDIR"]), self.root / ".tools" / "cargo-work")

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
                    cargo, env, identity = packaging.rust_toolchain(self.root)
                    self.assertEqual(cargo, "/installed/cargo")
                    self.assertEqual(env["RUSTC"], "/installed/rustc")
                    self.assertIn(version, identity)
                else:
                    with self.assertRaisesRegex(RuntimeError, ">= 1.89"):
                        packaging.rust_toolchain(self.root)
                self.assertEqual(len(run.call_args_list), 2)
                self.assertTrue(all(call.args[0][1:] == ["--version", "--verbose"]
                                    for call in run.call_args_list))
        with patch.object(packaging.shutil, "which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "no toolchain is downloaded"):
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
                self.assertEqual(run.call_args.kwargs["env"]["CARGO_NET_OFFLINE"], str(offline == "1").lower())

    def test_empty_target_offline_static_final_manager_build(self):
        target = self.root / ".tools/cargo-target/offline-verification"
        target.mkdir(parents=True)
        (target / "stale").touch()

        def build(command, **kwargs):
            self.assertFalse((target / "stale").exists())
            self.assertEqual(list(target.iterdir()), [])
            self.assertEqual(command, [
                "/installed/cargo", "rustc", "--locked", "--offline", "--release",
                "--bin", "manager", "--", "-C", "target-feature=+crt-static",
            ])
            self.assertEqual(kwargs["env"]["CARGO_NET_OFFLINE"], "true")
            self.assertNotIn("RUSTFLAGS", kwargs["env"])
            (target / "release").mkdir()
            (target / "release/manager").write_bytes(b"\x7fELFstatic")
            return response()

        with (
            patch.object(packaging, "fetch_controller_dependencies") as fetch,
            patch.object(packaging, "rust_toolchain", side_effect=self.toolchain),
            patch.object(packaging, "run", side_effect=build),
            patch.object(packaging, "verify_static_manager") as verify,
        ):
            output = packaging.build_controller_binary(self.root, CONFIG)
        fetch.assert_called_once_with(self.root, CONFIG)
        verify.assert_called_once_with(target / "release/manager")
        self.assertEqual(output.read_bytes(), b"\x7fELFstatic")
        self.assertEqual(output.stat().st_mode & 0o777, 0o700)

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
            "controller/Cargo.toml": "[package]", "controller/Cargo.lock": "lock",
            "controller/src/lib.rs": "source", "controller/Dockerfile": "FROM scratch",
            "controller/config/manager/manager.yaml.tpl": "manager",
            "controller/config/crd/bases/tenant.yaml": "crd",
            "controller/config/rbac/role.yaml": "role",
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
        with patch.object(packaging, "rust_toolchain", return_value=("cargo", {}, "different compiler")):
            self.assertNotEqual(original, packaging.controller_source_digest(self.root, CONFIG))

    def test_foundation_wires_the_schema_three_producer(self):
        self.assertIs(packaging._foundation_payload, foundation_payload)

    def test_manager_template_has_no_webhook_or_tls_and_single_recreate(self):
        text = (ROOT / "controller/config/manager/manager.yaml.tpl").read_text()
        for absent in ("webhook", "tls", "9443", "secretName"):
            self.assertNotIn(absent, text)
        for present in ("replicas: 1", "type: Recreate", "--leader-elect=true",
                        "/healthz", "/readyz", "/var/run/docker.sock"):
            self.assertIn(present, text)
        dockerfile = (ROOT / "controller/Dockerfile").read_text()
        self.assertIn("FROM scratch", dockerfile)
        self.assertIn("COPY assets /assets", dockerfile)


class CutoverPackagingTests(unittest.TestCase):
    @staticmethod
    def foundation(*, generation="generation-one", image="rust:image", enabled=True):
        raw = {
            "schema": 3, "cache": {"generation": generation},
            "controllerImage": image, "mutationEnabled": enabled,
        }
        return {"data": {
            "foundation.json": json.dumps(raw, sort_keys=True),
            "foundation.sha256": packaging._foundation_checksum(raw),
        }}

    def test_named_legacy_cleanup_does_not_need_manifest_files_and_waits(self):
        def handle(*args, **kwargs):
            return response(code=1, error="NotFound") if "get" in args else response()

        client = Client(handle)
        with patch.object(Path, "unlink") as unlink:
            cutover.delete_legacy_controller(Path("not-a-source-tree"), CONFIG, client)
        unlink.assert_called_once_with(missing_ok=True)
        deletes = [args for args, _ in client.calls if "delete" in args]
        self.assertEqual(len(deletes), 8)
        for args in deletes:
            self.assertNotIn("-f", args)
            self.assertIn("--wait=true", args)
            self.assertIn("--timeout=1s", args)
        self.assertEqual(deletes[-1][1], "crd/tenants.tenancy.cnpg-vcluster.io")
        for namespace, resource in cutover.LEGACY_WEBHOOK_RESOURCES:
            self.assertTrue(any(resource in args and "get" in args for args, _ in client.calls))

    def test_absence_is_not_assumed_on_forbidden_or_still_present(self):
        for result in (response(code=1, error="Forbidden"), response("service/present")):
            with self.subTest(result=result), self.assertRaisesRegex(RuntimeError, "prove"):
                cutover.verify_absent(Client(lambda *_a, **_k: result), "tenant-system", "service/old")

    def test_cutover_still_waits_for_orphan_pods_when_deployment_absent(self):
        client = Client(lambda *args, **kwargs: (
            response({"items": []}) if "pods" in args else response(code=1, error="NotFound")
        ))
        packaging.stop_controller_for_cutover(CONFIG, client)
        self.assertTrue(any("pods" in args for args, _ in client.calls))
        self.assertFalse(any("scale" in args for args, _ in client.calls))

    def test_crd_requires_exact_served_and_stored_version_and_status(self):
        valid = {
            "spec": {"versions": [{
                "name": "v1alpha2", "served": True, "storage": True, "subresources": {"status": {}},
            }]},
            "status": {"storedVersions": ["v1alpha2"]},
        }
        packaging.verify_controller_crd(Client(lambda *_a, **_k: response(valid)))
        for change in (
            lambda crd: crd["status"].update(storedVersions=["v1alpha1", "v1alpha2"]),
            lambda crd: crd["status"].update(storedVersions=[]),
            lambda crd: crd["spec"]["versions"].append({"name": "v1alpha1"}),
            lambda crd: crd["spec"]["versions"][0].update(served=False),
            lambda crd: crd["spec"]["versions"][0].update(subresources={}),
            lambda crd: crd["spec"].update(conversion={"strategy": "Webhook"}),
        ):
            invalid = copy.deepcopy(valid)
            change(invalid)
            with self.subTest(change=change), self.assertRaisesRegex(RuntimeError, "only v1alpha2"):
                packaging.verify_controller_crd(Client(lambda *_a, **_k: response(invalid)))

    def test_clean_inventory_rejects_allocation_leases_even_without_labels(self):
        def handle(*args, **kwargs):
            if "configmap/tenant-endpoint-allocations" in args:
                return response(code=1, error="NotFound")
            if "leases.coordination.k8s.io" in args:
                return response({"items": [{"metadata": {"name": "tenant-slot-replaced"}}]})
            if "clusters.cluster.x-k8s.io" in args:
                return response({"items": []})
            return response()

        with self.assertRaisesRegex(RuntimeError, "allocation Lease residue"):
            cutover.require_clean_controller_cutover(Path("nonexistent"), CONFIG, Client(handle))

    def test_clean_inventory_rejects_malformed_old_ledger(self):
        def handle(*args, **kwargs):
            if "configmap/tenant-endpoint-allocations" in args:
                return response({"data": {"allocations.json": "{}"}})
            if "clusters.cluster.x-k8s.io" in args:
                return response({"items": []})
            return response()

        with self.assertRaisesRegex(RuntimeError, "ledger is malformed"):
            cutover.require_clean_controller_cutover(Path("nonexistent"), CONFIG, Client(handle))

    def reconcile_events(self, failed_gate=None):
        events = []
        client = Client(lambda *args, **kwargs: (
            events.append("publish" if kwargs.get("input_text") else ("apply:" + args[-1] if args[0] == "apply" else "kubectl"))
            or response()
        ))
        stack = ExitStack()
        self.addCleanup(stack.close)
        for name in (
            "stop_controller_for_cutover", "require_clean_controller_cutover",
            "delete_legacy_controller", "verify_legacy_webhook_absent",
            "verify_controller_crd", "verify_running_controller",
            "verify_controller_image", "verify_controller_api", "set_controller_mutation",
        ):
            def effect(*args, _name=name, **kwargs):
                events.append(_name)
                if _name == failed_gate:
                    raise RuntimeError("gate failed")
            stack.enter_context(patch.object(packaging, name, side_effect=effect))
        stack.enter_context(patch.object(packaging, "controller_lifecycle_epoch", return_value="old"))
        stack.enter_context(patch.object(packaging, "build_controller_image", return_value="rust:image"))
        stack.enter_context(patch.object(packaging, "run"))
        stack.enter_context(patch.object(packaging, "render_controller_manager", side_effect=lambda *a, **kw: (
            events.append("render:" + str(kw["mutation_enabled"])) or Path("manager.yaml")
        )))
        stack.enter_context(patch.object(packaging, "_foundation_payload", return_value=self.foundation()))
        return events, client

    def test_cutover_order_foundation_after_gates_mutation_last(self):
        events, client = self.reconcile_events()
        packaging.reconcile_controller(Path("."), CONFIG, client, {}, Mock(), None)
        ordered = [
            "require_clean_controller_cutover", "stop_controller_for_cutover",
            "delete_legacy_controller", "render:False", "verify_controller_crd",
            "verify_running_controller", "verify_controller_image", "verify_controller_api",
            "publish", "set_controller_mutation", "render:True",
        ]
        positions = [events.index(event) for event in ordered]
        self.assertEqual(positions, sorted(positions))
        applied = [event for event in events if event.startswith("apply:")]
        self.assertTrue(any("config/crd/bases" in event for event in applied))
        self.assertTrue(any("config/rbac/role.yaml" in event for event in applied))
        self.assertFalse(any("webhook" in event or "config/staged" in event for event in applied))

    def test_failed_gate_never_publishes_or_enables_mutation(self):
        for gate in ("require_clean_controller_cutover", "verify_controller_crd",
                     "verify_running_controller", "verify_controller_image", "verify_controller_api"):
            with self.subTest(gate=gate):
                events, client = self.reconcile_events(gate)
                with self.assertRaisesRegex(RuntimeError, "gate failed"):
                    packaging.reconcile_controller(Path("."), CONFIG, client, {}, Mock(), None)
                self.assertNotIn("publish", events)
                self.assertNotIn("set_controller_mutation", events)

    def test_exact_current_install_does_not_toggle_mutation_or_repeat_creating_api_probe(self):
        events, _ = self.reconcile_events()
        foundation = self.foundation()
        deployment = {"spec": {"template": {"spec": {"containers": [{
            "name": "manager", "image": "rust:image", "args": ["--mutation-enabled=true"],
        }]}}}}

        def handle(*args, **kwargs):
            if "configmap/tenant-foundation" in args:
                return response(foundation)
            if "get" in args:
                return response(deployment)
            return response()

        with (
            patch.object(packaging, "controller_lifecycle_epoch", return_value=packaging.CONTROLLER_LIFECYCLE_EPOCH),
            patch.object(packaging, "_foundation_payload", return_value=foundation),
            patch.object(packaging, "require_clean_controller_cutover", side_effect=AssertionError("unnecessary cutover")),
        ):
            packaging.reconcile_controller(Path("."), CONFIG, Client(handle), {}, Mock(), None)
        self.assertNotIn("render:False", events)
        self.assertNotIn("set_controller_mutation", events)
        self.assertNotIn("verify_controller_api", events)
        self.assertIn("verify_controller_crd", events)
        self.assertIn("verify_running_controller", events)
        self.assertIn("verify_controller_image", events)

    def test_mutable_only_updates_do_not_require_clean_cutover(self):
        for change in ("image", "foundation-image", "foundation-mutation", "disabled"):
            with self.subTest(change=change):
                events, _ = self.reconcile_events()
                desired = self.foundation()
                current = copy.deepcopy(desired)
                manager = {"name": "manager", "image": "rust:image", "args": ["--mutation-enabled=true"]}
                if change == "image":
                    manager["image"] = "old:image"
                elif change == "foundation-image":
                    current = self.foundation(image="old:image")
                elif change == "foundation-mutation":
                    current = self.foundation(enabled=False)
                else:
                    manager["args"] = ["--mutation-enabled=false"]
                deployment = {"spec": {"template": {"spec": {"containers": [manager]}}}}
                client = Client(lambda *args, **kwargs: response(
                    current if "configmap/tenant-foundation" in args else deployment
                ) if "get" in args else response())
                with (
                    patch.object(packaging, "controller_lifecycle_epoch", return_value=packaging.CONTROLLER_LIFECYCLE_EPOCH),
                    patch.object(packaging, "_foundation_payload", return_value=desired),
                    patch.object(packaging, "require_clean_controller_cutover", side_effect=AssertionError("unnecessary cutover")),
                ):
                    packaging.reconcile_controller(Path("."), CONFIG, client, {}, Mock(), None)
                self.assertIn("render:False", events)
                self.assertIn("verify_controller_api", events)
                self.assertIn("set_controller_mutation", events)

    def test_immutable_or_unverified_foundation_with_active_tenant_cannot_mutate(self):
        desired = self.foundation()
        malformed_mutable = copy.deepcopy(desired)
        raw = json.loads(malformed_mutable["data"]["foundation.json"])
        raw["mutationEnabled"] = "true"
        malformed_mutable["data"]["foundation.json"] = json.dumps(raw)
        for current in (
            self.foundation(generation="generation-two"),
            None,
            {"data": {"foundation.json": "{", "foundation.sha256": "invalid"}},
            {**desired, "data": {**desired["data"], "foundation.sha256": "0" * 64}},
            malformed_mutable,
        ):
            with self.subTest(current=current), tempfile.TemporaryDirectory() as temporary:
                events, _ = self.reconcile_events()

                def handle(*args, **kwargs):
                    if "configmap/tenant-foundation" in args and "get" in args:
                        return response(current) if current is not None else response()
                    if "tenants.tenancy.cnpg-vcluster.io" in args:
                        return response("tenant.tenancy.cnpg-vcluster.io/active\n")
                    return response()

                client = Client(handle)
                with (
                    patch.object(packaging, "controller_lifecycle_epoch", return_value=packaging.CONTROLLER_LIFECYCLE_EPOCH),
                    patch.object(packaging, "_foundation_payload", return_value=desired),
                    patch.object(packaging, "require_clean_controller_cutover", side_effect=cutover.require_clean_controller_cutover),
                ):
                    with self.assertRaisesRegex(RuntimeError, "existing Tenant resources"):
                        packaging.reconcile_controller(Path(temporary), CONFIG, client, {}, Mock(), None)
                self.assertFalse(any(
                    "apply" in args or "patch" in args or "scale" in args
                    for args, _ in client.calls
                ))
                self.assertNotIn("render:False", events)
                self.assertNotIn("publish", events)

    def test_epoch_cutover_proves_clean_before_scaling_controller(self):
        events, _ = self.reconcile_events("require_clean_controller_cutover")
        with self.assertRaisesRegex(RuntimeError, "gate failed"):
            packaging.reconcile_controller(Path("."), CONFIG, Client(), {}, Mock(), None)
        self.assertNotIn("stop_controller_for_cutover", events)
        self.assertNotIn("publish", events)

    def test_same_epoch_immutable_change_requires_full_clean_inventory(self):
        events, _ = self.reconcile_events()

        def handle(*args, **kwargs):
            if "configmap/tenant-foundation" in args:
                return response(self.foundation(generation="old"))
            if "clusters.cluster.x-k8s.io" in args:
                return response({"items": [{"metadata": {"name": "orphan"}}]})
            return response()

        client = Client(handle)
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(packaging, "controller_lifecycle_epoch", return_value=packaging.CONTROLLER_LIFECYCLE_EPOCH),
            patch.object(packaging, "require_clean_controller_cutover", side_effect=cutover.require_clean_controller_cutover),
            self.assertRaisesRegex(RuntimeError, "existing CAPI Clusters"),
        ):
            packaging.reconcile_controller(Path(temporary), CONFIG, client, {}, Mock(), None)
        self.assertFalse(any("apply" in args or "scale" in args for args, _ in client.calls))
        self.assertNotIn("publish", events)

    def test_mutation_enable_and_disable_order_without_webhook(self):
        for enabled in (True, False):
            data = {"schema": 3, "mutationEnabled": not enabled, "controllerImage": "rust:image"}
            foundation = {"data": {
                "foundation.json": json.dumps(data),
                "foundation.sha256": packaging._foundation_checksum(data),
            }}
            deployment = {"spec": {"template": {"spec": {"containers": [{
                "name": "manager", "args": [f"--mutation-enabled={str(not enabled).lower()}"],
            }]}}}}

            def handle(*args, **kwargs):
                if "get" in args:
                    return response(foundation if "configmap/tenant-foundation" in args else deployment)
                return response()

            client = Client(handle)
            with patch.object(packaging, "verify_running_controller") as verify:
                packaging.set_controller_mutation(CONFIG, client, enabled=enabled)
            verify.assert_called_once_with(client, "rust:image", mutation_enabled=enabled)
            patches = [args for args, _ in client.calls if "patch" in args]
            self.assertIn("configmap/tenant-foundation", patches[0 if enabled else 1])
            self.assertIn("deployment/tenant-controller", patches[1 if enabled else 0])
            self.assertFalse(any("create" in args for args, _ in client.calls))

    def test_uninstall_keeps_controller_alive_until_ordinary_tenant_delete_finishes(self):
        events = []
        client = Client(lambda *args, **kwargs: events.append(args) or response())
        with ExitStack() as stack:
            for name in ("stop_controller_for_cutover", "require_clean_controller_cutover",
                         "delete_legacy_controller", "delete_named"):
                stack.enter_context(patch.object(packaging, name, side_effect=lambda *a, _n=name: events.append(_n)))
            packaging.delete_controller(Path("nonexistent"), CONFIG, client)
        delete = next(item for item in events if isinstance(item, tuple) and "delete" in item)
        self.assertIn("--wait=true", delete)
        self.assertLess(events.index(delete), events.index("stop_controller_for_cutover"))
        self.assertLess(events.index("require_clean_controller_cutover"), events.index("delete_legacy_controller"))

    def test_uninstall_failed_tenant_deletion_never_stops_manager_or_removes_crd(self):
        client = Client(lambda *args, **kwargs: response(code=1, error="finalizer blocked") if "delete" in args else response())
        with (
            patch.object(packaging, "stop_controller_for_cutover") as stop,
            patch.object(packaging, "delete_legacy_controller") as delete,
            self.assertRaisesRegex(RuntimeError, "finalizer blocked"),
        ):
            packaging.delete_controller(Path("nonexistent"), CONFIG, client)
        stop.assert_not_called()
        delete.assert_not_called()

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
        spec = {"kubernetesVersion": "1.36.4", "workers": 1, "databases": 1}

        def handle(*args, **kwargs):
            if "get" in args:
                return response(code=1, error="NotFound")
            if "delete" in args:
                return response()
            if "create" in args:
                value = json.loads(kwargs["input_text"])
                incoming = value["spec"]
                if value["metadata"]["name"] == "invalid.name":
                    return response(code=1, error="Invalid: Tenant name")
                for field, invalid in (("workers", 0), ("databases", 4), ("kubernetesVersion", "bad")):
                    if incoming[field] == invalid:
                        return response(code=1, error=f"Invalid {field}")
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

    def test_deployment_pod_epoch_image_and_live_health_are_verified(self):
        manager = {
            "name": "manager", "image": "rust:image",
            "args": [
                "--leader-elect=true", "--mutation-enabled=false",
                "--controller-image=rust:image",
                f"--lifecycle-epoch={packaging.CONTROLLER_LIFECYCLE_EPOCH}",
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
        packaging.verify_running_controller(client, "rust:image", mutation_enabled=False)
        health = [args for args, _ in client.calls if "--raw" in args]
        self.assertEqual(len(health), 2)
        self.assertTrue(health[0][-1].endswith("/proxy/healthz"))
        self.assertTrue(health[1][-1].endswith("/proxy/readyz"))
        manager = pods["items"][0]["spec"]["containers"][0]
        manager["image"] = "old:image"
        with self.assertRaisesRegex(RuntimeError, "image or runtime"):
            packaging.verify_running_controller(client, "rust:image", mutation_enabled=False)
