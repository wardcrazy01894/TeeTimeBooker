// compute.bicep — Container Apps Environment (Consumption) + Container Apps Jobs.
//
// Booking jobs are derived from `../release_events.json` (MULTIUSER_PLAN §6.2, MU-15a): one
// EDT + one STD job per release event (collapsed to one job when both DST halves land on the
// same UTC instant, e.g. a no-DST zone — see `jobEntries` below). v1 ships exactly one event,
// Mangrove Bay (`mb0600et`), whose job KEEPS the legacy `teetime-job-<env>-edt/-est` names (its
// `jobNamePrefix`); the tenant cutover (§11) changed ARGUMENTS on those resources instead of
// creating parallel ones:
//   50 9 * * *   = 09:50 UTC = 05:50 EDT, every day  (UTC-4, Mar-Nov)
//   50 10 * * *  = 10:50 UTC = 05:50 EST, every day  (UTC-5, Nov-Mar)
//
// Both same-day crons fire; the bot's DST gate (core/dst_gate.py) makes the wrong-season
// one exit 0.
// See: infra/AZURE_PLAN.md §5.3 (DST), §5.1 (jitter), §5.2 (cold-start)
//
// One path (MULTIUSER_PLAN §11 step 9, MU-19, 2026-11-14): every job runs the tenant path
// (`tenant-run --event <key>`, `tenant-watch`, `tenant-migrate`). The `bookingMode` / `watchMode`
// params and the TOML job wiring (`run|watch --config /app/config/container.toml` plus the
// single-user ForeUP credential, Player 1 and skip-date secrets) are gone. The TOML CLI still
// exists for local course testing, but no ACA job can run it. Every Key Vault secret named below
// MUST exist before a deploy (ACA validates secret refs at job-CREATE time, §10.1).
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

@description('Full container image reference (e.g. teetimeshared<suffix>.azurecr.io/teetime-dev:<sha> for dev, .../teetime:<sha> for prod).')
param containerImage string

@description('Resource ID of the user-assigned managed identity (from identity.bicep). Assigned to both ACA job resources so KV/ACR RBAC covers both.')
param userAssignedIdentityResourceId string

@description('Client ID of the user-assigned managed identity (from identity.bicep). Wired as the AZURE_CLIENT_ID env var so azure.identity.aio.ManagedIdentityCredential(client_id=...) resolves the right identity from the ACA identity endpoint (MULTIUSER_PLAN §10.2).')
param userAssignedIdentityClientId string

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

@description('Watch job cron expression (UTC). Prod default */10 * * * * (every 10 min, year-round — AZURE_PLAN §5.4). Dev may run hourly instead (operator directive, MULTIUSER_PLAN §12 MU-15a) to cut dev Log Analytics/compute noise now that the watcher polls on every run; this is a per-env PARAM, not a hard-coded value, so prod is untouched by the dev change.')
param watchCron string = '*/10 * * * *'

@description('Cosmos DB endpoint URI for the tenant store (e.g. https://cosmos-teetime-shared.documents.azure.com:443/). Wired as TENANT_COSMOS_ENDPOINT on every job. Empty by default; both envs pass the shared Cosmos account (MU-15b), and the tenant commands refuse to run without it.')
param tenantCosmosEndpoint string = ''

@description('ACS Email "from" sender address (e.g. DoNotReply@<acs-managed-domain>). Wired as ACS_EMAIL_SENDER (a plain value, not a secret — it is not sensitive) on the booking and watch jobs. Empty by default; main.bicep derives it from the email module.')
param acsEmailSender string = ''

// ---------------------------------------------------------------------------
// Variables
// ---------------------------------------------------------------------------

var acaEnvName = 'cae-teetime-${envName}'
var watchJobName = 'teetime-watch-job-${envName}'
// MU-16a: the Manual-trigger tenant data-migration job (<= 32 chars for both envs).
var migrateJobName = 'teetime-migrate-${envName}'

// The release-event table (MULTIUSER_PLAN §6.2). Single source of truth, shared with
// killswitch.bicep (job-name derivation) and the pure `core/release_policy.py` helpers
// (tests/test_release_events_parity.py pins this file to `cron_pair`).
var releaseEvents = loadJsonContent('../release_events.json')

