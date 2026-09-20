from __future__ import annotations

import io
import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts.azure import (
    AzureTenantAdapter,
    FOUNDATION_INVENTORY_SCHEMA,
    _azure_id_equal,
    _azure_tags,
    _capz_external_control_plane_webhook_ready,
    _capture_tenant_kubeconfig,
    _classify_management_owned_resources,
    _collect_ready_observations,
    _discover_owned_repeatedly,
    _exact_delete_management_resource,
    _enable_capz_external_control_plane_delete,
    _exclude_tenant_machines_from_drain,
    _foundation_defaults_checksum,
    _management_resource_specs,
    _reconcile_manifest,
    _render_addon_job,
    _render_tenant_control_plane,
    _render_worker_pool,
    _run_profile_mutation,
    _tenant_spec_blockers,
    _wait_ready_observations,
    _validate_networks,
    azure_tenant_runtime_path,
    classify_azure_owned_resources,
    create_foundation,
    discover_azure_owned_resources,
    load_azure_configuration,
    load_inventory,
    names,
    preflight,
    tenant_names,
)
from scripts.lib.config import ConfigError
from scripts.lib.files import write_private_file
from scripts.lib.locking import profile_lock
from scripts.lib.tenant_runtime import TenantRuntime, foundation_sha256
from scripts.lib.tenant_spec import TenantSpec, TenantSpecError
from scripts.lib.tenant_status import TenantStatus
from scripts.lib.tenant_timing import TenantTimings
from scripts.lib.tenants import LIFECYCLE_MARKERS, lifecycle_markers


DEFAULTS = """\
AZURE_AKS_KUBERNETES_VERSION=1.35.7
AZURE_SUPPORTED_TENANT_KUBERNETES_VERSION=1.32.13
AZURE_AKS_NODE_SKU=Standard_D4as_v5
AZURE_TENANT_NODE_SKU=Standard_B2s
AZURE_AKS_NODE_COUNT=2
AZURE_VNET_CIDR=10.220.0.0/16
AZURE_AKS_SUBNET_CIDR=10.220.0.0/20
AZURE_TENANT_SUBNET_CIDR=10.220.16.0/20
AZURE_AKS_POD_CIDR=10.221.0.0/16
AZURE_AKS_SERVICE_CIDR=10.222.0.0/16
AZURE_AKS_DNS_SERVICE_IP=10.222.0.10
AZURE_CAPI_VERSION=v1.10.7
AZURE_CAPZ_VERSION=v1.21.1
AZURE_KAMAJI_CAPI_VERSION=v0.19.0
AZURE_KAMAJI_CHART_VERSION=26.8.6-edge
AZURE_CLOUD_PROVIDER_VERSION=v1.32.3
AZURE_CALICO_VERSION=v3.32.2
AZURE_DEPLOY_TIMEOUT=30m
AZURE_CONTROLLER_TIMEOUT=15m
AZURE_TENANT_TIMEOUT=20m
"""
SUBSCRIPTION = "00000000-0000-0000-0000-000000000000"
FOUNDATION = {
    "foundationDefaultsSha256": "foundation-checksum",
    "resourceGroupId": "/subscriptions/redacted/resourceGroups/yy-cv-rg",
    "aksId": "/subscriptions/redacted/resourceGroups/yy-cv-rg/providers/Microsoft.ContainerService/managedClusters/yy-cv-mgmt",
    "aksNodeResourceGroup": "MC_yy-cv-rg_yy-cv-mgmt_westus2",
    "aksOidcIssuer": "https://example.invalid/issuer",
    "vnetId": "/subscriptions/redacted/resourceGroups/yy-cv-rg/providers/Microsoft.Network/virtualNetworks/yy-cv-vnet",
    "aksSubnetId": "/subscriptions/redacted/resourceGroups/yy-cv-rg/providers/Microsoft.Network/virtualNetworks/yy-cv-vnet/subnets/aks",
    "tenantSubnetId": "/subscriptions/redacted/resourceGroups/yy-cv-rg/providers/Microsoft.Network/virtualNetworks/yy-cv-vnet/subnets/tenant",
    "identityId": "/subscriptions/redacted/resourceGroups/yy-cv-rg/providers/Microsoft.ManagedIdentity/userAssignedIdentities/yy-cv-identity",
    "roleAssignmentId": "/subscriptions/redacted/providers/Microsoft.Authorization/roleAssignments/role",
    "aksRoleAssignmentId": "/subscriptions/redacted/providers/Microsoft.Authorization/roleAssignments/aks-role",
    "capzFederationId": "/subscriptions/redacted/resourceGroups/yy-cv-rg/providers/Microsoft.ManagedIdentity/userAssignedIdentities/yy-cv-identity/federatedIdentityCredentials/capz-manager",
    "asoFederationId": "/subscriptions/redacted/resourceGroups/yy-cv-rg/providers/Microsoft.ManagedIdentity/userAssignedIdentities/yy-cv-identity/federatedIdentityCredentials/azureserviceoperator-default",
    "controller:capi-system/capi-controller-manager": "capi-uid",
    "controller:capi-kubeadm-bootstrap-system/capi-kubeadm-bootstrap-controller-manager": "cabpk-uid",
    "controller:capz-system/capz-controller-manager": "capz-uid",
    "controller:capz-system/azureserviceoperator-controller-manager": "aso-uid",
    "controller:kamaji-system/kamaji": "kamaji-uid",
    "controller:kamaji-system/capi-kamaji-controller-manager": "provider-uid",
}


def completed(stdout: str = "", returncode: int = 0):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


