from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import os
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from scripts.lib.files import IntegrityError, write_private_file
from scripts.lib.ownership import OwnershipError
from scripts.lib.registry import (
    _configure_containerd,
    _data_path,
    _expected_images,
    _record_path,
    _tree_inventory,
    _validate_container,
    delete_offline_registry,
    load_mirror_image,
    reconcile_offline_registry,
    validate_retained_offline_registry,
)
from tests.test_cache import EXACT, SOURCE_DIGEST, TAGGED, write_archive


class RegistryTests(unittest.TestCase):
    def test_retained_offline_validation_checks_state_and_pulls(self) -> None:
        with (
            patch.dict(os.environ, {"CAPI_OFFLINE_ENFORCED": "1"}),
            patch(
                "scripts.lib.registry._validate_reusable_registry_record"
            ) as validate,
            patch(
                "scripts.lib.registry.verify_offline_registry_pulls"
            ) as pulls,
        ):
            validate_retained_offline_registry(
                Path("/tmp/example"),
                {},
                "management-control-plane",
                {"network": "kind"},
            )
        validate.assert_called_once()
        pulls.assert_called_once_with({}, "management-control-plane")

    @staticmethod
    def config() -> dict[str, str]:
        config = {
            "LAB_PREFIX": "lab",
            "OWNERSHIP_LABEL": "example.owner",
            "OFFLINE_REGISTRY_IMAGE": "registry:2@sha256:" + "f" * 64,
        }
        for key in (
            "KUBE_APISERVER_IMAGE",
            "KUBE_CONTROLLER_MANAGER_IMAGE",
            "KUBE_SCHEDULER_IMAGE",
            "KONNECTIVITY_SERVER_IMAGE",
            "CNPG_CONTROLLER_IMAGE",
        ):
            config[key] = (
                f"registry.k8s.io/repository/{key.lower()}:v1@sha256:"
                + "a" * 64
            )
            config[f"{key}_TAGGED"] = config[key].split("@", 1)[0]
        return config

    @staticmethod
    def state(root: Path, config: dict[str, str]) -> tuple[dict, dict]:
        data = _data_path(root)
        data.mkdir(parents=True, mode=0o700)
        for parent in (root / ".runtime", root / ".runtime/management"):
            parent.chmod(0o700)
        write_private_file(data / "blob", b"content")
        files = _tree_inventory(data)
        labels = {
            "example.owner": "lab",
            "cnpg-vcluster.capi/role": "offline-registry",
        }
        record = {
            "schema": 1,
            "name": "lab-offline-registry",
            "identifier": "container-id",
            "imageIdentifier": "image-id",
            "imageReference": config["OFFLINE_REGISTRY_IMAGE"],
            "labels": labels,
            "network": "kind",
            "networkIdentifier": "network-id",
            "address": "172.18.0.3",
            "generation": "g1",
            "files": files,
            "images": [],
        }
        write_private_file(
            _record_path(root),
            json.dumps(record, sort_keys=True) + "\n",
        )
        payload = {
            "Id": "container-id",
            "Image": "image-id",
            "Config": {"Labels": labels},
            "NetworkSettings": {
                "Networks": {"kind": {"IPAddress": "172.18.0.3"}}
            },
        }
        return record, payload

    def test_archive_maps_tag_and_digest_to_verified_source_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "image.tar"
            exact = write_archive(
                archive,
                tagged="registry.k8s.io/lab/image:v1",
            )
            image = load_mirror_image(
                archive,
                "TEST_IMAGE",
                "registry.k8s.io/lab/image:v1",
                exact.replace("example.invalid/lab/image", "registry.k8s.io/lab/image"),
            )
            self.assertEqual(image.repository, "lab/image")
            self.assertEqual(image.registry, "registry.k8s.io")
            self.assertEqual(image.tag, "v1")
            self.assertEqual(image.source_digest, SOURCE_DIGEST)
            self.assertIn(image.platform_digest, image.content)

    def test_archive_preserves_non_kubernetes_registry_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "image.tar"
            write_archive(archive)
            image = load_mirror_image(archive, "TEST_IMAGE", TAGGED, EXACT)
            self.assertEqual(image.registry, "example.invalid")

    def test_registry_tree_inventory_rejects_tampering_and_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_private_file(root / "blob", b"content")
            expected = hashlib.sha256(b"content").hexdigest()
            self.assertEqual(_tree_inventory(root), {"blob": expected})
            (root / "blob").write_bytes(b"tampered")
            self.assertNotEqual(_tree_inventory(root), {"blob": expected})
            (root / "link").symlink_to(root / "blob")
            with self.assertRaises(OwnershipError):
                _tree_inventory(root)

    def test_registry_tree_rejects_root_mode_and_root_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            data.mkdir(mode=0o700)
            write_private_file(data / "blob", b"content")
            data.chmod(0o755)
            with self.assertRaises(OwnershipError):
                _tree_inventory(data)
            data.chmod(0o700)
            link = root / "link"
            link.symlink_to(data, target_is_directory=True)
            with self.assertRaises(OwnershipError):
                _tree_inventory(link)

    def test_container_validation_rejects_identifier_and_label_mismatch(self) -> None:
        config = {
            "LAB_PREFIX": "lab",
            "OWNERSHIP_LABEL": "example.owner",
            "OFFLINE_REGISTRY_IMAGE": "registry:2@sha256:" + "a" * 64,
        }
        record = {
            "name": "lab-offline-registry",
            "identifier": "container-id",
            "imageIdentifier": "image-id",
            "imageReference": config["OFFLINE_REGISTRY_IMAGE"],
            "labels": {
                "example.owner": "lab",
                "cnpg-vcluster.capi/role": "offline-registry",
            },
            "network": "kind",
            "address": "172.18.0.3",
        }
        payload = {
            "Id": "container-id",
            "Image": "image-id",
            "Config": {"Labels": dict(record["labels"])},
            "NetworkSettings": {
                "Networks": {"kind": {"IPAddress": "172.18.0.3"}}
            },
        }
        _validate_container(config, record, payload)
        payload["Id"] = "foreign"
        with self.assertRaises(OwnershipError):
            _validate_container(config, record, payload)

    def test_containerd_mirror_configuration_targets_only_local_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            calls: list[list[str]] = []
            config = {
                key: f"registry.k8s.io/repository/{key.lower()}:v1@sha256:"
                + "a" * 64
                for key in (
                    "KUBE_APISERVER_IMAGE",
                    "KUBE_CONTROLLER_MANAGER_IMAGE",
                    "KUBE_SCHEDULER_IMAGE",
                    "KONNECTIVITY_SERVER_IMAGE",
                    "CNPG_CONTROLLER_IMAGE",
                )
            }
            for key in tuple(config):
                config[f"{key}_TAGGED"] = config[key].split("@", 1)[0]
            config["CNPG_CONTROLLER_IMAGE"] = (
                "ghcr.io/cloudnative-pg/controller:v1@sha256:" + "b" * 64
            )
            config["CNPG_CONTROLLER_IMAGE_TAGGED"] = (
                "ghcr.io/cloudnative-pg/controller:v1"
            )
            with patch(
                "scripts.lib.registry.run",
                side_effect=lambda command, **_: calls.append(command)
                or CompletedProcess(command, 0, "", ""),
            ):
                _configure_containerd(
                    root,
                    config,
                    "management-control-plane",
                    "172.18.0.3",
                )
            hosts = (
                root / ".runtime/rendered/registry-hosts-registry.k8s.io.toml"
            ).read_text(encoding="utf-8")
            self.assertIn('server = "https://registry.k8s.io"', hosts)
            self.assertIn('[host."http://172.18.0.3:5000"]', hosts)
            self.assertNotIn("skip_verify", hosts)
            ghcr_hosts = (
                root / ".runtime/rendered/registry-hosts-ghcr.io.toml"
            ).read_text(encoding="utf-8")
            self.assertIn('server = "https://ghcr.io"', ghcr_hosts)
            self.assertTrue(any(command[:2] == ["docker", "cp"] for command in calls))

    def test_expected_images_rejects_tag_and_digest_repository_drift(self) -> None:
        config = {
            key: f"registry.k8s.io/repository/{key.lower()}:v1@sha256:" + "a" * 64
            for key in (
                "KUBE_APISERVER_IMAGE",
                "KUBE_CONTROLLER_MANAGER_IMAGE",
                "KUBE_SCHEDULER_IMAGE",
                "KONNECTIVITY_SERVER_IMAGE",
                "CNPG_CONTROLLER_IMAGE",
            )
        }
        for key in tuple(config):
            config[f"{key}_TAGGED"] = config[key].split("@", 1)[0]
        self.assertEqual(len(_expected_images(config)), 5)
        config["KUBE_APISERVER_IMAGE_TAGGED"] = "registry.k8s.io/other:v1"
        with self.assertRaises(IntegrityError):
            _expected_images(config)

    def test_cleanup_removes_only_exact_recorded_registry_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.config()
            _, payload = self.state(root, config)
            with (
                patch(
                    "scripts.lib.registry._inspect_container",
                    side_effect=[payload, payload, None],
                ),
                patch("scripts.lib.registry.run") as run,
            ):
                delete_offline_registry(root, config)
            run.assert_called_once_with(
                ["docker", "rm", "--force", "lab-offline-registry"],
                timeout=30,
            )
            self.assertFalse(os.path.lexists(_record_path(root)))
            self.assertFalse(os.path.lexists(_data_path(root)))

    def test_cleanup_refuses_partial_identity_and_tampered_state(self) -> None:
        config = self.config()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _data_path(root).mkdir(parents=True, mode=0o700)
            with patch("scripts.lib.registry._inspect_container", return_value=None):
                with self.assertRaises(OwnershipError):
                    delete_offline_registry(root, config)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, payload = self.state(root, config)
            payload["Id"] = "foreign"
            with patch("scripts.lib.registry._inspect_container", return_value=payload):
                with self.assertRaises(OwnershipError):
                    delete_offline_registry(root, config)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, payload = self.state(root, config)
            (_data_path(root) / "blob").write_bytes(b"tampered")
            with patch("scripts.lib.registry._inspect_container", return_value=payload):
                with self.assertRaises(OwnershipError):
                    delete_offline_registry(root, config)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, payload = self.state(root, config)
            data = _data_path(root)
            target = root / "foreign-data"
            data.rename(target)
            data.symlink_to(target, target_is_directory=True)
            with patch("scripts.lib.registry._inspect_container", return_value=payload):
                with self.assertRaises(OwnershipError):
                    delete_offline_registry(root, config)

    def test_reuse_refuses_network_generation_and_image_drift(self) -> None:
        config = self.config()
        for mutation in ("network", "generation", "images"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                record, payload = self.state(root, config)
                record["images"] = _expected_images(config)
                if mutation == "network":
                    record["networkIdentifier"] = "other-network"
                elif mutation == "generation":
                    record["generation"] = "old"
                else:
                    record["images"][0]["sourceDigest"] = "sha256:" + "b" * 64
                with (
                    patch.dict(os.environ, {"CAPI_OFFLINE_ENFORCED": "1"}),
                    patch("scripts.lib.registry._inspect_container", return_value=payload),
                    patch("scripts.lib.registry.validate_registry_state", return_value=record),
                    patch("scripts.lib.registry.verify_cache"),
                    patch(
                        "scripts.lib.registry.active_generation",
                        return_value=Path("/cache/generations/g1"),
                    ),
                ):
                    with self.assertRaises(OwnershipError):
                        reconcile_offline_registry(
                            root,
                            config,
                            "management-control-plane",
                            {"network": "kind", "network_id": "network-id"},
                        )


if __name__ == "__main__":
    unittest.main()
