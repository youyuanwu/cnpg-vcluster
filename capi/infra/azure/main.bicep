targetScope = 'subscription'

@minLength(2)
@maxLength(20)
param prefix string

param location string
param aksKubernetesVersion string
param aksNodeSku string
param aksNodeCount int = 2
param vnetCidr string
param aksSubnetCidr string
param tenantSubnetCidr string
param aksPodCidr string
param aksServiceCidr string
param aksDnsServiceIP string

var resourceGroupName = '${prefix}-rg'

resource resourceGroup 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
  tags: {
    'cnpg-vcluster-experiment': 'azure-capi'
    'cnpg-vcluster-prefix': prefix
    'cnpg-vcluster-owner': 'cnpg-vcluster'
  }
}

module foundation './foundation.bicep' = {
  name: '${prefix}-foundation'
  scope: resourceGroup
  params: {
    prefix: prefix
    location: location
    aksKubernetesVersion: aksKubernetesVersion
    aksNodeSku: aksNodeSku
    aksNodeCount: aksNodeCount
    vnetCidr: vnetCidr
    aksSubnetCidr: aksSubnetCidr
    tenantSubnetCidr: tenantSubnetCidr
    aksPodCidr: aksPodCidr
    aksServiceCidr: aksServiceCidr
    aksDnsServiceIP: aksDnsServiceIP
  }
}

output resourceGroupName string = resourceGroup.name
output resourceGroupId string = resourceGroup.id
output aksName string = foundation.outputs.aksName
output aksId string = foundation.outputs.aksId
output aksNodeResourceGroup string = foundation.outputs.aksNodeResourceGroup
output aksOidcIssuer string = foundation.outputs.aksOidcIssuer
output vnetName string = foundation.outputs.vnetName
output vnetId string = foundation.outputs.vnetId
output aksSubnetName string = foundation.outputs.aksSubnetName
output aksSubnetId string = foundation.outputs.aksSubnetId
output tenantSubnetName string = foundation.outputs.tenantSubnetName
output tenantSubnetId string = foundation.outputs.tenantSubnetId
output identityName string = foundation.outputs.identityName
output identityId string = foundation.outputs.identityId
output identityClientId string = foundation.outputs.identityClientId
output identityPrincipalId string = foundation.outputs.identityPrincipalId
output tenantId string = foundation.outputs.tenantId
output roleAssignmentId string = foundation.outputs.roleAssignmentId
output aksRoleAssignmentId string = foundation.outputs.aksRoleAssignmentId
