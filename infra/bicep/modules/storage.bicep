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

// TODO(M-azure-T3): implement storage account resource.
// Resource type: Microsoft.Storage/storageAccounts
// Key properties:
//   kind: 'StorageV2'
//   sku.name: 'Standard_LRS'
//   accessTier: 'Hot'
//   minimumTlsVersion: 'TLS1_2'
//   supportsHttpsTrafficOnly: true
//   allowBlobPublicAccess: false  (no public blob access)
//   publicNetworkAccess: 'Enabled'  (ACA accesses over public endpoint)
// Reference: https://learn.microsoft.com/en-us/azure/templates/microsoft.storage/storageaccounts

// TODO(M-azure-T3): implement blob service properties child resource.
// Resource type: Microsoft.Storage/storageAccounts/blobServices
// Key properties:
//   deleteRetentionPolicy.enabled: true
//   deleteRetentionPolicy.days: 7
//     (This enables blob soft-delete for the 7-day DR window — AZURE_PLAN.md §6.3)
//   containerDeleteRetentionPolicy.enabled: true
//   containerDeleteRetentionPolicy.days: 7
//     IMPORTANT: blob soft-delete and container soft-delete are SEPARATE properties.
//     deleteRetentionPolicy protects individual blobs (e.g. teetime.db deleted).
//     containerDeleteRetentionPolicy protects the container itself (e.g. teetime-state
//     deleted). Both must be set; only blob soft-delete is insufficient for full
//     DR coverage. If the container is deleted, blob soft-delete cannot recover it.
//     See: AZURE_PLAN.md §6.3 (SF3 resolution)
// Note: blob versioning is disabled (adds cost; soft-delete is sufficient).

// TODO(M-azure-T3): implement blob container child resource.
// Resource type: Microsoft.Storage/storageAccounts/blobServices/containers
// Name: blobContainerName ('teetime-state')
// Key properties:
//   publicAccess: 'None'

// TODO(M-azure-T3): implement role assignment for job MI.
// Only create if jobPrincipalId is non-empty.
// Resource type: Microsoft.Authorization/roleAssignments (on storage account scope)
// Role: Storage Blob Data Contributor (storageBlobDataContributorRoleId)
// principalType: 'ServicePrincipal'
// See: infra/AZURE_PLAN.md §7.2
// NOTE: Storage Blob Data Contributor is required (not Storage Blob Data Reader)
// because the bot needs to acquire a blob lease and upload (write) the blob.

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------

@description('Storage account name. Passed as a plain (non-secret) env var AZURE_STORAGE_ACCOUNT_NAME to the ACA job. The bot uses DefaultAzureCredential for access; no connection string is stored in Key Vault.')
output storageAccountName string = storageAccountName

@description('Blob container name. Hard-coded to teetime-state; exposed for reference.')
output blobContainerName string = blobContainerName

@description('Storage account resource ID. Used for role assignment scope in other modules.')
output storageAccountResourceId string = 'TODO(M-azure-T3): storageAccount.id'
