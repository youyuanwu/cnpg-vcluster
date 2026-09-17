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
    _azure_tags,
    _capture_tenant_kubeconfig,
    _collect_ready_observations,
    _foundation_defaults_checksum,
    _reconcile_manifest,
    _render_addon_job,
    _render_tenant_control_plane,
    _render_worker_pool,
    _run_profile_mutation,
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

        unknown = {
            "kind": "VirtualNetwork",
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
                    [], 0, stdout="virtualnetworks.network.azure.com\n", stderr=""
                ),
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

    def test_ready_status_requires_matching_current_evidence_and_identities(self):
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


if __name__ == "__main__":
    unittest.main()
