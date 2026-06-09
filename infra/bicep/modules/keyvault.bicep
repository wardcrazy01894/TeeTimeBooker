// keyvault.bicep — Azure Key Vault (Standard) for bot credentials.
// Grants Key Vault Secrets User role to the Container Apps Job MI.
// Soft-delete: 90 days (default for new vaults, always on since 2019).
// Purge protection: EXPLICITLY ENABLED here — not on by default. Irreversible.
// Audit logging: AuditEvent logs shipped to Log Analytics (diagnosticSettings) for forensics.
//
// Secret names stored here (operator must populate after deploy):
//   MB-USERNAME, MB-PASSWORD, PLAYER1-EMAIL, PLAYER1-PHONE, PLAYER1-MB-MEMBER,
//   TWOCAPTCHA-API-KEY, TEETIME-SKIP-DATES
//   (plus any additional PLAYER* secrets)
//   The last (TEETIME-SKIP-DATES, LEADTIME_SKIP_PLAN F2) MUST be pre-created (value " " = no
//   skips; Azure rejects an empty value) BEFORE compute.bicep references it — ACA validates KV
//   secret refs at job-CREATE time, so a missing secret FAILS the deploy. Edit later in the
//   Portal, no redeploy.
//
// NOTE: the bot makes no authenticated Azure SDK calls at runtime (state is
// in-process; no Blob Storage), so no storage connection string or account name
// is needed here. See: infra/AZURE_PLAN.md §7.1.
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

@description('Log Analytics workspace resource ID (from logs.bicep) that receives Key Vault AuditEvent logs. Wiring it here creates an implicit dependency so logs deploys before this module.')
// Captures the data-plane audit trail (who/what read which secret, when) so a suspected
// credential leak has a forensic record. See AZURE_PLAN.md §11 (security checklist).
param logAnalyticsWorkspaceId string

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

// Audit logging: ship Key Vault AuditEvent (data-plane secret reads/writes + control-plane
// events) to Log Analytics. This is the forensic record for a suspected credential leak —
// without it, secret access is invisible. Only the `audit` categoryGroup is sent (no
// metrics) so ingestion stays minimal — audit volume for a single-user bot is tiny
// (well under the workspace's 5 GB/month free tier). See AZURE_PLAN.md §11.
//
// API version 2021-05-01-preview is the latest for Microsoft.Insights/diagnosticSettings;
// there is NO GA (non-preview) version (a known Azure naming quirk — the API is production-
// stable). Don't "downgrade" to an older GA-looking version; none exists.
resource kvAuditDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'kv-audit-to-loganalytics'
  scope: keyVault
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [
      {
        categoryGroup: 'audit'
        enabled: true
      }
    ]
  }
}

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------

@description('Key Vault URI (https://<name>.vault.azure.net/). Used in compute.bicep for keyVaultUrl secret references.')
output vaultUri string = keyVault.properties.vaultUri

@description('Key Vault resource name.')
output vaultName string = kvName
