// registry.bicep — Azure Container Registry (ACR) Basic SKU.
// Hosts the bot container image. The Container Apps Job pulls from this
// registry using a system-assigned managed identity with the AcrPull role.
// ACR admin account is disabled; no password stored anywhere.
//
// Cost: ~$5.00/mo flat for Basic SKU (East US 2, April 2026).
// See: infra/AZURE_PLAN.md §2 (service selection), §7.2 (AcrPull role),
//      §9.1 (cost estimate)

targetScope = 'resourceGroup'

// ---------------------------------------------------------------------------
// Parameters
// ---------------------------------------------------------------------------

@description('Environment name suffix.')
param envName string

@description('Azure region.')
param location string

@description('ACR SKU tier.')
@allowed(['Basic', 'Standard', 'Premium'])
param acrSku string = 'Basic'

@description('Principal ID of the user-assigned managed identity (from identity.bicep) for AcrPull role assignment.')
// Wired from identity.outputs.principalId in main.bicep. Because identity.bicep
// deploys before this module, the principalId is known at registry deploy time.
// See: infra/AZURE_PLAN.md §7.2
param jobPrincipalId string

// ---------------------------------------------------------------------------
// Variables
// ---------------------------------------------------------------------------

// TODO(M-azure-T2): ACR name must be globally unique, 5–50 chars, alphanumeric.
// Proposed: teetime{envName}{uniqueSuffix} where uniqueSuffix is derived from
// subscription ID. See AZURE_PLAN.md §12 Q7.
// Use uniqueString() built-in for deterministic suffix.
var acrName = 'teetime${envName}${uniqueString(resourceGroup().id)}'

// AcrPull built-in role GUID — stable across all Azure environments.
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'

// ---------------------------------------------------------------------------
// Resources
// ---------------------------------------------------------------------------

// TODO(M-azure-T2): implement ACR resource.
// Resource type: Microsoft.ContainerRegistry/registries
// Properties to set:
//   sku.name: acrSku
//   adminUserEnabled: false  (use managed identity, not admin creds)
//   publicNetworkAccess: 'Enabled'  (ACA pulls over public endpoint)
//   zoneRedundancy: 'Disabled'  (Basic SKU does not support zone redundancy)
// Reference: https://learn.microsoft.com/en-us/azure/templates/microsoft.containerregistry/registries

// TODO(M-azure-T2): implement AcrPull role assignment for job MI.
// Only create if jobPrincipalId is non-empty (to allow staging deployment
// before compute is deployed).
// Resource type: Microsoft.Authorization/roleAssignments
// Properties:
//   roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
//   principalId: jobPrincipalId
//   principalType: 'ServicePrincipal'
// Scope: the ACR resource (use ::roleAssignments syntax in Bicep)
// See: infra/AZURE_PLAN.md §7.2

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------

@description('ACR login server (e.g. teetimedev<suffix>.azurecr.io). Used in containerImage parameter.')
output loginServer string = 'TODO(M-azure-T2): acr.properties.loginServer'

@description('ACR resource name.')
output acrName string = acrName
