// compute.bicep — Container Apps Environment (Consumption) + Container Apps Jobs.
//
// Booking jobs are derived from `../release_events.json` (MULTIUSER_PLAN §6.2, MU-15a): one
// EDT + one STD job per release event (collapsed to one job when both DST halves land on the
// same UTC instant, e.g. a no-DST zone — see `jobEntries` below). v1 ships exactly one event,
// Mangrove Bay (`mb0600et`), whose job KEEPS the legacy `teetime-job-<env>-edt/-est` names (its
// `jobNamePrefix`) so a future mode cutover (§11) flips ARGUMENTS on the existing resources
// instead of creating parallel ones:
//   50 9 * * *   = 09:50 UTC = 05:50 EDT, every day  (UTC-4, Mar-Nov)
//   50 10 * * *  = 10:50 UTC = 05:50 EST, every day  (UTC-5, Nov-Mar)
//
// Both same-day crons fire; the bot's DST gate (core/dst_gate.py) makes the wrong-season
// one exit 0, and the booking-day gate (core/booking_day_gate.py) fast-exits mornings whose
// today+offset isn't a wanted weekday.
// See: infra/AZURE_PLAN.md §5.3 (DST), §5.1 (jitter), §5.2 (cold-start)
//
// Mode (MULTIUSER_PLAN §6.2, §11): `bookingMode` / `watchMode` select which CLI subcommand +
// env/secrets the SAME job resources run. DEFAULT is `toml` in both params — the day-0 wiring —
// so a plain merge changes nothing. Tenant resources (Cosmos endpoint, ACS email, the tenant
// creds keyring) are added to a job's env/secrets ONLY when its mode param is `tenant`, so the
// toml-mode (default) dev auto-deploy can never fail on a Key Vault secret the operator has not
// pre-created yet (TENANT-CREDS-KEYRING, ACS-EMAIL-CONNECTION, OPERATOR-NOTIFY-EMAIL — §10.1).
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

@description('Client ID of the user-assigned managed identity (from identity.bicep). Wired as the AZURE_CLIENT_ID env var in tenant mode ONLY, so azure.identity.aio.ManagedIdentityCredential(client_id=...) resolves the right identity from the ACA identity endpoint (MULTIUSER_PLAN §10.2). Unused (not read) in toml mode.')
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

@description('''Which code path the booking jobs run (MULTIUSER_PLAN §6.2/§11). "toml" (default,
byte-identical to pre-MU-15a): `teetime run --config /app/config/container.toml --wait --dry-run
<x>` against the single-user credentials. "tenant": `teetime tenant-run --event <key> --wait
--dry-run <x>` against the multi-user Cosmos store. Exactly one mode is active per env at a
time — a structural guard against the TOML and tenant paths both acting for the same ForeUP
account. Flipped only at the §11 cutover steps, never by this PR.''')
@allowed(['toml', 'tenant'])
param bookingMode string = 'toml'

@description('Which code path the watch job runs, mirroring bookingMode: "toml" (default) runs `teetime watch --config ... --dry-run <x>`; "tenant" runs `teetime tenant-watch --dry-run <x>`.')
@allowed(['toml', 'tenant'])
param watchMode string = 'toml'

@description('Watch job cron expression (UTC). Prod default */10 * * * * (every 10 min, year-round — AZURE_PLAN §5.4). Dev may run hourly instead (operator directive, MULTIUSER_PLAN §12 MU-15a) to cut dev Log Analytics/compute noise now that the watcher polls on every run; this is a per-env PARAM, not a hard-coded value, so prod is untouched by the dev change.')
param watchCron string = '*/10 * * * *'

@description('Cosmos DB endpoint URI for the tenant store (e.g. https://cosmos-teetime-shared.documents.azure.com:443/). Wired as TENANT_COSMOS_ENDPOINT ONLY when bookingMode/watchMode == "tenant". Empty by default — MU-15a ships no Cosmos account (that is MU-15b); the default toml mode never reads this value.')
param tenantCosmosEndpoint string = ''

