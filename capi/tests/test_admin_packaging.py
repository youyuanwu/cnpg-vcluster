from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from scripts.lib import admin as packaging


ROOT = Path(__file__).resolve().parents[1]
CONFIG = {
    "COMMAND_TIMEOUT": "1s",
    "TENANT_ADMIN_IMAGE_REPOSITORY": "cnpg-vcluster/tenant-admin",
    "TRUNK_VERSION": "0.21.14",
    "TRUNK_URL": (
        "https://github.com/trunk-rs/trunk/releases/download/v0.21.14/"
        "trunk-x86_64-unknown-linux-gnu.tar.gz"
    ),
    "TRUNK_SHA256": (
        "f2b4680cd239693a646a2795e4633c625328d7b2a044fbe749fa3a2fe9e7036b"
    ),
    "WASM_BINDGEN_VERSION": "0.2.129",
    "WASM_BINDGEN_URL": (
        "https://github.com/wasm-bindgen/wasm-bindgen/releases/download/"
        "0.2.129/wasm-bindgen-0.2.129-x86_64-unknown-linux-musl.tar.gz"
    ),
    "WASM_BINDGEN_SHA256": (
        "82d12bb940e2d4e72e0d5605387fc1b8ca179044e012b620f0ce4e7440e8320e"
    ),
}


def response(value: str = "", code: int = 0, error: str = "") -> CompletedProcess:
    return CompletedProcess([], code, value, error)


