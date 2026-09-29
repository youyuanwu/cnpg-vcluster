from __future__ import annotations

import unittest
from pathlib import Path

import yaml

from scripts import generate_admin_resources as generator


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_RBAC = {
    "": {
        "configmaps",
        "namespaces",
        "persistentvolumeclaims",
        "services",
    },
    "apps": {"deployments", "statefulsets"},
    "batch": {"jobs"},
    "bootstrap.cluster.x-k8s.io": {
        "kubeadmconfigs",
        "kubeadmconfigtemplates",
    },
    "cert-manager.io": {"certificates", "certificaterequests", "issuers"},
    "cluster.x-k8s.io": {
        "clusters",
        "machinedeployments",
        "machinepools",
        "machines",
        "machinesets",
    },
    "controlplane.cluster.x-k8s.io": {"kamajicontrolplanes"},
    "coordination.k8s.io": {"leases"},
    "infrastructure.cluster.x-k8s.io": {
        "azureclusteridentities",
        "azureclusters",
        "azuremachinepoolmachines",
        "azuremachinepools",
        "devclusters",
        "devmachines",
        "devmachinetemplates",
    },
    "kamaji.clastix.io": {"tenantcontrolplanes"},
    "network.azure.com": {
        "natgateways",
        "virtualnetworkssubnets",
        "virtualnetworks",
    },
    "policy": {"poddisruptionbudgets"},
    "rbac.authorization.k8s.io": {"rolebindings", "roles"},
    "resources.azure.com": {"resourcegroups"},
    "tenancy.cnpg-vcluster.io": {"tenants"},
}


def load(relative_path: str) -> dict:
    return yaml.safe_load((ROOT / relative_path).read_text(encoding="utf-8"))


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
        service_account = load("admin/config/rbac/service-account.yaml")
        binding = load("admin/config/rbac/cluster-role-binding.yaml")
        service = load("admin/config/service/service.yaml")

        for resource in (service_account, service):
            self.assertEqual("tenant-admin", resource["metadata"]["name"])
            self.assertEqual("tenant-system", resource["metadata"]["namespace"])
        self.assertFalse(service_account["automountServiceAccountToken"])

        self.assertEqual("tenant-admin", binding["metadata"]["name"])
        self.assertEqual(
            {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "ClusterRole",
                "name": "tenant-admin",
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

    def test_cluster_role_is_exact_read_only_topology_union(self) -> None:
        role = load("admin/config/rbac/cluster-role.yaml")
        self.assertEqual("tenant-admin", role["metadata"]["name"])
        actual = {}
        for rule in role["rules"]:
            self.assertEqual(["get", "list"], rule["verbs"])
            self.assertNotIn("nonResourceURLs", rule)
            self.assertEqual(1, len(rule["apiGroups"]))
            group = rule["apiGroups"][0]
            self.assertNotEqual("*", group)
            resources = set(rule["resources"])
            self.assertNotIn("*", resources)
            self.assertNotIn("secrets", resources)
            self.assertTrue(all("/" not in resource for resource in resources))
            self.assertNotIn(group, actual)
            actual[group] = resources
        self.assertEqual(EXPECTED_RBAC, actual)

    def test_deployments_are_provider_specific_and_hardened(self) -> None:
        images = set()
        for provider in ("local", "azure"):
            deployment = load(
                f"admin/config/deployment/deployment-{provider}.yaml.tpl"
            )
            self.assertEqual("tenant-admin", deployment["metadata"]["name"])
            self.assertEqual("tenant-system", deployment["metadata"]["namespace"])
            spec = deployment["spec"]
            self.assertEqual(1, spec["replicas"])
            self.assertEqual(
                {
                    "type": "RollingUpdate",
                    "rollingUpdate": {"maxUnavailable": 0, "maxSurge": 1},
                },
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
