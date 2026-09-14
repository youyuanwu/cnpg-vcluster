targetScope = 'resourceGroup'

param prefix string
param location string
param aksKubernetesVersion string
param aksNodeSku string
param aksNodeCount int
param vnetCidr string
param aksSubnetCidr string
param tenantSubnetCidr string
param aksPodCidr string
param aksServiceCidr string
param aksDnsServiceIP string

var commonTags = {
  'cnpg-vcluster-experiment': 'azure-capi'
  'cnpg-vcluster-prefix': prefix
  'cnpg-vcluster-owner': 'cnpg-vcluster'
}
var aksName = '${prefix}-mgmt'
var identityName = '${prefix}-identity'
var contributorRoleDefinitionId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  'b24988ac-6180-42a0-ab88-20f7382dd24c'
)

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: '${prefix}-vnet'
  location: location
  tags: commonTags
  properties: {
    addressSpace: {
      addressPrefixes: [
        vnetCidr
      ]
    }
  }
}

resource aksSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' = {
  parent: vnet
  name: 'aks'
  properties: {
    addressPrefix: aksSubnetCidr
  }
}

resource tenantSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' = {
  parent: vnet
  name: 'tenant'
  properties: {
    addressPrefix: tenantSubnetCidr
  }
}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: identityName
  location: location
  tags: commonTags
}

resource contributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, identity.id, contributorRoleDefinitionId)
  properties: {
    roleDefinitionId: contributorRoleDefinitionId
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource aks 'Microsoft.ContainerService/managedClusters@2024-10-01' = {
  name: aksName
  location: location
  tags: commonTags
  sku: {
    name: 'Base'
    tier: 'Free'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    dnsPrefix: aksName
    kubernetesVersion: aksKubernetesVersion
    enableRBAC: true
    disableLocalAccounts: false
    oidcIssuerProfile: {
      enabled: true
    }
    securityProfile: {
      workloadIdentity: {
        enabled: true
      }
    }
    addonProfiles: {
      azurepolicy: {
        enabled: false
      }
    }
    agentPoolProfiles: [
      {
        name: 'system'
        count: aksNodeCount
        vmSize: aksNodeSku
        osDiskSizeGB: 30
        osType: 'Linux'
        osSKU: 'Ubuntu'
        type: 'VirtualMachineScaleSets'
        mode: 'System'
        vnetSubnetID: aksSubnet.id
        enableAutoScaling: false
        maxPods: 30
        orchestratorVersion: aksKubernetesVersion
        upgradeSettings: {
          maxSurge: '33%'
        }
      }
    ]
    networkProfile: {
      networkPlugin: 'azure'
      networkPluginMode: 'overlay'
      loadBalancerSku: 'standard'
      outboundType: 'loadBalancer'
      podCidr: aksPodCidr
      serviceCidr: aksServiceCidr
      dnsServiceIP: aksDnsServiceIP
    }
  }
}

resource aksContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, aks.id, contributorRoleDefinitionId)
  properties: {
    roleDefinitionId: contributorRoleDefinitionId
    principalId: aks.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource capzFederation 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-01-31' = {
  parent: identity
  name: 'capz-manager'
  properties: {
    audiences: [
      'api://AzureADTokenExchange'
    ]
    issuer: aks.properties.oidcIssuerProfile.issuerURL
    subject: 'system:serviceaccount:capz-system:capz-manager'
  }
}

resource asoFederation 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-01-31' = {
  parent: identity
  name: 'azureserviceoperator-default'
  dependsOn: [
    capzFederation
  ]
  properties: {
    audiences: [
      'api://AzureADTokenExchange'
    ]
    issuer: aks.properties.oidcIssuerProfile.issuerURL
    subject: 'system:serviceaccount:capz-system:azureserviceoperator-default'
  }
}

output aksName string = aks.name
output aksId string = aks.id
output aksNodeResourceGroup string = aks.properties.nodeResourceGroup
output aksOidcIssuer string = aks.properties.oidcIssuerProfile.issuerURL
output vnetName string = vnet.name
output vnetId string = vnet.id
output aksSubnetName string = aksSubnet.name
output aksSubnetId string = aksSubnet.id
output tenantSubnetName string = tenantSubnet.name
output tenantSubnetId string = tenantSubnet.id
output identityName string = identity.name
output identityId string = identity.id
output identityClientId string = identity.properties.clientId
output identityPrincipalId string = identity.properties.principalId
output tenantId string = identity.properties.tenantId
output roleAssignmentId string = contributor.id
output aksRoleAssignmentId string = aksContributor.id
