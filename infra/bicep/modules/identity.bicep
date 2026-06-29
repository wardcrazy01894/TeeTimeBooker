// identity.bicep — user-assigned managed identity (THE default path).
//
// A single user-assigned MI is created first in the dependency chain. Its
// principalId is known before compute.bicep runs, which breaks the
// chicken-and-egg cycle: keyvault/registry can both receive the
// principalId for RBAC assignments without waiting for the ACA job to exist.
// The ACA job then references this identity by resource ID.
//
// There is NO system-assigned MI path in this plan. System-assigned MI was
// rejected because its principalId is unavailable until after the job resource
// is created, making it impossible to pre-stage RBAC assignments for KV and
// ACR in the same Bicep deployment without a two-pass workaround.
//
// See: infra/AZURE_PLAN.md §7.2

targetScope = 'resourceGroup'

// ---------------------------------------------------------------------------
// Parameters
// ---------------------------------------------------------------------------

@description('Environment name suffix.')
param envName string

@description('Azure region.')
param location string

// ---------------------------------------------------------------------------
// Resources
// ---------------------------------------------------------------------------

resource managedIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'mi-teetime-${envName}'
  location: location
  tags: {
    application: 'teetime'
    environment: envName
    managedBy: 'bicep'
  }
}

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------

@description('Resource ID of the user-assigned managed identity.')
output identityResourceId string = managedIdentity.id

@description('Principal ID of the user-assigned managed identity (for role assignments).')
output principalId string = managedIdentity.properties.principalId

@description('Client ID of the user-assigned managed identity.')
output clientId string = managedIdentity.properties.clientId
