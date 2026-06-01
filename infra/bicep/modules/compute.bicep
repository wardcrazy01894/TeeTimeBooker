// compute.bicep — Container Apps Environment (Consumption) + Container Apps Job.
//
// Two scheduled triggers (identical job, one per DST half) book ONE tee time every
// SUNDAY (M6 — Sunday-only schedule):
//   50 9 * * 0   = 09:50 UTC = 05:50 EDT Sunday    (UTC-4, Mar-Nov)
//   50 10 * * 0  = 10:50 UTC = 05:50 EST Sunday    (UTC-5, Nov-Mar)
//
// Both same-day crons fire; the bot's DST gate (core/dst_gate.py, --wait path:
// proceed iff ET wall-clock hour == 5) makes the wrong-season one exit 0. This
// re-homes the deleted book.yml `dst` step.
// See: infra/AZURE_PLAN.md §5.3 (DST), §5.1 (jitter), §5.2 (cold-start)
//
// Concurrency control:
//   parallelism = 1            — one replica per execution (no concurrent replicas)
//   replicaCompletionCount = 1 — execution completes when that one replica finishes
//   replicaRetryLimit = 0      — no ACA-level retry; bot handles retry internally
//   bookingReplicaTimeout = 1200 — 20 min; covers the in-replica busy-wait to 06:00 ET
//   watchReplicaTimeout = 300  — 5 min; headroom for in-run retries on idempotent
//                                ForeUP calls (warm-up/login/search; base.py _send_with_retry)
// See: infra/AZURE_PLAN.md §6.2 (concurrency safety), §4 (hard-coded constants)
//
// Identity: user-assigned managed identity (MI). A single MI resource is
// created by identity.bicep and passed in here as userAssignedIdentityResourceId.
// Both ACA job resources (EDT + EST) reference the SAME MI, so RBAC assignments
// in keyvault/registry cover both jobs with a single principalId. There is NO
// system-assigned MI.
// See: infra/AZURE_PLAN.md §7.2
//
// Secret injection:
//   Secrets are declared with keyVaultUrl references (platform resolves at
//   container start via the user-assigned MI). Env vars reference secret names
//   via secretRef. Bot reads from os.environ — no SDK changes required.
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

@description('Resource ID of the user-assigned managed identity (from identity.bicep). Assigned to both ACA job resources so KV/ACR RBAC covers both.')
param userAssignedIdentityResourceId string

@description('Key Vault URI for secret references (e.g. https://kv-teetime-dev-xxxx.vault.azure.net/).')
param keyVaultUri string

@description('Log Analytics Workspace resource ID for ACA environment diagnostics.')
param logAnalyticsWorkspaceId string

@description('Log Analytics Workspace primary shared key.')
@secure()
param logAnalyticsWorkspaceKey string

@description('Permanent dry-run flag. When true (the dev default), the bot performs no real booking POSTs. Dev MUST stay in dry-run until production cutover (AZURE_PLAN.md §10.3).')
param dryRun bool = true

@description('True when containerImage is a PUBLIC bootstrap image (deploy pass 1). Drops the registries[] auth block so ACA pulls anonymously — listing a public registry (MCR) with the MI causes "Operation expired" at job provisioning. Set false on pass 2 (real ACR image) so the MI + AcrPull engage.')
param usePublicBootstrapImage bool = false

@description('When true (default), the booking + watch jobs use Schedule triggers (crons). When false, they are created with a Manual trigger (no cron) so they NEVER auto-fire — used to silence a non-primary environment (e.g. dev) once prod is live, avoiding two environments hitting ForeUP with the same credentials / concurrent logins. See AZURE_PLAN.md §10.3.')
param enableSchedules bool = true

// ---------------------------------------------------------------------------
// Variables
// ---------------------------------------------------------------------------

var acaEnvName = 'cae-teetime-${envName}'
var jobName = 'teetime-job-${envName}'
var watchJobName = 'teetime-watch-job-${envName}'

// DST cron expressions (UTC). Two crons: Sunday × EDT+EST (Sunday-only schedule, M6).
// The bot's DST gate (core/dst_gate.py) selects the correct half; the wrong-season
// cron exits 0. See: infra/AZURE_PLAN.md §5.3
var cronEdtSun = '50 9 * * 0'    // 09:50 UTC = 05:50 EDT Sunday
var cronEstSun = '50 10 * * 0'   // 10:50 UTC = 05:50 EST Sunday

// Hard-coded parallelism settings. See AZURE_PLAN.md §4.
// The booking job busy-waits up to ~12 min to T0 (06:00:00 ET) INSIDE the replica
// (teetime run --wait), so its timeout must cover lead + busy-wait + post-T0 poll/book.
// Worst tolerated early-jitter case (~:47 land = 780s wait + 30s poll + 60s book = 870s)
// fits 1200 with ~330s slack. The DST gate (core/dst_gate.py) caps the busy-wait by
// skipping the wrong-season cron, so 1200 need not cover the ~70-min wrong-season wait.
// See AZURE_PLAN.md §5.2 / M6_PLAN §2 PR3.
var bookingReplicaTimeout = 1200   // 20 minutes in seconds
var replicaRetryLimit = 0     // bot handles retry; ACA retry would bypass idempotency
var parallelism = 1
var replicaCompletionCount = 1