@description('ACS Email "from" sender address (e.g. DoNotReply@<acs-managed-domain>). Wired as ACS_EMAIL_SENDER (a plain value, not a secret — it is not sensitive) ONLY when bookingMode/watchMode == "tenant". Empty by default; MU-15a\'s toml mode never reads this value.')
param acsEmailSender string = ''

// ---------------------------------------------------------------------------
// Variables
// ---------------------------------------------------------------------------

var acaEnvName = 'cae-teetime-${envName}'
var watchJobName = 'teetime-watch-job-${envName}'

// The release-event table (MULTIUSER_PLAN §6.2). Single source of truth, shared with
// killswitch.bicep (job-name derivation) and the pure `core/release_policy.py` helpers
// (tests/test_release_events_parity.py pins this file to `cron_pair`).
var releaseEvents = loadJsonContent('../release_events.json')

// One EDT + one STD job entry per release event, FLATTENED into a single array the job-loop
// below iterates once. A deduped event (both DST halves the same UTC instant — a no-DST zone,
// e.g. America/Phoenix) collapses to ONE entry: deploying both would let two runners race the
// same release instant with no lease in toml mode (core/release_policy.py CronPair.jobs).
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
// (teetime run --wait), so its timeout must cover lead + busy-wait + post-T0 poll/book.
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
// This array is used UNCONDITIONALLY (both modes) until MU-19 retires the toml wiring —
// see MULTIUSER_PLAN §11 step 9. It must never grow a tenant-only secret (see jobSecretsTenant
// below), or the default toml-mode dev auto-deploy would fail at job-CREATE on a KV secret the
// operator has not pre-created.
var jobSecrets = [
  { name: 'mb-username',        keyVaultUrl: '${keyVaultUri}secrets/MB-USERNAME',        identity: userAssignedIdentityResourceId }
  { name: 'mb-password',        keyVaultUrl: '${keyVaultUri}secrets/MB-PASSWORD',        identity: userAssignedIdentityResourceId }
  { name: 'player1-email',      keyVaultUrl: '${keyVaultUri}secrets/PLAYER1-EMAIL',      identity: userAssignedIdentityResourceId }
  { name: 'player1-phone',      keyVaultUrl: '${keyVaultUri}secrets/PLAYER1-PHONE',      identity: userAssignedIdentityResourceId }
  { name: 'player1-mb-member',  keyVaultUrl: '${keyVaultUri}secrets/PLAYER1-MB-MEMBER',  identity: userAssignedIdentityResourceId }
  { name: 'twocaptcha-api-key', keyVaultUrl: '${keyVaultUri}secrets/TWOCAPTCHA-API-KEY', identity: userAssignedIdentityResourceId }
  // No-redeploy "skip this day" (LEADTIME_SKIP_PLAN F2). The KV secret TEETIME-SKIP-DATES MUST
  // already exist (operator pre-creates it, value " " = no skips; Azure rejects an empty value)
  // — ACA validates KV secret refs at job-CREATE time, so a missing secret FAILS the deploy. Edit
  // the value in the Portal later with no redeploy. The bot reads it fail-open, so a blank/garbage
  // value never crashes a run.
  { name: 'teetime-skip-dates', keyVaultUrl: '${keyVaultUri}secrets/TEETIME-SKIP-DATES', identity: userAssignedIdentityResourceId }
]