// One EDT + one STD job entry per release event, FLATTENED into a single array the job-loop
// below iterates once. A deduped event (both DST halves the same UTC instant — a no-DST zone,
// e.g. America/Phoenix) collapses to ONE entry: deploying both would let two runners race the
// same release instant (core/release_policy.py CronPair.jobs).
//
// Naming (MULTIUSER_PLAN §6.2): an event with a non-empty `jobNamePrefix` (Mangrove Bay's
// `mb0600et`, keeping the legacy names) gets `<prefix>-<env>-edt` / `-est`. Any OTHER event
// (none ship in v1 — see release_events.json) gets the generic `teetime-rel-<key>-<env>-<half>`,
// which the ACA 32-char job-name limit bounds to key<=11 chars for a realistic (<=4-char) env
// name (tests/test_compute_bicep_tenant_mode.py::test_job_names_le_32_chars pins the arithmetic).
// Bicep for-expressions may only be the direct value of a variable/resource/module/output
// declaration (BCP138) — they cannot be nested inside a function call like flatten(). So the
// per-event nested arrays are built as their own variable first, then flattened separately.
var jobEntriesByEvent = [
  for event in releaseEvents: concat(
    [
      {
        eventKey: event.key
        cron: event.cronDst
        name: !empty(event.jobNamePrefix) ? '${event.jobNamePrefix}-${envName}-edt' : 'teetime-rel-${event.key}-${envName}-dst'
      }
    ],
    event.cronDst == event.cronStd
      ? []
      : [
          {
            eventKey: event.key
            cron: event.cronStd
            name: !empty(event.jobNamePrefix) ? '${event.jobNamePrefix}-${envName}-est' : 'teetime-rel-${event.key}-${envName}-std'
          }
        ]
  )
]

var bookingJobs = flatten(jobEntriesByEvent)

// Hard-coded parallelism settings. See AZURE_PLAN.md §4.
// The booking job busy-waits up to ~12 min to T0 (06:00:00 ET) INSIDE the replica
// (teetime tenant-run --wait), so its timeout must cover lead + busy-wait + post-T0 poll/book.
// Worst tolerated early-jitter case (~:47 land = 780s wait + 30s poll + 60s book = 870s)
// fits 1200 with ~330s slack. The DST gate (core/dst_gate.py) caps the busy-wait by
// skipping the wrong-season cron, so 1200 need not cover the ~70-min wrong-season wait.
// See AZURE_PLAN.md §5.2 / M6_PLAN §2 PR3.
var bookingReplicaTimeout = 1200   // 20 minutes in seconds
var replicaRetryLimit = 0     // bot handles retry; ACA retry would bypass idempotency
var parallelism = 1
var replicaCompletionCount = 1

// Watch job replica timeout. A normal run is one HTTP round-trip (~30s), but the adapter
// retries transient transport failures on idempotent calls (warm-up/login/search;
// base.py _send_with_retry). 300s gives headroom so a slow-upstream run that retries can
// never hit the replica cap and turn a recovered run into a Failure. See AZURE_PLAN.md §5.4.
// (The cron ITSELF is the `watchCron` param above, not a hard-coded var — see its @description.)
var watchReplicaTimeout = 300

// Migrate job replica timeout (MU-16a). v1 runs no migration (a connect + two point reads); a
// future backfill must fit 10 min or split itself across runs (every step is idempotent).
var migrateReplicaTimeout = 600

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
// The set is exactly what `tenant-run` / `tenant-watch` read from the environment: the shared
// 2captcha key, the credential keyring (per-user ForeUP passwords are decrypted from the tenant
// store with it), the ACS email connection string (written by email.bicep) and the operator's
// address. The single-user secrets the TOML jobs read (MB-USERNAME, MB-PASSWORD, PLAYER1-EMAIL,
// PLAYER1-PHONE, PLAYER1-MB-MEMBER, TEETIME-SKIP-DATES) were dropped at MU-19: no tenant command
// reads them (tests/test_compute_bicep_tenant_mode.py::test_no_toml_mode_remaining).
//
// ACA validates every KV secret ref at job-CREATE time, so each secret below must already exist
// in the env's vault (MULTIUSER_PLAN §10.1) or the deploy fails.
var jobSecrets = [
  { name: 'twocaptcha-api-key',    keyVaultUrl: '${keyVaultUri}secrets/TWOCAPTCHA-API-KEY',    identity: userAssignedIdentityResourceId }
  { name: 'tenant-creds-keyring',  keyVaultUrl: '${keyVaultUri}secrets/TENANT-CREDS-KEYRING',  identity: userAssignedIdentityResourceId }
  { name: 'acs-email-connection',  keyVaultUrl: '${keyVaultUri}secrets/ACS-EMAIL-CONNECTION',  identity: userAssignedIdentityResourceId }
  { name: 'operator-notify-email', keyVaultUrl: '${keyVaultUri}secrets/OPERATOR-NOTIFY-EMAIL', identity: userAssignedIdentityResourceId }
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

// Container env vars shared by the booking + watch jobs (MULTIUSER_PLAN §10.1/§10.2). The
// secretRef entries point at the jobSecrets names above; the value entries are plain (non-secret)
// config. TEETIME_ENV feeds the dry-run defaults and the environment tag on operator email.
// TENANT_COSMOS_DATABASE is the env name itself ('dev'/'prod' — §10.2's two Cosmos databases).
// There is no DB secret: Cosmos auth is MI + RBAC, not a connection string (§10.2).
var jobEnv = [
  { name: 'TWOCAPTCHA_API_KEY',      secretRef: 'twocaptcha-api-key' }
  { name: 'TEETIME_ENV',             value: envName }
  { name: 'TENANT_COSMOS_ENDPOINT',  value: tenantCosmosEndpoint }
  { name: 'TENANT_COSMOS_DATABASE',  value: envName }
  { name: 'AZURE_CLIENT_ID',         value: userAssignedIdentityClientId }
  { name: 'TENANT_CREDS_KEYRING',    secretRef: 'tenant-creds-keyring' }
  { name: 'ACS_EMAIL_CONNECTION',    secretRef: 'acs-email-connection' }
  { name: 'ACS_EMAIL_SENDER',        value: acsEmailSender }
  { name: 'OPERATOR_NOTIFY_EMAIL',   secretRef: 'operator-notify-email' }
]

// The migrate job's env (MU-16a): the tenant store ONLY — no keyring, no ACS, no ForeUP
// credentials, and so NO Key Vault secret ref (the job cannot fail on a secret the operator has
// not created). `teetime tenant-migrate` refuses to run without TENANT_COSMOS_ENDPOINT.
var migrateEnv = [
  { name: 'TENANT_COSMOS_ENDPOINT',  value: tenantCosmosEndpoint }
  { name: 'TENANT_COSMOS_DATABASE',  value: envName }
  { name: 'AZURE_CLIENT_ID',         value: userAssignedIdentityClientId }
  { name: 'TEETIME_ENV',             value: envName }
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

// One booking job per jobEntries row (§6.2), each an independent Microsoft.App/jobs resource
// sharing the same image, identity, registries, secrets and env — only name, cron and the
// `--event` key vary.
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
            'tenant-run'
            '--event'
            job.eventKey
            '--wait' // the real-timing busy-wait (DST gate + release-instant race)
            '--dry-run'
            dryRun ? 'true' : 'false'
          ]
          env: jobEnv
        }
      ]
    }
  }
}]

