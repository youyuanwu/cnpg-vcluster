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
var acrName = toLower(replace('${prefix}acr', '-', ''))
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

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: acrName
  location: location
  tags: commonTags
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false
    publicNetworkAccess: 'Enabled'
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

module acrPull './acr-pull.bicep' = {
  name: '${prefix}-acr-pull'
  params: {
    acrName: acr.name
    kubeletPrincipalId: aks.properties.identityProfile.kubeletidentity.objectId
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

resource databaseIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${prefix}-database-controller'
  location: location
  tags: commonTags
}

resource databaseDiskRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(resourceGroup().id, 'database-disk-read-delete')
  properties: {
    roleName: '${prefix}-database-disk-read-delete'
    description: 'Read and remove exact managed disks during catalog entry finalization'
    type: 'CustomRole'
    permissions: [
      {
        actions: [
          'Microsoft.Compute/disks/read'
          'Microsoft.Compute/disks/delete'
        ]
        notActions: []
        dataActions: []
        notDataActions: []
      }
    ]
    assignableScopes: [
      resourceGroup().id
    ]
  }
}

resource databaseDiskAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, databaseIdentity.id, databaseDiskRole.id)
  properties: {
    roleDefinitionId: databaseDiskRole.id
    principalId: databaseIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource databaseFederation 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-01-31' = {
  parent: databaseIdentity
  name: 'database-controller'
  properties: {
    audiences: [
      'api://AzureADTokenExchange'
    ]
    issuer: aks.properties.oidcIssuerProfile.issuerURL
    subject: 'system:serviceaccount:tenant-system:database-controller'
  }
}

output aksName string = aks.name
output aksId string = aks.id
output aksNodeResourceGroup string = aks.properties.nodeResourceGroup
output aksOidcIssuer string = aks.properties.oidcIssuerProfile.issuerURL
output aksKubeletPrincipalId string = aks.properties.identityProfile.kubeletidentity.objectId
output acrName string = acr.name
output acrId string = acr.id
output acrLoginServer string = acr.properties.loginServer
output acrPullRoleAssignmentId string = acrPull.outputs.roleAssignmentId
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
output capzFederationId string = capzFederation.id
output asoFederationId string = asoFederation.id
output databaseIdentityId string = databaseIdentity.id
output databaseIdentityClientId string = databaseIdentity.properties.clientId
output databaseIdentityPrincipalId string = databaseIdentity.properties.principalId
output databaseDiskRoleId string = databaseDiskRole.id
output databaseDiskAssignmentId string = databaseDiskAssignment.id
output databaseFederationId string = databaseFederation.id