class AdminPackagingTests(unittest.TestCase):
    def setUp(self) -> None:
        (ROOT / ".runtime").mkdir(exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=ROOT / ".runtime")
        self.addCleanup(self.directory.cleanup)
        self.repository = Path(self.directory.name)
        self.root = self.repository / "capi"
        (self.root / "admin/server").mkdir(parents=True)
        self.root.chmod(0o700)
        (self.root / ".runtime").mkdir(mode=0o700)

    @staticmethod
    def toolchain(*_args):
        return ("/installed/cargo", "rustc 1.98.1\ncargo 1.98.1")

    def install_trunk(self, version: str = "trunk 0.21.14") -> Path:
        binary = self.root / ".tools/bin/trunk"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"trunk")
        binary.chmod(0o755)
        return binary

    def install_wasm_bindgen(self) -> Path:
        binary = self.root / ".tools/bin/wasm-bindgen"
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_bytes(b"wasm-bindgen")
        binary.chmod(0o755)
        return binary

    @staticmethod
    def tool_version(command, **_kwargs):
        name = Path(command[0]).name
        if name == "trunk":
            return response("trunk 0.21.14\n")
        if name == "wasm-bindgen":
            return response("wasm-bindgen 0.2.129\n")
        raise AssertionError(command)

    def write_web(self) -> Path:
        web = self.root / ".runtime/rendered/admin/web"
        packaging.ensure_private_dir(web)
        for name in (
            "index.html",
            "tenant-admin-web-0123456789abcdef.js",
            "tenant-admin-web-0123456789abcdef_bg.wasm",
            "style-fedcba9876543210.css",
        ):
            (web / name).write_bytes(b"fixture")
        return web

    def test_fetch_and_rust_gates_are_locked_offline_and_package_scoped(self) -> None:
        with (
            patch.object(packaging, "rust_toolchain", side_effect=self.toolchain),
            patch.object(packaging, "run") as run,
            patch.dict(os.environ, {"CAPI_OFFLINE_ENFORCED": "1"}),
        ):
            packaging.fetch_admin_dependencies(self.root, CONFIG)
        self.assertEqual(
            ["/installed/cargo", "fetch", "--locked", "--offline"],
            run.call_args.args[0],
        )
        self.assertEqual(self.root / "admin/server", run.call_args.kwargs["cwd"])

        with (
            patch.object(packaging, "fetch_admin_dependencies"),
            patch.object(packaging, "_cargo") as cargo,
        ):
            packaging.test_admin(self.root, CONFIG)
            packaging.vet_admin(self.root, CONFIG)
        test_command = cargo.call_args_list[0].args[2]
        clippy_command = cargo.call_args_list[2].args[2]
        for command in (test_command, clippy_command):
            self.assertIn("--locked", command)
            self.assertIn("--offline", command)
            for package in packaging.ADMIN_PACKAGES:
                self.assertIn(package, command)
        self.assertEqual(["fmt", "--all", "--check"], cargo.call_args_list[1].args[2])

    def test_generator_check_is_read_only_and_fail_closed(self) -> None:
        with patch.object(
            packaging.admin_resource_generator,
            "generate",
            return_value=True,
        ) as generate:
            packaging.generate_admin_resources(root=self.root, check=True)
        generate.assert_called_once_with(self.root, check=True)
        with patch.object(
            packaging.admin_resource_generator,
            "generate",
            return_value=False,
        ), self.assertRaisesRegex(RuntimeError, "stale"):
            packaging.generate_admin_resources(root=self.root, check=True)

    def test_trunk_identity_is_exact_and_tracks_binary(self) -> None:
        binary = self.install_trunk()
        with patch.object(packaging, "run", return_value=response("trunk 0.21.14\n")):
            path, identity = packaging.trunk_identity(self.root, CONFIG)
        self.assertEqual(binary, path)
        self.assertEqual("trunk 0.21.14", identity["version"])
        self.assertEqual(CONFIG["TRUNK_URL"], identity["url"])
        self.assertEqual(64, len(identity["binarySha256"]))
        with patch.object(packaging, "run", return_value=response("trunk 0.21.13\n")):
            with self.assertRaisesRegex(RuntimeError, "identity mismatch"):
                packaging.trunk_identity(self.root, CONFIG)

    def test_wasm_bindgen_identity_is_exact_and_tracks_binary(self) -> None:
        binary = self.install_wasm_bindgen()
        with patch.object(
            packaging,
            "run",
            return_value=response("wasm-bindgen 0.2.129\n"),
        ):
            path, identity = packaging.wasm_bindgen_identity(self.root, CONFIG)
        self.assertEqual(binary, path)
        self.assertEqual("wasm-bindgen 0.2.129", identity["version"])
        self.assertEqual(CONFIG["WASM_BINDGEN_URL"], identity["url"])

    def test_web_inventory_allows_only_index_js_wasm_and_css(self) -> None:
        web = self.write_web()
        outputs = packaging.validate_web_output(web)
        self.assertEqual(4, len(outputs))
        source_map = web / "tenant-admin-web-0123456789abcdef.js.map"
        source_map.write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "unexpected"):
            packaging.validate_web_output(web)
        source_map.unlink()
        (web / "style-fedcba9876543210.css").unlink()
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            packaging.validate_web_output(web)

    def test_static_server_build_uses_json_artifact_and_crt_static(self) -> None:
        binary = self.root / "target/release/tenant-admin-server"

        def build(command, **kwargs):
            self.assertEqual(
                ["/installed/cargo", *packaging.SERVER_BUILD_COMMAND[1:]],
                command,
            )
            self.assertEqual(self.root / "admin/server", kwargs["cwd"])
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"\x7fELFstatic")
            return response(
                json.dumps(
                    {
                        "reason": "compiler-artifact",
                        "target": {"name": "tenant-admin-server"},
                        "executable": str(binary),
                    }
                )
            )

        with (
            patch.object(packaging, "rust_toolchain", side_effect=self.toolchain),
            patch.object(packaging, "run", side_effect=build),
            patch.object(packaging, "verify_static_admin_server") as verify,
        ):
            output = packaging._build_admin_server(self.root, CONFIG)
        self.assertEqual(b"\x7fELFstatic", output.read_bytes())
        self.assertEqual(0o700, output.stat().st_mode & 0o777)
        self.assertEqual([binary, output], [call.args[0] for call in verify.call_args_list])

    def test_static_server_rejects_non_elf_interp_and_needed(self) -> None:
        binary = self.root / "tenant-admin"
        binary.write_bytes(b"not ELF")
        with self.assertRaisesRegex(RuntimeError, "not an ELF"):
            packaging.verify_static_admin_server(binary)
        binary.write_bytes(b"\x7fELF")
        for headers, dynamic in (("INTERP", ""), ("", "(NEEDED) libc.so.6")):
            with self.subTest(headers=headers, dynamic=dynamic), patch.object(
                packaging,
                "run",
                side_effect=[response(headers), response(dynamic)],
            ), self.assertRaisesRegex(RuntimeError, "must be static"):
                packaging.verify_static_admin_server(binary)

    def test_prebuilt_inputs_require_exact_owned_sibling_artifacts(self) -> None:
        artifact = self.root / ".tools/artifacts/commit/admin"
        server = artifact / "tenant-admin"
        web = artifact / "web"
        web.mkdir(parents=True)
        server.write_bytes(b"\x7fELFprebuilt")
        for name in (
            "index.html",
            "tenant-admin-web-0123456789abcdef.js",
            "tenant-admin-web-0123456789abcdef_bg.wasm",
            "style-fedcba9876543210.css",
        ):
            (web / name).write_bytes(b"fixture")
        environment = {
            packaging.PREBUILT_SERVER_ENV: str(server),
            packaging.PREBUILT_WEB_ENV: str(web),
        }
        with (
            patch.dict(os.environ, environment, clear=False),
            patch.object(packaging, "verify_static_admin_server") as verify,
        ):
            selected = packaging._prebuilt_admin_inputs(self.root)
        self.assertEqual((server.resolve(), web.resolve()), selected)
        verify.assert_called_once_with(server.resolve())

        with patch.dict(
            os.environ,
            {packaging.PREBUILT_SERVER_ENV: str(server)},
            clear=True,
        ), self.assertRaisesRegex(RuntimeError, "must be set together"):
            packaging._prebuilt_admin_inputs(self.root)

        outside = self.root / "outside"
        outside.mkdir()
        with patch.dict(
            os.environ,
            {
                packaging.PREBUILT_SERVER_ENV: str(server),
                packaging.PREBUILT_WEB_ENV: str(outside),
            },
            clear=False,
        ), self.assertRaisesRegex(RuntimeError, "below .tools/artifacts"):
            packaging._prebuilt_admin_inputs(self.root)

        extra = web / "unexpected.txt"
        extra.write_text("unexpected", encoding="utf-8")
        with (
            patch.dict(os.environ, environment, clear=False),
            patch.object(packaging, "verify_static_admin_server"),
            self.assertRaisesRegex(RuntimeError, "unexpected admin web output"),
        ):
            packaging._prebuilt_admin_inputs(self.root)
        extra.unlink()

        server.chmod(0o622)
        with patch.dict(
            os.environ,
            environment,
            clear=False,
        ), self.assertRaisesRegex(RuntimeError, "owned, non-writable"):
            packaging._prebuilt_admin_inputs(self.root)

    def test_build_materializes_prebuilt_inputs_without_build_tools(self) -> None:
        artifact = self.root / ".tools/artifacts/commit/admin"
        server = artifact / "tenant-admin"
        web = artifact / "web"
        web.mkdir(parents=True)
        server.write_bytes(b"\x7fELFprebuilt")
        for name in (
            "index.html",
            "tenant-admin-web-0123456789abcdef.js",
            "tenant-admin-web-0123456789abcdef_bg.wasm",
            "style-fedcba9876543210.css",
        ):
            (web / name).write_bytes(b"fixture")
        with (
            patch.dict(
                os.environ,
                {
                    packaging.PREBUILT_SERVER_ENV: str(server),
                    packaging.PREBUILT_WEB_ENV: str(web),
                },
                clear=False,
            ),
            patch.object(packaging, "_prepare_admin_build"),
            patch.object(packaging, "verify_static_admin_server"),
            patch.object(packaging, "_build_admin_server") as build_server,
            patch.object(packaging, "_build_admin_web") as build_web,
        ):
            output_server, output_web = packaging.build_admin(self.root, CONFIG)
        build_server.assert_not_called()
        build_web.assert_not_called()
        self.assertEqual(b"\x7fELFprebuilt", output_server.read_bytes())
        self.assertEqual(0o700, output_server.stat().st_mode & 0o777)
        self.assertEqual(
            sorted(path.name for path in web.iterdir()),
            sorted(path.name for path in output_web.iterdir()),
        )
        self.assertTrue(
            all(
                path.stat().st_mode & 0o777 == 0o600
                for path in output_web.iterdir()
            )
        )

    def test_reproducibility_check_compares_exact_server_and_web_inventory(
        self,
    ) -> None:
        first_server = self.root / "first/tenant-admin"
        first_web = self.root / "first/web"
        second_server = self.root / "second/tenant-admin"
        second_web = self.root / "second/web"
        for server, web in (
            (first_server, first_web),
            (second_server, second_web),
        ):
            server.parent.mkdir(parents=True)
            server.write_bytes(b"\x7fELFsame")
            web.mkdir()
            for name in (
                "index.html",
                "tenant-admin-web-0123456789abcdef.js",
                "tenant-admin-web-0123456789abcdef_bg.wasm",
                "style-fedcba9876543210.css",
            ):
                (web / name).write_bytes(name.encode())
        with (
            patch.object(
                packaging,
                "build_admin",
                side_effect=[
                    (first_server, first_web),
                    (second_server, second_web),
                ],
            ),
            patch.object(packaging, "verify_static_admin_server"),
        ):
            self.assertEqual(
                (second_server, second_web),
                packaging.verify_reproducible_admin_build(self.root, CONFIG),
            )
        (second_web / "index.html").write_bytes(b"changed")
        with (
            patch.object(
                packaging,
                "build_admin",
                side_effect=[
                    (first_server, first_web),
                    (second_server, second_web),
                ],
            ),
            patch.object(packaging, "verify_static_admin_server"),
            self.assertRaisesRegex(RuntimeError, "not reproducible"),
        ):
            packaging.verify_reproducible_admin_build(self.root, CONFIG)

    def test_source_digest_covers_admin_root_inputs_tools_and_commands(self) -> None:
        files = {
            "../Cargo.toml": "[workspace]",
            "../Cargo.lock": "lock",
            "../rust-toolchain.toml": "[toolchain]",
            "admin/Dockerfile": "FROM scratch",
            "admin/shared/src/lib.rs": "shared",
            "admin/server/src/main.rs": "server",
            "admin/web/src/main.rs": "web",
            "admin/web/Trunk.toml": "config",
            "admin/config/rbac/cluster-role.yaml": "generated",
            "scripts/generate_admin_resources.py": "generator",
        }
        for name, content in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        self.install_trunk()
        self.install_wasm_bindgen()
        with (
            patch.object(packaging, "rust_toolchain", side_effect=self.toolchain),
            patch.object(
                packaging,
                "run",
                side_effect=self.tool_version,
            ),
        ):
            original = packaging.admin_source_digest(self.root, CONFIG)
            for name, content in files.items():
                with self.subTest(name=name):
                    path = self.root / name
                    path.write_text(content + "changed", encoding="utf-8")
                    self.assertNotEqual(
                        original,
                        packaging.admin_source_digest(self.root, CONFIG),
                    )
                    path.write_text(content, encoding="utf-8")
            with patch.object(packaging, "WEB_BUILD_COMMAND", ("trunk", "changed")):
                self.assertNotEqual(
                    original,
                    packaging.admin_source_digest(self.root, CONFIG),
                )
        with (
            patch.object(
                packaging,
                "rust_toolchain",
                return_value=("cargo", "different compiler"),
            ),
            patch.object(
                packaging,
                "run",
                side_effect=self.tool_version,
            ),
        ):
            self.assertNotEqual(
                original,
                packaging.admin_source_digest(self.root, CONFIG),
            )

    def test_image_context_is_minimal_pull_free_and_always_cleaned(self) -> None:
        server = self.root / ".runtime/rendered/admin/tenant-admin"
        packaging.ensure_private_dir(server.parent)
        server.write_bytes(b"\x7fELFserver")
        web = self.write_web()
        dockerfile = self.root / "admin/Dockerfile"
        dockerfile.parent.mkdir(parents=True, exist_ok=True)
        dockerfile.write_text(
            "FROM scratch\n"
            "COPY --chown=65532:65532 tenant-admin /tenant-admin\n"
            "COPY --chown=65532:65532 web /web\n"
            "USER 65532:65532\nENTRYPOINT [\"/tenant-admin\"]\n",
            encoding="utf-8",
        )

        def build(command, **_kwargs):
            context = Path(command[-1])
            self.assertEqual(
                ["Dockerfile", "tenant-admin", "web"],
                sorted(path.name for path in context.iterdir()),
            )
            self.assertEqual("docker", command[0])
            self.assertIn("--pull=false", command)
            return response()

        with (
            patch.object(packaging, "build_admin", return_value=(server, web)),
            patch.object(packaging, "verify_static_admin_server"),
            patch.object(packaging, "admin_image", return_value="example/admin:tag"),
            patch.object(packaging, "run", side_effect=build),
        ):
            self.assertEqual(
                "example/admin:tag",
                packaging.build_admin_image(self.root, CONFIG),
            )
        self.assertFalse(
            (self.root / ".runtime/rendered/admin-build").exists()
        )

    def test_azure_deployment_render_requires_one_immutable_image(self) -> None:
        template = self.root / "admin/config/deployment/deployment-azure.yaml.tpl"
        template.parent.mkdir(parents=True)
        template.write_text(
            "image: ${TENANT_ADMIN_IMAGE}\n"
            "env:\n- name: TENANT_ADMIN_PROVIDER\n  value: azure\n",
            encoding="utf-8",
        )
        image = "example.azurecr.io/tenant-admin@sha256:" + "a" * 64
        rendered = packaging.render_azure_admin_deployment(self.root, image)
        self.assertEqual(0o600, rendered.stat().st_mode & 0o777)
        content = rendered.read_text(encoding="utf-8")
        self.assertIn(f"image: {image}", content)
        self.assertNotIn("${TENANT_ADMIN_IMAGE}", content)
        for invalid in (
            "example.azurecr.io/tenant-admin:latest",
            "example.azurecr.io/tenant-admin@sha256:short",
        ):
            with self.subTest(image=invalid), self.assertRaisesRegex(
                RuntimeError, "immutable digest"
            ):
                packaging.render_azure_admin_deployment(self.root, invalid)
        template.write_text("image: fixed\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "exactly one"):
            packaging.render_azure_admin_deployment(self.root, image)

    def test_tracked_dockerfile_is_scratch_non_root_and_provider_neutral(self) -> None:
        dockerfile = (ROOT / "admin/Dockerfile").read_text(encoding="utf-8")
        self.assertEqual(
            "FROM scratch\n"
            "COPY --chown=65532:65532 tenant-admin /tenant-admin\n"
            "COPY --chown=65532:65532 web /web\n"
            "USER 65532:65532\n"
            "ENTRYPOINT [\"/tenant-admin\"]\n",
            dockerfile,
        )
        self.assertNotIn("local", dockerfile)
        self.assertNotIn("azure", dockerfile)
        index = (ROOT / "admin/web/index.html").read_text(encoding="utf-8")
        self.assertIn('data-wasm-opt="0"', index)


if __name__ == "__main__":
    unittest.main()
