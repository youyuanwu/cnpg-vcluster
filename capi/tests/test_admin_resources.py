from __future__ import annotations

import json
import unittest
from pathlib import Path

from scripts import generate_admin_resources as generator


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_RBAC = {
    "local": {
        ("", ("namespaces",), ("get",)),
        (
            "bootstrap.cluster.x-k8s.io",
            ("kubeadmconfigs", "kubeadmconfigtemplates"),
            ("list",),
        ),
        (
            "cluster.x-k8s.io",
            ("clusters", "machinedeployments", "machines", "machinesets"),
            ("list",),
        ),
        (
            "controlplane.cluster.x-k8s.io",
            ("kamajicontrolplanes",),
            ("list",),
        ),
        ("coordination.k8s.io", ("leases",), ("list",)),
        (
            "infrastructure.cluster.x-k8s.io",
            ("devclusters", "devmachines", "devmachinetemplates"),
            ("list",),
        ),
        ("kamaji.clastix.io", ("tenantcontrolplanes",), ("list",)),
        (
            "tenancy.cnpg-vcluster.io",
            ("tenants",),
            ("get", "list"),
        ),
    },
    "azure": {
        (
            "",
            ("configmaps", "persistentvolumeclaims", "services"),
            ("list",),
        ),
        ("", ("namespaces",), ("get",)),
        ("apps", ("deployments", "statefulsets"), ("list",)),
        ("batch", ("jobs",), ("list",)),
        ("bootstrap.cluster.x-k8s.io", ("kubeadmconfigs",), ("list",)),
        (
            "cert-manager.io",
            ("certificaterequests", "certificates", "issuers"),
            ("list",),
        ),
        (
            "cluster.x-k8s.io",
            ("clusters", "machinepools", "machines", "machinesets"),
            ("list",),
        ),
        (
            "controlplane.cluster.x-k8s.io",
            ("kamajicontrolplanes",),
            ("list",),
        ),
        (
            "infrastructure.cluster.x-k8s.io",
            (
                "azureclusteridentities",
                "azureclusters",
                "azuremachinepoolmachines",
                "azuremachinepools",
            ),
            ("list",),
        ),
        ("kamaji.clastix.io", ("tenantcontrolplanes",), ("list",)),
        (
            "network.azure.com",
            ("natgateways", "virtualnetworks", "virtualnetworkssubnets"),
            ("list",),
        ),
        ("policy", ("poddisruptionbudgets",), ("list",)),
        (
            "rbac.authorization.k8s.io",
            ("rolebindings", "roles"),
            ("list",),
        ),
        ("resources.azure.com", ("resourcegroups",), ("list",)),
        (
            "tenancy.cnpg-vcluster.io",
            ("tenants",),
            ("get", "list"),
        ),
    },
}


def load(relative_path: str) -> dict:
    return json.loads((ROOT / relative_path).read_text(encoding="utf-8"))