class AzurePhaseFourTests(unittest.TestCase):
    def test_ready_wait_retries_until_cloud_and_network_converge(self):
        root = self.make_root()
        config = load_azure_configuration(root)
        spec = self.spec()
        ready = {"nodes": [{"name": "node-0"}]}
        with (
            patch(
                "scripts.azure._collect_ready_observations",
                side_effect=[
                    ({}, ("Node is not Ready",)),
                    ({}, ("calicoNode is not Ready",)),
                    (ready, ()),
                ],
            ) as collect,
            patch("scripts.azure.time.sleep"),
            patch(
                "scripts.azure.time.monotonic",
                side_effect=[0, 1, 2, 3],
            ),
        ):
            self.assertEqual(
                _wait_ready_observations(root, config, spec),
                ready,
            )
        self.assertEqual(collect.call_count, 3)

    def test_azure_resource_ids_are_case_insensitive(self):
        self.assertTrue(
            _azure_id_equal(
                "/subscriptions/x/resourcegroups/rg/providers/example/item",
                "/subscriptions/x/resourceGroups/rg/providers/example/item",
            )
        )
        self.assertFalse(
            _azure_id_equal(
                "/subscriptions/x/resourceGroups/a",
                "/subscriptions/x/resourceGroups/b",
            )
        )

    def make_root(self) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "config" / "azure").mkdir(parents=True)
        (root / "config" / "azure" / "defaults.env").write_text(
            DEFAULTS,
            encoding="utf-8",
        )
        local = root / "config" / "azure.local.env"
        local.write_text(
            f"AZURE_SUBSCRIPTION_ID={SUBSCRIPTION}\n"
            "AZURE_LOCATION=westus2\n"
            "AZURE_PREFIX=yy-cv\n",
            encoding="utf-8",
        )
        local.chmod(0o600)
        return root

    @staticmethod
    def spec(name: str = "tenant-c", **overrides) -> TenantSpec:
        payload = {
            "schema": 1,
            "profile": "azure",
            "name": name,
            "kubernetesVersion": "1.32.13",
            "workers": 1,
            "podCIDR": "10.72.0.0/16",
            "serviceCIDR": "10.142.0.0/16",
        }
        payload.update(overrides)
        return TenantSpec.from_mapping(
            payload,
            expected_profile="azure",
            supported_versions={"azure": "1.32.13"},
        )

    def start_journal(self, root: Path, spec: TenantSpec):
        runtime = TenantRuntime(root, "azure", spec.name)
        journal = runtime.start_operation(
            operation="create",
            spec=spec,
            foundation_identity=FOUNDATION,
            intended_resources=(f"Cluster/{spec.name}",),
            operation_id="operation-1",
        )
        journal = runtime.update_operation(
            journal,
            phase="markers-recorded",
            observed={"markerOperationId": journal.operation_id},
        )
        return runtime, journal

    def inventory(self, root: Path, config: dict[str, str]) -> dict[str, object]:
        outputs = {
            "resourceGroupName": "yy-cv-rg",
            "resourceGroupId": FOUNDATION["resourceGroupId"],
            "aksName": "yy-cv-mgmt",
            "aksId": FOUNDATION["aksId"],
            "aksNodeResourceGroup": FOUNDATION["aksNodeResourceGroup"],
            "aksOidcIssuer": FOUNDATION["aksOidcIssuer"],
            "vnetName": "yy-cv-vnet",
            "vnetId": FOUNDATION["vnetId"],
            "aksSubnetName": "aks",
            "aksSubnetId": FOUNDATION["aksSubnetId"],
            "tenantSubnetName": "tenant",
            "tenantSubnetId": FOUNDATION["tenantSubnetId"],
            "identityName": "yy-cv-identity",
            "identityId": FOUNDATION["identityId"],
            "identityClientId": "client-id",
            "tenantId": "tenant-id",
            "roleAssignmentId": FOUNDATION["roleAssignmentId"],
            "aksRoleAssignmentId": FOUNDATION["aksRoleAssignmentId"],
            "capzFederationId": FOUNDATION["capzFederationId"],
            "asoFederationId": FOUNDATION["asoFederationId"],
        }
        controllers = {
            key.removeprefix("controller:"): value
            for key, value in FOUNDATION.items()
            if key.startswith("controller:")
        }
        return {
            "schema": FOUNDATION_INVENTORY_SCHEMA,
            "subscriptionId": SUBSCRIPTION,
            "location": "westus2",
            "prefix": "yy-cv",
            "names": names(config),
            "foundationDefaultsSha256": _foundation_defaults_checksum(root, config),
            "deploymentId": "/subscriptions/redacted/providers/Microsoft.Resources/deployments/yy-cv-foundation",
            "deploymentName": "yy-cv-foundation",
            "outputs": outputs,
            "controllers": controllers,
        }

    def write_inventory(self, root: Path, payload: dict[str, object]) -> Path:
        path = root / ".runtime" / "azure" / "resources.json"
        write_private_file(path, json.dumps(payload))
        return path

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

    def test_preflight_output_does_not_expose_subscription_id(self):
        root = self.make_root()
        config = load_azure_configuration(root)
        output = io.StringIO()
        with (
            patch(
                "scripts.azure._active_subscription",
                return_value={"id": SUBSCRIPTION, "state": "Enabled"},
            ),
            patch("scripts.azure._az", return_value=completed("Registered\n")),
            patch("scripts.azure._sku_available"),
            patch("scripts.azure._reference_image_available"),
            patch("scripts.azure.run", return_value=completed()),
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
            patch("scripts.azure.preflight", return_value={}),
            patch("scripts.azure._json") as deploy,
            self.assertRaisesRegex(RuntimeError, "pre-cutover"),
        ):
            create_foundation(root, config)
        deploy.assert_not_called()

    def test_tenant_names_and_artifacts_are_tenant_keyed(self):
        root = self.make_root()
        spec = self.spec("blue")
        selected = tenant_names(spec)
        self.assertEqual(selected["cluster"], "blue")
        self.assertEqual(selected["pool"], "blue-worker")
        path = azure_tenant_runtime_path(root, "blue")
        self.assertEqual(path, root / ".runtime" / "azure" / "tenants" / "blue")
        self.assertNotIn("yy-cv-tenant", json.dumps(selected))

    def test_tenant_runtime_removal_cannot_remove_foundation_files(self):
        root = self.make_root()
        foundation = root / ".runtime" / "azure"
        tenant = azure_tenant_runtime_path(root, "blue")
        write_private_file(foundation / "resources.json", "{}")
        write_private_file(foundation / "management.kubeconfig", "foundation")
        write_private_file(tenant / "endpoint.json", "{}")
        for child in tenant.iterdir():
            child.unlink()
        tenant.rmdir()
        self.assertTrue((foundation / "resources.json").is_file())
        self.assertTrue((foundation / "management.kubeconfig").is_file())

    def test_rendered_resources_use_spec_and_all_have_markers(self):
        root = self.make_root()
        config = load_azure_configuration(root)
        spec = self.spec("blue", workers=3)
        runtime, journal = self.start_journal(root, spec)
        inventory = self.inventory(root, config)
        paths = (
            _render_tenant_control_plane(root, config, inventory, spec, journal),
            _render_worker_pool(root, config, inventory, spec, journal),
            _render_addon_job(root, config, spec, journal),
        )
        expected = lifecycle_markers(spec, journal)
        kinds = set()
        combined = ""
        for path in paths:
            payload = json.loads(path.read_text(encoding="utf-8"))
            for item in payload["items"]:
                kinds.add(item["kind"])
                annotations = item["metadata"]["annotations"]
                self.assertEqual(
                    {key: annotations[value] for key, value in LIFECYCLE_MARKERS.items()},
                    expected,
                )
                if item["kind"] == "AzureCluster":
                    self.assertEqual(
                        item["metadata"]["labels"][
                            "cnpg-vcluster-external-control-plane"
                        ],
                        "true",
                    )
                if item["kind"] == "Deployment":
                    container = item["spec"]["template"]["spec"]["containers"][0]
                    self.assertIn("--address=127.0.0.1", container["args"])
                    self.assertNotIn("--address=0.0.0.0", container["args"])
                    self.assertNotIn("ports", container)
                    self.assertEqual(
                        container["readinessProbe"]["exec"]["command"][-1],
                        "--raw=/readyz",
                    )
            combined += path.read_text(encoding="utf-8")
        self.assertIn('"replicas": 3', combined)
        self.assertIn("10.72.0.0/16", combined)
        self.assertIn("10.142.0.0/16", combined)
        self.assertIn("1.32.13", combined)
        self.assertNotIn("yy-cv-tenant", combined)
        self.assertEqual(
            kinds,
            {
                "Namespace",
                "AzureClusterIdentity",
                "Cluster",
                "AzureCluster",
                "KamajiControlPlane",
                "KubeadmConfig",
                "AzureMachinePool",
                "MachinePool",
                "ConfigMap",
                "Deployment",
                "Job",
            },
        )
        self.assertTrue(runtime.operation_exists())

    def test_network_validation_checks_every_foundation_and_recorded_range(self):
        root = self.make_root()
        config = load_azure_configuration(root)
        recorded = self.spec(
            "other",
            podCIDR="10.73.0.0/16",
            serviceCIDR="10.143.0.0/16",
        )
        cases = {
            "VNet": "10.220.32.0/24",
            "AKS Pod": "10.221.2.0/24",
            "AKS Service": "10.222.2.0/24",
            "recorded Pod": "10.73.2.0/24",
            "recorded Service": "10.143.2.0/24",
        }
        for label, pod in cases.items():
            with self.subTest(label=label):
                spec = self.spec(podCIDR=pod)
                with self.assertRaisesRegex(TenantSpecError, "overlaps"):
                    _validate_networks(config, spec, recorded_specs=(recorded,))

    def test_network_validation_supplies_all_shared_and_recorded_ranges(self):
        root = self.make_root()
        config = load_azure_configuration(root)
        spec = self.spec()
        recorded = self.spec(
            "other",
            podCIDR="10.73.0.0/16",
            serviceCIDR="10.143.0.0/16",
        )
        with patch(
            "scripts.azure.require_non_overlapping_networks"
        ) as validate:
            _validate_networks(config, spec, recorded_specs=(recorded,))
        networks = validate.call_args.args[1]
        self.assertEqual(
            set(networks),
            {
                "Azure VNet",
                "AKS subnet",
                "tenant node subnet",
                "AKS Pod CIDR",
                "AKS Service CIDR",
                "tenant other Pod CIDR",
                "tenant other Service CIDR",
            },
        )

    def test_reconcile_recovers_marked_uid_before_next_resource(self):
        root = self.make_root()
        spec = self.spec()
        runtime, journal = self.start_journal(root, spec)
        markers = lifecycle_markers(spec, journal)
        item = {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {
                "name": spec.name,
                "annotations": {
                    LIFECYCLE_MARKERS[key]: value
                    for key, value in markers.items()
                },
            },
        }
        path = azure_tenant_runtime_path(root, spec.name) / "one.json"
        write_private_file(
            path,
            json.dumps({"apiVersion": "v1", "kind": "List", "items": [item]}),
        )
        observed = {
            "metadata": {
                "uid": "namespace-uid",
                "annotations": item["metadata"]["annotations"],
            }
        }
        with (
            patch("scripts.azure._get_management_resource", return_value=observed),
            patch("scripts.azure._kubectl") as kubectl,
        ):
            updated = _reconcile_manifest(
                root,
                spec,
                runtime,
                journal,
                path,
                phase="control-plane-resources",
            )
        self.assertEqual(updated.observed["namespaceUid"], "namespace-uid")
        kubectl.assert_called_once()

    def test_reconcile_refuses_foreign_markers_before_apply(self):
        root = self.make_root()
        spec = self.spec()
        runtime, journal = self.start_journal(root, spec)
        item = {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {"name": spec.name},
        }
        path = azure_tenant_runtime_path(root, spec.name) / "one.json"
        write_private_file(
            path,
            json.dumps({"apiVersion": "v1", "kind": "List", "items": [item]}),
        )
        foreign = {
            "metadata": {
                "uid": "foreign",
                "annotations": {
                    value: "foreign" for value in LIFECYCLE_MARKERS.values()
                },
            }
        }
        with (
            patch("scripts.azure._get_management_resource", return_value=foreign),
            patch("scripts.azure._kubectl") as kubectl,
            self.assertRaisesRegex(RuntimeError, "foreign.*markers"),
        ):
            _reconcile_manifest(
                root,
                spec,
                runtime,
                journal,
                path,
                phase="control-plane-resources",
            )
        kubectl.assert_not_called()

    def test_reconcile_refuses_recorded_uid_mismatch_before_apply(self):
        root = self.make_root()
        spec = self.spec()
        runtime, journal = self.start_journal(root, spec)
        journal = runtime.update_operation(
            journal,
            phase="control-plane-resources",
            observed={"clusterUid": "recorded-uid"},
        )
        markers = lifecycle_markers(spec, journal)
        item = {
            "apiVersion": "cluster.x-k8s.io/v1beta1",
            "kind": "Cluster",
            "metadata": {
                "name": spec.name,
                "namespace": spec.namespace,
                "annotations": {
                    LIFECYCLE_MARKERS[key]: value
                    for key, value in markers.items()
                },
            },
        }
        path = azure_tenant_runtime_path(root, spec.name) / "cluster.json"
        write_private_file(
            path,
            json.dumps({"apiVersion": "v1", "kind": "List", "items": [item]}),
        )
        existing = {
            "metadata": {
                "uid": "replacement-uid",
                "annotations": item["metadata"]["annotations"],
            }
        }
        with (
            patch("scripts.azure._get_management_resource", return_value=existing),
            patch("scripts.azure._kubectl") as kubectl,
            self.assertRaisesRegex(RuntimeError, "identity changed"),
        ):
            _reconcile_manifest(
                root,
                spec,
                runtime,
                journal,
                path,
                phase="control-plane-resources",
            )
        kubectl.assert_not_called()

    def test_reconcile_recovers_real_stage_resources_after_uid_write_failure(self):
        cases = (
            ("Cluster", "tenant-c", "clusterUid", "control-plane-resources"),
            ("MachinePool", "tenant-c-worker", "machinePoolUid", "worker-resources"),
            (
                "ConfigMap",
                "tenant-c-azure-cloud-provider-values",
                "cloudValuesConfigMapUid",
                "addon-resources",
            ),
        )
        for kind, name, key, phase in cases:
            with self.subTest(kind=kind):
                root = self.make_root()
                spec = self.spec()
                runtime, journal = self.start_journal(root, spec)
                markers = lifecycle_markers(spec, journal)
                item = {
                    "apiVersion": "v1",
                    "kind": kind,
                    "metadata": {
                        "name": name,
                        "namespace": spec.namespace,
                        "annotations": {
                            LIFECYCLE_MARKERS[marker]: value
                            for marker, value in markers.items()
                        },
                    },
                }
                path = azure_tenant_runtime_path(root, spec.name) / f"{kind}.json"
                write_private_file(
                    path,
                    json.dumps(
                        {"apiVersion": "v1", "kind": "List", "items": [item]}
                    ),
                )
                observed = {
                    "metadata": {
                        "uid": f"{kind.lower()}-uid",
                        "annotations": item["metadata"]["annotations"],
                    }
                }
                original_update = runtime.update_operation
                calls = 0

                def fail_first_update(*args, **kwargs):
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        raise RuntimeError("injected UID persistence failure")
                    return original_update(*args, **kwargs)

                with (
                    patch(
                        "scripts.azure._get_management_resource",
                        side_effect=[None, observed],
                    ),
                    patch("scripts.azure._kubectl"),
                    patch.object(
                        runtime,
                        "update_operation",
                        side_effect=fail_first_update,
                    ),
                    self.assertRaisesRegex(RuntimeError, "UID persistence"),
                ):
                    _reconcile_manifest(
                        root,
                        spec,
                        runtime,
                        journal,
                        path,
                        phase=phase,
                    )
                self.assertNotIn(key, runtime.load_operation().observed)
                with (
                    patch(
                        "scripts.azure._get_management_resource",
                        side_effect=[observed, observed],
                    ),
                    patch("scripts.azure._kubectl"),
                ):
                    recovered = _reconcile_manifest(
                        root,
                        spec,
                        runtime,
                        runtime.load_operation(),
                        path,
                        phase=phase,
                    )
                self.assertEqual(recovered.observed[key], f"{kind.lower()}-uid")

    def test_tenant_kubeconfig_requires_verified_controller_owner(self):
        root = self.make_root()
        spec = self.spec()
        runtime, journal = self.start_journal(root, spec)
        journal = runtime.update_operation(
            journal,
            phase="control-plane-resources",
            observed={
                "clusterUid": "cluster-uid",
                "kamajiControlPlaneUid": "control-plane-uid",
            },
        )
        encoded = base64.b64encode(b"kubeconfig").decode()
        for owner_uid, accepted in (
            ("foreign-uid", False),
            ("control-plane-uid", True),
        ):
            with self.subTest(owner_uid=owner_uid):
                secret = {
                    "metadata": {
                        "uid": "secret-uid",
                        "ownerReferences": [
                            {
                                "controller": True,
                                "uid": owner_uid,
                                "kind": "KamajiControlPlane",
                            }
                        ],
                    },
                    "data": {"value": encoded},
                }
                with patch(
                    "scripts.azure._get_management_resource",
                    return_value=secret,
                ):
                    if accepted:
                        updated = _capture_tenant_kubeconfig(
                            root,
                            spec,
                            runtime,
                            journal,
                        )
                        self.assertEqual(
                            updated.observed["tenantKubeconfigSecretUid"],
                            "secret-uid",
                        )
                    else:
                        with self.assertRaisesRegex(RuntimeError, "incomplete"):
                            _capture_tenant_kubeconfig(
                                root,
                                spec,
                                runtime,
                                journal,
                            )

    def test_interrupted_create_retains_journal_at_each_stage(self):
        for failing_phase in ("control-plane", "workers", "add-ons"):
            with self.subTest(phase=failing_phase):
                root = self.make_root()
                spec = self.spec()
                runtime, journal = self.start_journal(root, spec)
                adapter = AzureTenantAdapter()
                timings = TenantTimings(
                    root,
                    profile="azure",
                    tenant=spec.name,
                    operation="create",
                    operation_id=journal.operation_id,
                )

                def reconcile(_root, _spec, actual_runtime, current, _path, *, phase):
                    stage = {
                        "control-plane-resources": "control-plane",
                        "worker-resources": "workers",
                        "addon-resources": "add-ons",
                    }[phase]
                    if stage == failing_phase:
                        raise RuntimeError(f"stop-{stage}")
                    return actual_runtime.update_operation(
                        current,
                        phase=phase,
                        observed={f"{stage}Uid": f"{stage}-uid"},
                    )

                endpoint = {
                    "spec": {
                        "controlPlaneEndpoint": {
                            "host": "10.220.0.6",
                            "port": 6443,
                        }
                    }
                }
                with (
                    patch.object(adapter, "_config", return_value=load_azure_configuration(root)),
                    patch("scripts.azure.load_inventory", return_value={}),
                    patch("scripts.azure._render_tenant_control_plane", return_value=Path("cp")),
                    patch("scripts.azure._render_worker_pool", return_value=Path("worker")),
                    patch("scripts.azure._render_addon_job", return_value=Path("addons")),
                    patch("scripts.azure._reconcile_manifest", side_effect=reconcile),
                    patch("scripts.azure._retain_external_control_plane_lb"),
                    patch("scripts.azure._wait_tenant_endpoint", return_value=endpoint),
                    patch(
                        "scripts.azure._capture_tenant_kubeconfig",
                        side_effect=lambda _r, _s, rt, current: rt.update_operation(
                            current,
                            phase="control-plane-ready",
                            observed={"credentialUid": "credential-uid"},
                        ),
                    ),
                    patch("scripts.azure._wait_worker_registered"),
                    patch(
                        "scripts.azure._capture_vmss_identities",
                        side_effect=lambda _r, _c, _s, rt, current: rt.update_operation(
                            current,
                            phase="workers-registered",
                            observed={"vmssId": "vmss-id"},
                        ),
                    ),
                    self.assertRaisesRegex(RuntimeError, f"stop-{failing_phase}"),
                ):
                    adapter.create(root, spec, runtime, journal, timings)
                self.assertTrue(runtime.operation_exists())
                self.assertFalse(runtime.identity_exists())

    def test_ready_observations_enforce_node_cloud_identity_and_subnet(self):
        root = self.make_root()
        config = load_azure_configuration(root)
        spec = self.spec()
        cluster = {
            "status": {
                "conditions": [
                    {"type": "ControlPlaneAvailable", "status": "True"}
                ]
            }
        }
        control_plane = {"status": {"ready": True}}
        pool = {
            "status": {
                "readyReplicas": 1,
                "nodeRefs": [{"name": "node-0"}],
            }
        }

        def management(_root, _namespace, resource):
            if resource.startswith("cluster/"):
                return cluster
            if resource.startswith("kamajicontrolplane/"):
                return control_plane
            return pool

        node = {
            "metadata": {"name": "node-0", "uid": "node-uid"},
            "spec": {"providerID": "azure:///vmss/0"},
            "status": {
                "addresses": [{"type": "InternalIP", "address": "10.220.16.4"}],
                "conditions": [{"type": "Ready", "status": "True"}],
            },
        }
        workload = {
            "metadata": {"uid": "workload-uid"},
            "spec": {"replicas": 1},
            "status": {
                "availableReplicas": 1,
                "updatedReplicas": 1,
                "desiredNumberScheduled": 1,
                "numberReady": 1,
                "updatedNumberScheduled": 1,
            },
        }
        responses = [completed(json.dumps({"items": [node]}))] + [
            completed(json.dumps(workload)) for _ in range(4)
        ]
        with (
            patch("scripts.azure._get_management_resource", side_effect=management),
            patch("scripts.azure._tenant_kubectl", side_effect=responses),
        ):
            observations, blockers = _collect_ready_observations(root, config, spec)
        self.assertEqual(blockers, ())
        self.assertEqual(observations["readyReplicas"], 1)
        node["status"]["addresses"][0]["address"] = "10.99.0.4"
        responses = [completed(json.dumps({"items": [node]}))] + [
            completed(json.dumps(workload)) for _ in range(4)
        ]
        with (
            patch("scripts.azure._get_management_resource", side_effect=management),
            patch("scripts.azure._tenant_kubectl", side_effect=responses),
        ):
            _, blockers = _collect_ready_observations(root, config, spec)
        self.assertTrue(any("outside the tenant subnet" in item for item in blockers))

    def test_tenant_spec_accepts_capz_defaulted_network_interface(self):
        spec = self.spec()
        selected = tenant_names(spec)
        payloads = {
            "clusterUid": {
                "spec": {
                    "clusterNetwork": {
                        "pods": {"cidrBlocks": [str(spec.pod_network)]},
                        "services": {"cidrBlocks": [str(spec.service_network)]},
                        "serviceDomain": spec.cluster_domain,
                    }
                }
            },
            "kamajiControlPlaneUid": {
                "spec": {"version": spec.kubernetes_version}
            },
            "machinePoolUid": {
                "spec": {"replicas": spec.workers, "clusterName": spec.name}
            },
            "azureMachinePoolUid": {
                "spec": {
                    "template": {
                        "vmSize": "Standard_B2s",
                        "networkInterfaces": [
                            {
                                "subnetName": "tenant",
                                "privateIPConfigs": 1,
                            }
                        ],
                        "image": {
                            "computeGallery": {
                                "version": spec.kubernetes_version
                            }
                        },
                    }
                }
            },
        }
        self.assertEqual(
            _tenant_spec_blockers(
                spec,
                selected,
                payloads,
                {"AZURE_TENANT_NODE_SKU": "Standard_B2s"},
            ),
            (),
        )

    def test_worker_pool_bounds_node_drain(self):
        root = self.make_root()
        config = load_azure_configuration(root)
        spec = self.spec()
        _, journal = self.start_journal(root, spec)
        manifest = _render_worker_pool(
            root,
            config,
            self.inventory(root, config),
            spec,
            journal,
        )
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        machine_pool = next(
            item for item in payload["items"] if item["kind"] == "MachinePool"
        )
        self.assertEqual(
            machine_pool["spec"]["template"]["spec"]["nodeDrainTimeout"],
            "2m",
        )

    def test_discovery_classifies_complete_known_resource_set(self):
        spec = self.spec()
        markers = {
            "tenant": spec.name,
            "profile": "azure",
            "specificationSha256": spec.sha256(),
            "foundationSha256": "foundation",
            "operationId": "operation",
        }
        tags = _azure_tags(markers)
        vmss = "/subscriptions/x/resourceGroups/rg/providers/Microsoft.Compute/virtualMachineScaleSets/tenant-c-worker"
        resources = [
            {"id": vmss, "type": "Microsoft.Compute/virtualMachineScaleSets", "tags": tags},
            {
                "id": vmss + "/virtualMachines/0",
                "type": "Microsoft.Compute/virtualMachineScaleSets/virtualMachines",
                "tags": {},
            },
            {
                "id": "/subscriptions/x/resourceGroups/rg/providers/Microsoft.Network/networkInterfaces/nic-0",
                "type": "Microsoft.Network/networkInterfaces",
                "tags": tags,
            },
            {
                "id": "/subscriptions/x/resourceGroups/rg/providers/Microsoft.Network/natGateways/nat",
                "type": "Microsoft.Network/natGateways",
                "tags": tags,
            },
            {
                "id": "/subscriptions/x/resourceGroups/rg/providers/Microsoft.Network/publicIPAddresses/pip",
                "type": "Microsoft.Network/publicIPAddresses",
                "tags": tags,
            },
        ]
        aso = {
            "kind": "NatGateway",
            "metadata": {
                "name": "nat",
                "uid": "aso-uid",
                "ownerReferences": [{"uid": "azure-cluster-uid"}],
            },
            "status": {
                "id": "/subscriptions/x/resourceGroups/rg/providers/Microsoft.Network/natGateways/nat"
            },
        }
        result = classify_azure_owned_resources(
            resources,
            markers,
            parent_ids=(vmss,),
            aso_objects=(aso,),
            parent_uids=("azure-cluster-uid",),
        )
        self.assertEqual(len(result["azure"]), 5)
        self.assertEqual(result["aso"][0]["uid"], "aso-uid")

    def test_discovery_fails_closed_for_unknown_or_foreign_ownership(self):
        markers = {
            "tenant": "tenant-c",
            "profile": "azure",
            "specificationSha256": "spec",
            "foundationSha256": "foundation",
            "operationId": "operation",
        }
        tags = _azure_tags(markers)
        resources = [
            {
                "id": "/subscriptions/x/resourceGroups/rg/providers/Microsoft.Storage/storageAccounts/unknown",
                "type": "Microsoft.Storage/storageAccounts",
                "tags": tags,
            }
        ]
        with self.assertRaisesRegex(RuntimeError, "ownership is unknown"):
            classify_azure_owned_resources(resources, markers)

    def test_discovery_fails_closed_on_aso_api_failure_and_unknown_kind(self):
        root = self.make_root()
        config = load_azure_configuration(root)
        spec = self.spec()
        runtime, journal = self.start_journal(root, spec)
        journal = runtime.update_operation(
            journal,
            phase="workers",
            observed={
                "markerOperationId": journal.operation_id,
                "azureClusterUid": "azure-cluster-uid",
                "azureMachinePoolUid": "azure-pool-uid",
            },
        )
        markers = lifecycle_markers(spec, journal)

        def parent(_root, _namespace, resource):
            uid = (
                "azure-cluster-uid"
                if resource.startswith("azurecluster/")
                else "azure-pool-uid"
            )
            return {
                "metadata": {
                    "uid": uid,
                    "annotations": {
                        LIFECYCLE_MARKERS[key]: value
                        for key, value in markers.items()
                    },
                }
            }

        with (
            patch(
                "scripts.azure._get_management_resource",
                side_effect=parent,
            ),
            patch("scripts.azure.load_inventory", return_value=self.inventory(root, config)),
            patch("scripts.azure._json", return_value=[]),
            patch(
                "scripts.azure._kubectl",
                return_value=subprocess.CompletedProcess(
                    [], 1, stdout="", stderr="forbidden"
                ),
            ),
            self.assertRaisesRegex(RuntimeError, "API discovery failed"),
        ):
            discover_azure_owned_resources(root, config, spec, journal)

        parentless = {
            "kind": "NatGateway",
            "metadata": {
                "name": "parentless",
                "uid": "parentless-uid",
            },
            "status": {
                "id": (
                    "/subscriptions/redacted/resourceGroups/yy-cv-rg/"
                    "providers/Microsoft.Network/natGateways/parentless"
                )
            },
        }
        responses = iter(
            (
                subprocess.CompletedProcess(
                    [], 0, stdout="natgateways.network.azure.com\n", stderr=""
                ),
                subprocess.CompletedProcess([], 0, stdout="", stderr=""),
                subprocess.CompletedProcess([], 0, stdout="", stderr=""),
                subprocess.CompletedProcess(
                    [],
                    0,
                    stdout=json.dumps({"items": [parentless]}),
                    stderr="",
                ),
            )
        )
        with (
            patch(
                "scripts.azure._get_management_resource",
                side_effect=parent,
            ),
            patch(
                "scripts.azure.load_inventory",
                return_value=self.inventory(root, config),
            ),
            patch("scripts.azure._json", return_value=[]),
            patch(
                "scripts.azure._kubectl",
                side_effect=lambda *_a, **_k: next(responses),
            ),
            self.assertRaisesRegex(RuntimeError, "foreign ASO ownership"),
        ):
            discover_azure_owned_resources(root, config, spec, journal)

        unknown = {
            "kind": "PrivateEndpoint",
            "metadata": {
                "name": "unknown",
                "uid": "unknown-uid",
                "ownerReferences": [{"uid": "azure-cluster-uid"}],
            },
            "status": {},
        }
        responses = iter(
            (
                subprocess.CompletedProcess(
                    [], 0, stdout="privateendpoints.network.azure.com\n", stderr=""
                ),
                subprocess.CompletedProcess([], 0, stdout="", stderr=""),
                subprocess.CompletedProcess([], 0, stdout="", stderr=""),
                subprocess.CompletedProcess(
                    [],
                    0,
                    stdout=json.dumps({"items": [unknown]}),
                    stderr="",
                ),
            )
        )
        with (
            patch(
                "scripts.azure._get_management_resource",
                side_effect=parent,
            ),
            patch("scripts.azure.load_inventory", return_value=self.inventory(root, config)),
            patch("scripts.azure._json", return_value=[]),
            patch("scripts.azure._kubectl", side_effect=lambda *_a, **_k: next(responses)),
            self.assertRaisesRegex(RuntimeError, "ownership is unknown"),
        ):
            discover_azure_owned_resources(root, config, spec, journal)

    def test_discovery_accepts_exact_skipped_foundation_references(self):
        root = self.make_root()
        config = load_azure_configuration(root)
        spec = self.spec()
        runtime, journal = self.start_journal(root, spec)
        journal = runtime.update_operation(
            journal,
            phase="workers",
            observed={
                "markerOperationId": journal.operation_id,
                "azureClusterUid": "azure-cluster-uid",
                "azureMachinePoolUid": "azure-pool-uid",
            },
        )
        markers = lifecycle_markers(spec, journal)
        inventory = self.inventory(root, config)

        def parent(_root, _namespace, resource):
            return {
                "metadata": {
                    "uid": (
                        "azure-cluster-uid"
                        if resource.startswith("azurecluster/")
                        else "azure-pool-uid"
                    ),
                    "annotations": {
                        LIFECYCLE_MARKERS[key]: value
                        for key, value in markers.items()
                    },
                }
            }

        references = {
            "resourcegroups.resources.azure.com": {
                "kind": "ResourceGroup",
                "metadata": {
                    "name": "resource-group",
                    "annotations": {
                        "serviceoperator.azure.com/reconcile-policy": "skip"
                    },
                },
                "status": {"id": inventory["outputs"]["resourceGroupId"]},
            },
            "virtualnetworks.network.azure.com": {
                "kind": "VirtualNetwork",
                "metadata": {
                    "name": "vnet",
                    "ownerReferences": [{"uid": "azure-cluster-uid"}],
                    "annotations": {
                        "serviceoperator.azure.com/reconcile-policy": "skip"
                    },
                },
                "status": {"id": inventory["outputs"]["vnetId"]},
            },
            "virtualnetworkssubnets.network.azure.com": {
                "kind": "VirtualNetworksSubnet",
                "metadata": {
                    "name": "subnet",
                    "ownerReferences": [{"uid": "azure-cluster-uid"}],
                    "annotations": {
                        "serviceoperator.azure.com/reconcile-policy": "skip"
                    },
                },
                "status": {"id": inventory["outputs"]["tenantSubnetId"]},
            },
        }

        def kubectl(_root, *arguments, **_kwargs):
            if arguments[0] == "api-resources":
                group = arguments[2]
                output = "\n".join(
                    name
                    for name in references
                    if name.endswith(group)
                )
                if output:
                    output += "\n"
                return subprocess.CompletedProcess([], 0, stdout=output, stderr="")
            resource = arguments[3]
            return subprocess.CompletedProcess(
                [],
                0,
                stdout=json.dumps({"items": [references[resource]]}),
                stderr="",
            )

        with (
            patch("scripts.azure._get_management_resource", side_effect=parent),
            patch("scripts.azure.load_inventory", return_value=inventory),
            patch("scripts.azure._json", return_value=[]),
            patch("scripts.azure._kubectl", side_effect=kubectl),
        ):
            discovery = discover_azure_owned_resources(
                root,
                config,
                spec,
                journal,
            )
        self.assertEqual(discovery, {"azure": [], "aso": [], "unknown": []})
        references["resourcegroups.resources.azure.com"]["status"]["id"] = (
            "/subscriptions/redacted/resourceGroups/foreign"
        )
        with (
            patch("scripts.azure._get_management_resource", side_effect=parent),
            patch("scripts.azure.load_inventory", return_value=inventory),
            patch("scripts.azure._json", return_value=[]),
            patch("scripts.azure._kubectl", side_effect=kubectl),
            self.assertRaisesRegex(RuntimeError, "foundation reference changed"),
        ):
            discover_azure_owned_resources(
                root,
                config,
                spec,
                journal,
            )
        tags = _azure_tags(markers)
        foreign = dict(tags)
        foreign["cnpg-vcluster-operation-id"] = "other"
        resources = [
            {
                "id": "/subscriptions/x/resourceGroups/rg/providers/Microsoft.Network/publicIPAddresses/pip",
                "type": "Microsoft.Network/publicIPAddresses",
                "tags": foreign,
            }
        ]
        with self.assertRaisesRegex(RuntimeError, "ownership is unknown"):
            classify_azure_owned_resources(resources, markers)

    def test_foreign_aso_id_cannot_promote_azure_resource(self):
        markers = {
            "tenant": "tenant-c",
            "profile": "azure",
            "specificationSha256": "spec",
            "foundationSha256": "foundation",
            "operationId": "operation",
        }
        resource_id = (
            "/subscriptions/x/resourceGroups/rg/providers/"
            "Microsoft.Network/publicIPAddresses/foreign"
        )
        resources = [
            {
                "id": resource_id,
                "type": "Microsoft.Network/publicIPAddresses",
                "tags": {},
            }
        ]
        foreign = {
            "kind": "PublicIPAddress",
            "metadata": {
                "name": "foreign",
                "uid": "foreign-uid",
                "ownerReferences": [],
            },
            "status": {"id": resource_id},
        }
        with self.assertRaisesRegex(RuntimeError, "foreign ASO ownership"):
            classify_azure_owned_resources(
                resources,
                markers,
                aso_objects=(foreign,),
                verified_ids=(),
            )

    def test_absent_status_is_read_only_and_distinguishes_foundation_health(self):
        root = self.make_root()
        adapter = AzureTenantAdapter()
        before = sorted(path.relative_to(root) for path in root.rglob("*"))
        with (
            patch(
                "scripts.azure._inspect_foundation",
                return_value=(FOUNDATION, True, ()),
            ),
            patch("scripts.azure._get_management_resource", return_value=None),
            patch("scripts.azure._json", return_value=[]),
        ):
            status = adapter.status(root, "missing")
        after = sorted(path.relative_to(root) for path in root.rglob("*"))
        self.assertEqual(status.classification, "absent")
        self.assertTrue(status.foundation_healthy)
        self.assertEqual(before, after)
        with patch(
            "scripts.azure._inspect_foundation",
            side_effect=RuntimeError("foundation unavailable"),
        ):
            status = adapter.status(root, "missing")
        self.assertEqual(status.classification, "degraded")
        self.assertFalse(status.foundation_healthy)

    def test_absent_status_rejects_tenant_runtime_residue(self):
        root = self.make_root()
        adapter = AzureTenantAdapter()
        residue = azure_tenant_runtime_path(root, "missing") / "endpoint.json"
        write_private_file(residue, "{}\n")
        with (
            patch(
                "scripts.azure._inspect_foundation",
                return_value=(FOUNDATION, True, ()),
            ),
            patch("scripts.azure._get_management_resource", return_value=None),
            patch("scripts.azure._json", return_value=[]),
        ):
            status = adapter.status(root, "missing")
        self.assertEqual(status.classification, "ownership-invalid")
        self.assertIn("endpoint.json", status.components["runtimeResidue"])

    def test_absent_status_fails_closed_on_inspection_error_or_malformed_azure_list(
        self,
    ):
        root = self.make_root()
        adapter = AzureTenantAdapter()
        with (
            patch(
                "scripts.azure._inspect_foundation",
                return_value=(FOUNDATION, True, ()),
            ),
            patch(
                "scripts.azure._kubectl",
                return_value=subprocess.CompletedProcess(
                    [], 1, stdout="", stderr="Forbidden"
                ),
            ),
            patch("scripts.azure._json", return_value=[]),
        ):
            status = adapter.status(root, "missing")
        self.assertEqual(status.classification, "ownership-invalid")
        with (
            patch(
                "scripts.azure._inspect_foundation",
                return_value=(FOUNDATION, True, ()),
            ),
            patch("scripts.azure._get_management_resource", return_value=None),
            patch("scripts.azure._json", return_value={"unexpected": True}),
        ):
            status = adapter.status(root, "missing")
        self.assertEqual(status.classification, "ownership-invalid")

    def test_management_get_only_accepts_kubernetes_object_not_found(self):
        from scripts.azure import _get_management_resource

        failures = (
            "Unable to connect to the server: getting credentials: "
            "exec: executable kubelogin not found",
            "dial tcp: lookup host: no such host",
            "transport connection failed: host not found",
            "Error from server (Forbidden): forbidden",
        )
        for stderr in failures:
            with (
                self.subTest(stderr=stderr),
                patch(
                    "scripts.azure._kubectl",
                    return_value=subprocess.CompletedProcess(
                        [], 1, stdout="", stderr=stderr
                    ),
                ),
                self.assertRaisesRegex(RuntimeError, "inspection failed"),
            ):
                _get_management_resource(
                    self.make_root(),
                    "tenant-c",
                    "cluster/tenant-c",
                )
        with patch(
            "scripts.azure._kubectl",
            return_value=subprocess.CompletedProcess(
                [],
                1,
                stdout="",
                stderr=(
                    'Error from server (NotFound): clusters "tenant-c" not found'
                ),
            ),
        ):
            self.assertIsNone(
                _get_management_resource(
                    self.make_root(),
                    "tenant-c",
                    "cluster/tenant-c",
                )
            )

    def test_ready_status_rejects_future_evidence_and_identity_changes(self):
        root = self.make_root()
        config = load_azure_configuration(root)
        spec = self.spec()
        runtime, journal = self.start_journal(root, spec)
        inventory = self.inventory(root, config)
        manifests = (
            _render_tenant_control_plane(root, config, inventory, spec, journal),
            _render_worker_pool(root, config, inventory, spec, journal),
            _render_addon_job(root, config, spec, journal),
        )
        payloads = {}
        observed = dict(journal.observed)
        kind_keys = {
            "Namespace": "namespaceUid",
            "AzureClusterIdentity": "azureClusterIdentityUid",
            "Cluster": "clusterUid",
            "AzureCluster": "azureClusterUid",
            "KamajiControlPlane": "kamajiControlPlaneUid",
            "KubeadmConfig": "kubeadmConfigUid",
            "AzureMachinePool": "azureMachinePoolUid",
            "MachinePool": "machinePoolUid",
            "Job": "addonJobUid",
            "Deployment": "statusProbeDeploymentUid",
        }
        configmap_keys = iter(("cloudValuesConfigMapUid", "networkValuesConfigMapUid"))
        for path in manifests:
            for item in json.loads(path.read_text(encoding="utf-8"))["items"]:
                key = (
                    next(configmap_keys)
                    if item["kind"] == "ConfigMap"
                    else kind_keys[item["kind"]]
                )
                uid = f"{key}-value"
                item["metadata"]["uid"] = uid
                payloads[
                    (
                        item["metadata"].get("namespace"),
                        f"{item['kind'].lower()}/{item['metadata']['name']}",
                    )
                ] = item
                observed[key] = uid
        endpoint = {"host": "10.220.0.6", "port": 6443}
        payloads[(spec.namespace, f"kamajicontrolplane/{spec.name}")]["spec"][
            "controlPlaneEndpoint"
        ] = endpoint
        kubeconfig = b"apiVersion: v1\n"
        write_private_file(
            azure_tenant_runtime_path(root, spec.name) / "kubeconfig",
            kubeconfig,
        )
        observed.update(
            {
                "endpoint": "10.220.0.6:6443",
                "tenantKubeconfigSecretUid": "secret-uid",
                "tenantKubeconfigSha256": hashlib.sha256(kubeconfig).hexdigest(),
            }
        )
        ready = {
            "controlPlaneAvailable": True,
            "kamajiReady": True,
            "requestedWorkers": 1,
            "readyReplicas": 1,
            "nodeRefs": ["node-0"],
            "nodes": [
                {
                    "name": "node-0",
                    "uid": "node-uid",
                    "providerID": "azure:///vmss/0",
                    "internalIP": "10.220.16.4",
                }
            ],
            "componentIdentities": {
                "cloudController": "cloud-controller-uid",
                "cloudNode": "cloud-node-uid",
                "calicoNode": "calico-node-uid",
                "calicoControllers": "calico-controllers-uid",
            },
            "cloudController": True,
            "cloudNode": True,
            "calicoNode": True,
            "calicoControllers": True,
        }
        discovery = {"azure": [], "aso": [], "unknown": []}
        observed["azureResources"] = json.dumps(
            discovery,
            sort_keys=True,
            separators=(",", ":"),
        )
        observed["nodeIdentities"] = json.dumps(
            ready["nodes"],
            sort_keys=True,
            separators=(",", ":"),
        )
        observed.update(
            {
                f"{key}Uid": value
                for key, value in ready["componentIdentities"].items()
            }
        )
        current = runtime.load_operation()
        runtime.complete_create(current, spec, observed)
        runtime.write_ready_evidence(
            {
                "schema": 1,
                "profile": "azure",
                "tenant": spec.name,
                "specificationSha256": spec.sha256(),
                "foundationIdentity": FOUNDATION,
                "observed": observed,
                "verifiedAt": 100,
                "ready": ready,
            }
        )
        secret = {
            "metadata": {"uid": "secret-uid"},
            "data": {"value": base64.b64encode(kubeconfig).decode()},
        }

        def resource(_root, namespace, name):
            if name == f"secret/{spec.name}-kubeconfig":
                return secret
            return payloads.get((namespace, name))

        adapter = AzureTenantAdapter(clock=lambda: 101)
        with (
            patch.object(adapter, "_config", return_value=config),
            patch(
                "scripts.azure._inspect_foundation",
                return_value=(FOUNDATION, True, ()),
            ),
            patch("scripts.azure._get_management_resource", side_effect=resource),
            patch(
                "scripts.azure._collect_ready_observations",
                return_value=(ready, ()),
            ),
            patch(
                "scripts.azure.discover_azure_owned_resources",
                return_value=discovery,
            ),
        ):
            status = adapter.status(root, spec.name)
        self.assertEqual(status.classification, "ready")
        self.assertTrue(status.components["tenantNetworkReady"])

        evidence = runtime.load_ready_evidence()
        evidence["verifiedAt"] = 102
        runtime.write_ready_evidence(evidence)
        with (
            patch.object(adapter, "_config", return_value=config),
            patch(
                "scripts.azure._inspect_foundation",
                return_value=(FOUNDATION, True, ()),
            ),
            patch("scripts.azure._get_management_resource", side_effect=resource),
            patch(
                "scripts.azure._collect_ready_observations",
                return_value=(ready, ()),
            ),
            patch(
                "scripts.azure.discover_azure_owned_resources",
                return_value=discovery,
            ),
        ):
            status = adapter.status(root, spec.name)
        self.assertEqual(status.classification, "degraded")
        self.assertIn(
            "Azure Ready evidence does not match current identities",
            status.blockers,
        )

        evidence["verifiedAt"] = float("nan")
        runtime.write_ready_evidence(evidence)
        with (
            patch.object(adapter, "_config", return_value=config),
            patch(
                "scripts.azure._inspect_foundation",
                return_value=(FOUNDATION, True, ()),
            ),
            patch("scripts.azure._get_management_resource", side_effect=resource),
            patch(
                "scripts.azure._collect_ready_observations",
                return_value=(ready, ()),
            ),
            patch(
                "scripts.azure.discover_azure_owned_resources",
                return_value=discovery,
            ),
        ):
            status = adapter.status(root, spec.name)
        self.assertEqual(status.classification, "degraded")
        self.assertIn(
            "Azure Ready evidence does not match current identities",
            status.blockers,
        )

        evidence["verifiedAt"] = 100
        evidence["observed"]["machinePoolUid"] = "foreign"
        runtime.write_ready_evidence(evidence)
        with (
            patch.object(adapter, "_config", return_value=config),
            patch(
                "scripts.azure._inspect_foundation",
                return_value=(FOUNDATION, True, ()),
            ),
            patch("scripts.azure._get_management_resource", side_effect=resource),
            patch(
                "scripts.azure._collect_ready_observations",
                return_value=(ready, ()),
            ),
            patch(
                "scripts.azure.discover_azure_owned_resources",
                return_value=discovery,
            ),
        ):
            status = adapter.status(root, spec.name)
        self.assertEqual(status.classification, "degraded")
        self.assertIn(
            "Azure Ready evidence does not match current identities",
            status.blockers,
        )

    def test_foundation_mutation_waits_for_generic_azure_profile_lock(self):
        root = self.make_root()
        marker = root / "acquired"
        script = (
            "from pathlib import Path; "
            "from scripts.azure import _run_profile_mutation; "
            f"root=Path({str(root)!r}); marker=Path({str(marker)!r}); "
            "_run_profile_mutation(root, {}, "
            "lambda _root, _config: marker.write_text('yes'))"
        )
        with profile_lock(root, "azure", exclusive=True, create=True):
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

    def test_staged_commands_are_removed(self):
        root = Path(__file__).resolve().parents[1]
        justfile = (root / "Justfile").read_text(encoding="utf-8")
        source = (root / "scripts" / "azure.py").read_text(encoding="utf-8")
        for command in (
            "azure-create-tenant-control-plane",
            "azure-create-worker",
            "azure-install-addons",
            "azure-status:",
            '"create-tenant-control-plane"',
            '"create-worker"',
            '"install-addons"',
        ):
            self.assertNotIn(command, justfile + source)
        self.assertIn("azure-foundation-status:", justfile)

    def test_tracked_example_contains_no_subscription_or_secret(self):
        example = (
            Path(__file__).resolve().parents[1]
            / "config"
            / "tenants"
            / "examples"
            / "azure.json"
        ).read_text(encoding="utf-8")
        self.assertNotRegex(example, r"[0-9a-f]{8}-[0-9a-f-]{27,}")
        self.assertNotRegex(example.lower(), r"subscription|clientsecret|password")


