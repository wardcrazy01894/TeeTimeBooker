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

@description('Permanent dry-run flag. When true (the dev default), the bot performs no real booking POSTs. Dev MUST stay in dry-run until production cutover (AZURE_PLAN.md §10.3).')
param dryRun bool = true

// ---------------------------------------------------------------------------
// Variables
// ---------------------------------------------------------------------------

var acaEnvName = 'cae-teetime-${envName}'
var jobName = 'teetime-job-${envName}'
var watchJobName = 'teetime-watch-job-${envName}'

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

// Watch job runs every 10 minutes year-round (no DST gate needed; the
// WatchOrchestrator gates polling hours internally). One HTTP round-trip, so
// a short replicaTimeout is sufficient. See AZURE_PLAN.md §5.4.
var watchCron = '*/10 * * * *'
var watchReplicaTimeout = 120

// Booking-job cron table. A single array + loop avoids four copy-pasted job
// resources. Each entry pairs a resource-name suffix with its UTC cron.
// One job resource per cron => independent execution history; any one cron-half
// can be disabled without touching the others (a single job with multiple
// scheduleTriggerConfigs is NOT supported by the ACA ARM/Bicep API — see stub).
var bookingJobs = [
  { name: '${jobName}-edt-sat', cron: cronEdtSat }
  { name: '${jobName}-est-sat', cron: cronEstSat }
  { name: '${jobName}-edt-sun', cron: cronEdtSun }
  { name: '${jobName}-est-sun', cron: cronEstSun }
]

// Derive the ACR login server from the container image reference. The image is
// '<registry>.azurecr.io/teetime:<tag>'; the registries[].server entry needs
// just the '<registry>.azurecr.io' prefix so the job can pull via the MI's
// AcrPull role. Split on '/' and take the first segment.
var acrLoginServer = split(containerImage, '/')[0]

// Secret names in the ACA job secrets block (these are ACA-internal names,
// not KV secret names). Each maps to a KV secret via keyVaultUrl.
// Naming convention: lowercase-hyphenated for ACA; env var names use UPPER_SNAKE.
//
// IMPORTANT: for keyVaultUrl-backed secrets, ACA expects the identity RESOURCE ID
// (not the client ID) in the `identity` field. The platform uses that MI to fetch
// the secret value at container start.
//
// These secrets MUST exactly cover every *_env name the bot resolves from
// config/container.toml. config.py:_resolve_env RAISES on any referenced env var
// that is missing, so an under-wired job crashes at config load before doing
// anything. The set: course creds (MB-*), Player 1 (account holder) contact +
// member number, and the 2captcha key. Guests 2-4 are name-only (ForeUP's
// booking POST sends only the player count, not guest contact info), so there
// are deliberately NO player2/3/4 secrets here. There is a parity test
// (tests/test_container_config_parity.py) that fails CI if container.toml ever
// references an env var not wired below.
//
// SMTP-* secrets are intentionally OMITTED: the notifications backend is
// 'console' (config/container.toml) until email is enabled. When switching to
// backend = 'email', add smtp-host / smtp-user / smtp-pass secretRefs here (and
// matching SMTP_* env vars below) plus the KV secrets. See AZURE_PLAN.md §12 Q10.
var jobSecrets = [
  { name: 'mb-username',        keyVaultUrl: '${keyVaultUri}secrets/MB-USERNAME',        identity: userAssignedIdentityResourceId }
  { name: 'mb-password',        keyVaultUrl: '${keyVaultUri}secrets/MB-PASSWORD',        identity: userAssignedIdentityResourceId }
  { name: 'player1-email',      keyVaultUrl: '${keyVaultUri}secrets/PLAYER1-EMAIL',      identity: userAssignedIdentityResourceId }
  { name: 'player1-phone',      keyVaultUrl: '${keyVaultUri}secrets/PLAYER1-PHONE',      identity: userAssignedIdentityResourceId }
  { name: 'player1-mb-member',  keyVaultUrl: '${keyVaultUri}secrets/PLAYER1-MB-MEMBER',  identity: userAssignedIdentityResourceId }
  { name: 'twocaptcha-api-key', keyVaultUrl: '${keyVaultUri}secrets/TWOCAPTCHA-API-KEY', identity: userAssignedIdentityResourceId }
]

// Registry block: the job pulls the image from ACR using the user-assigned MI
// (AcrPull granted in registry.bicep). Identity here is the MI resource ID.
var jobRegistries = [
  { server: acrLoginServer, identity: userAssignedIdentityResourceId }
]

// Common container env vars shared by booking + watch jobs. The secretRef
// entries point at the jobSecrets names above; the value entries are plain
// (non-secret) config. AZURE_CLIENT_ID is REQUIRED so DefaultAzureCredential
// selects the user-assigned MI (not a system-assigned one) for Blob Storage.
var commonEnv = [
  { name: 'MB_USERNAME',                secretRef: 'mb-username' }
  { name: 'MB_PASSWORD',                secretRef: 'mb-password' }
  { name: 'PLAYER1_EMAIL',              secretRef: 'player1-email' }
  { name: 'PLAYER1_PHONE',              secretRef: 'player1-phone' }
  { name: 'PLAYER1_MB_MEMBER',          secretRef: 'player1-mb-member' }
  { name: 'TWOCAPTCHA_API_KEY',         secretRef: 'twocaptcha-api-key' }
  { name: 'AZURE_STORAGE_ACCOUNT_NAME', value: storageAccountName }
  { name: 'AZURE_CLIENT_ID',            value: userAssignedIdentityClientId }
  { name: 'TEETIME_ENV',                value: envName }
]

