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
