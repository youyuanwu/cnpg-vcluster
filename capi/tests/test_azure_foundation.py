from __future__ import annotations

import copy
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

import yaml

from scripts.azure import _run_profile_mutation
from scripts.lib.azure.common import (
    _foundation_defaults_checksum,
    load_azure_configuration,
    tenant_names,
)
from scripts.lib.azure.foundation import (
    ACR_PULL_ROLE_DEFINITION_ID,
    CAPI_CAPZ_DEPLOYMENTS,
    TENANT_CONTROLLER_CONFIG,
    TENANT_CONTROLLER_CONFIG_KEY,
    _azure_provider_configuration,
    _foundation_identity,
    _inspect_admin,
    _install_capi_capz,
    _install_admin,
    _inspect_foundation,
    _push_admin_image,
    _push_controller_image,
    create_management,
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
    def test_existing_complete_capi_stack_skips_clusterctl_init(self) -> None:
        root = self.make_root()
        config = load_azure_configuration(root)
        inventory = self.inventory(root, config)
        with (
            patch(
                "scripts.lib.azure.foundation._get_management_resource",
                return_value={"metadata": {"uid": "existing"}},
            ),
            patch("scripts.lib.azure.foundation.run") as run_command,
            patch("scripts.lib.azure.foundation._patch_capz_identity"),
            patch(
                "scripts.lib.azure.foundation._configure_capz_external_control_plane_webhook"
            ),
            patch("scripts.lib.azure.foundation._kubectl"),
        ):
            _install_capi_capz(root, config, inventory)
        run_command.assert_not_called()

    def test_partial_capi_stack_is_rejected(self) -> None:
        root = self.make_root()
        config = load_azure_configuration(root)
        inventory = self.inventory(root, config)
        observed = iter(
            [{"metadata": {"uid": "existing"}}, None]
            + [{"metadata": {"uid": "existing"}}] * (len(CAPI_CAPZ_DEPLOYMENTS) - 2)
        )
        with patch(
            "scripts.lib.azure.foundation._get_management_resource",
            side_effect=lambda *_: next(observed),
        ):
            with self.assertRaisesRegex(RuntimeError, "installation is incomplete"):
                _install_capi_capz(root, config, inventory)

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
        self.assertEqual(config["AZURE_ADMIN_REPOSITORY"], "tenant-admin")
        self.assertEqual(config["AZURE_ADMIN_TAG"], "v1alpha1")
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
                return_value=completed(f"v1alpha2: digest: {digest} size: 123\n"),
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
    def test_controller_push_rejects_mutable_tag_digest_race(self):
        root = self.make_root()
        config = load_azure_configuration(root)
        pushed = "sha256:" + "a" * 64
        raced = "sha256:" + "b" * 64
        with (
            patch("scripts.lib.azure.foundation.load_configuration", return_value={}),
            patch("scripts.lib.azure.foundation.build_azure_controller_image"),
            patch(
                "scripts.lib.azure.foundation.run",
                return_value=completed(f"digest: {pushed} size: 123\n"),
            ),
            patch(
                "scripts.lib.azure.foundation._az",
                side_effect=[completed(), completed(raced + "\n")],
            ),
            self.assertRaisesRegex(RuntimeError, "changed before deployment"),
        ):
            _push_controller_image(root, config, self.inventory(root, config))
    def test_controller_push_uses_unique_tag_when_push_digest_is_ambiguous(self):
        root = self.make_root()
        config = load_azure_configuration(root)
        digest = "sha256:" + "c" * 64
        with (
            patch("scripts.lib.azure.foundation.load_configuration", return_value={}),
            patch("scripts.lib.azure.foundation.build_azure_controller_image"),
            patch(
                "scripts.lib.azure.foundation.uuid.uuid4",
                return_value=type("Uuid", (), {"hex": "operation123"})(),
            ),
            patch(
                "scripts.lib.azure.foundation.run",
                side_effect=[
                    completed("push output without one digest"),
                    completed(),
                    completed("still ambiguous"),
                ],
            ) as run_command,
            patch(
                "scripts.lib.azure.foundation._az",
                side_effect=[completed(), completed(digest + "\n")],
            ) as az,
        ):
            image = _push_controller_image(
                root,
                config,
                self.inventory(root, config),
            )
        unique = "yycvacr.azurecr.io/tenant-controller:publish-operation123"
        self.assertEqual(
            run_command.call_args_list[1].args[0],
            [
                "docker",
                "tag",
                "yycvacr.azurecr.io/tenant-controller:v1alpha2",
                unique,
            ],
        )
        self.assertEqual(
            run_command.call_args_list[2].args[0],
            ["docker", "push", unique],
        )
        self.assertIn(
            "tenant-controller:publish-operation123",
            az.call_args_list[1].args,
        )
        self.assertEqual(
            image,
            f"yycvacr.azurecr.io/tenant-controller@{digest}",
        )
    def test_admin_push_builds_tags_pushes_and_verifies_digest(self):
        root = self.make_root()
        config = load_azure_configuration(root)
        inventory = self.inventory(root, config)
        digest = "sha256:" + "d" * 64
        admin_config = {"COMMAND_TIMEOUT": "1s"}
        with (
            patch(
                "scripts.lib.azure.foundation.load_configuration",
                return_value=admin_config,
            ),
            patch(
                "scripts.lib.azure.foundation.build_admin_image"
            ) as build,
            patch(
                "scripts.lib.azure.foundation.run",
                return_value=completed(f"digest: {digest} size: 123\n"),
            ) as run_command,
            patch(
                "scripts.lib.azure.foundation._az",
                side_effect=[completed(), completed(digest + "\n")],
            ),
        ):
            image = _push_admin_image(root, config, inventory)
        tagged = "yycvacr.azurecr.io/tenant-admin:v1alpha1"
        build.assert_called_once_with(root, admin_config, tagged)
        self.assertEqual(
            run_command.call_args.args[0],
            ["docker", "push", tagged],
        )
        self.assertEqual(
            image,
            f"yycvacr.azurecr.io/tenant-admin@{digest}",
        )

    def test_admin_push_rejects_digest_mismatch_and_uses_unique_fallback(self):
        root = self.make_root()
        config = load_azure_configuration(root)
        inventory = self.inventory(root, config)
        pushed = "sha256:" + "e" * 64
        raced = "sha256:" + "f" * 64
        with (
            patch("scripts.lib.azure.foundation.load_configuration", return_value={}),
            patch("scripts.lib.azure.foundation.build_admin_image"),
            patch(
                "scripts.lib.azure.foundation.run",
                return_value=completed(f"digest: {pushed} size: 123\n"),
            ),
            patch(
                "scripts.lib.azure.foundation._az",
                side_effect=[completed(), completed(raced + "\n")],
            ),
            self.assertRaisesRegex(RuntimeError, "changed before deployment"),
        ):
            _push_admin_image(root, config, inventory)

        digest = "sha256:" + "9" * 64
        with (
            patch("scripts.lib.azure.foundation.load_configuration", return_value={}),
            patch("scripts.lib.azure.foundation.build_admin_image"),
            patch(
                "scripts.lib.azure.foundation.uuid.uuid4",
                return_value=type("Uuid", (), {"hex": "admin123"})(),
            ),
            patch(
                "scripts.lib.azure.foundation.run",
                side_effect=[
                    completed("ambiguous"),
                    completed(),
                    completed(f"digest: {digest} size: 123\n"),
                ],
            ) as run_command,
            patch(
                "scripts.lib.azure.foundation._az",
                side_effect=[completed(), completed(digest + "\n")],
            ),
        ):
            image = _push_admin_image(root, config, inventory)
        unique = "yycvacr.azurecr.io/tenant-admin:publish-admin123"
        self.assertEqual(
            run_command.call_args_list[1].args[0],
            [
                "docker",
                "tag",
                "yycvacr.azurecr.io/tenant-admin:v1alpha1",
                unique,
            ],
        )
        self.assertEqual(
            run_command.call_args_list[2].args[0],
            ["docker", "push", unique],
        )
        self.assertEqual(
            image,
            f"yycvacr.azurecr.io/tenant-admin@{digest}",
        )
    def test_admin_install_applies_generated_resources_then_exact_deployment(self):
        root = self.make_root()
        config = load_azure_configuration(root)
        inventory = self.inventory(root, config)
        image = "yycvacr.azurecr.io/tenant-admin@sha256:" + "7" * 64
        rendered = root / ".runtime/rendered/azure-admin/deployment.yaml"
        with (
            patch(
                "scripts.lib.azure.foundation._push_admin_image",
                return_value=image,
            ),
            patch(
                "scripts.lib.azure.foundation.render_azure_admin_deployment",
                return_value=rendered,
            ) as render,
            patch(
                "scripts.lib.azure.foundation._inspect_admin",
                return_value=("admin-uid", ()),
            ) as inspect,
            patch("scripts.lib.azure.foundation._kubectl") as kubectl,
        ):
            installed = _install_admin(root, config, inventory)
        self.assertEqual((image, "admin-uid"), installed)
        render.assert_called_once_with(root, image)
        applied = [
            call.args[call.args.index("-f") + 1]
            for call in kubectl.call_args_list
            if "apply" in call.args
        ]
        self.assertEqual(
            [
                str(root / "admin/config/rbac/service-account.yaml"),
                str(root / "admin/config/rbac/cluster-role-azure.yaml"),
                str(
                    root
                    / "admin/config/rbac/cluster-role-binding-azure.yaml"
                ),
                str(root / "admin/config/service/service.yaml"),
                str(rendered),
            ],
            applied,
        )
        self.assertIn("rollout", kubectl.call_args_list[-1].args)
        inspect.assert_called_once_with(
            root,
            image,
            None,
            verify_api=True,
        )
        recorded = dict(inventory)
        recorded["adminImage"] = (
            "yycvacr.azurecr.io/tenant-admin@sha256:" + "6" * 64
        )
        recorded["adminDeploymentUid"] = "admin-uid"
        with (
            patch(
                "scripts.lib.azure.foundation._push_admin_image",
                return_value=image,
            ),
            patch(
                "scripts.lib.azure.foundation.render_azure_admin_deployment"
            ) as render,
            patch("scripts.lib.azure.foundation._kubectl") as kubectl,
            self.assertRaisesRegex(RuntimeError, "image identity changed"),
        ):
            _install_admin(root, config, recorded)
        render.assert_not_called()
        kubectl.assert_not_called()

    def test_admin_live_health_requires_identity_image_service_and_api(self):
        root = ROOT
        image = "yycvacr.azurecr.io/tenant-admin@sha256:" + "8" * 64
        deployment = {
            "metadata": {"uid": "admin-uid", "generation": 4},
            "spec": {
                "replicas": 1,
                "strategy": {"type": "Recreate"},
                "template": {
                    "spec": {
                        "serviceAccountName": "tenant-admin",
                        "automountServiceAccountToken": True,
                        "enableServiceLinks": False,
                        "securityContext": {
                            "runAsNonRoot": True,
                            "runAsUser": 65532,
                            "runAsGroup": 65532,
                            "seccompProfile": {"type": "RuntimeDefault"},
                        },
                        "containers": [{
                            "name": "admin",
                            "image": image,
                            "imagePullPolicy": "IfNotPresent",
                            "env": [{
                                "name": "TENANT_ADMIN_PROVIDER",
                                "value": "azure",
                            }],
                            "ports": [{
                                "name": "http",
                                "containerPort": 8080,
                                "protocol": "TCP",
                            }],
                            "livenessProbe": {
                                "httpGet": {"path": "/healthz", "port": "http"}
                            },
                            "readinessProbe": {
                                "httpGet": {"path": "/readyz", "port": "http"}
                            },
                            "securityContext": {
                                "runAsNonRoot": True,
                                "privileged": False,
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "resources": {
                                "requests": {"cpu": "25m", "memory": "32Mi"},
                                "limits": {"cpu": "250m", "memory": "128Mi"},
                            },
                        }],
                    }
                },
            },
            "status": {
                "availableReplicas": 1,
                "updatedReplicas": 1,
                "observedGeneration": 4,
            },
        }
        service = {
            "spec": {
                "type": "ClusterIP",
                "selector": {"app.kubernetes.io/name": "tenant-admin"},
                "ports": [{
                    "name": "http",
                    "port": 80,
                    "targetPort": 8080,
                    "protocol": "TCP",
                }],
            }
        }
        service_account = yaml.safe_load(
            (ROOT / "admin/config/rbac/service-account.yaml").read_text()
        )
        role = yaml.safe_load(
            (ROOT / "admin/config/rbac/cluster-role-azure.yaml").read_text()
        )
        binding = yaml.safe_load(
            (
                ROOT / "admin/config/rbac/cluster-role-binding-azure.yaml"
            ).read_text()
        )
        rules_review = {
            "apiVersion": "authorization.k8s.io/v1",
            "kind": "SelfSubjectRulesReview",
            "status": {
                "resourceRules": [
                    *copy.deepcopy(role["rules"]),
                    {
                        "apiGroups": ["authorization.k8s.io"],
                        "resources": [
                            "selfsubjectaccessreviews",
                            "selfsubjectrulesreviews",
                        ],
                        "verbs": ["create"],
                    },
                    {
                        "apiGroups": ["authentication.k8s.io"],
                        "resources": ["selfsubjectreviews"],
                        "verbs": ["create"],
                    },
                ],
                "nonResourceRules": [{
                    "verbs": ["get"],
                    "nonResourceURLs": [
                        "/api",
                        "/apis",
                        "/healthz",
                        "/readyz",
                        "/version",
                    ],
                }],
                "incomplete": False,
            },
        }

        def inspect(
            *,
            resource_overrides=None,
            review_mutator=None,
            api_overrides=None,
        ):
            resources = {
                "deployment/tenant-admin": deployment,
                "service/tenant-admin": service,
                "serviceaccount/tenant-admin": service_account,
                "clusterrole/tenant-admin-azure": role,
                "clusterrolebinding/tenant-admin": binding,
            }
            resources.update(resource_overrides or {})
            review = copy.deepcopy(rules_review)
            if review_mutator is not None:
                review_mutator(review)
            api = dict(api_overrides or {})

            def get_resource(_root, _namespace, selected):
                return copy.deepcopy(resources[selected])

            def kubectl(_root, *arguments, **_kwargs):
                if arguments[0] == "create" and "-f" in arguments:
                    return completed(json.dumps(review))
                path = arguments[-1]
                for suffix, payload in api.items():
                    if path.endswith(suffix):
                        return completed(json.dumps(payload))
                if path.endswith("/api/v1/overview"):
                    return completed(json.dumps({
                        "schemaVersion": 1,
                        "data": {
                            "overview": {
                                "providerMode": "azure",
                                "tenants": {"total": 0},
                            },
                            "tenants": [],
                        },
                    }))
                if path.endswith("/api/v1/tenants"):
                    return completed(
                        json.dumps({"schemaVersion": 1, "data": []})
                    )
                return completed()

            with (
                patch(
                    "scripts.lib.azure.foundation._get_management_resource",
                    side_effect=get_resource,
                ),
                patch(
                    "scripts.lib.azure.foundation._kubectl",
                    side_effect=kubectl,
                ) as kubectl_mock,
            ):
                result = _inspect_admin(
                    root,
                    image,
                    "admin-uid",
                    verify_api=True,
                )
            return result, kubectl_mock.call_args_list

        (uid, blockers), calls = inspect()
        self.assertEqual("admin-uid", uid)
        self.assertEqual((), blockers)
        proxy_paths = [
            call.args[-1]
            for call in calls
            if "--raw" in call.args
        ]
        for endpoint in (
            "/healthz",
            "/readyz",
            "/api/v1/overview",
            "/api/v1/tenants",
        ):
            self.assertTrue(
                any(path.endswith(endpoint) for path in proxy_paths),
                endpoint,
            )
        review_calls = [
            call.args[1:]
            for call in calls
            if call.args[1:]
            and call.args[1] == "create"
            and "-f" in call.args[1:]
        ]
        self.assertEqual(1, len(review_calls))
        self.assertIn(
            "--as=system:serviceaccount:tenant-system:tenant-admin",
            review_calls[0],
        )
        overview_summary = {"name": "tenant-a", "classification": "ready"}
        snapshot_topology = {
            "tenantName": "tenant-a",
            "provider": "azure",
            "nodes": [{"id": "tenant:tenant-a", "health": "ready"}],
            "edges": [],
        }
        api_overrides = {
            "/api/v1/overview": {
                "schemaVersion": 1,
                "data": {
                    "overview": {
                        "providerMode": "azure",
                        "tenants": {"total": 1},
                    },
                    "tenants": [overview_summary],
                },
            },
            "/api/v1/tenants": {
                "schemaVersion": 1,
                "data": [{
                    "name": "tenant-a",
                    "classification": "progressing",
                }],
            },
            "/api/v1/tenants/tenant-a": {
                "schemaVersion": 1,
                "data": {
                    "identity": {
                        "uid": "tenant-uid",
                        "generation": 2,
                        "observedGeneration": 1,
                    },
                    "detail": {
                        "summary": overview_summary,
                        "uid": "tenant-uid",
                        "generation": 2,
                        "observedGeneration": 1,
                    },
                    "topology": snapshot_topology,
                },
            },
            "/api/v1/tenants/tenant-a/topology": {
                "schemaVersion": 1,
                "data": {
                    "tenantName": "tenant-a",
                    "provider": "azure",
                    "nodes": [{
                        "id": "tenant:tenant-a",
                        "health": "progressing",
                    }],
                    "edges": [],
                },
            },
        }
        (_, blockers), transition_calls = inspect(
            api_overrides=api_overrides
        )
        self.assertEqual((), blockers)
        transition_paths = [
            call.args[-1]
            for call in transition_calls
            if "--raw" in call.args
        ]
        self.assertTrue(
            any(
                path.endswith("/api/v1/tenants/tenant-a/topology")
                for path in transition_paths
            )
        )

        drifted = copy.deepcopy(deployment)
        drifted["spec"]["template"]["spec"]["containers"][0]["image"] = "old:image"
        (_, blockers), _ = inspect(
            resource_overrides={"deployment/tenant-admin": drifted}
        )
        self.assertIn("Azure admin image identity changed", blockers)

        broken_service = copy.deepcopy(service)
        broken_service["spec"]["ports"][0]["port"] = 443
        (_, blockers), _ = inspect(
            resource_overrides={"service/tenant-admin": broken_service}
        )
        self.assertIn("Azure admin Service port changed", blockers)

        misbound = copy.deepcopy(binding)
        misbound["roleRef"]["name"] = "foreign-role"
        extra_subject = copy.deepcopy(binding)
        extra_subject["subjects"].append(
            {
                "kind": "ServiceAccount",
                "name": "foreign",
                "namespace": "tenant-system",
            }
        )
        wrong_service_account_namespace = copy.deepcopy(service_account)
        wrong_service_account_namespace["metadata"]["namespace"] = "default"
        token_enabled_service_account = copy.deepcopy(service_account)
        token_enabled_service_account["automountServiceAccountToken"] = True
        wrong_role_name = copy.deepcopy(role)
        wrong_role_name["metadata"]["name"] = "foreign-role"
        removed_rule = copy.deepcopy(role)
        removed_rule["rules"].pop()
        extra_read_rule = copy.deepcopy(role)
        extra_read_rule["rules"].append(
            {
                "apiGroups": [""],
                "resources": ["pods"],
                "verbs": ["get", "list"],
            }
        )
        for name, overrides, expected in (
            (
                "missing-service-account",
                {"serviceaccount/tenant-admin": None},
                "Azure admin ServiceAccount is absent",
            ),
            (
                "missing-role",
                {"clusterrole/tenant-admin-azure": None},
                "Azure admin ClusterRole is absent",
            ),
            (
                "missing-binding",
                {"clusterrolebinding/tenant-admin": None},
                "Azure admin ClusterRoleBinding is absent",
            ),
            (
                "misbound",
                {"clusterrolebinding/tenant-admin": misbound},
                "Azure admin ClusterRoleBinding contract changed",
            ),
            (
                "extra-subject",
                {"clusterrolebinding/tenant-admin": extra_subject},
                "Azure admin ClusterRoleBinding contract changed",
            ),
            (
                "wrong-service-account-namespace",
                {
                    "serviceaccount/tenant-admin": (
                        wrong_service_account_namespace
                    )
                },
                "Azure admin ServiceAccount contract changed",
            ),
            (
                "service-account-token-enabled",
                {
                    "serviceaccount/tenant-admin": token_enabled_service_account
                },
                "Azure admin ServiceAccount contract changed",
            ),
            (
                "wrong-role-name",
                {"clusterrole/tenant-admin-azure": wrong_role_name},
                "Azure admin ClusterRole contract changed",
            ),
            (
                "removed-rule",
                {"clusterrole/tenant-admin-azure": removed_rule},
                "Azure admin ClusterRole contract changed",
            ),
            (
                "extra-read-rule",
                {"clusterrole/tenant-admin-azure": extra_read_rule},
                "Azure admin ClusterRole contract changed",
            ),
        ):
            with self.subTest(name=name):
                (_, blockers), _ = inspect(resource_overrides=overrides)
                self.assertIn(expected, blockers)

        def add_effective_rule(rule):
            return lambda review: review["status"]["resourceRules"].append(rule)

        def remove_expected_rule(review):
            review["status"]["resourceRules"].pop(0)

        for name, mutate in (
            (
                "extra-binding-create-pods",
                add_effective_rule({
                    "apiGroups": [""],
                    "resources": ["pods"],
                    "verbs": ["create"],
                }),
            ),
            (
                "extra-binding-patch-deployments",
                add_effective_rule({
                    "apiGroups": ["apps"],
                    "resources": ["deployments"],
                    "verbs": ["patch"],
                }),
            ),
            (
                "group-contributed-right",
                add_effective_rule({
                    "apiGroups": [""],
                    "resources": ["pods"],
                    "verbs": ["list"],
                }),
            ),
            (
                "missing-expected-permission",
                remove_expected_rule,
            ),
        ):
            with self.subTest(name=name):
                (_, blockers), _ = inspect(review_mutator=mutate)
                self.assertTrue(
                    any("effective RBAC" in blocker for blocker in blockers),
                    blockers,
                )

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
    def test_rejects_invalid_controller_or_admin_repository_and_tag(self):
        for key, value in (
            ("AZURE_CONTROLLER_REPOSITORY", "Upper/Repo"),
            ("AZURE_CONTROLLER_TAG", "bad tag"),
            ("AZURE_ADMIN_REPOSITORY", "Upper/Admin"),
            ("AZURE_ADMIN_TAG", "bad tag"),
        ):
            root = self.make_root()
            defaults = root / "config" / "azure" / "defaults.env"
            original = {
                "AZURE_CONTROLLER_REPOSITORY": "tenant-controller",
                "AZURE_CONTROLLER_TAG": "v1alpha2",
                "AZURE_ADMIN_REPOSITORY": "tenant-admin",
                "AZURE_ADMIN_TAG": "v1alpha1",
            }[key]
            defaults.write_text(
                defaults.read_text().replace(
                    f"{key}={original}",
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
        changed["AZURE_ADMIN_REPOSITORY"] = "different-admin"
        changed["AZURE_ADMIN_TAG"] = "different-tag"
        self.assertEqual(baseline, _foundation_defaults_checksum(root, changed))
        changed["AZURE_AKS_NODE_COUNT"] = "3"
        self.assertNotEqual(baseline, _foundation_defaults_checksum(root, changed))
    def test_schema_three_inventory_accepts_optional_admin_identity_as_a_pair(self):
        root = self.make_root()
        config = load_azure_configuration(root)
        pre_ui = self.inventory(root, config)
        self.write_inventory(root, pre_ui)
        self.assertNotIn("adminImage", load_inventory(root, config))

        image = (
            "yycvacr.azurecr.io/tenant-admin@sha256:" + "a" * 64
        )
        installed = dict(pre_ui)
        installed["adminImage"] = image
        installed["adminDeploymentUid"] = "admin-uid"
        self.assertEqual(
            _azure_provider_configuration(config, pre_ui),
            _azure_provider_configuration(config, installed),
        )
        self.write_inventory(root, installed)
        loaded = load_inventory(root, config)
        self.assertEqual(image, loaded["adminImage"])
        identity = _foundation_identity(loaded)
        self.assertEqual(image, identity["adminImage"])
        self.assertEqual("admin-uid", identity["adminDeploymentUid"])

        for missing in ("adminImage", "adminDeploymentUid"):
            broken = dict(installed)
            broken.pop(missing)
            self.write_inventory(root, broken)
            with self.subTest(missing=missing), self.assertRaisesRegex(
                RuntimeError, "recorded together"
            ):
                load_inventory(root, config)
        broken = dict(installed)
        broken["adminImage"] = "mutable:tag"
        self.write_inventory(root, broken)
        with self.assertRaisesRegex(RuntimeError, "admin image is invalid"):
            load_inventory(root, config)
        with self.assertRaisesRegex(RuntimeError, "admin inventory"):
            _foundation_identity(broken)
    def test_management_records_admin_only_after_controller_and_admin_install(self):
        root = self.make_root()
        config = load_azure_configuration(root)
        inventory = self.inventory(root, config)
        admin_image = (
            "yycvacr.azurecr.io/tenant-admin@sha256:" + "b" * 64
        )
        calls = []
        written = []
        with (
            patch("scripts.lib.azure.foundation.preflight"),
            patch(
                "scripts.lib.azure.foundation.load_inventory",
                return_value=inventory,
            ),
            patch("scripts.lib.azure.foundation._kubectl", return_value=completed()),
            patch(
                "scripts.lib.azure.foundation._verify_recorded_admin",
                side_effect=lambda *_: calls.append("verify-recorded"),
            ),
            patch(
                "scripts.lib.azure.foundation._install_capi_capz",
                side_effect=lambda *_: calls.append("capi"),
            ),
            patch(
                "scripts.lib.azure.foundation._install_kamaji",
                side_effect=lambda *_: calls.append("kamaji"),
            ),
            patch(
                "scripts.lib.azure.foundation._install_kamaji_provider",
                side_effect=lambda *_: calls.append("provider"),
            ),
            patch(
                "scripts.lib.azure.foundation._install_tenant_controller",
                side_effect=lambda *_: (
                    calls.append("controller")
                    or (inventory["controllerImage"], "provider-config-uid")
                ),
            ),
            patch(
                "scripts.lib.azure.foundation._install_admin",
                side_effect=lambda *_: (
                    calls.append("admin")
                    or (admin_image, "admin-deployment-uid")
                ),
            ),
            patch(
                "scripts.lib.azure.foundation._controller_identities",
                return_value=inventory["controllers"],
            ),
            patch(
                "scripts.lib.azure.foundation._write_inventory",
                side_effect=lambda _root, payload: written.append(dict(payload)),
            ),
            redirect_stdout(io.StringIO()),
        ):
            create_management(root, config)
        self.assertLess(calls.index("controller"), calls.index("admin"))
        self.assertEqual(1, len(written))
        self.assertEqual(admin_image, written[0]["adminImage"])
        self.assertEqual(
            "admin-deployment-uid",
            written[0]["adminDeploymentUid"],
        )
        self.assertEqual(
            inventory["foundationDefaultsSha256"],
            written[0]["foundationDefaultsSha256"],
        )

    def test_repeated_management_install_accepts_exact_admin_and_rejects_drift(self):
        root = self.make_root()
        config = load_azure_configuration(root)
        inventory = self.inventory(root, config)
        inventory["adminImage"] = (
            "yycvacr.azurecr.io/tenant-admin@sha256:" + "c" * 64
        )
        inventory["adminDeploymentUid"] = "admin-uid"
        with (
            patch("scripts.lib.azure.foundation.preflight"),
            patch(
                "scripts.lib.azure.foundation.load_inventory",
                return_value=inventory,
            ),
            patch("scripts.lib.azure.foundation._kubectl", return_value=completed()),
            patch("scripts.lib.azure.foundation._verify_recorded_admin") as verify,
            patch("scripts.lib.azure.foundation._install_capi_capz"),
            patch("scripts.lib.azure.foundation._install_kamaji"),
            patch("scripts.lib.azure.foundation._install_kamaji_provider"),
            patch(
                "scripts.lib.azure.foundation._install_tenant_controller",
                return_value=(
                    inventory["controllerImage"],
                    inventory["azureProviderConfigUid"],
                ),
            ),
            patch(
                "scripts.lib.azure.foundation._install_admin",
                return_value=(
                    inventory["adminImage"],
                    inventory["adminDeploymentUid"],
                ),
            ),
            patch(
                "scripts.lib.azure.foundation._controller_identities",
                return_value=inventory["controllers"],
            ),
            patch("scripts.lib.azure.foundation._write_inventory") as write,
            redirect_stdout(io.StringIO()),
        ):
            create_management(root, config)
        verify.assert_called_once_with(root, inventory)
        self.assertEqual(
            inventory["adminImage"],
            write.call_args.args[1]["adminImage"],
        )
        self.assertEqual(
            inventory["adminDeploymentUid"],
            write.call_args.args[1]["adminDeploymentUid"],
        )

        with (
            patch("scripts.lib.azure.foundation.preflight"),
            patch(
                "scripts.lib.azure.foundation.load_inventory",
                return_value=inventory,
            ),
            patch("scripts.lib.azure.foundation._kubectl", return_value=completed()),
            patch(
                "scripts.lib.azure.foundation._verify_recorded_admin",
                side_effect=RuntimeError("recorded Azure admin identity is unhealthy"),
            ),
            patch("scripts.lib.azure.foundation._install_capi_capz") as capi,
            self.assertRaisesRegex(RuntimeError, "admin identity is unhealthy"),
        ):
            create_management(root, config)
        capi.assert_not_called()
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