// Resource tags applied to every resource in this module.
var tags = {
  application: 'teetime'
  environment: envName
  managedBy: 'bicep'
}

// ---------------------------------------------------------------------------
// Resources
// ---------------------------------------------------------------------------

// Container Apps Environment (Consumption). Diagnostics flow to the Log
// Analytics workspace created by logs.bicep. workloadProfiles is omitted =>
// Consumption-only plan; zone redundancy requires Dedicated, so it is false.
resource acaEnv 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: acaEnvName
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: reference(logAnalyticsWorkspaceId, '2023-09-01').customerId
        sharedKey: logAnalyticsWorkspaceKey
      }
    }
    zoneRedundant: false
  }
}

// Four booking jobs (Sat EDT, Sat EST, Sun EDT, Sun EST) via a loop over the
// bookingJobs table. Each is an independent Microsoft.App/jobs resource sharing
// the same image, identity, registries, secrets, and env — only name + cron vary.
//
// @batchSize(1) serializes their creation (one at a time, not all four at once).
// On a freshly-created Consumption environment the ACA control plane times out
// ("ContainerAppOperationError: Operation expired") when several job revisions
// are provisioned simultaneously against the still-cold env. Serial creation
// keeps concurrent provisioning load to one and makes first-deploy reliable;
// it costs a little wall-clock on the initial deploy only (redeploys are fast).
@batchSize(1)
resource bookingJob 'Microsoft.App/jobs@2024-03-01' = [for job in bookingJobs: {
  name: job.name
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${userAssignedIdentityResourceId}': {}
    }
  }
  properties: {
    environmentId: acaEnv.id
    configuration: {
      triggerType: 'Schedule'
      scheduleTriggerConfig: {
        cronExpression: job.cron
        parallelism: parallelism
        replicaCompletionCount: replicaCompletionCount
      }
      replicaRetryLimit: replicaRetryLimit
      replicaTimeout: replicaTimeout
      registries: jobRegistries
      secrets: jobSecrets
    }
    template: {
      containers: [
        {
          image: containerImage
          name: 'teetime'
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
          command: [
            'teetime'
          ]
          args: [
            'run'
            '--config'
            '/app/config/container.toml'
            '--dry-run'
            dryRun ? 'true' : 'false'
          ]
          env: commonEnv
        }
      ]
    }
  }
}]

// Watch job (M-feature-1): polls every 10 minutes for cancellation slots.
// Same identity / registries / secrets / env as the booking jobs.
// Safety: this is safe to run unconditionally because (a) watcher.enabled = false
// in config/container.toml makes the watch CLI exit 0 with no booking, and
// (b) dry-run blocks any booking POST regardless. No DST gate needed — the
// WatchOrchestrator gates polling hours internally via zoneinfo.
resource watchJob 'Microsoft.App/jobs@2024-03-01' = {
  name: watchJobName
  location: location
  tags: tags
  // Provision the watch job AFTER the four booking jobs (not concurrently) for
  // the same cold-environment reason as @batchSize(1) above — avoids the
  // "Operation expired" control-plane timeout on first deploy.
  dependsOn: [bookingJob]
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${userAssignedIdentityResourceId}': {}
    }
  }
  properties: {
    environmentId: acaEnv.id
    configuration: {
      triggerType: 'Schedule'
      scheduleTriggerConfig: {
        cronExpression: watchCron
        parallelism: parallelism
        replicaCompletionCount: replicaCompletionCount
      }
      replicaRetryLimit: replicaRetryLimit
      replicaTimeout: watchReplicaTimeout
      registries: jobRegistries
      secrets: jobSecrets
    }
    template: {
      containers: [
        {
          image: containerImage
          name: 'teetime'
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
          command: [
            'teetime'
          ]
          args: [
            'watch'
            '--config'
            '/app/config/container.toml'
            '--dry-run'
            dryRun ? 'true' : 'false'
          ]
          env: commonEnv
        }
      ]
    }
  }
}

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------

@description('Container Apps Job base name. Four jobs are created: -edt-sat, -est-sat, -edt-sun, -est-sun. For az containerapp job start, target one by appending the suffix.')
output jobName string = bookingJob[0].name

@description('Container Apps Environment resource ID.')
output acaEnvironmentId string = acaEnv.id

// NOTE: There are no per-job principalId outputs. RBAC is handled by the
// single user-assigned MI (identity.bicep). The principalId is already wired
// from identity.outputs.principalId to keyvault/registry/storage before
// compute.bicep runs. No post-compute RBAC pass needed.
