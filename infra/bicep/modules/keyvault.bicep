// keyvault.bicep — Azure Key Vault (Standard) for bot credentials.
// Grants Key Vault Secrets User role to the Container Apps Job MI.
// Soft-delete: 90 days (default for new vaults, always on since 2019).
// Purge protection: EXPLICITLY ENABLED here — not on by default. Irreversible.
//
// Secret names stored here (operator must populate after deploy):
//   MB-USERNAME, MB-PASSWORD, SMTP-HOST, SMTP-USER, SMTP-PASS,
//   PLAYER1-EMAIL, PLAYER1-PHONE
//   (plus any additional PLAYER* secrets)
//
// NOTE: STORAGE-CONN-STR is NOT stored here. The bot uses DefaultAzureCredential
// with Storage Blob Data Contributor (via user-assigned MI) for Blob access.
// The storage account name is passed as a plain (non-secret) env var.
// See: infra/AZURE_PLAN.md §7.1, §6.4
//
// See: infra/AZURE_PLAN.md §7 (secrets & identity), §7.3 (injection pattern),
//      §7.4 (secret rotation), §11 (security checklist)
//
// IMPORTANT: Legacy access policies are NOT used. RBAC is the only access
// control mechanism. Access policies must remain disabled in the vault properties.

targetScope = 'resourceGroup'

// ---------------------------------------------------------------------------
// Parameters
// ---------------------------------------------------------------------------

@description('Environment name suffix.')
param envName string

@description('Azure region.')
param location string

@description('Key Vault SKU name.')
@allowed(['standard', 'premium'])
param kvSku string = 'standard'

@description('Principal ID of the user-assigned managed identity (from identity.bicep) for Key Vault Secrets User role assignment.')
// Wired from identity.outputs.principalId in main.bicep. Because identity.bicep
// deploys before this module, the principalId is known at keyvault deploy time —
// no chicken-and-egg dependency. See AZURE_PLAN.md §7.2.
param jobPrincipalId string

// ---------------------------------------------------------------------------
// Variables
// ---------------------------------------------------------------------------

// KV name: 3–24 chars, alphanumeric and hyphens, must start with letter.
// Must be globally unique. See AZURE_PLAN.md §12 Q9.
var kvName = 'kv-teetime-${envName}-${take(uniqueString(resourceGroup().id), 4)}'

// Key Vault Secrets User — read-only on secret contents. Stable Azure built-in GUID.
// See: https://learn.microsoft.com/en-us/azure/key-vault/general/rbac-guide
var kvSecretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6'

// ---------------------------------------------------------------------------
// Resources
// ---------------------------------------------------------------------------

// TODO(M-azure-T4): implement Key Vault resource.
// Resource type: Microsoft.KeyVault/vaults
// Key properties:
//   sku.name: kvSku
//   sku.family: 'A'
//   tenantId: tenant().tenantId
//   enableRbacAuthorization: true        (RBAC mode, NOT access policies)
//   enableSoftDelete: true               (default for new vaults; set explicitly)
//   softDeleteRetentionInDays: 90        (default 90; maximum)
//   enablePurgeProtection: true          (NOT default — must be explicit; irreversible)
//   publicNetworkAccess: 'Enabled'       (ACA accesses over public endpoint at container start)
//   networkAcls.defaultAction: 'Allow'  (no VNet restriction; public access for ACA job MI)
// Reference: https://learn.microsoft.com/en-us/azure/templates/microsoft.keyvault/vaults
// See: AZURE_PLAN.md §7, §11

// TODO(M-azure-T4): implement Key Vault Secrets User role assignment for job MI.
// Only create if jobPrincipalId is non-empty.
// Resource type: Microsoft.Authorization/roleAssignments (scoped to the vault)
// Properties:
//   roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', kvSecretsUserRoleId)
//   principalId: jobPrincipalId
//   principalType: 'ServicePrincipal'
// Scope: the Key Vault resource (vault::roleAssignments in Bicep)
// See: infra/AZURE_PLAN.md §7.2
// IMPORTANT: Do NOT grant Key Vault Secrets Officer or higher — Secrets User is read-only.

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------

@description('Key Vault URI (https://<name>.vault.azure.net/). Used in compute.bicep for keyVaultUrl secret references.')
output vaultUri string = 'TODO(M-azure-T4): keyVault.properties.vaultUri'

@description('Key Vault resource name.')
output vaultName string = kvName