// Watch job runs every 10 minutes year-round (no DST gate needed; the
// WatchOrchestrator gates polling hours internally). A normal run is one HTTP
// round-trip (~30s), but the adapter now retries transient transport failures on
// idempotent calls (warm-up/login/search; base.py _send_with_retry). 300s gives
// headroom so a slow-upstream run that retries can never hit the replica cap and
// turn a recovered run into a Failure. See AZURE_PLAN.md §5.4.
var watchCron = '*/10 * * * *'
var watchReplicaTimeout = 300

// Booking-job cron table. A single array + loop avoids two copy-pasted job
// resources. Each entry pairs a resource-name suffix with its UTC cron.
// One job resource per cron => independent execution history; any one cron-half
// can be disabled without touching the others (a single job with multiple
// scheduleTriggerConfigs is NOT supported by the ACA ARM/Bicep API — see stub).
var bookingJobs = [
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
// No SMTP-* secrets: the bot sends no email (M4/email was cut — the golf course
// sends booking confirmations directly). The notifier is 'console' only.
// See AZURE_PLAN.md §12 Q10.
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
//
// CRITICAL: when containerImage is a PUBLIC bootstrap image (deploy pass 1, e.g.
// mcr.microsoft.com/k8se/quickstart-jobs), this MUST be empty. ACA attempts MI
// auth against every server listed here; MCR (and other public registries) do
// NOT accept managed-identity bearer tokens, so the pull hangs until the control
// plane returns "ContainerAppOperationError: Operation expired" and the job fails
// to provision. usePublicBootstrapImage=true drops the block so the public image
// pulls anonymously. Pass 2 (real ACR image) sets it false → MI + AcrPull engage.
var jobRegistries = usePublicBootstrapImage ? [] : [
  { server: acrLoginServer, identity: userAssignedIdentityResourceId }
]

// Common container env vars shared by booking + watch jobs. The secretRef
// entries point at the jobSecrets names above; the value entries are plain
// (non-secret) config. The bot makes no authenticated Azure SDK calls at
// runtime (state is in-process; no Blob Storage), so no AZURE_CLIENT_ID is
// needed — Key Vault secret resolution uses the job's identity block directly.
var commonEnv = [
  { name: 'MB_USERNAME',                secretRef: 'mb-username' }
  { name: 'MB_PASSWORD',                secretRef: 'mb-password' }
  { name: 'PLAYER1_EMAIL',              secretRef: 'player1-email' }
  { name: 'PLAYER1_PHONE',              secretRef: 'player1-phone' }
  { name: 'PLAYER1_MB_MEMBER',          secretRef: 'player1-mb-member' }
  { name: 'TWOCAPTCHA_API_KEY',         secretRef: 'twocaptcha-api-key' }
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

// Two booking jobs (Sun EDT, Sun EST) via a loop over the bookingJobs table.
// Each is an independent Microsoft.App/jobs resource sharing the same image,
// identity, registries, secrets, and env — only name + cron vary.
//
// @batchSize(1) serializes their creation (one at a time, not both at once).
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
      triggerType: enableSchedules ? 'Schedule' : 'Manual'
      scheduleTriggerConfig: enableSchedules ? {
        cronExpression: job.cron
        parallelism: parallelism
        replicaCompletionCount: replicaCompletionCount
      } : null
      manualTriggerConfig: enableSchedules ? null : {
        parallelism: parallelism
        replicaCompletionCount: replicaCompletionCount
      }
      replicaRetryLimit: replicaRetryLimit
      replicaTimeout: bookingReplicaTimeout
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
            '--wait' // M6: select the real-timing busy-wait (DST gate + 06:00:00 ET race)
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
      triggerType: enableSchedules ? 'Schedule' : 'Manual'
      scheduleTriggerConfig: enableSchedules ? {
        cronExpression: watchCron
        parallelism: parallelism
        replicaCompletionCount: replicaCompletionCount
      } : null
      manualTriggerConfig: enableSchedules ? null : {
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

@description('Container Apps Job base name. Two jobs are created: -edt-sun, -est-sun (Sunday-only schedule). This output is index 0 (-edt-sun). For az containerapp job start, target a specific job by appending the suffix.')
output jobName string = bookingJob[0].name

@description('Container Apps Environment resource ID.')
output acaEnvironmentId string = acaEnv.id

// NOTE: There are no per-job principalId outputs. RBAC is handled by the
// single user-assigned MI (identity.bicep). The principalId is already wired
// from identity.outputs.principalId to keyvault/registry before compute.bicep
// runs. No post-compute RBAC pass needed.
