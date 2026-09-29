from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from scripts.destroy import _validate_runtime_inventory, inspect_host_residue
from scripts.lib.ownership import IdentityRecord, OwnershipError


class HostOwnershipTests(unittest.TestCase):
    def test_kind_identity_requires_exact_labels_and_identifier(self) -> None:
        expected = IdentityRecord(
            "container",
            "management-control-plane",
            "expected-id",
            {"io.x-k8s.kind.cluster": "management", "owner": ""},
        )
        mismatches = (
            IdentityRecord(
                "container",
                "management-control-plane",
                "other-id",
                expected.labels,
            ),
            IdentityRecord(
                "container",
                "management-control-plane",
                "expected-id",
                {"io.x-k8s.kind.cluster": "other", "owner": ""},
            ),
        )
        for observed in mismatches:
            with self.assertRaises(OwnershipError):
                expected.require_exact(observed)

    def test_runtime_inventory_rejects_unknown_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / ".runtime"
            runtime.mkdir(mode=0o700)
            unknown = runtime / "foreign"
            unknown.write_text("foreign\n", encoding="utf-8")
            unknown.chmod(0o600)
            with self.assertRaises(RuntimeError):
                _validate_runtime_inventory(root)

    def test_runtime_inventory_removes_private_obsolete_local_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / ".runtime"
            legacy = runtime / "lifecycle" / "local" / "tenant-a"
            legacy.mkdir(parents=True, mode=0o700)
            for parent in (
                runtime,
                runtime / "lifecycle",
                runtime / "lifecycle" / "local",
            ):
                parent.chmod(0o700)
            identity = legacy / "identity.json"
            identity.write_text("{}\n", encoding="utf-8")
            identity.chmod(0o600)
            endpoint = runtime / "management" / "tenant-endpoints.json"
            endpoint.parent.mkdir(mode=0o700)
            endpoint.write_text("{}\n", encoding="utf-8")
            endpoint.chmod(0o600)

            _validate_runtime_inventory(root)

            self.assertFalse((runtime / "lifecycle" / "local").exists())
            self.assertFalse(endpoint.exists())

    def test_runtime_inventory_removes_legacy_tenant_files_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / ".runtime"
            retained = (
                runtime / "tenants" / "tenant-a" / "kubeconfig"
            )
            foundation = runtime / "azure" / "resources.json"
            obsolete = (
                runtime / "azure" / "deletion-proofs" / "tenant-a.json",
                runtime / "azure-gate" / "state" / "tenant-a.json",
                runtime
                / "lifecycle"
                / "azure"
                / "tenant-a"
                / "evidence"
                / "delete-operation.json",
                runtime / "rendered" / "storage" / "tenant-a" / "smoke.yaml",
                runtime / "rendered" / "cnpg" / "tenant-a" / "sql.json",
                runtime / "evidence" / "endpoint-success.json",
                runtime / "storage" / "tenant-a" / "volume.json",
                runtime
                / "tenants"
                / "tenant-a"
                / (".kubeconfig-candidate-123-" + "a" * 32),
            )
            for path in (retained, foundation, *obsolete):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}\n", encoding="utf-8")
                path.chmod(0o600)
            for path in runtime.rglob("*"):
                if path.is_dir():
                    path.chmod(0o700)
            legacy_azure_tenants = runtime / "azure" / "tenants"
            legacy_azure_tenants.mkdir(mode=0o700)
            runtime.chmod(0o700)

            _validate_runtime_inventory(root)

            self.assertTrue(retained.exists())
            self.assertTrue(foundation.exists())
            self.assertTrue(all(not path.exists() for path in obsolete))
            self.assertFalse(legacy_azure_tenants.exists())

    def test_runtime_inventory_allows_only_kubeconfig_in_tenant_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tenant = root / ".runtime" / "tenants" / "tenant-a"
            tenant.mkdir(parents=True, mode=0o700)
            for parent in tenant.parents:
                if parent == root:
                    break
                parent.chmod(0o700)
            kubeconfig = tenant / "kubeconfig"
            kubeconfig.write_text("config\n", encoding="utf-8")
            kubeconfig.chmod(0o600)
            identity = tenant / "identity.json"
            identity.write_text("{}\n", encoding="utf-8")
            identity.chmod(0o600)

            with self.assertRaisesRegex(RuntimeError, "identity.json"):
                _validate_runtime_inventory(root)
            self.assertTrue(kubeconfig.exists())
            self.assertTrue(identity.exists())

    def test_runtime_inventory_allows_sixty_three_character_cache_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            name = "a" * 63
            kubeconfig = (
                root / ".runtime" / "tenants" / name / "kubeconfig"
            )
            kubeconfig.parent.mkdir(parents=True, mode=0o700)
            for parent in kubeconfig.parent.parents:
                if parent == root:
                    break
                parent.chmod(0o700)
            kubeconfig.write_text("config\n", encoding="utf-8")
            kubeconfig.chmod(0o600)

            _validate_runtime_inventory(root)

            self.assertTrue(kubeconfig.exists())

    def test_runtime_inventory_rejects_symlinked_obsolete_local_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as target:
            root = Path(temporary)
            runtime = root / ".runtime"
            runtime.mkdir(mode=0o700)
            external = Path(target)
            endpoint = external / "tenant-endpoints.json"
            endpoint.write_text("outside\n", encoding="utf-8")
            (runtime / "management").symlink_to(external, target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, "symlink"):
                _validate_runtime_inventory(root)
            self.assertEqual(endpoint.read_text(encoding="utf-8"), "outside\n")

    def test_runtime_inventory_rejects_symlinked_root_without_obsolete_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as target:
            root = Path(temporary)
            external = Path(target)
            marker = external / "foreign"
            marker.write_text("outside\n", encoding="utf-8")
            (root / ".runtime").symlink_to(external, target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, "runtime root"):
                _validate_runtime_inventory(root)
            self.assertEqual(marker.read_text(encoding="utf-8"), "outside\n")

    def test_unknown_file_inside_obsolete_tree_blocks_without_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy = root / ".runtime/lifecycle/local/tenant-a"
            legacy.mkdir(parents=True, mode=0o700)
            for parent in legacy.parents:
                if parent == root:
                    break
                parent.chmod(0o700)
            identity = legacy / "identity.json"
            identity.write_text("{}\n", encoding="utf-8")
            identity.chmod(0o600)
            unknown = legacy / "unrelated.json"
            unknown.write_text("{}\n", encoding="utf-8")
            unknown.chmod(0o600)
            with self.assertRaisesRegex(RuntimeError, "unexpected runtime file"):
                _validate_runtime_inventory(root)
            self.assertTrue(identity.exists())
            self.assertTrue(unknown.exists())

    def test_unknown_directory_inside_obsolete_tree_blocks_without_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy = root / ".runtime/lifecycle/local/tenant-a"
            unexpected = legacy / "unexpected"
            unexpected.mkdir(parents=True, mode=0o700)
            for parent in unexpected.parents:
                if parent == root:
                    break
                parent.chmod(0o700)
            identity = legacy / "ready.json"
            identity.write_text("{}\n", encoding="utf-8")
            identity.chmod(0o600)
            with self.assertRaisesRegex(RuntimeError, "unexpected runtime directory"):
                _validate_runtime_inventory(root)
            self.assertTrue(identity.exists())
            self.assertTrue(unexpected.exists())

    def test_unknown_directory_outside_obsolete_tree_blocks_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unexpected = root / ".runtime/management/foreign"
            unexpected.mkdir(parents=True, mode=0o700)
            for parent in unexpected.parents:
                if parent == root:
                    break
                parent.chmod(0o700)
            with self.assertRaisesRegex(RuntimeError, "unexpected runtime directory"):
                _validate_runtime_inventory(root)
            self.assertTrue(unexpected.exists())

    def test_empty_obsolete_directories_are_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for relative in (
                ".runtime/lifecycle/local",
                ".runtime/lifecycle/rejected/local",
            ):
                directory = root / relative
                directory.mkdir(parents=True, mode=0o700)
                for parent in directory.parents:
                    if parent == root:
                        break
                    parent.chmod(0o700)
            _validate_runtime_inventory(root)
            self.assertFalse((root / ".runtime/lifecycle/local").exists())
            self.assertFalse((root / ".runtime/lifecycle/rejected/local").exists())

    def test_host_residue_includes_unrecorded_capd_worker_role(self) -> None:
        config = {
            "SPIKE_NAME": "spike",
            "OWNERSHIP_LABEL": "owner",
            "LAB_PREFIX": "lab",
        }

        def docker(command, **_kwargs):
            output = (
                "worker-id\n"
                if "label=io.x-k8s.kind.role=worker" in command
                else ""
            )
            return type("Result", (), {"stdout": output})()

        with patch("scripts.destroy.run", side_effect=docker):
            residue = inspect_host_residue(config)

        self.assertEqual(residue["containers"], ["worker-id"])

    def test_host_residue_includes_project_labeled_volume(self) -> None:
        config = {
            "SPIKE_NAME": "spike",
            "OWNERSHIP_LABEL": "owner",
            "LAB_PREFIX": "lab",
        }

        def docker(command, **_kwargs):
            output = (
                "volume-id\n"
                if command[:4] == ["docker", "volume", "ls", "-q"]
                and "label=cnpg-vcluster.capi/role" in command
                else ""
            )
            return type("Result", (), {"stdout": output})()

        with (
            patch("scripts.destroy.run", side_effect=docker),
            patch(
                "scripts.destroy.tenant_storage_volumes",
                return_value={"volume-id"},
            ),
        ):
            residue = inspect_host_residue(config)

        self.assertEqual(residue["volumes"], ["volume-id"])
