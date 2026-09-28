from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts.azure import _run_profile_mutation
from scripts.lib.azure.common import (
    _foundation_defaults_checksum,
    load_azure_configuration,
    tenant_names,
)
from scripts.lib.azure.foundation import (
    ACR_PULL_ROLE_DEFINITION_ID,
    TENANT_CONTROLLER_CONFIG,
    TENANT_CONTROLLER_CONFIG_KEY,
    _azure_provider_configuration,
    _foundation_identity,
    _inspect_foundation,
    _push_controller_image,
    create_foundation,
    load_inventory,
    preflight,
)
from scripts.lib.config import ConfigError
from scripts.lib.files import write_private_file
from scripts.lib.locking import azure_lock
from scripts.lib.tenant_spec import TenantSpecError
from tests.azure_fixtures import AzureFixtureMixin, FOUNDATION, SUBSCRIPTION


ROOT = Path(__file__).resolve().parents[1]


def completed(stdout: str = "", returncode: int = 0):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


class AzureFoundationTests(AzureFixtureMixin, unittest.TestCase):
    def test_configuration_contains_only_foundation_and_profile_limits(self):
        root = self.make_root()
        config = load_azure_configuration(root)
        self.assertEqual(config["AZURE_PREFIX"], "yy-cv")
        for removed in (
            "AZURE_TENANT_NODE_COUNT",
            "AZURE_TENANT_POD_CIDR",
            "AZURE_TENANT_SERVICE_CIDR",
            "AZURE_TENANT_DNS_SERVICE_IP",
        ):
            self.assertNotIn(removed, config)
        self.assertEqual(
            config["AZURE_SUPPORTED_TENANT_KUBERNETES_VERSION"],
            "1.32.13",
        )
        self.assertEqual(config["AZURE_CONTROLLER_REPOSITORY"], "tenant-controller")
        self.assertEqual(config["AZURE_CONTROLLER_TAG"], "v1alpha2")
        self.assertEqual(config["AZURE_PREFIX"].replace("-", "") + "acr", "yycvacr")
    def test_bicep_defines_exact_acr_and_kubelet_pull_outputs(self):
        foundation = (ROOT / "infra" / "azure" / "foundation.bicep").read_text()
        acr_pull = (ROOT / "infra" / "azure" / "acr-pull.bicep").read_text()
        main = (ROOT / "infra" / "azure" / "main.bicep").read_text()
        for expected in (
            "Microsoft.ContainerRegistry/registries",
            "aks.properties.identityProfile.kubeletidentity.objectId",
            "output acrName string",
            "output acrId string",
            "output acrLoginServer string",
            "output acrPullRoleAssignmentId string",
        ):
            self.assertIn(expected, foundation)
        for expected in (
            "7f951dda-4ed3-4680-a7ca-43fe172d538d",
            "guid(acr.id, kubeletPrincipalId, acrPullRoleDefinitionId)",
            "principalId: kubeletPrincipalId",
            "output roleAssignmentId string",
        ):
            self.assertIn(expected, acr_pull)
        for output in (
            "aksKubeletPrincipalId",
            "acrName",
            "acrId",
            "acrLoginServer",
            "acrPullRoleAssignmentId",
        ):
            self.assertIn(f"output {output} string", main)
    def test_foundation_identity_requires_acr_role_and_controller_digest(self):
        root = self.make_root()
        config = load_azure_configuration(root)
        inventory = self.inventory(root, config)
        identity = _foundation_identity(inventory)
        self.assertEqual(identity["acrId"], inventory["outputs"]["acrId"])
        self.assertEqual(identity["controllerImage"], inventory["controllerImage"])
        for missing in ("acrId", "acrPullRoleAssignmentId"):
            broken = json.loads(json.dumps(inventory))
            broken["outputs"].pop(missing)
            with self.subTest(missing=missing), self.assertRaisesRegex(
                RuntimeError, "incomplete"
            ):
                _foundation_identity(broken)
        broken = json.loads(json.dumps(inventory))
        broken["controllerImage"] = "mutable:tag"
        with self.assertRaisesRegex(RuntimeError, "controller inventory"):
            _foundation_identity(broken)
        broken["controllerImage"] = (
            "other.azurecr.io/tenant-controller@sha256:" + "1" * 64
        )
        with self.assertRaisesRegex(RuntimeError, "controller inventory"):
            _foundation_identity(broken)
    def test_foundation_health_fails_closed_on_acr_or_pull_role_drift(self):
        root = self.make_root()
        config = load_azure_configuration(root)
        inventory = self.inventory(root, config)
        self.write_inventory(root, inventory)
        outputs = inventory["outputs"]

        def resource(namespace, name):
            key = f"{namespace}/{name}"
            containers = (
                [{
                    "name": "manager",
                    "image": inventory["controllerImage"],
                    "args": ["--provider=azure"],
                }]
                if key == "tenant-system/tenant-controller"
                else []
            )
            return {
                "metadata": {"uid": inventory["controllers"][key]},
                "spec": {
                    "replicas": 1,
                    "template": {"spec": {"containers": containers}},
                },
                "status": {"availableReplicas": 1, "updatedReplicas": 1},
            }

        def management(_root, namespace, selected):
            if selected.startswith("deployment/"):
                return resource(namespace, selected.removeprefix("deployment/"))
            if selected == f"configmap/{TENANT_CONTROLLER_CONFIG}":
                return {
                    "metadata": {"uid": inventory["azureProviderConfigUid"]},
                    "data": {
                        TENANT_CONTROLLER_CONFIG_KEY: json.dumps(
                            _azure_provider_configuration(config, inventory),
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    },
                }
            if selected.startswith("mutatingwebhookconfiguration/"):
                return {
                    "webhooks": [{
                        "name": "default.azurecluster.infrastructure.cluster.x-k8s.io",
                        "objectSelector": {
                            "matchExpressions": [{
                                "key": "cnpg-vcluster-external-control-plane",
                                "operator": "NotIn",
                                "values": ["true"],
                            }]
                        },
                    }]
                }
            raise AssertionError(selected)

        def azure(role_present=True, acr_present=True):
            def invoke(*arguments, **_kwargs):
                if arguments[:2] == ("aks", "show"):
                    return completed(json.dumps({
                        "id": outputs["aksId"],
                        "provisioningState": "Succeeded",
                        "powerState": "Running",
                        "kubernetesVersion": config["AZURE_AKS_KUBERNETES_VERSION"],
                        "nodeResourceGroup": outputs["aksNodeResourceGroup"],
                        "oidcIssuer": outputs["aksOidcIssuer"],
                    }))
                if arguments[:2] == ("acr", "show"):
                    self.assertNotIn("--ids", arguments)
                    self.assertEqual(
                        arguments[arguments.index("--name") + 1],
                        outputs["acrName"],
                    )
                    if not acr_present:
                        return completed(returncode=1)
                    return completed(json.dumps({
                        "id": outputs["acrId"],
                        "name": outputs["acrName"],
                        "loginServer": outputs["acrLoginServer"],
                        "adminUserEnabled": False,
                        "provisioningState": "Succeeded",
                    }))
                if arguments[:3] == ("role", "assignment", "list"):
                    assignments = [] if not role_present else [{
                        "id": outputs["acrPullRoleAssignmentId"],
                        "principalId": outputs["aksKubeletPrincipalId"],
                        "scope": outputs["acrId"],
                        "roleDefinitionId": (
                            "/subscriptions/redacted"
                            + ACR_PULL_ROLE_DEFINITION_ID
                        ),
                    }]
                    return completed(json.dumps(assignments))
                if "--ids" in arguments:
                    return completed(arguments[arguments.index("--ids") + 1] + "\n")
                if arguments[:2] == ("group", "show"):
                    return completed(outputs["resourceGroupId"] + "\n")
                if arguments[:3] == ("network", "vnet", "show"):
                    return completed(outputs["vnetId"] + "\n")
                if arguments[:4] == ("network", "vnet", "subnet", "show"):
                    selected = (
                        outputs["aksSubnetId"]
                        if arguments[arguments.index("--name") + 1] == "aks"
                        else outputs["tenantSubnetId"]
                    )
                    return completed(selected + "\n")
                if arguments[:2] == ("identity", "show"):
                    return completed(outputs["identityId"] + "\n")
                raise AssertionError(arguments)
            return invoke

        common = (
            patch("scripts.lib.azure.foundation._validate_foundation_networks"),
            patch(
                "scripts.lib.azure.foundation._active_subscription",
                return_value={"id": SUBSCRIPTION},
            ),
            patch(
                "scripts.lib.azure.foundation._kubectl",
                return_value=completed("ok"),
            ),
            patch(
                "scripts.lib.azure.foundation._get_management_resource",
                side_effect=management,
            ),
        )
        with common[0], common[1], common[2], common[3], patch(
            "scripts.lib.azure.foundation._az",
            side_effect=azure(),
        ):
            _, healthy, blockers = _inspect_foundation(
                root, config, require_healthy=False
            )
        self.assertTrue(healthy, blockers)
        for missing, expected in (
            ({"acr_present": False}, "container registry is absent"),
            ({"role_present": False}, "AcrPull role assignment is absent"),
        ):
            with (
                patch("scripts.lib.azure.foundation._validate_foundation_networks"),
                patch(
                    "scripts.lib.azure.foundation._active_subscription",
                    return_value={"id": SUBSCRIPTION},
                ),
                patch(
                    "scripts.lib.azure.foundation._kubectl",
                    return_value=completed("ok"),
                ),
                patch(
                    "scripts.lib.azure.foundation._get_management_resource",
                    side_effect=management,
                ),
                patch(
                    "scripts.lib.azure.foundation._az",
                    side_effect=azure(**missing),
                ),
            ):
                _, healthy, blockers = _inspect_foundation(
                    root, config, require_healthy=False
                )
            self.assertFalse(healthy)
            self.assertTrue(
                any(expected in blocker for blocker in blockers),
                blockers,
            )
    def test_controller_push_uses_acr_login_mutable_tag_and_resolved_digest(self):
        root = self.make_root()
        config = load_azure_configuration(root)
        inventory = self.inventory(root, config)
        digest = "sha256:" + "a" * 64
        with (
            patch(
                "scripts.lib.azure.foundation.load_configuration",
                return_value={"COMMAND_TIMEOUT": "1s"},
            ),
            patch(
                "scripts.lib.azure.foundation.build_azure_controller_image"
            ) as build,
            patch(
                "scripts.lib.azure.foundation.run",
                return_value=completed(),
            ) as run_command,
            patch(
                "scripts.lib.azure.foundation._az",
                side_effect=[completed(), completed(digest + "\n")],
            ) as az,
        ):
            image = _push_controller_image(root, config, inventory)
        tagged = "yycvacr.azurecr.io/tenant-controller:v1alpha2"
        build.assert_called_once_with(
            root,
            {"COMMAND_TIMEOUT": "1s"},
            tagged,
        )
        self.assertEqual(run_command.call_args.args[0], ["docker", "push", tagged])
        self.assertEqual(az.call_args_list[0].args[:3], ("acr", "login", "--name"))
        self.assertEqual(
            az.call_args_list[1].args[:4],
            ("acr", "manifest", "show-metadata", "--registry"),
        )
        self.assertEqual(image, f"yycvacr.azurecr.io/tenant-controller@{digest}")
    def test_controller_push_rejects_unresolved_manifest_digest(self):
        root = self.make_root()
        config = load_azure_configuration(root)
        with (
            patch("scripts.lib.azure.foundation.load_configuration", return_value={}),
            patch("scripts.lib.azure.foundation.build_azure_controller_image"),
            patch("scripts.lib.azure.foundation.run", return_value=completed()),
            patch(
                "scripts.lib.azure.foundation._az",
                side_effect=[completed(), completed("latest\n")],
            ),
            self.assertRaisesRegex(RuntimeError, "manifest digest"),
        ):
            _push_controller_image(root, config, self.inventory(root, config))
    def test_preflight_output_does_not_expose_subscription_id(self):
        root = self.make_root()
        config = load_azure_configuration(root)
        output = io.StringIO()
        with (
            patch(
                "scripts.lib.azure.foundation._active_subscription",
                return_value={"id": SUBSCRIPTION, "state": "Enabled"},
            ),
            patch("scripts.lib.azure.foundation._az", return_value=completed("Registered\n")),
            patch("scripts.lib.azure.foundation._sku_available"),
            patch("scripts.lib.azure.foundation._reference_image_available"),
            patch("scripts.lib.azure.foundation.run", return_value=completed()),
            redirect_stdout(output),
        ):
            result = preflight(root, config)
        self.assertEqual(result["subscriptionId"], SUBSCRIPTION)
        self.assertNotIn(SUBSCRIPTION, output.getvalue())
    def test_rejects_broad_local_parameter_permissions(self):
        root = self.make_root()
        (root / "config" / "azure.local.env").chmod(0o644)
        with self.assertRaisesRegex(ConfigError, "owner-only"):
            load_azure_configuration(root)
    def test_rejects_invalid_prefix(self):
        root = self.make_root()
        path = root / "config" / "azure.local.env"
        path.write_text(
            f"AZURE_SUBSCRIPTION_ID={SUBSCRIPTION}\n"
            "AZURE_LOCATION=westus2\nAZURE_PREFIX=YY_cv\n",
            encoding="utf-8",
        )
        path.chmod(0o600)
        with self.assertRaisesRegex(ConfigError, "AZURE_PREFIX"):
            load_azure_configuration(root)
    def test_rejects_invalid_controller_repository_and_tag(self):
        for key, value in (
            ("AZURE_CONTROLLER_REPOSITORY", "Upper/Repo"),
            ("AZURE_CONTROLLER_TAG", "bad tag"),
        ):
            root = self.make_root()
            defaults = root / "config" / "azure" / "defaults.env"
            defaults.write_text(
                defaults.read_text().replace(
                    f"{key}={'tenant-controller' if key.endswith('REPOSITORY') else 'v1alpha2'}",
                    f"{key}={value!r}",
                )
            )
            with self.subTest(key=key), self.assertRaises(ConfigError):
                load_azure_configuration(root)
    def test_rejects_pre_cutover_tenant_configuration(self):
        root = self.make_root()
        path = root / "config" / "azure.local.env"
        with path.open("a", encoding="utf-8") as output:
            output.write("AZURE_TENANT_NODE_COUNT=1\n")
        with self.assertRaisesRegex(ConfigError, "pre-cutover Azure tenant configuration"):
            load_azure_configuration(root)
    def test_azure_database_count_is_rejected(self):
        with self.assertRaisesRegex(TenantSpecError, "unknown.*databaseCount"):
            self.spec(databaseCount=1)
    def test_foundation_checksum_ignores_tenant_profile_limits(self):
        root = self.make_root()
        config = load_azure_configuration(root)
        baseline = _foundation_defaults_checksum(root, config)
        changed = dict(config)
        changed["AZURE_SUPPORTED_TENANT_KUBERNETES_VERSION"] = "9.9.9"
        changed["AZURE_TENANT_NODE_SKU"] = "different"
        changed["AZURE_TENANT_TIMEOUT"] = "1m"
        self.assertEqual(baseline, _foundation_defaults_checksum(root, changed))
        changed["AZURE_AKS_NODE_COUNT"] = "3"
        self.assertNotEqual(baseline, _foundation_defaults_checksum(root, changed))
    def test_old_foundation_inventory_requires_clean_redeploy(self):
        root = self.make_root()
        config = load_azure_configuration(root)
        old = self.inventory(root, config)
        old["schema"] = 1
        old["defaultsSha256"] = old.pop("foundationDefaultsSha256")
        self.write_inventory(root, old)
        with self.assertRaisesRegex(RuntimeError, "pre-cutover.*clean foundation redeploy"):
            load_inventory(root, config)
    def test_foundation_checksum_mismatch_requires_clean_redeploy(self):
        root = self.make_root()
        config = load_azure_configuration(root)
        payload = self.inventory(root, config)
        payload["foundationDefaultsSha256"] = "stale"
        self.write_inventory(root, payload)
        with self.assertRaisesRegex(RuntimeError, "checksum changed.*clean foundation"):
            load_inventory(root, config)
    def test_foundation_create_refuses_old_inventory_before_deployment(self):
        root = self.make_root()
        config = load_azure_configuration(root)
        old = self.inventory(root, config)
        old["schema"] = 1
        self.write_inventory(root, old)
        with (
            patch("scripts.lib.azure.foundation.preflight", return_value={}),
            patch("scripts.lib.azure.foundation._json") as deploy,
            self.assertRaisesRegex(RuntimeError, "pre-cutover"),
        ):
            create_foundation(root, config)
        deploy.assert_not_called()
    def test_tenant_names_are_tenant_keyed(self):
        spec = self.spec("blue")
        selected = tenant_names(spec)
        self.assertEqual(selected["cluster"], "blue")
        self.assertEqual(selected["pool"], "blue-worker")
        self.assertNotIn("yy-cv-tenant", json.dumps(selected))
    def test_foundation_mutation_waits_for_azure_lock(self):
        root = self.make_root()
        marker = root / "acquired"
        script = (
            "from pathlib import Path; "
            "from scripts.azure import _run_profile_mutation; "
            f"root=Path({str(root)!r}); marker=Path({str(marker)!r}); "
            "_run_profile_mutation(root, {}, "
            "lambda _root, _config: marker.write_text('yes'))"
        )
        with azure_lock(root, exclusive=True, create=True):
            process = subprocess.Popen(
                [sys.executable, "-c", script],
                cwd=Path(__file__).resolve().parents[1],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            time.sleep(0.2)
            self.assertIsNone(process.poll())
            self.assertFalse(marker.exists())
        _, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertEqual(marker.read_text(encoding="utf-8"), "yes")


if __name__ == "__main__":
    unittest.main()
