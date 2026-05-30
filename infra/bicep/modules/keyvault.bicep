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

@description('Whether to enable purge protection on the Key Vault. Dev passes false so the vault can be torn down and recreated during iteration. Prod must pass true. NOTE: once enabled, purge protection cannot be disabled for the vault\'s lifetime (irreversible). Soft-delete cannot be disabled regardless of this setting.')
// AZURE_PLAN.md §7.4 and §11: "Key Vault purge protection: EXPLICITLY ENABLED".
// Parameterised here because AZ will block deletion of a vault with purge
// protection enabled (soft-delete means it lands in deleted state; purge
// protection prevents the permanent purge). For dev iteration, pass false.
param enablePurgeProtection bool = true

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

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: kvName
  location: location
  tags: {
    environment: envName
    managedBy: 'bicep'
  }
  properties: {
    sku: {
      name: kvSku
      family: 'A'
    }
    tenantId: tenant().tenantId

    // RBAC mode — legacy access policies are NOT used. See file header.
    enableRbacAuthorization: true

    // Soft-delete: on by default for vaults created since 2019, but set
    // explicitly so the property is visible in what-if diffs and code review.
    enableSoftDelete: true
    softDeleteRetentionInDays: 90

    // Purge protection: NOT on by default — must be explicit. See param comment.
    // Irreversible once enabled; soft-delete cannot be disabled regardless.
    // Azure rejects enablePurgeProtection=false explicitly ("cannot be set to
    // false … irreversible action"). The property accepts only `true` or being
    // ABSENT, so emit true when enabled and null (Bicep omits it) otherwise.
    enablePurgeProtection: enablePurgeProtection ? true : null

    // ACA resolves KV secret references over the public endpoint at container
    // start using the job's managed identity. No VNet integration required for
    // this plan. See: AZURE_PLAN.md §11.
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      defaultAction: 'Allow'
      bypass: 'AzureServices'
    }
  }
}

// Key Vault Secrets User for the Container Apps Job MI.
// Only created when jobPrincipalId is provided (allows staging KV before compute).
// Role: read-only on secret contents. NOT Secrets Officer or higher.
// Name: deterministic GUID so re-deploys are idempotent.
// See: AZURE_PLAN.md §7.2, §7.3
resource kvSecretsUserAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(jobPrincipalId)) {
  name: guid(keyVault.id, jobPrincipalId, kvSecretsUserRoleId)
  scope: keyVault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', kvSecretsUserRoleId)
    principalId: jobPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------

@description('Key Vault URI (https://<name>.vault.azure.net/). Used in compute.bicep for keyVaultUrl secret references.')
output vaultUri string = keyVault.properties.vaultUri

@description('Key Vault resource name.')
output vaultName string = kvName