// Tenant-mode-only KV secret refs (MULTIUSER_PLAN §10.1). These are appended to `secrets:`
// ONLY inside a `bookingMode/watchMode == 'tenant'` ternary branch below — NEVER unconditionally
// — because ACA validates every KV secret ref at job-CREATE time, and these three secrets do
// not exist until the operator pre-creates them (§10.1: TENANT-CREDS-KEYRING, ACS-EMAIL-
// CONNECTION written by email.bicep, OPERATOR-NOTIFY-EMAIL). Default toml mode in both envs
// never evaluates this array, so a plain merge of this PR cannot break the dev auto-deploy.
var jobSecretsTenant = [
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

// Common container env vars shared by booking + watch jobs, both modes. The secretRef
// entries point at the jobSecrets names above; the value entries are plain (non-secret)
// config. TEETIME_ENV is also read by the tenant CLI commands (dry-run defaults, logging).
var commonEnv = [
  { name: 'MB_USERNAME',                secretRef: 'mb-username' }
  { name: 'MB_PASSWORD',                secretRef: 'mb-password' }
  { name: 'PLAYER1_EMAIL',              secretRef: 'player1-email' }
  { name: 'PLAYER1_PHONE',              secretRef: 'player1-phone' }
  { name: 'PLAYER1_MB_MEMBER',          secretRef: 'player1-mb-member' }
  { name: 'TWOCAPTCHA_API_KEY',         secretRef: 'twocaptcha-api-key' }
  { name: 'TEETIME_SKIP_DATES',         secretRef: 'teetime-skip-dates' }
  { name: 'TEETIME_ENV',                value: envName }
]

// Tenant-mode-only env vars (MULTIUSER_PLAN §10.1/§10.2). Appended to `env:` ONLY inside a
// `== 'tenant'` ternary branch, mirroring jobSecretsTenant above — retires "no Azure SDK calls
// at runtime" for the tenant path only (§10.2), never evaluated by the default toml mode.
// TENANT_COSMOS_DATABASE is the env name itself ('dev'/'prod' — §10.2's two Cosmos databases).
// There is no DB secret: Cosmos auth is MI + RBAC, not a connection string (§10.2).
var tenantEnv = [
  { name: 'TENANT_COSMOS_ENDPOINT',  value: tenantCosmosEndpoint }
  { name: 'TENANT_COSMOS_DATABASE',  value: envName }
  { name: 'AZURE_CLIENT_ID',         value: userAssignedIdentityClientId }
  { name: 'TENANT_CREDS_KEYRING',    secretRef: 'tenant-creds-keyring' }
  { name: 'ACS_EMAIL_CONNECTION',    secretRef: 'acs-email-connection' }
  { name: 'ACS_EMAIL_SENDER',        value: acsEmailSender }
  { name: 'OPERATOR_NOTIFY_EMAIL',   secretRef: 'operator-notify-email' }
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
// sharing the same image, identity, registries — only name, cron, and mode-selected args/
// secrets/env vary.
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
      secrets: bookingMode == 'tenant' ? concat(jobSecrets, jobSecretsTenant) : jobSecrets
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
          args: bookingMode == 'tenant' ? [
            'tenant-run'
            '--event'
            job.eventKey
            '--wait' // the real-timing busy-wait (DST gate + release-instant race), same as toml mode
            '--dry-run'
            dryRun ? 'true' : 'false'
          ] : [
            'run'
            '--config'
            '/app/config/container.toml'
            '--wait' // M6: select the real-timing busy-wait (DST gate + 06:00:00 ET race)
            '--dry-run'
            dryRun ? 'true' : 'false'
          ]
          env: bookingMode == 'tenant' ? concat(commonEnv, tenantEnv) : commonEnv
        }
      ]
    }
  }
}]

// Watch job (M-feature-1): polls on a `watchCron` schedule for cancellation slots.
// Same identity / registries as the booking jobs; secrets/env/args are mode-selected the
// same way (watchMode, mirroring bookingMode).
// Safety: the watcher is ENABLED (watcher.enabled = true) and polls on every run —
// the time-of-day polling gate was removed in the multi-day re-arch. It is safe to run
// unconditionally because (a) in dev dryRun=true suppresses every booking POST, and
// (b) the watch request is scoped per target date, so an upgrade only ever acts within
// the intended date+window. No DST gate needed (the watcher is season-agnostic).
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
      secrets: watchMode == 'tenant' ? concat(jobSecrets, jobSecretsTenant) : jobSecrets
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
          args: watchMode == 'tenant' ? [
            'tenant-watch'
            '--dry-run'
            dryRun ? 'true' : 'false'
          ] : [
            'watch'
            '--config'
            '/app/config/container.toml'
            '--dry-run'
            dryRun ? 'true' : 'false'
          ]
          env: watchMode == 'tenant' ? concat(commonEnv, tenantEnv) : commonEnv
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
