// compute.bicep — Container Apps Environment (Consumption) + Container Apps Job.
//
// Four scheduled triggers (identical job, four cron entries) handle Sat+Sun × DST:
//   50 9 * * 6   = 09:50 UTC = 05:50 EDT Saturday  (UTC-4, Mar-Nov)
//   50 10 * * 6  = 10:50 UTC = 05:50 EST Saturday  (UTC-5, Nov-Mar)
//   50 9 * * 0   = 09:50 UTC = 05:50 EDT Sunday    (UTC-4, Mar-Nov)
//   50 10 * * 0  = 10:50 UTC = 05:50 EST Sunday    (UTC-5, Nov-Mar)
//
// The bot's DST gate (wall-clock ET hour == 5 check) ensures the wrong-half
// cron exits immediately. This is identical to the book.yml pattern.
// See: infra/AZURE_PLAN.md §5.3 (DST), §5.1 (jitter), §5.2 (cold-start)
//
// Concurrency control:
//   parallelism = 1            — one replica per execution (no concurrent replicas)
//   replicaCompletionCount = 1 — execution completes when that one replica finishes
//   replicaRetryLimit = 0      — no ACA-level retry; bot handles retry internally
//   replicaTimeout = 900       — 15 minutes; matches v0 timeout-minutes: 15
// See: infra/AZURE_PLAN.md §6.2 (concurrency safety), §4 (hard-coded constants)
//
// Identity: user-assigned managed identity (MI). A single MI resource is
// created by identity.bicep and passed in here as userAssignedIdentityResourceId
// and userAssignedIdentityClientId. Both ACA job resources (EDT + EST) reference
// the SAME MI, so RBAC assignments in keyvault/registry/storage cover both jobs
// with a single principalId. There is NO system-assigned MI.
// See: infra/AZURE_PLAN.md §7.2
//
// Secret injection:
//   Secrets are declared with keyVaultUrl references (platform resolves at
//   container start via the user-assigned MI). Env vars reference secret names
//   via secretRef. Bot reads from os.environ — no SDK changes required.
//   Storage account name is passed as a plain (non-secret) env var; the bot
//   uses DefaultAzureCredential for Blob Storage access, not a connection string.
// See: infra/AZURE_PLAN.md §7.3 (injection pattern)

targetScope = 'resourceGroup'

// ---------------------------------------------------------------------------
// Parameters
// ---------------------------------------------------------------------------

@description('Environment name suffix.')
param envName string

@description('Azure region.')
param location string

@description('Full container image reference (e.g. teetimedev<suffix>.azurecr.io/teetime:v1.0.0).')
param containerImage string

@description('Resource ID of the user-assigned managed identity (from identity.bicep). Assigned to both ACA job resources so KV/ACR/Storage RBAC covers both.')
param userAssignedIdentityResourceId string

@description('Client ID of the user-assigned managed identity. Required in the ACA job identity block for KV secret resolution.')
param userAssignedIdentityClientId string

@description('Key Vault URI for secret references (e.g. https://kv-teetime-dev-xxxx.vault.azure.net/).')
param keyVaultUri string

@description('Storage account name passed as a plain env var. Bot uses DefaultAzureCredential for Blob Storage; no connection string needed.')
param storageAccountName string

@description('Log Analytics Workspace resource ID for ACA environment diagnostics.')
param logAnalyticsWorkspaceId string

@description('Log Analytics Workspace primary shared key.')
@secure()
param logAnalyticsWorkspaceKey string

// ---------------------------------------------------------------------------
// Variables
// ---------------------------------------------------------------------------

var acaEnvName = 'cae-teetime-${envName}'
var jobName = 'teetime-job-${envName}'

// DST cron expressions (UTC). Four crons: Sat+Sun × EDT+EST.
// DST gate in bot selects correct half; wrong-half cron exits immediately.
// See: infra/AZURE_PLAN.md §5.3
var cronEdtSat = '50 9 * * 6'    // 09:50 UTC = 05:50 EDT Saturday
var cronEstSat = '50 10 * * 6'   // 10:50 UTC = 05:50 EST Saturday
var cronEdtSun = '50 9 * * 0'    // 09:50 UTC = 05:50 EDT Sunday
var cronEstSun = '50 10 * * 0'   // 10:50 UTC = 05:50 EST Sunday

// Hard-coded parallelism settings. See AZURE_PLAN.md §4.
var replicaTimeout = 900      // 15 minutes in seconds
var replicaRetryLimit = 0     // bot handles retry; ACA retry would bypass idempotency
var parallelism = 1
var replicaCompletionCount = 1

// Secret names in the ACA job secrets block (these are ACA-internal names,
// not KV secret names). Each maps to a KV secret via keyVaultUrl.
// Naming convention: lowercase-hyphenated for ACA; env var names use UPPER_SNAKE.

// ---------------------------------------------------------------------------
// Resources
// ---------------------------------------------------------------------------

// TODO(M-azure-T6): implement Container Apps Environment resource.
// Resource type: Microsoft.App/managedEnvironments
// Key properties:
//   appLogsConfiguration.destination: 'log-analytics'
//   appLogsConfiguration.logAnalyticsConfiguration.customerId: <workspace customer ID>
//   appLogsConfiguration.logAnalyticsConfiguration.sharedKey: logAnalyticsWorkspaceKey
//   zoneRedundant: false   (Consumption plan; zone redundancy requires Dedicated)
//   workloadProfiles: []   (empty = Consumption plan)
// Reference: https://learn.microsoft.com/en-us/azure/templates/microsoft.app/managedenvironments
// NOTE: the Log Analytics workspace customer ID is retrieved via
//   reference(logAnalyticsWorkspaceId).customerId
// The shared key is passed as logAnalyticsWorkspaceKey (secureString).

