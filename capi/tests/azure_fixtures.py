import json
import tempfile
from pathlib import Path

from scripts.lib.azure.common import (
    FOUNDATION_INVENTORY_SCHEMA,
    _foundation_defaults_checksum,
    names,
)
from scripts.lib.files import write_private_file
from scripts.lib.tenant_spec import TenantSpec


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
AZURE_TENANT_ALLOCATION_APPROVED_SHA256=2f614c0f2e575eb0544ff6190c05d7242f7e19894bacb1346f4efc9a02b79fe7
AZURE_CONTROLLER_REPOSITORY=tenant-controller
AZURE_CONTROLLER_TAG=v1alpha3
AZURE_ADMIN_REPOSITORY=tenant-admin
AZURE_ADMIN_TAG=v1alpha1
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
    "aksKubeletPrincipalId": "kubelet-principal-id",
    "acrName": "yycvacr",
    "acrId": "/subscriptions/redacted/resourceGroups/yy-cv-rg/providers/Microsoft.ContainerRegistry/registries/yycvacr",
    "acrLoginServer": "yycvacr.azurecr.io",
    "acrPullRoleAssignmentId": "/subscriptions/redacted/resourceGroups/yy-cv-rg/providers/Microsoft.ContainerRegistry/registries/yycvacr/providers/Microsoft.Authorization/roleAssignments/acr-pull",
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
    "controller:tenant-system/tenant-controller": "tenant-controller-uid",
}
CONTROLLER_IMAGE = (
    "yycvacr.azurecr.io/tenant-controller@sha256:"
    "1111111111111111111111111111111111111111111111111111111111111111"
)


class AzureFixtureMixin:
    def make_root(self) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "config" / "azure").mkdir(parents=True)
        (root / "config" / "azure" / "defaults.env").write_text(
            DEFAULTS,
            encoding="utf-8",
        )
        (root / "config" / "azure" / "tenant-allocation-slots.json").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "slots": [
                        {
                            "slotId": "azure-01",
                            "podCIDR": "10.72.0.0/16",
                            "serviceCIDR": "10.142.0.0/16",
                        },
                        {
                            "slotId": "azure-02",
                            "podCIDR": "10.73.0.0/16",
                            "serviceCIDR": "10.143.0.0/16",
                        },
                        {
                            "slotId": "azure-03",
                            "podCIDR": "10.74.0.0/16",
                            "serviceCIDR": "10.144.0.0/16",
                        },
                    ],
                }
            ),
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
        }
        payload.update(overrides)
        return TenantSpec.from_mapping(
            payload,
            expected_profile="azure",
            supported_versions={"azure": "1.32.13"},
        )

    def inventory(self, root: Path, config: dict[str, str]) -> dict[str, object]:
        outputs = {
            "resourceGroupName": "yy-cv-rg",
            "resourceGroupId": FOUNDATION["resourceGroupId"],
            "aksName": "yy-cv-mgmt",
            "aksId": FOUNDATION["aksId"],
            "aksNodeResourceGroup": FOUNDATION["aksNodeResourceGroup"],
            "aksOidcIssuer": FOUNDATION["aksOidcIssuer"],
            "aksKubeletPrincipalId": FOUNDATION["aksKubeletPrincipalId"],
            "acrName": FOUNDATION["acrName"],
            "acrId": FOUNDATION["acrId"],
            "acrLoginServer": FOUNDATION["acrLoginServer"],
            "acrPullRoleAssignmentId": FOUNDATION["acrPullRoleAssignmentId"],
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
            "controllerImage": CONTROLLER_IMAGE,
            "azureProviderConfigUid": "azure-provider-config-uid",
            "azureAllocationConfigUid": "azure-allocation-config-uid",
        }

    def write_inventory(self, root: Path, payload: dict[str, object]) -> Path:
        path = root / ".runtime" / "azure" / "resources.json"
        write_private_file(path, json.dumps(payload))
        return path