// Watch job (M-feature-1): `teetime tenant-watch` on the `watchCron` schedule, polling for
// cancellation slots. Same identity / registries / secrets / env as the booking jobs.
// Safety: it polls on every run (no time-of-day gate). In dev dryRun=true means the tenant
// watcher never mutates a reservation (MULTIUSER_PLAN §7.8); in prod every upgrade or cancel is
// gated on the ownership record. No DST gate needed (the watcher is season-agnostic).
resource watchJob 'Microsoft.App/jobs@2024-03-01' = {
  name: watchJobName
  location: location
  tags: tags
  // Provision the watch job AFTER the booking jobs (not concurrently) for
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
            'tenant-watch'
            '--dry-run'
            dryRun ? 'true' : 'false'
          ]
          env: jobEnv
        }
      ]
    }
  }
}

// Migrate job (MULTIUSER_PLAN §10.1/§10.2, MU-16a): `teetime tenant-migrate`, the ordered,
// idempotent tenant data-migration list (v1 ships none — the job connects, runs nothing, exits 0).
// Deployed unconditionally since MU-19 (it was tenant-mode-only before). ALWAYS Manual-trigger:
// it never auto-fires, so it is deliberately NOT a killswitch target (§10.3, asserted by
// tests/test_migrate_job_infra.py) and ignores enableSchedules. CI starts it and awaits the
// result right after deploy pass 2 (.github/workflows/azure-iac.yml, "Run tenant migrations"):
// pass 1 runs every job on the public bootstrap image, so the migration can only run on the real
// image once pass 2 has also switched the other jobs — safe because readers accept schemaVersion
// N and N-1 (§10.2). Only an operator or that CI step starts it (the deploy guard forbids agents).
resource migrateJob 'Microsoft.App/jobs@2024-03-01' = {
  name: migrateJobName
  location: location
  tags: tags
  // Serial provisioning, same cold-environment reason as the watch job.
  dependsOn: [watchJob]
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${userAssignedIdentityResourceId}': {}
    }
  }
  properties: {
    environmentId: acaEnv.id
    configuration: {
      triggerType: 'Manual'
      manualTriggerConfig: {
        parallelism: parallelism
        replicaCompletionCount: replicaCompletionCount
      }
      replicaRetryLimit: replicaRetryLimit
      replicaTimeout: migrateReplicaTimeout
      registries: jobRegistries
      secrets: []
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
            'tenant-migrate'
          ]
          env: migrateEnv
        }
      ]
    }
  }
}

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------

@description('Container Apps Job base name of the first release event\'s daylight-half job (index 0). For az containerapp job start, target a specific job by its full name (see jobEntries/bookingJobs above).')
output jobName string = bookingJob[0].name

@description('Container Apps Environment resource ID.')
output acaEnvironmentId string = acaEnv.id

// NOTE: There are no per-job principalId outputs. RBAC is handled by the
// single user-assigned MI (identity.bicep). The principalId is already wired
// from identity.outputs.principalId to keyvault/registry before compute.bicep
// runs. No post-compute RBAC pass needed.
