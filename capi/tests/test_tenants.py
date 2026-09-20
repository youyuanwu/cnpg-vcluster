from __future__ import annotations

import tempfile
import unittest
import json
import shutil
import base64
from pathlib import Path
from unittest.mock import patch

from scripts.lib.tenants import (
    Tenant,
    _tenant_values,
    _render_template,
    lifecycle_markers,
    load_local_tenant_spec,
    prepare_storage_directory,
    remove_tenant_storage_volume,
    storage_volume_name,
    tenant_from_spec,
    validate_tenant_kubeconfig_view,
    verify_worker_preload_contract,
)
from scripts.lib.files import IntegrityError
from scripts.lib.images import WORKER_IMAGE_KEYS
from scripts.lib.tenant_spec import TenantSpec, TenantSpecError


class TenantTests(unittest.TestCase):
    def test_local_tenant_spec_constructs_dynamic_tenant(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "tenant.json"
            path.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "profile": "local",
                        "name": "tenant-c",
                        "kubernetesVersion": "1.36.4",
                        "workers": 1,
                        "podCIDR": "10.72.0.0/16",
                        "serviceCIDR": "10.142.0.0/16",
                        "databaseCount": 3,
                    }
                ),
                encoding="utf-8",
            )
            spec = load_local_tenant_spec(
                root,
                path,
                {"KUBERNETES_VERSION": "v1.36.4"},
            )
            tenant = tenant_from_spec(root, spec, "172.18.255.240")
        self.assertEqual(tenant.name, "tenant-c")
        self.assertEqual(tenant.namespace, "tenant-c")
        self.assertEqual(tenant.vip, "172.18.255.240")
        self.assertEqual(tenant.dns_ip, "10.142.0.10")
        self.assertEqual(tenant.domain, "tenant-c.capi.local")
        self.assertEqual(tenant.cnpg_cluster, "tenant-c-postgres")
        self.assertEqual(tenant.workers, 1)
        self.assertEqual(tenant.database_count, 3)
        self.assertEqual(tenant.specification_sha256, spec.sha256())

    def test_local_tenant_spec_rejects_existing_network_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "tenant.json"
            path.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "profile": "local",
                        "name": "tenant-c",
                        "kubernetesVersion": "1.36.4",
                        "workers": 1,
                        "podCIDR": "10.70.0.0/16",
                        "serviceCIDR": "10.142.0.0/16",
                        "databaseCount": 1,
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(TenantSpecError, "existing Pod CIDR"):
                load_local_tenant_spec(
                    Path(temporary),
                    path,
                    {"KUBERNETES_VERSION": "v1.36.4"},
                    existing_specs=(
                        TenantSpec.from_mapping(
                            {
                                "schema": 1,
                                "profile": "local",
                                "name": "existing",
                                "kubernetesVersion": "1.36.4",
                                "workers": 1,
                                "podCIDR": "10.70.0.0/16",
                                "serviceCIDR": "10.150.0.0/16",
                                "databaseCount": 1,
                            }
                        ),
                    ),
                )

    def test_local_tenant_spec_rejects_management_subnet_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "tenant.json"
            path.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "profile": "local",
                        "name": "tenant-c",
                        "kubernetesVersion": "1.36.4",
                        "workers": 1,
                        "podCIDR": "172.18.0.0/16",
                        "serviceCIDR": "10.143.0.0/16",
                        "databaseCount": 1,
                    }
                ),
                encoding="utf-8",
            )
            network = root / ".runtime" / "management" / "network.json"
            network.parent.mkdir(parents=True)
            for parent in (root / ".runtime", network.parent):
                parent.chmod(0o700)
            network.write_text(
                json.dumps({"subnet": "172.18.0.0/16"}),
                encoding="utf-8",
            )
            network.chmod(0o600)
            with self.assertRaisesRegex(
                TenantSpecError,
                "management Docker subnet",
            ):
                load_local_tenant_spec(
                    root,
                    path,
                    {"KUBERNETES_VERSION": "v1.36.4"},
                )

    def test_live_worker_templates_match_exact_preload_contract(self) -> None:
        tenant = type(
            "Tenant", (), {"name": "tenant-a", "namespace": "tenant-a"}
        )()
        config = {
            key: f"example/{key.lower()}:v1@sha256:{index:064x}"
            for index, key in enumerate(WORKER_IMAGE_KEYS, 1)
        }
        config.update(
            {
                f"{key}_TAGGED": config[key].split("@", 1)[0]
                for key in WORKER_IMAGE_KEYS
            }
        )
        devmachine = {
            "spec": {
                "template": {
                    "spec": {
                        "backend": {
                            "docker": {
                                "extraMounts": [
                                    {
                                        "hostPath": "/repo/.tools/cache",
                                        "containerPath": "/var/lib/capi-image-cache",
                                        "readOnly": True,
                                    }
                                ],
                            }
                        }
                    }
                }
            }
        }
        client = type("Client", (), {})()
        client.kubectl = lambda *args: type(
            "Result",
            (),
            {
                "stdout": json.dumps(
                    devmachine
                )
            },
        )()
        verify_worker_preload_contract(
            Path("/repo"), config, client, tenant
        )

    def test_worker_preload_values_are_sorted_exact_references(self) -> None:
        tenant = Tenant(
            name="tenant-a",
            namespace="tenant-a",
            vip="172.18.0.10",
            pod_cidr="10.1.0.0/16",
            service_cidr="10.2.0.0/16",
            dns_ip="10.2.0.10",
            domain="tenant-a.local",
            storage_host_path=Path("/storage"),
            cnpg_cluster="postgres",
            workers=3,
        )
        config = {
            "OWNERSHIP_LABEL": "example.owner",
            "LAB_PREFIX": "example",
            "SPIKE_API_PORT": "6443",
            "KUBERNETES_VERSION": "v1.36.4",
            "KONNECTIVITY_SERVER_IMAGE_TAGGED": "server:v1",
            "KONNECTIVITY_SERVER_IMAGE": "server:v1@sha256:" + "1" * 64,
            "KONNECTIVITY_AGENT_IMAGE_TAGGED": "agent:v1",
            "KONNECTIVITY_AGENT_IMAGE": "agent:v1@sha256:" + "2" * 64,
            "KIND_NODE_IMAGE": "kind:v1@sha256:" + "3" * 64,
            "SPIKE_STORAGE_CONTAINER_PATH": "/storage",
        }
        for index, key in enumerate(
            (
                "CALICO_CNI_IMAGE",
                "CALICO_KUBE_CONTROLLERS_IMAGE",
                "CALICO_NODE_IMAGE",
                "KUBE_PROXY_IMAGE",
                "CNPG_CONTROLLER_IMAGE",
                "POSTGRES_IMAGE",
                "VERIFY_IMAGE",
            ),
            4,
        ):
            config[key] = f"example/{key.lower()}:v1@sha256:{index:064x}"
        for key in WORKER_IMAGE_KEYS:
            config[f"{key}_TAGGED"] = config[key].split("@", 1)[0]
        values = _tenant_values(Path("/repo"), config, tenant)
        self.assertEqual(
            values["IMAGE_CACHE_HOST_PATH"],
            "/repo/.tools/cache",
        )

    def test_template_rejects_unresolved_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.yaml"
            destination = Path(temporary) / "output.yaml"
            source.write_text("name: ${NAME}\nother: ${MISSING}\n", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                _render_template(source, destination, {"NAME": "value"})

    def test_storage_volume_name_is_tenant_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tenant = Tenant(
                name="spike",
                namespace="spike",
                vip="172.18.0.10",
                pod_cidr="10.1.0.0/16",
                service_cidr="10.2.0.0/16",
                dns_ip="10.2.0.10",
                domain="spike.local",
                storage_host_path=root / ".runtime" / "storage" / "spike",
                cnpg_cluster="spike-postgres",
                workers=1,
            )
            self.assertEqual(
                storage_volume_name({"LAB_PREFIX": "test"}, tenant),
                "test-spike-storage",
            )

    def test_absent_volume_removes_valid_orphaned_identity_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tenant = Tenant(
                name="tenant-a",
                namespace="tenant-a",
                vip="172.18.0.10",
                pod_cidr="10.70.0.0/16",
                service_cidr="10.140.0.0/16",
                dns_ip="10.140.0.10",
                domain="tenant-a.local",
                storage_host_path=root / "storage",
                cnpg_cluster="tenant-a-postgres",
                workers=3,
            )
            record = root / ".runtime" / "storage" / tenant.name / "volume.json"
            record.parent.mkdir(parents=True)
            record.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "tenant": tenant.name,
                        "volumeName": "lab-tenant-a-storage",
                        "createdAt": "2026-01-01T00:00:00Z",
                        "mountpoint": "/var/lib/docker/volumes/test/_data",
                    }
                ),
                encoding="utf-8",
            )
            record.chmod(0o600)
            with patch(
                "scripts.lib.tenants.inspect_storage_volume",
                return_value=None,
            ):
                remove_tenant_storage_volume(
                    root, {"LAB_PREFIX": "lab"}, tenant
                )
            self.assertFalse(record.exists())
            self.assertFalse(record.parent.exists())

    def test_kubeconfig_validation_uses_active_context_only(self) -> None:
        tenant = type("Tenant", (), {"vip": "172.18.0.10"})()
        expected_ca = b"tenant-ca"
        view = {
            "current-context": "wrong",
            "contexts": [
                {"name": "expected", "context": {"cluster": "expected"}},
                {"name": "wrong", "context": {"cluster": "wrong"}},
            ],
            "clusters": [
                {
                    "name": "expected",
                    "cluster": {
                        "server": "https://172.18.0.10:6443",
                        "certificate-authority-data": base64.b64encode(
                            expected_ca
                        ).decode(),
                    },
                },
                {
                    "name": "wrong",
                    "cluster": {
                        "server": "https://172.18.0.11:6443",
                        "certificate-authority-data": base64.b64encode(
                            expected_ca
                        ).decode(),
                    },
                },
            ],
        }
        with self.assertRaisesRegex(RuntimeError, "active context"):
            validate_tenant_kubeconfig_view(
                {"SPIKE_API_PORT": "6443"},
                tenant,
                view,
                expected_ca,
            )

    def test_stale_storage_record_blocks_before_volume_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tenant = Tenant(
                name="tenant-a",
                namespace="tenant-a",
                vip="172.18.0.10",
                pod_cidr="10.70.0.0/16",
                service_cidr="10.140.0.0/16",
                dns_ip="10.140.0.10",
                domain="tenant-a.local",
                storage_host_path=root / "storage",
                cnpg_cluster="tenant-a-postgres",
                workers=3,
            )
            record = root / ".runtime" / "storage" / tenant.name / "volume.json"
            for directory in (
                root / ".runtime",
                root / ".runtime" / "storage",
                record.parent,
            ):
                directory.mkdir(mode=0o700)
            record.write_text("{}\n", encoding="utf-8")
            record.chmod(0o600)
            with (
                patch(
                    "scripts.lib.tenants.inspect_storage_volume",
                    return_value=None,
                ),
                patch("scripts.lib.tenants.run") as run,
            ):
                with self.assertRaisesRegex(RuntimeError, "stale"):
                    prepare_storage_directory(
                        root,
                        {
                            "LAB_PREFIX": "lab",
                            "OWNERSHIP_LABEL": "example.owner",
                        },
                        tenant,
                    )
            run.assert_not_called()

    def test_storage_record_failure_rolls_back_new_volume(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tenant = Tenant(
                name="tenant-a",
                namespace="tenant-a",
                vip="172.18.0.10",
                pod_cidr="10.70.0.0/16",
                service_cidr="10.140.0.0/16",
                dns_ip="10.140.0.10",
                domain="tenant-a.local",
                storage_host_path=root / "storage",
                cnpg_cluster="tenant-a-postgres",
                workers=3,
            )
            payload = {
                "Name": "lab-tenant-a-storage",
                "CreatedAt": "2026-01-01T00:00:00Z",
                "Mountpoint": "/var/lib/docker/volumes/test/_data",
                "Labels": {
                    "example.owner": "lab",
                    "cnpg-vcluster.capi/role": "tenant-storage",
                    "cnpg-vcluster.capi/tenant": "tenant-a",
                },
            }
            with (
                patch(
                    "scripts.lib.tenants.inspect_storage_volume",
                    side_effect=[None, payload, payload],
                ),
                patch("scripts.lib.tenants.run") as run,
                patch(
                    "scripts.lib.tenants.write_private_file",
                    side_effect=IntegrityError("injected write failure"),
                ),
            ):
                with self.assertRaises(IntegrityError):
                    prepare_storage_directory(
                        root,
                        {
                            "LAB_PREFIX": "lab",
                            "OWNERSHIP_LABEL": "example.owner",
                        },
                        tenant,
                    )
            self.assertEqual(run.call_count, 2)
            self.assertEqual(
                run.call_args_list[-1].args[0],
                ["docker", "volume", "rm", "lab-tenant-a-storage"],
            )

    def test_storage_post_create_inspection_failure_attempts_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tenant = Tenant(
                name="tenant-a",
                namespace="tenant-a",
                vip="172.18.0.10",
                pod_cidr="10.70.0.0/16",
                service_cidr="10.140.0.0/16",
                dns_ip="10.140.0.10",
                domain="tenant-a.local",
                storage_host_path=root / "storage",
                cnpg_cluster="tenant-a-postgres",
                workers=3,
            )
            payload = {
                "Name": "lab-tenant-a-storage",
                "CreatedAt": "2026-01-01T00:00:00Z",
                "Mountpoint": "/var/lib/docker/volumes/test/_data",
                "Labels": {
                    "example.owner": "lab",
                    "cnpg-vcluster.capi/role": "tenant-storage",
                    "cnpg-vcluster.capi/tenant": "tenant-a",
                },
            }
            with (
                patch(
                    "scripts.lib.tenants.inspect_storage_volume",
                    side_effect=[
                        None,
                        RuntimeError("injected inspect failure"),
                        payload,
                    ],
                ),
                patch("scripts.lib.tenants.run") as run,
            ):
                with self.assertRaisesRegex(RuntimeError, "inspect failure"):
                    prepare_storage_directory(
                        root,
                        {
                            "LAB_PREFIX": "lab",
                            "OWNERSHIP_LABEL": "example.owner",
                        },
                        tenant,
                    )
            self.assertEqual(
                run.call_args_list[-1].args[0],
                ["docker", "volume", "rm", "lab-tenant-a-storage"],
            )