// TODO(M-azure-T6): implement Container Apps Job resource — EDT half.
// Two separate job resources, one per cron entry, so each has an independent
// execution history and can be independently disabled without touching the other.
// A single job with two scheduleTriggerConfigs is NOT supported by the ACA
// ARM/Bicep API (scheduleTriggerConfig accepts one cronExpression). Therefore
// deploy two job resources sharing the same image and config.
//
// Resource type: Microsoft.App/jobs
// Name: '${jobName}-edt'
// Key properties:
//   identity.type: 'UserAssigned'
//   identity.userAssignedIdentities: { '${userAssignedIdentityResourceId}': {} }
//   configuration.triggerType: 'Schedule'
//   configuration.scheduleTriggerConfig.cronExpression: cronEdtHalf
//   configuration.scheduleTriggerConfig.parallelism: parallelism
//   configuration.scheduleTriggerConfig.replicaCompletionCount: replicaCompletionCount
//   configuration.replicaRetryLimit: replicaRetryLimit
//   configuration.replicaTimeout: replicaTimeout
//   configuration.secrets: [
//     { name: 'mb-username',    keyVaultUrl: '${keyVaultUri}secrets/MB-USERNAME',    identity: userAssignedIdentityClientId }
//     { name: 'mb-password',    keyVaultUrl: '${keyVaultUri}secrets/MB-PASSWORD',    identity: userAssignedIdentityClientId }
//     { name: 'smtp-host',      keyVaultUrl: '${keyVaultUri}secrets/SMTP-HOST',      identity: userAssignedIdentityClientId }
//     { name: 'smtp-user',      keyVaultUrl: '${keyVaultUri}secrets/SMTP-USER',      identity: userAssignedIdentityClientId }
//     { name: 'smtp-pass',      keyVaultUrl: '${keyVaultUri}secrets/SMTP-PASS',      identity: userAssignedIdentityClientId }
//     { name: 'player1-email',         keyVaultUrl: '${keyVaultUri}secrets/PLAYER1-EMAIL',         identity: userAssignedIdentityClientId }
//     { name: 'player1-phone',         keyVaultUrl: '${keyVaultUri}secrets/PLAYER1-PHONE',         identity: userAssignedIdentityClientId }
//     { name: 'twocaptcha-api-key',    keyVaultUrl: '${keyVaultUri}secrets/TWOCAPTCHA-API-KEY',    identity: userAssignedIdentityClientId }
//   ]
//   NOTE: No storage-conn secret. Storage account name is a plain env var;
//   the bot uses DefaultAzureCredential (picks up the user-assigned MI
//   automatically). See AZURE_PLAN.md §6.4 and §7.1.
//   template.containers[0]:
//     image: containerImage
//     name: 'teetime'
//     resources.cpu: '0.25'
//     resources.memory: '0.5'
//       NOTE: ACA Jobs memory is specified as a unitless decimal GiB string
//       ('0.5', not '0.5Gi'). The ARM layer accepts both, but the canonical
//       form emitted by the platform and expected by some API versions is the
//       unitless form. Verify against target apiVersion before implementing:
//       https://learn.microsoft.com/en-us/azure/templates/microsoft.app/jobs
//     env: [
//       { name: 'MB_USERNAME',                secretRef: 'mb-username' }
//       { name: 'MB_PASSWORD',                secretRef: 'mb-password' }
//       { name: 'SMTP_HOST',                  secretRef: 'smtp-host' }
//       { name: 'SMTP_USER',                  secretRef: 'smtp-user' }
//       { name: 'SMTP_PASS',                  secretRef: 'smtp-pass' }
//       { name: 'PLAYER1_EMAIL',              secretRef: 'player1-email' }
//       { name: 'PLAYER1_PHONE',              secretRef: 'player1-phone' }
//       { name: 'TWOCAPTCHA_API_KEY',         secretRef: 'twocaptcha-api-key' }
//       { name: 'AZURE_STORAGE_ACCOUNT_NAME', value: storageAccountName }
//       { name: 'TEETIME_ENV',                value: envName }
//     ]
// Reference: https://learn.microsoft.com/en-us/azure/templates/microsoft.app/jobs
// See: AZURE_PLAN.md §5.3 (DST), §7.3 (Key Vault injection)

// TODO(M-azure-T6): implement Container Apps Job resource — EST half.
// Name: '${jobName}-est'
// Identical to the EDT job except:
//   configuration.scheduleTriggerConfig.cronExpression: cronEstHalf
// All other properties identical (same userAssignedIdentityResourceId,
// same secrets block, same env block).
// See: AZURE_PLAN.md §5.3

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------

@description('Container Apps Job name (EDT half). For az containerapp job start manual trigger.')
output jobName string = '${jobName}-edt'

@description('Container Apps Environment resource ID.')
output acaEnvironmentId string = 'TODO(M-azure-T6): acaEnv.id'

// NOTE: There are no per-job principalId outputs. RBAC is handled by the
// single user-assigned MI (identity.bicep). The principalId is already wired
// from identity.outputs.principalId to keyvault/registry/storage before
// compute.bicep runs. No post-compute RBAC pass needed.
