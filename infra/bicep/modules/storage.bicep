// storage.bicep — Azure Blob Storage for cross-run SQLite state persistence.
// Replaces v0 actions/cache. The bot downloads teetime.db at job start,
// acquires an exclusive blob lease, and uploads on exit.
//
// Blob layout:
//   Storage Account: teetime{envName}sa{suffix} (LRS, Hot)
//     Container: teetime-state
//       Blob: teetime.db
//
// See: infra/AZURE_PLAN.md §6 (state persistence), §6.2 (blob lease),
//      §6.3 (soft-delete DR)

targetScope = 'resourceGroup'

// ---------------------------------------------------------------------------
// Parameters
// ---------------------------------------------------------------------------

@description('Environment name suffix.')
param envName string

@description('Azure region.')
param location string

@description('Principal ID of the user-assigned managed identity (from identity.bicep). Grants Storage Blob Data Contributor so the job can acquire blob leases and upload the SQLite file.')
// Wired from identity.outputs.principalId in main.bicep. Because identity.bicep
// deploys before this module, the principalId is known at storage deploy time.
// See: infra/AZURE_PLAN.md §7.2
param jobPrincipalId string

// ---------------------------------------------------------------------------
// Variables
// ---------------------------------------------------------------------------

// Storage account names: 3–24 chars, lowercase alphanumeric only.
// Must be globally unique. uniqueString() is deterministic per resource group.
// See: infra/AZURE_PLAN.md §12 Q8
var storageAccountName = 'teetime${envName}sa${take(uniqueString(resourceGroup().id), 6)}'

// Blob container name — hard-coded; matches the constant the bot code uses.
// See: infra/AZURE_PLAN.md §4 (hard-coded constants)
var blobContainerName = 'teetime-state'

// Storage Blob Data Contributor role GUID — stable Azure built-in.
// Grants read, write, delete, and lease operations on blobs.
// See: infra/AZURE_PLAN.md §7.2
var storageBlobDataContributorRoleId = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'

// ---------------------------------------------------------------------------
// Resources
// ---------------------------------------------------------------------------

// Storage account — LRS Hot, StorageV2, no public blob access.
// publicNetworkAccess is 'Enabled' because ACA accesses over the public
// endpoint; no VNet integration in v1. See: AZURE_PLAN.md §11.
resource storageAccount 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: storageAccountName
  location: location
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    accessTier: 'Hot'
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
    // The bot authenticates with the user-assigned MI via DefaultAzureCredential.
    // No connection string / account key is ever used (AZURE_PLAN.md §7.1), so
    // shared-key auth is disabled — a leaked key would otherwise bypass all RBAC
    // scoping. Security review H2.
    allowSharedKeyAccess: false
    // ACA Consumption has no static egress IP to allow-list, so the account stays
    // on the public endpoint gated by AAD RBAC. Accepted risk (security review H1).
    publicNetworkAccess: 'Enabled'
  }
}

// Blob service configuration — enables BOTH blob soft-delete AND container
// soft-delete with 7-day retention. These are separate properties:
//   deleteRetentionPolicy: protects individual blobs (e.g. teetime.db deleted).
//   containerDeleteRetentionPolicy: protects the container itself (e.g.
//     teetime-state accidentally deleted). Blob soft-delete alone cannot
//     recover a deleted container; both must be set for full DR coverage.
// Versioning is disabled — soft-delete is sufficient and versioning adds cost.
// See: AZURE_PLAN.md §6.3 (SF3 resolution)
resource blobServices 'Microsoft.Storage/storageAccounts/blobServices@2023-01-01' = {
  parent: storageAccount
  name: 'default'
  properties: {
    deleteRetentionPolicy: {
      enabled: true
      days: 7
    }
    containerDeleteRetentionPolicy: {
      enabled: true
      days: 7
    }
    isVersioningEnabled: false
  }
}

// Blob container 'teetime-state' — no public access.
// Hard-coded name matches the constant the bot reads from the env/config.
resource blobContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-01-01' = {
  parent: blobServices
  name: blobContainerName
  properties: {
    publicAccess: 'None'
  }
}

// Storage Blob Data Contributor role assignment for the Container Apps Job MI.
// Scoped to the storage account (not subscription/RG) for minimum privilege.
// Required for: blob download, blob upload, and exclusive blob lease operations.
// Only created when jobPrincipalId is non-empty — allows staging this module
// before identity.bicep has resolved a real principalId.
// Name is a deterministic GUID so re-deploys are fully idempotent.
// See: AZURE_PLAN.md §7.2
resource roleAssignmentStorageBlobDataContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(jobPrincipalId)) {
  name: guid(storageAccount.id, jobPrincipalId, storageBlobDataContributorRoleId)
  scope: storageAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataContributorRoleId)
    principalId: jobPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------

@description('Storage account name. Passed as a plain (non-secret) env var AZURE_STORAGE_ACCOUNT_NAME to the ACA job. The bot uses DefaultAzureCredential for access; no connection string is stored in Key Vault.')
output storageAccountName string = storageAccount.name

@description('Blob container name. Hard-coded to teetime-state; exposed for reference.')
output blobContainerName string = blobContainerName

@description('Storage account resource ID. Used for role assignment scope in other modules.')
output storageAccountResourceId string = storageAccount.id
