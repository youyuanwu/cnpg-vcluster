from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.lib.addons import (
    NetworkProbeCleanupError,
    REFERENCE_LIMIT,
    SOURCE_LIMIT,
    _source_object,
    package_source,
    validate_inventory,
    validate_manifest_hashes,
    validate_resource_set_references,
    render_resource_set,
    verify_addon_source_ownership,
    verify_network,
    network_status,
)
from scripts.lib.files import IntegrityError
from scripts.lib.tenants import Tenant


class AddonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tenant = Tenant(
            name="spike",
            namespace="spike",
            vip="172.18.0.20",
            pod_cidr="10.1.0.0/16",
            service_cidr="10.2.0.0/16",
            dns_ip="10.2.0.10",
            domain="spike.local",
            storage_host_path=Path("/tmp/spike"),
            cnpg_cluster="spike-postgres",
            workers=1,
        )
        self.config = {
            "OWNERSHIP_LABEL": "example.owner",
            "LAB_PREFIX": "example",
        }

    def test_source_object_is_below_selected_limit(self) -> None:
        payload = _source_object(self.config, self.tenant, "source", "kind: List\n")
        serialized = json.dumps(payload, separators=(",", ":")).encode()
        self.assertLess(len(serialized), SOURCE_LIMIT)

    def test_network_probe_is_removed_after_probe_failure(self) -> None:
        calls: list[tuple[str, ...]] = []

        def kubectl(*args, **_kwargs):
            calls.append(tuple(str(item) for item in args))
            if "run" in args:
                raise RuntimeError("probe failed")
            if "delete" in args:
                return type(
                    "Result",
                    (),
                    {"returncode": 0, "stdout": "", "stderr": ""},
                )()
            if any(str(item).startswith("pod/network-smoke-") for item in args):
                return type(
                    "Result",
                    (),
                    {
                        "returncode": 1,
                        "stdout": "",
                        "stderr": "Error from server (NotFound): pod not found",
                    },
                )()
            if "configmap/capi-kube-proxy" in args:
                stdout = json.dumps({"data": {"config.conf": "maxPerCore: 0"}})
            elif "daemonset/capi-kube-proxy" in args:
                stdout = "proxy@sha256:" + "a" * 64
            else:
                stdout = json.dumps(
                    {
                        "roleRef": {
                            "apiGroup": "rbac.authorization.k8s.io",
                            "kind": "ClusterRole",
                            "name": "system:node-proxier",
                        },
                        "subjects": [
                            {
                                "kind": "ServiceAccount",
                                "name": "capi-kube-proxy",
                                "namespace": "kube-system",
                            }
                        ],
                    }
                )
            return type(
                "Result",
                (),
                {"returncode": 0, "stdout": stdout, "stderr": ""},
            )()

        config = {
            "KUBE_PROXY_IMAGE": "proxy@sha256:" + "a" * 64,
            "VERIFY_IMAGE": "verify@sha256:" + "b" * 64,
        }
        with (
            patch("scripts.lib.addons._tenant_kubectl", side_effect=kubectl),
            patch("scripts.lib.addons.time.time_ns", return_value=123),
        ):
            with self.assertRaisesRegex(RuntimeError, "probe failed"):
                verify_network(Path("."), config, self.tenant)
        self.assertTrue(
            any(
                "delete" in call and "pod/network-smoke-123" in call
                for call in calls
            )
        )

    def test_network_probe_cleanup_inspection_failure_is_fatal(self) -> None:
        result = lambda stdout="", returncode=0, stderr="": type(
            "Result",
            (),
            {
                "returncode": returncode,
                "stdout": stdout,
                "stderr": stderr,
            },
        )()
        responses = [
            result(json.dumps({"data": {"config.conf": "maxPerCore: 0"}})),
            result("proxy@sha256:" + "a" * 64),
            result(
                json.dumps(
                    {
                        "roleRef": {
                            "apiGroup": "rbac.authorization.k8s.io",
                            "kind": "ClusterRole",
                            "name": "system:node-proxier",
                        },
                        "subjects": [
                            {
                                "kind": "ServiceAccount",
                                "name": "capi-kube-proxy",
                                "namespace": "kube-system",
                            }
                        ],
                    }
                )
            ),
            result(),
            result(),
            result(returncode=1, stderr="tenant API connection refused"),
        ]
        config = {
            "KUBE_PROXY_IMAGE": "proxy@sha256:" + "a" * 64,
            "VERIFY_IMAGE": "verify@sha256:" + "b" * 64,
        }
        with (
            patch(
                "scripts.lib.addons._tenant_kubectl",
                side_effect=responses,
            ),
            patch("scripts.lib.addons.time.time_ns", return_value=123),
        ):
            with self.assertRaises(NetworkProbeCleanupError):
                verify_network(Path("."), config, self.tenant)

    def test_strict_network_status_treats_not_found_as_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            kubeconfig = (
                root / ".runtime/tenants" / self.tenant.name / "kubeconfig"
            )
            kubeconfig.parent.mkdir(parents=True)
            kubeconfig.write_text("config")
            with patch(
                "scripts.lib.addons._tenant_kubectl",
                side_effect=RuntimeError(
                    'Error from server (NotFound): nodes "worker" not found'
                ),
            ):
                result = network_status(
                    root, {}, object(), self.tenant, strict=True
                )
        self.assertFalse(result["ready"])
        self.assertIn("NotFound", result["reason"])

    def test_addon_source_ownership_rejects_foreign_configmap(self) -> None:
        client = type(
            "Client",
            (),
            {
                "kubectl": lambda self, *args, **kwargs: type(
                    "Result",
                    (),
                    {
                        "returncode": 0,
                        "stdout": json.dumps(
                            {"metadata": {"labels": {}}}
                        ),
                        "stderr": "",
                    },
                )()
            },
        )()
        with patch(
            "scripts.lib.addons.render_resource_set",
            return_value=(Path("resource-set.json"), {"source": "a" * 64}),
        ):
            with self.assertRaisesRegex(RuntimeError, "ownership mismatch"):
                verify_addon_source_ownership(
                    Path("."), self.config, client, self.tenant
                )

    def test_addon_source_presence_is_required_for_survivor(self) -> None:
        client = type(
            "Client",
            (),
            {
                "kubectl": lambda self, *args, **kwargs: type(
                    "Result",
                    (),
                    {
                        "returncode": 1,
                        "stdout": "",
                        "stderr": 'Error from server (NotFound): item not found',
                    },
                )()
            },
        )()
        with patch(
            "scripts.lib.addons.render_resource_set",
            return_value=(Path("resource-set.json"), {"source": "a" * 64}),
        ):
            with self.assertRaisesRegex(RuntimeError, "sources are missing"):
                verify_addon_source_ownership(
                    Path("."),
                    self.config,
                    client,
                    self.tenant,
                    require_present=True,
                )

    def test_limits_are_explicit(self) -> None:
        self.assertEqual(SOURCE_LIMIT, 900 * 1024)
        self.assertEqual(REFERENCE_LIMIT, 100)

    def test_deterministic_document_splitting(self) -> None:
        content = "\n---\n".join(
            f"apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: item-{index}\n"
            f"data:\n  value: {'x' * 120}\n"
            for index in range(4)
        )
        first = package_source(
            self.config, self.tenant, "source", content, limit=500
        )
        second = package_source(
            self.config, self.tenant, "source", content, limit=500
        )
        self.assertEqual(first, second)
        self.assertGreater(len(first), 1)
        self.assertTrue(all(name.startswith("source-") for name, _, _ in first))

    def test_rejects_oversized_single_document(self) -> None:
        content = "apiVersion: v1\nkind: ConfigMap\ndata:\n  value: " + "x" * 500
        with self.assertRaises(IntegrityError):
            package_source(
                self.config, self.tenant, "source", content, limit=200
            )

    def test_rejects_101_references(self) -> None:
        with self.assertRaises(IntegrityError):
            validate_inventory({f"source-{index}": "a" * 64 for index in range(101)})

    def test_rejects_missing_or_extra_hash_coverage(self) -> None:
        resource_set = {
            "spec": {
                "resources": [
                    {"kind": "ConfigMap", "name": "source-a"},
                    {"kind": "ConfigMap", "name": "source-extra"},
                ]
            }
        }
        with self.assertRaises(IntegrityError):
            validate_resource_set_references(
                resource_set,
                {"source-a": "a" * 64, "source-b": "b" * 64},
            )

    def test_rejects_non_configmap_reference_kind(self) -> None:
        resource_set = {
            "spec": {
                "resources": [
                    {"kind": "Secret", "name": "source-a"},
                ]
            }
        }
        with self.assertRaises(IntegrityError):
            validate_resource_set_references(
                resource_set,
                {"source-a": "a" * 64},
            )

    def test_rendered_resource_set_references_every_chunk(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            calico = root / "calico.yaml"
            proxy = root / "proxy.yaml"
            calico.write_text(
                "\n---\n".join(
                    f"apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: item-{i}\n"
                    f"data:\n  value: {'x' * 500000}\n"
                    for i in range(3)
                ),
                encoding="utf-8",
            )
            proxy.write_text(
                "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: proxy\n",
                encoding="utf-8",
            )
            with patch("scripts.lib.addons.render_calico", return_value=calico), patch(
                "scripts.lib.addons.render_kube_proxy", return_value=proxy
            ):
                manifest, inventory = render_resource_set(
                    root, self.config, self.tenant
                )
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            resource_set = next(
                item
                for item in payload["items"]
                if item["kind"] == "ClusterResourceSet"
            )
            self.assertEqual(
                [item["name"] for item in resource_set["spec"]["resources"]],
                sorted(inventory),
            )

    def test_rejects_tampered_manifest_chunk_before_apply(self) -> None:
        manifest = {
            "items": [
                {
                    "kind": "ConfigMap",
                    "metadata": {"name": "source"},
                    "data": {"addons.yaml": "tampered"},
                }
            ]
        }
        with self.assertRaises(IntegrityError):
            validate_manifest_hashes(manifest, {"source": "a" * 64})

    def test_rejects_duplicate_manifest_source_names(self) -> None:
        content = "kind: ConfigMap\n"
        digest = hashlib.sha256(content.encode()).hexdigest()
        manifest = {
            "items": [
                {
                    "kind": "ConfigMap",
                    "metadata": {"name": "source"},
                    "data": {"addons.yaml": "tampered"},
                },
                {
                    "kind": "ConfigMap",
                    "metadata": {"name": "source"},
                    "data": {"addons.yaml": content},
                },
            ]
        }
        with self.assertRaises(IntegrityError):
            validate_manifest_hashes(manifest, {"source": digest})