class AdminResourceTests(unittest.TestCase):
    def test_generation_is_byte_stable_and_checked_in(self) -> None:
        first = generator.generated_documents()
        second = generator.generated_documents()
        self.assertEqual(first, second)
        self.assertEqual(tuple(sorted(first)), generator.OUTPUT_PATHS)
        for relative_path, expected in first.items():
            self.assertEqual(
                expected,
                (ROOT / relative_path).read_text(encoding="utf-8"),
                relative_path,
            )
            self.assertTrue(expected.endswith("\n"))
        self.assertTrue(generator.generate(ROOT, check=True))

    def test_generator_rejects_unexpected_arguments(self) -> None:
        self.assertEqual(2, generator.main(["--verify"]))
        self.assertEqual(2, generator.main(["--check", "extra"]))

    def test_service_account_binding_and_service_contract(self) -> None:
        service_account = load("admin/config/rbac/service-account.json")
        service = load("admin/config/service/service.json")

        for resource in (service_account, service):
            self.assertEqual("tenant-admin", resource["metadata"]["name"])
            self.assertEqual("tenant-system", resource["metadata"]["namespace"])
        self.assertFalse(service_account["automountServiceAccountToken"])

        for provider in generator.PROVIDERS:
            binding = load(
                f"admin/config/rbac/cluster-role-binding-{provider}.json"
            )
            self.assertEqual("tenant-admin", binding["metadata"]["name"])
            self.assertEqual(
                {
                    "apiGroup": "rbac.authorization.k8s.io",
                    "kind": "ClusterRole",
                    "name": f"tenant-admin-{provider}",
                },
                binding["roleRef"],
            )
            self.assertEqual(
                [
                    {
                        "kind": "ServiceAccount",
                        "name": "tenant-admin",
                        "namespace": "tenant-system",
                    }
                ],
                binding["subjects"],
            )

        self.assertEqual("ClusterIP", service["spec"]["type"])
        self.assertEqual(
            {"app.kubernetes.io/name": "tenant-admin"},
            service["spec"]["selector"],
        )
        self.assertEqual(
            [
                {
                    "name": "http",
                    "port": 80,
                    "targetPort": 8080,
                    "protocol": "TCP",
                }
            ],
            service["spec"]["ports"],
        )

    def test_cluster_roles_are_exact_provider_catalog_permissions(self) -> None:
        for provider in generator.PROVIDERS:
            role = load(f"admin/config/rbac/cluster-role-{provider}.json")
            self.assertEqual(
                f"tenant-admin-{provider}",
                role["metadata"]["name"],
            )
            actual = set()
            for rule in role["rules"]:
                self.assertNotIn("nonResourceURLs", rule)
                self.assertEqual(1, len(rule["apiGroups"]))
                group = rule["apiGroups"][0]
                self.assertNotEqual("*", group)
                resources = tuple(rule["resources"])
                verbs = tuple(rule["verbs"])
                self.assertNotIn("*", resources)
                self.assertNotIn("secrets", resources)
                self.assertTrue(
                    all("/" not in resource for resource in resources)
                )
                self.assertTrue(set(verbs).issubset({"get", "list"}))
                actual.add((group, resources, verbs))
            self.assertEqual(EXPECTED_RBAC[provider], actual)
            self.assertEqual(
                tuple(sorted(EXPECTED_RBAC[provider])),
                generator.provider_rules(ROOT, provider),
            )

    def test_deployments_are_provider_specific_and_hardened(self) -> None:
        images = set()
        for provider in ("local", "azure"):
            deployment = load(
                f"admin/config/deployment/deployment-{provider}.json.tpl"
            )
            self.assertEqual("tenant-admin", deployment["metadata"]["name"])
            self.assertEqual("tenant-system", deployment["metadata"]["namespace"])
            spec = deployment["spec"]
            self.assertEqual(1, spec["replicas"])
            self.assertEqual(
                {"type": "Recreate"},
                spec["strategy"],
            )

            pod = spec["template"]["spec"]
            self.assertEqual("tenant-admin", pod["serviceAccountName"])
            self.assertTrue(pod["automountServiceAccountToken"])
            self.assertNotIn("volumes", pod)
            self.assertEqual(
                {
                    "runAsNonRoot": True,
                    "runAsUser": 65532,
                    "runAsGroup": 65532,
                    "seccompProfile": {"type": "RuntimeDefault"},
                },
                pod["securityContext"],
            )

            self.assertEqual(1, len(pod["containers"]))
            container = pod["containers"][0]
            images.add(container["image"])
            self.assertEqual(
                [{"name": "TENANT_ADMIN_PROVIDER", "value": provider}],
                container["env"],
            )
            self.assertNotIn("envFrom", container)
            self.assertNotIn("volumeMounts", container)
            self.assertEqual(
                [{"name": "http", "containerPort": 8080, "protocol": "TCP"}],
                container["ports"],
            )
            self.assertEqual(
                ("/healthz", "http"),
                (
                    container["livenessProbe"]["httpGet"]["path"],
                    container["livenessProbe"]["httpGet"]["port"],
                ),
            )
            self.assertEqual(
                ("/readyz", "http"),
                (
                    container["readinessProbe"]["httpGet"]["path"],
                    container["readinessProbe"]["httpGet"]["port"],
                ),
            )
            self.assertEqual(
                {
                    "runAsNonRoot": True,
                    "privileged": False,
                    "allowPrivilegeEscalation": False,
                    "readOnlyRootFilesystem": True,
                    "capabilities": {"drop": ["ALL"]},
                },
                container["securityContext"],
            )
            self.assertEqual(
                {"cpu": "25m", "memory": "32Mi"},
                container["resources"]["requests"],
            )
            self.assertEqual(
                {"cpu": "250m", "memory": "128Mi"},
                container["resources"]["limits"],
            )
        self.assertEqual({"${TENANT_ADMIN_IMAGE}"}, images)


if __name__ == "__main__":
    unittest.main()