class AzurePhaseFiveTests(unittest.TestCase):
    make_root = AzurePhaseFourTests.make_root
    spec = staticmethod(AzurePhaseFourTests.spec)
    start_journal = AzurePhaseFourTests.start_journal
    inventory = AzurePhaseFourTests.inventory

    def ready_identity(self, root: Path, spec: TenantSpec):
        runtime, journal = self.start_journal(root, spec)
        observed = {
            "markerOperationId": journal.operation_id,
            "tenantKubeconfigSecretUid": "tenant-kubeconfig-secret-uid",
            "tenantKubeconfigSha256": "kubeconfig-sha256",
            "vmssId": (
                "/subscriptions/redacted/resourceGroups/yy-cv-rg/providers/"
                "Microsoft.Compute/virtualMachineScaleSets/tenant-c-worker"
            ),
            "vmssInstanceIds": "[]",
            "azureResources": json.dumps(
                {"azure": [], "aso": [], "unknown": []},
                sort_keys=True,
                separators=(",", ":"),
            ),
        }
        for key, _, _, _ in _management_resource_specs(spec):
            observed[key] = f"{key}-value"
        runtime.complete_create(runtime.load_operation(), spec, observed)
        write_private_file(
            azure_tenant_runtime_path(root, spec.name) / "endpoint.json",
            "{}\n",
        )
        return runtime, runtime.load_identity()

    def test_capz_webhook_selector_must_be_exact(self):
        expected = {
            "matchExpressions": [
                {
                    "key": "cnpg-vcluster-external-control-plane",
                    "operator": "NotIn",
                    "values": ["true"],
                }
            ]
        }
        payload = {
            "webhooks": [
                {
                    "name": "default.azurecluster.infrastructure.cluster.x-k8s.io",
                    "objectSelector": expected,
                }
            ]
        }
        self.assertTrue(_capz_external_control_plane_webhook_ready(payload))
        payload["webhooks"][0]["objectSelector"] = {
            **expected,
            "matchLabels": {"other": "value"},
        }
        self.assertFalse(_capz_external_control_plane_webhook_ready(payload))
        payload["webhooks"][0]["objectSelector"] = {
            "matchExpressions": [
                *expected["matchExpressions"],
                {
                    "key": "cnpg-vcluster-external-control-plane",
                    "operator": "Exists",
                },
            ]
        }
        self.assertFalse(_capz_external_control_plane_webhook_ready(payload))

    def management_payloads(self, spec, identity):
        markers = {
            "tenant": spec.name,
            "profile": "azure",
            "specificationSha256": spec.sha256(),
            "foundationSha256": foundation_sha256(identity.foundation_identity),
            "operationId": identity.observed["markerOperationId"],
        }
        payloads = []
        namespace = None
        for key, namespace_name, kind, name in _management_resource_specs(spec):
            payload = {
                "apiVersion": "v1",
                "kind": kind,
                "metadata": {
                    "name": name,
                    "uid": identity.observed[key],
                    "resourceVersion": f"{key}-rv",
                    "annotations": {
                        LIFECYCLE_MARKERS[marker]: value
                        for marker, value in markers.items()
                    },
                },
            }
            if namespace_name is not None:
                payload["metadata"]["namespace"] = namespace_name
            if kind == "Namespace":
                namespace = payload
            else:
                payloads.append(payload)
        payloads.append(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {
                    "name": f"{spec.name}-kubeconfig",
                    "uid": identity.observed["tenantKubeconfigSecretUid"],
                    "resourceVersion": "secret-rv",
                    "ownerReferences": [
                        {
                            "uid": identity.observed["kamajiControlPlaneUid"],
                            "controller": True,
                        }
                    ],
                },
            }
        )
        return namespace, payloads

    def test_management_inventory_refuses_unknown_and_foreign_state(self):
        root = self.make_root()
        spec = self.spec()
        _, identity = self.ready_identity(root, spec)
        namespace, payloads = self.management_payloads(spec, identity)
        payloads.append(
            {
                "apiVersion": "unknown.example/v1",
                "kind": "Mystery",
                "metadata": {
                    "name": "late",
                    "uid": "late-uid",
                    "resourceVersion": "1",
                },
            }
        )
        with self.assertRaisesRegex(RuntimeError, "ownership is unknown"):
            _classify_management_owned_resources(
                spec,
                identity,
                namespace,
                payloads,
                require_complete=True,
            )
        payloads.pop()
        payloads[0]["metadata"]["annotations"][
            LIFECYCLE_MARKERS["tenant"]
        ] = "foreign"
        with self.assertRaisesRegex(RuntimeError, "foreign.*markers"):
            _classify_management_owned_resources(
                spec,
                identity,
                namespace,
                payloads,
                require_complete=True,
            )

    def test_management_inventory_accepts_capz_generated_and_shared_references(self):
        root = self.make_root()
        spec = self.spec()
        _, identity = self.ready_identity(root, spec)
        namespace, payloads = self.management_payloads(spec, identity)
        markers = {
            LIFECYCLE_MARKERS["tenant"]: spec.name,
            LIFECYCLE_MARKERS["profile"]: "azure",
            LIFECYCLE_MARKERS["specificationSha256"]: spec.sha256(),
            LIFECYCLE_MARKERS["foundationSha256"]: foundation_sha256(
                identity.foundation_identity
            ),
            LIFECYCLE_MARKERS["operationId"]: identity.observed["markerOperationId"],
        }
        azure_cluster_uid = identity.observed["azureClusterUid"]
        payloads.extend(
            (
                {
                    "kind": "AzureMachinePoolMachine",
                    "metadata": {
                        "name": f"{spec.name}-worker-0",
                        "uid": "azure-pool-machine-uid",
                        "ownerReferences": [
                            {"uid": identity.observed["azureMachinePoolUid"]}
                        ],
                    },
                },
                {
                    "kind": "PodMetrics",
                    "metadata": {
                        "name": "status-probe",
                        "uid": "metrics-uid",
                    },
                },
                {
                    "kind": "NatGateway",
                    "metadata": {
                        "name": f"{spec.name}-node-natgw-1",
                        "uid": "orphaned-nat-gateway-uid",
                    },
                    "spec": {
                        "tags": {
                            "cnpg-vcluster-tenant": spec.name,
                            "cnpg-vcluster-profile": "azure",
                            "cnpg-vcluster-spec-sha256": spec.sha256(),
                            "cnpg-vcluster-foundation-sha256": foundation_sha256(
                                identity.foundation_identity
                            ),
                            "cnpg-vcluster-operation-id": identity.observed[
                                "markerOperationId"
                            ],
                        }
                    },
                },
                {
                    "kind": "ReplicaSet",
                    "metadata": {
                        "name": "status-probe",
                        "uid": "replica-set-uid",
                        "ownerReferences": [
                            {"uid": identity.observed["statusProbeDeploymentUid"]}
                        ],
                    },
                },
                {
                    "kind": "Pod",
                    "metadata": {
                        "name": "status-probe-pod",
                        "uid": "pod-uid",
                        "ownerReferences": [{"uid": "replica-set-uid"}],
                    },
                },
                *(
                    {
                        "kind": kind,
                        "metadata": {
                            "name": name,
                            "uid": f"{kind}-uid",
                            "ownerReferences": [{"uid": azure_cluster_uid}],
                            "annotations": markers,
                        },
                    }
                    for kind, name in (
                        ("ResourceGroup", "yy-cv-rg"),
                        ("VirtualNetwork", "yy-cv-vnet"),
                        ("VirtualNetworksSubnet", "yy-cv-vnet-tenant"),
                    )
                ),
            )
        )
        classified = _classify_management_owned_resources(
            spec,
            identity,
            namespace,
            payloads,
            require_complete=True,
        )
        controller_kinds = {item["kind"] for item in classified["controller"]}
        self.assertTrue(
            {
                "AzureMachinePoolMachine",
                "NatGateway",
                "ResourceGroup",
                "VirtualNetwork",
                "VirtualNetworksSubnet",
            }.issubset(controller_kinds)
        )
        self.assertIn(
            "PodMetrics",
            {item["kind"] for item in classified["namespaceChildren"]},
        )
        self.assertTrue(
            {"Pod", "ReplicaSet"}.issubset(
                {item["kind"] for item in classified["namespaceChildren"]}
            )
        )

    def test_management_cleanup_accepts_only_snapshotted_orphan_uid(self):
        root = self.make_root()
        spec = self.spec()
        _, identity = self.ready_identity(root, spec)
        namespace, payloads = self.management_payloads(spec, identity)
        orphan = {
            "kind": "Service",
            "metadata": {
                "name": spec.name,
                "uid": "snapshotted-service-uid",
                "deletionTimestamp": "2026-01-01T00:00:00Z",
                "ownerReferences": [{"uid": "deleted-owner-uid"}],
            },
        }
        payloads.append(orphan)
        with self.assertRaisesRegex(RuntimeError, "ownership is unknown"):
            _classify_management_owned_resources(
                spec,
                identity,
                namespace,
                payloads,
                require_complete=False,
            )
        classified = _classify_management_owned_resources(
            spec,
            identity,
            namespace,
            payloads,
            require_complete=False,
            verified_uids=("snapshotted-service-uid",),
        )
        self.assertIn(
            "snapshotted-service-uid",
            {item["uid"] for item in classified["controller"]},
        )

    def test_delete_resume_restores_recorded_discovery_identities(self):
        root = self.make_root()
        spec = self.spec()
        runtime, identity = self.ready_identity(root, spec)
        journal = runtime.start_operation(
            operation="delete",
            spec=spec,
            foundation_identity=identity.foundation_identity,
            intended_resources=AzureTenantAdapter.intended_resources(spec),
            operation_id="delete-resume",
        )
        management = {
            "controller": [
                {
                    "kind": "Service",
                    "name": spec.name,
                    "uid": "recorded-service-uid",
                }
            ],
            "orchestration": [],
            "namespaceChildren": [],
            "unknown": [],
        }
        azure = {
            "azure": [
                {
                    "id": (
                        "/subscriptions/redacted/resourceGroups/yy-cv-rg/"
                        "providers/Microsoft.Network/publicIPAddresses/recorded"
                    ),
                    "type": "microsoft.network/publicipaddresses",
                }
            ],
            "aso": [],
            "unknown": [],
        }
        runtime.update_operation(
            journal,
            phase="controller-cleanup",
            observed={
                "deleteManagementBefore": json.dumps(management),
                "deleteAzureBefore": json.dumps(azure),
            },
        )
        empty_management = {
            "controller": [],
            "orchestration": [],
            "namespaceChildren": [],
            "unknown": [],
        }
        empty_azure = {"azure": [], "aso": [], "unknown": []}
        adapter = AzureTenantAdapter()
        with (
            patch.object(
                adapter,
                "_config",
                return_value=load_azure_configuration(root),
            ),
            patch("scripts.azure._active_subscription"),
            patch(
                "scripts.azure._inspect_foundation",
                return_value=(FOUNDATION, True, ()),
            ),
            patch(
                "scripts.azure.discover_management_owned_resources",
                return_value=empty_management,
            ) as management_discovery,
            patch(
                "scripts.azure._discover_owned_repeatedly",
                return_value=empty_azure,
            ) as azure_discovery,
        ):
            adapter.validate_delete(root, spec, identity)
        self.assertIn(
            "recorded-service-uid",
            management_discovery.call_args.kwargs["verified_uids"],
        )
        self.assertIn(
            azure["azure"][0]["id"],
            azure_discovery.call_args.kwargs["verified_resource_ids"],
        )
        self.assertEqual(
            adapter._delete_snapshots[spec.name]["management"]["controller"][0][
                "uid"
            ],
            "recorded-service-uid",
        )

    def test_capz_external_control_plane_delete_workaround_is_delete_only(self):
        root = self.make_root()
        config = load_azure_configuration(root)
        spec = self.spec()
        _, identity = self.ready_identity(root, spec)
        markers = {
            LIFECYCLE_MARKERS["tenant"]: spec.name,
            LIFECYCLE_MARKERS["profile"]: "azure",
            LIFECYCLE_MARKERS["specificationSha256"]: spec.sha256(),
            LIFECYCLE_MARKERS["foundationSha256"]: foundation_sha256(
                identity.foundation_identity
            ),
            LIFECYCLE_MARKERS["operationId"]: identity.observed["markerOperationId"],
        }
        payload = {
            "metadata": {
                "uid": identity.observed["azureClusterUid"],
                "deletionTimestamp": "2026-01-01T00:00:00Z",
                "annotations": markers,
                "labels": {
                    "cnpg-vcluster-external-control-plane": "true",
                },
            },
            "spec": {
                "controlPlaneEnabled": False,
                "networkSpec": {"subnets": [{"name": "tenant", "role": "node"}]},
            },
        }
        updated = {
            **payload,
            "spec": {
                "controlPlaneEnabled": False,
                "networkSpec": {
                    "apiServerLB": {"type": "Public"},
                    "subnets": payload["spec"]["networkSpec"]["subnets"],
                },
            },
        }
        with (
            patch("scripts.azure._get_management_resource", side_effect=(payload, updated)),
            patch("scripts.azure._kubectl") as kubectl,
        ):
            _enable_capz_external_control_plane_delete(
                root,
                spec,
                identity,
            )
        patch_payload = json.loads(kubectl.call_args.args[-1])
        self.assertEqual(
            patch_payload,
            {"spec": {"networkSpec": {"apiServerLB": {"type": "Public"}}}},
        )

        not_deleting = {
            **payload,
            "metadata": {
                **payload["metadata"],
                "deletionTimestamp": None,
            },
        }
        with (
            patch(
                "scripts.azure._get_management_resource",
                return_value=not_deleting,
            ),
            patch("scripts.azure.time.monotonic", side_effect=(0, 121)),
            patch("scripts.azure.time.sleep"),
            patch("scripts.azure._kubectl") as kubectl,
            self.assertRaisesRegex(RuntimeError, "deletion did not start"),
        ):
            _enable_capz_external_control_plane_delete(
                root,
                spec,
                identity,
            )
        kubectl.assert_not_called()

    def test_delete_marks_only_owned_tenant_machines_to_skip_drain(self):
        root = self.make_root()
        spec = self.spec()
        _, identity = self.ready_identity(root, spec)
        markers = {
            LIFECYCLE_MARKERS["tenant"]: spec.name,
            LIFECYCLE_MARKERS["profile"]: "azure",
            LIFECYCLE_MARKERS["specificationSha256"]: spec.sha256(),
            LIFECYCLE_MARKERS["foundationSha256"]: foundation_sha256(
                identity.foundation_identity
            ),
            LIFECYCLE_MARKERS["operationId"]: identity.observed["markerOperationId"],
        }
        machine = {
            "kind": "Machine",
            "metadata": {
                "name": f"{spec.name}-worker-0",
                "uid": "machine-uid",
                "annotations": markers,
                "ownerReferences": [{"uid": identity.observed["machinePoolUid"]}],
            },
        }
        with patch(
            "scripts.azure._kubectl",
            side_effect=(
                subprocess.CompletedProcess(
                    [],
                    0,
                    stdout=json.dumps({"items": [machine]}),
                    stderr="",
                ),
                subprocess.CompletedProcess([], 0, stdout="", stderr=""),
            ),
        ) as kubectl:
            _exclude_tenant_machines_from_drain(root, spec, identity)
        patch_arguments = kubectl.call_args_list[1].args
        patch_payload = json.loads(patch_arguments[-1])
        self.assertEqual(patch_payload[0]["value"], "machine-uid")
        self.assertEqual(
            patch_payload[1]["path"],
            "/metadata/annotations/machine.cluster.x-k8s.io~1exclude-node-draining",
        )

    def test_worker_cleanup_waits_for_machines_and_vmss(self):
        root = self.make_root()
        config = load_azure_configuration(root)
        spec = self.spec()
        _, identity = self.ready_identity(root, spec)
        adapter = AzureTenantAdapter(
            monotonic=iter((0, 10_000)).__next__,
            sleep=lambda _seconds: None,
        )
        machine = {
            "metadata": {
                "name": f"{spec.name}-worker-0",
            }
        }
        with (
            patch(
                "scripts.azure.load_inventory",
                return_value={
                    "outputs": {"resourceGroupName": "yy-cv-rg"}
                },
            ),
            patch("scripts.azure._get_management_resource", return_value=None),
            patch(
                "scripts.azure._owned_tenant_machines",
                return_value=[machine],
            ),
            patch(
                "scripts.azure._az",
                return_value=subprocess.CompletedProcess(
                    [],
                    0,
                    stdout=json.dumps([identity.observed["vmssId"]]),
                    stderr="",
                ),
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "machine/tenant-c-worker-0.*vmss/tenant-c-worker",
            ),
        ):
            adapter._wait_for_worker_cleanup(
                root,
                config,
                spec,
                identity,
            )

    def test_delete_validation_refuses_foundation_and_uid_before_mutation(self):
        root = self.make_root()
        spec = self.spec()
        _, identity = self.ready_identity(root, spec)
        adapter = AzureTenantAdapter()
        with (
            patch.object(adapter, "_config", return_value=load_azure_configuration(root)),
            patch("scripts.azure._active_subscription"),
            patch(
                "scripts.azure._inspect_foundation",
                return_value=({**FOUNDATION, "aksId": "foreign"}, True, ()),
            ),
            patch("scripts.azure._exact_delete_management_resource") as mutate,
            self.assertRaisesRegex(RuntimeError, "foundation binding changed"),
        ):
            adapter.validate_delete(root, spec, identity)
        mutate.assert_not_called()

        namespace, payloads = self.management_payloads(spec, identity)
        payloads[0]["metadata"]["uid"] = "replacement"
        with self.assertRaisesRegex(RuntimeError, "UID changed"):
            _classify_management_owned_resources(
                spec,
                identity,
                namespace,
                payloads,
                require_complete=True,
            )

    def test_exact_delete_validates_uid_resource_version_and_markers(self):
        root = self.make_root()
        spec = self.spec()
        _, identity = self.ready_identity(root, spec)
        markers = {
            "tenant": spec.name,
            "profile": "azure",
            "specificationSha256": spec.sha256(),
            "foundationSha256": foundation_sha256(identity.foundation_identity),
            "operationId": identity.observed["markerOperationId"],
        }
        payload = {
            "apiVersion": "cluster.x-k8s.io/v1beta1",
            "kind": "Cluster",
            "metadata": {
                "name": spec.name,
                "uid": identity.observed["clusterUid"],
                "resourceVersion": "42",
                "annotations": {
                    LIFECYCLE_MARKERS[key]: value
                    for key, value in markers.items()
                },
            }
        }
        with (
            patch("scripts.azure._get_management_resource", return_value=payload),
            patch("scripts.azure._kubectl") as kubectl,
        ):
            self.assertTrue(
                _exact_delete_management_resource(
                    root,
                    spec,
                    identity,
                    namespace=spec.namespace,
                    resource=f"cluster/{spec.name}",
                    uid_key="clusterUid",
                    cascade="foreground",
                )
            )
        arguments = kubectl.call_args.args
        self.assertIn(
            (
                "--raw=/apis/cluster.x-k8s.io/v1beta1/namespaces/"
                f"{spec.namespace}/clusters/{spec.name}"
            ),
            arguments,
        )
        options = json.loads(kubectl.call_args.kwargs["input_text"])
        self.assertEqual(
            options["preconditions"],
            {
                "uid": identity.observed["clusterUid"],
                "resourceVersion": "42",
            },
        )
        self.assertEqual(options["propagationPolicy"], "Foreground")
        payload["metadata"]["uid"] = "foreign"
        with (
            patch("scripts.azure._get_management_resource", return_value=payload),
            patch("scripts.azure._kubectl") as kubectl,
            self.assertRaisesRegex(RuntimeError, "UID changed"),
        ):
            _exact_delete_management_resource(
                root,
                spec,
                identity,
                namespace=spec.namespace,
                resource=f"cluster/{spec.name}",
                uid_key="clusterUid",
                cascade="foreground",
            )
        kubectl.assert_not_called()
        namespace, _ = self.management_payloads(spec, identity)
        with (
            patch("scripts.azure._get_management_resource", return_value=namespace),
            patch("scripts.azure._kubectl") as kubectl,
        ):
            _exact_delete_management_resource(
                root,
                spec,
                identity,
                namespace=None,
                resource=f"namespace/{spec.name}",
                uid_key="namespaceUid",
                cascade="foreground",
            )
        self.assertIn(
            f"--raw=/api/v1/namespaces/{spec.name}",
            kubectl.call_args.args,
        )

    def test_repeated_discovery_captures_late_resources(self):
        root = self.make_root()
        spec = self.spec()
        _, identity = self.ready_identity(root, spec)
        first = {
            "azure": [
                {
                    "id": "/subscriptions/redacted/resourceGroups/rg/providers/"
                    "Microsoft.Compute/virtualMachineScaleSets/pool",
                    "type": "microsoft.compute/virtualmachinescalesets",
                }
            ],
            "aso": [],
            "unknown": [],
        }
        late = {
            "azure": [
                {
                    "id": "/subscriptions/redacted/resourceGroups/rg/providers/"
                    "Microsoft.Network/publicIPAddresses/late",
                    "type": "microsoft.network/publicipaddresses",
                }
            ],
            "aso": [],
            "unknown": [],
        }
        with patch(
            "scripts.azure.discover_azure_owned_resources",
            side_effect=(first, late),
        ) as discover:
            result = _discover_owned_repeatedly(
                root,
                load_azure_configuration(root),
                spec,
                identity,
                passes=2,
                require_parents=False,
                require_azure_resources=False,
            )
        self.assertEqual(discover.call_count, 2)
        self.assertEqual(len(result["azure"]), 2)
        self.assertIn(
            first["azure"][0]["id"],
            discover.call_args_list[1].kwargs["verified_resource_ids"],
        )

    def test_delete_orders_controllers_before_orchestration_and_runtime(self):
        root = self.make_root()
        spec = self.spec()
        runtime, identity = self.ready_identity(root, spec)
        journal = runtime.start_operation(
            operation="delete",
            spec=spec,
            foundation_identity=identity.foundation_identity,
            intended_resources=AzureTenantAdapter.intended_resources(spec),
            operation_id="delete-operation",
        )
        adapter = AzureTenantAdapter()
        adapter._delete_snapshots[spec.name] = {
            "foundation": FOUNDATION,
            "management": {
                "controller": [],
                "orchestration": [],
                "namespaceChildren": [],
                "unknown": [],
            },
            "azure": {"azure": [], "aso": [], "unknown": []},
        }
        calls = []

        def delete_resource(*_args, resource, **_kwargs):
            calls.append(f"delete:{resource}")
            return True

        timings = TenantTimings(
            root,
            profile="azure",
            tenant=spec.name,
            operation="delete",
            operation_id=journal.operation_id,
        )
        with (
            patch.object(adapter, "_config", return_value=load_azure_configuration(root)),
            patch(
                "scripts.azure._exact_delete_management_resource",
                side_effect=delete_resource,
            ),
            patch("scripts.azure._exclude_tenant_machines_from_drain"),
            patch("scripts.azure._enable_capz_external_control_plane_delete"),
            patch.object(
                adapter,
                "_wait_for_worker_cleanup",
                side_effect=lambda *_a: calls.append("workers-absent"),
            ),
            patch.object(
                adapter,
                "_wait_for_controller_cleanup",
                side_effect=lambda *_a: (
                    calls.append("controllers-absent")
                    or (
                        {
                            "controller": [],
                            "orchestration": [],
                            "namespaceChildren": [],
                            "unknown": [],
                        },
                        {"azure": [], "aso": [], "unknown": []},
                    )
                ),
            ),
            patch.object(
                adapter,
                "_wait_for_tenant_absence",
                side_effect=lambda *_a: (
                    calls.append("tenant-absent")
                    or {"azure": [], "aso": [], "unknown": []}
                ),
            ),
            patch(
                "scripts.azure._inspect_foundation",
                side_effect=lambda *_a, **_k: (
                    calls.append("foundation-verified")
                    or (FOUNDATION, True, ())
                ),
            ),
            patch(
                "scripts.azure._remove_private_tree",
                side_effect=lambda *_a: calls.append("runtime-removed"),
            ),
            patch.object(
                adapter,
                "_inspect_absence",
                return_value=TenantStatus(
                    profile="azure",
                    tenant=spec.name,
                    classification="absent",
                    foundation_healthy=True,
                ),
            ),
        ):
            adapter.delete(root, spec, identity, runtime, journal, timings)
        self.assertEqual(calls[0], f"delete:machinepool/{spec.name}-worker")
        self.assertEqual(calls[1], "workers-absent")
        self.assertEqual(calls[2], f"delete:cluster/{spec.name}")
        controller_index = calls.index("controllers-absent")
        namespace_index = calls.index(f"delete:namespace/{spec.name}")
        self.assertLess(controller_index, namespace_index)
        self.assertLess(namespace_index, calls.index("tenant-absent"))
        self.assertLess(
            calls.index("foundation-verified"),
            calls.index("runtime-removed"),
        )
        phases = [record["phase"] for record in timings.records()]
        self.assertEqual(
            phases,
            [
                "worker-deletion",
                "deletion",
                "controller-cleanup",
                "orchestration-cleanup",
                "azure-absence",
                "foundation-verification",
                "runtime-cleanup",
            ],
        )

    def test_delete_timeout_retains_state_and_sanitized_diagnostics(self):
        root = self.make_root()
        spec = self.spec()
        runtime, identity = self.ready_identity(root, spec)
        journal = runtime.start_operation(
            operation="delete",
            spec=spec,
            foundation_identity=identity.foundation_identity,
            intended_resources=AzureTenantAdapter.intended_resources(spec),
            operation_id="delete-timeout",
        )
        adapter = AzureTenantAdapter()
        adapter._delete_snapshots[spec.name] = {
            "foundation": FOUNDATION,
            "management": {
                "controller": [],
                "orchestration": [],
                "namespaceChildren": [],
                "unknown": [],
            },
            "azure": {"azure": [], "aso": [], "unknown": []},
        }
        timings = TenantTimings(
            root,
            profile="azure",
            tenant=spec.name,
            operation="delete",
            operation_id=journal.operation_id,
        )
        with (
            patch.object(adapter, "_config", return_value=load_azure_configuration(root)),
            patch("scripts.azure._exact_delete_management_resource", return_value=True),
            patch.object(adapter, "_wait_for_worker_cleanup"),
            patch.object(
                adapter,
                "_wait_for_controller_cleanup",
                side_effect=RuntimeError(
                    "CAPZ panic: finalizer blocked "
                    "/subscriptions/00000000-0000-0000-0000-000000000000/"
                    "resourceGroups/rg/providers/Microsoft.Compute/"
                    "virtualMachineScaleSets/pool"
                ),
            ),
            patch("scripts.azure._get_management_resource", return_value=None),
            patch(
                "scripts.azure._kubectl",
                return_value=completed(json.dumps({"items": []})),
            ),
            self.assertRaisesRegex(RuntimeError, "CAPZ panic"),
        ):
            adapter.delete(root, spec, identity, runtime, journal, timings)
        self.assertTrue(runtime.identity_exists())
        self.assertTrue(runtime.operation_exists())
        diagnostic = (
            runtime.paths.evidence
            / f"delete-diagnostics-{journal.operation_id}.json"
        )
        self.assertTrue(diagnostic.is_file())
        self.assertEqual(diagnostic.stat().st_mode & 0o777, 0o600)
        text = diagnostic.read_text(encoding="utf-8")
        self.assertIn("CAPZ panic", text)
        self.assertNotIn(SUBSCRIPTION, text)
        self.assertTrue(
            any(
                record["phase"] == "controller-cleanup"
                and record["status"] == "failed"
                for record in timings.records()
            )
        )

    def test_controller_wait_is_bounded_and_reports_exact_residue(self):
        root = self.make_root()
        spec = self.spec()
        _, identity = self.ready_identity(root, spec)
        adapter = AzureTenantAdapter(
            monotonic=iter((0.0, 1.0)).__next__,
            sleep=lambda _seconds: None,
        )
        management = {
            "controller": [
                {"kind": "AzureMachinePool", "name": "tenant-c-worker"}
            ],
            "orchestration": [],
            "namespaceChildren": [],
            "unknown": [],
        }
        azure = {
            "azure": [
                {
                    "id": "/subscriptions/redacted/resourceGroups/rg/providers/"
                    "Microsoft.Compute/virtualMachineScaleSets/tenant-c-worker",
                    "type": "microsoft.compute/virtualmachinescalesets",
                }
            ],
            "aso": [
                {
                    "kind": "NatGateway",
                    "name": "tenant-c-nat",
                    "uid": "nat-uid",
                    "azureResourceId": "/subscriptions/redacted/nat",
                }
            ],
            "unknown": [],
        }
        with (
            patch("scripts.azure.parse_duration", return_value=0),
            patch(
                "scripts.azure.discover_management_owned_resources",
                return_value=management,
            ),
            patch(
                "scripts.azure._discover_owned_repeatedly",
                return_value=azure,
            ),
            self.assertRaises(RuntimeError) as raised,
        ):
            adapter._wait_for_controller_cleanup(
                root,
                load_azure_configuration(root),
                spec,
                identity,
                {
                    "controller": [],
                    "orchestration": [],
                    "namespaceChildren": [],
                    "unknown": [],
                },
                {"azure": [], "aso": [], "unknown": []},
            )
        message = str(raised.exception)
        self.assertIn("AzureMachinePool/tenant-c-worker", message)
        self.assertIn("virtualMachineScaleSets/tenant-c-worker", message)
        self.assertIn("NatGateway/tenant-c-nat", message)

    def test_foundation_change_fails_before_runtime_cleanup(self):
        root = self.make_root()
        spec = self.spec()
        runtime, identity = self.ready_identity(root, spec)
        journal = runtime.start_operation(
            operation="delete",
            spec=spec,
            foundation_identity=identity.foundation_identity,
            intended_resources=AzureTenantAdapter.intended_resources(spec),
            operation_id="foundation-change",
        )
        adapter = AzureTenantAdapter()
        adapter._delete_snapshots[spec.name] = {
            "foundation": FOUNDATION,
            "management": {
                "controller": [],
                "orchestration": [],
                "namespaceChildren": [],
                "unknown": [],
            },
            "azure": {"azure": [], "aso": [], "unknown": []},
        }
        timings = TenantTimings(
            root,
            profile="azure",
            tenant=spec.name,
            operation="delete",
            operation_id=journal.operation_id,
        )
        with (
            patch.object(adapter, "_config", return_value=load_azure_configuration(root)),
            patch("scripts.azure._exact_delete_management_resource", return_value=True),
            patch.object(adapter, "_wait_for_worker_cleanup"),
            patch.object(
                adapter,
                "_wait_for_controller_cleanup",
                return_value=(
                    {
                        "controller": [],
                        "orchestration": [],
                        "namespaceChildren": [],
                        "unknown": [],
                    },
                    {"azure": [], "aso": [], "unknown": []},
                ),
            ),
            patch.object(
                adapter,
                "_wait_for_tenant_absence",
                return_value={"azure": [], "aso": [], "unknown": []},
            ),
            patch(
                "scripts.azure._inspect_foundation",
                return_value=({**FOUNDATION, "vnetId": "changed"}, True, ()),
            ),
            patch("scripts.azure._remove_private_tree") as remove_runtime,
            patch("scripts.azure._get_management_resource", return_value=None),
            patch(
                "scripts.azure._kubectl",
                return_value=completed(json.dumps({"items": []})),
            ),
            self.assertRaisesRegex(RuntimeError, "foundation identity changed"),
        ):
            adapter.delete(root, spec, identity, runtime, journal, timings)
        remove_runtime.assert_not_called()
        self.assertTrue(runtime.identity_exists())
        self.assertTrue(runtime.operation_exists())

    def test_authoritative_absence_ignores_pending_delete_journal(self):
        root = self.make_root()
        spec = self.spec()
        runtime = TenantRuntime(root, "azure", spec.name)
        runtime.start_operation(
            operation="delete",
            spec=spec,
            foundation_identity=FOUNDATION,
            intended_resources=AzureTenantAdapter.intended_resources(spec),
            operation_id="pending-delete",
        )
        adapter = AzureTenantAdapter()
        with (
            patch.object(adapter, "_config", return_value=load_azure_configuration(root)),
            patch(
                "scripts.azure._inspect_foundation",
                return_value=(FOUNDATION, True, ()),
            ),
            patch("scripts.azure._get_management_resource", return_value=None),
            patch("scripts.azure._json", return_value=[]),
        ):
            status = adapter.authoritative_absence(root, spec.name)
        self.assertEqual(status.classification, "absent")
        self.assertTrue(runtime.operation_exists())

    def test_source_forbids_direct_vmss_delete_and_provider_finalizer_removal(self):
        source = (
            Path(__file__).resolve().parents[1] / "scripts" / "azure.py"
        ).read_text(encoding="utf-8")
        self.assertNotRegex(
            source,
            r"[\"']vmss[\"']\s*,\s*[\"']delete[\"']",
        )
        self.assertNotIn("/metadata/finalizers", source)
        self.assertNotRegex(
            source.lower(),
            r"(azurecluster|azuremachinepool|natgateway).{0,120}finalizers.{0,120}"
            r"(patch|replace)",
        )

    def test_live_gate_recipe_exists_but_is_not_invoked_by_tests(self):
        root = Path(__file__).resolve().parents[1]
        justfile = (root / "Justfile").read_text(encoding="utf-8")
        script = (
            root / "scripts" / "test_azure_tenant_lifecycle.py"
        ).read_text(encoding="utf-8")
        self.assertIn("azure-test-tenant-lifecycle", justfile)
        self.assertIn("destroy-legacy-foundation", script)
        self.assertIn("targeted-delete-absent", script)
        self.assertIn('"recreation"', script)
        self.assertIn('"status", "--porcelain", "--untracked-files=no"', script)
        self.assertIn('"revision": revision', script)


if __name__ == "__main__":
    unittest.main()
