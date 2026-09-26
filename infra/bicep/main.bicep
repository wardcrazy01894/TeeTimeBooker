// main.bicep — entry point for TeeTimeBooker v1 Azure infrastructure.
// Orchestrates all modules per environment. Deploy with:
//   az deployment group create \
//     --resource-group rg-teetime-<envName> \
//     --template-file infra/bicep/main.bicep \
//     --parameters @infra/bicep/main.bicepparam.dev
//
// See: infra/AZURE_PLAN.md §3 (module layout), §4 (parameter strategy),
//      §10 (deploy runbook).

targetScope = 'resourceGroup'

// ---------------------------------------------------------------------------
// Parameters
// ---------------------------------------------------------------------------

@description('Environment name suffix used in all resource names (e.g. dev, prod).')
@minLength(2)
@maxLength(8)
param envName string

@description('Azure region for all resources. Must match the resource group region.')
param location string = resourceGroup().location

@description('Full image reference for the bot container (e.g. teetime.azurecr.io/teetime:dev).')
param containerImage string

// NOTE: budgetAmountUsd and budgetAlertEmail are NOT parameters here.
// budget.bicep is subscription-scoped and deployed in a separate
// az deployment sub create command from azure-iac.yml. See AZURE_PLAN.md §9.2.

@description('''Shared-ACR consolidation (AZURE_PLAN.md §2.1): the single ACR lives in a
DEDICATED resource group (rg-teetime-shared) and is deployed separately (registry.bicep,
envName=shared). NEITHER env creates an ACR — each env grants its own job MI a cross-RG
AcrPull on the shared registry (acr-pull-cross-rg.bicep). Name of that shared ACR.''')
param sharedAcrName string

@description('Resource group holding the shared ACR (rg-teetime-shared). The cross-RG AcrPull grant is deployed there.')
param sharedAcrResourceGroup string

@description('Key Vault SKU name.')
@allowed(['standard', 'premium'])
param kvSku string = 'standard'

@description('Permanent dry-run flag for the ACA jobs. Defaults true (no real bookings). Dev MUST stay true until production cutover (AZURE_PLAN.md §10.3).')
param dryRun bool = true

@description('Key Vault purge protection. Defaults true (safe). Prod MUST stay true; dev passes false so the vault can be torn down and recreated during iteration. See AZURE_PLAN.md §7.4, §11.')
param enablePurgeProtection bool = true

@description('True when containerImage is a PUBLIC bootstrap image (deploy pass 1). The CI workflow sets this true for pass 1 and false for pass 2 (real ACR image). Drops the ACA registries[] auth block for the public pass — listing a public registry (MCR) with the MI causes job provisioning to hang ("Operation expired"). See compute.bicep.')
param usePublicBootstrapImage bool = false

@description('When true (default), booking + watch jobs use Schedule (cron) triggers. Set false to deploy an environment with the jobs present but on Manual triggers (never auto-fire) — used to silence dev once prod is live so two envs do not hit ForeUP on the same credentials. See AZURE_PLAN.md §10.3.')
param enableSchedules bool = true

@description('''Cost-killswitch-fired safety bit. Defaults false. When the $50 budget killswitch
fires, the operator sets this to true in BOTH param files and pushes to main. CI will then
deploy with effectiveEnableSchedules=false regardless of the enableSchedules value — preventing
any subsequent infra/** merge from re-arming the cron schedules until this bit is explicitly
cleared. This is the checked-in safety latch that survives across PR merges. To re-enable
schedules after a killswitch event: fix the root cause, set killswitchFired=false AND
verify enableSchedules=true, then push. See COST_KILLSWITCH_PLAN.md §2/Item2 for full runbook.
IMPORTANT: do NOT clear this param until the overspend root cause is diagnosed and resolved.
''')
param killswitchFired bool = false

@description('Enable the cost-killswitch Logic App + Action Group. Defaults false in main.bicep; param files set true (enabled on deploy). See COST_KILLSWITCH_PLAN.md §3/PR-KS1.')
param enableKillswitch bool = false

@description('GUID of the pre-created "ACA Job Schedule Manager" custom role. Required when enableKillswitch=true. Must be created manually by the operator (subscription-level roleDefinitions/write required). See COST_KILLSWITCH_PLAN.md §2/Item4.')
param killswitchRbacRoleId string = ''

@description('Which code path the ACA booking jobs run: "toml" (default, byte-identical to pre-MU-15a) or "tenant". See compute.bicep and MULTIUSER_PLAN.md §6.2/§11. Flipped only at the cutover steps in that plan, never by a routine deploy.')
@allowed(['toml', 'tenant'])
param bookingMode string = 'toml'

@description('Which code path the ACA watch job runs, mirroring bookingMode. See compute.bicep.')
@allowed(['toml', 'tenant'])
param watchMode string = 'toml'

@description('Watch job cron expression (UTC). Prod stays */10 * * * * (AZURE_PLAN §5.4); dev may run hourly (operator directive, MULTIUSER_PLAN §12 MU-15a) — a per-env param, so this cannot touch prod.')
param watchCron string = '*/10 * * * *'

@description('Cosmos DB endpoint for the tenant store, wired into ACA jobs ONLY when bookingMode/watchMode == "tenant". Empty by default — MU-15a ships no Cosmos account (MU-15b); toml mode (the default) never reads it.')
param tenantCosmosEndpoint string = ''

@description('ACS Email "from" sender address, wired ONLY when bookingMode/watchMode == "tenant". Empty by default; see email.bicep\'s mailFromSenderDomain output once deployAcsEmail=true.')
param acsEmailSender string = ''

@description('''Deploy the `teetime-web-<env>` Container App (MULTIUSER_PLAN §8/§12 MU-15a).
Defaults FALSE in both envs: the app\'s Key Vault secret refs (WEB-SESSION-SECRET,
OAUTH-GOOGLE-CLIENT-ID, OAUTH-GOOGLE-CLIENT-SECRET) do not exist yet — the Google OAuth client
has not been registered — and ACA validates KV secret refs at container-CREATE time, so
deploying this unconditionally would break the very next dev auto-deploy. Flip true only after
the operator has created those three secrets (see the PR body / webapp.bicep header).''')
param deployWebApp bool = false

@description('''Deploy the shared ACS Email resources (email.bicep: Communication Service +
Email Service + Azure-managed domain). Defaults FALSE in both envs (operator decision, MU-15a —
nothing consumes ACS_EMAIL_CONNECTION until bookingMode/watchMode or deployWebApp actually needs
it). Requires the operator to `az provider register --namespace Microsoft.Communication` first
(the CI service principal is RG-scoped and cannot self-register a provider) and to grant the CI
deploy identity "Key Vault Secrets Officer" on the vault (see email.bicep header) — read-only
`az provider show` can verify registration; the grant itself is an explicit operator action.''')
param deployAcsEmail bool = false

@description('Public base URL the web app is reachable at (only meaningful when deployWebApp=true) — see webapp.bicep header for why this is a param, not derived.')
param webPublicBaseUrl string = ''

@description('The operator\'s sign-in email for the web app (implicitly invited). Only meaningful when deployWebApp=true.')
param operatorEmail string = ''

// When the killswitch has fired, force schedules off regardless of enableSchedules.
// This ensures that any CI deploy — even one that does not touch the killswitchFired
// param — cannot silently re-arm the jobs. The enableSchedules param retains its value
// so the intent is preserved; only the effective value passed to compute is changed.
var effectiveEnableSchedules = enableSchedules && !killswitchFired

// ---------------------------------------------------------------------------
// Module: identity
// User-assigned managed identity — the SINGLE principalId for all RBAC.
// Deploying identity first breaks the chicken-and-egg cycle: keyvault and
// registry can both receive the principalId before compute is declared, and
// compute references the identity by resource ID.
// See: infra/AZURE_PLAN.md §7.2
// ---------------------------------------------------------------------------

module identity 'modules/identity.bicep' = {
  name: 'identity-${envName}'
  params: {
    envName: envName
    location: location
  }
}

// ---------------------------------------------------------------------------
// Module: shared-ACR cross-RG AcrPull
// The single ACR lives in a DEDICATED rg-teetime-shared (deployed separately via
// registry.bicep, envName=shared). NEITHER env creates an ACR — each env grants its own
// job MI AcrPull on the shared registry via a cross-RG role assignment (nested deployment
// scoped to sharedAcrResourceGroup), same pattern as killswitch-rbac-prod.bicep. The CI SP
// has User Access Administrator on rg-teetime-shared, so this needs no operator step.
// See: infra/AZURE_PLAN.md §2.1.
// ---------------------------------------------------------------------------

module sharedAcrPull 'modules/acr-pull-cross-rg.bicep' = {
  name: 'shared-acrpull-${envName}'
  scope: resourceGroup(sharedAcrResourceGroup)
  params: {
    acrName: sharedAcrName
    jobPrincipalId: identity.outputs.principalId
  }
}

// ---------------------------------------------------------------------------
// Module: keyvault
// Key Vault Standard + RBAC role assignments.
// See: infra/AZURE_PLAN.md §7
// ---------------------------------------------------------------------------

module keyvault 'modules/keyvault.bicep' = {
  name: 'keyvault-${envName}'
  params: {
    envName: envName
    location: location
    kvSku: kvSku
    jobPrincipalId: identity.outputs.principalId
    enablePurgeProtection: enablePurgeProtection
    // Implicit dependency: consuming the logs output makes Bicep deploy logs first,
    // so the Key Vault AuditEvent diagnostic setting has a workspace to target.
    logAnalyticsWorkspaceId: logs.outputs.workspaceId
  }
}

// ---------------------------------------------------------------------------
// Module: logs
// Log Analytics Workspace + Application Insights.
// See: infra/AZURE_PLAN.md §11
// ---------------------------------------------------------------------------

module logs 'modules/logs.bicep' = {
  name: 'logs-${envName}'
  params: {
    envName: envName
    location: location
  }
}

// ---------------------------------------------------------------------------
// Module: compute
// Container Apps Environment (Consumption) + Container Apps Job (two booking crons,
// one per DST half (EDT+EST), firing DAILY; the booking-day gate selects wanted weekdays).
// See: infra/AZURE_PLAN.md §5 (race), §6.2 (parallelism=1)
// ---------------------------------------------------------------------------

module compute 'modules/compute.bicep' = {
  name: 'compute-${envName}'
  params: {
    envName: envName
    location: location
    containerImage: containerImage
    userAssignedIdentityResourceId: identity.outputs.identityResourceId
    userAssignedIdentityClientId: identity.outputs.clientId
    keyVaultUri: keyvault.outputs.vaultUri
    logAnalyticsWorkspaceId: logs.outputs.workspaceId
    logAnalyticsWorkspaceKey: logs.outputs.workspaceKey
    dryRun: dryRun
    usePublicBootstrapImage: usePublicBootstrapImage
    enableSchedules: effectiveEnableSchedules
    bookingMode: bookingMode
    watchMode: watchMode
    watchCron: watchCron
    tenantCosmosEndpoint: tenantCosmosEndpoint
    acsEmailSender: acsEmailSender
  }
  // keyvault and logs are already implicit dependencies via their outputs
  // consumed above (vaultUri, workspaceId/Key), so they are NOT listed here
  // (Bicep no-unnecessary-dependson). The cross-RG AcrPull grant IS listed: compute derives
  // the ACR login server from the containerImage string (not a module output), so there is no
  // implicit edge — but the job's AcrPull role assignment on the shared ACR must exist before
  // the job can pull.
  dependsOn: [sharedAcrPull]
}

// ---------------------------------------------------------------------------
// Module: killswitch (optional — dev only, gated on enableKillswitch + role GUID)
// Cost-killswitch Logic App + Action Group + RBAC.
// Deployed ONLY when: (a) enableKillswitch=true, (b) killswitchRbacRoleId is
// non-empty (custom role pre-created), AND (c) envName=='dev' (the killswitch
// lives in rg-teetime-dev and manages BOTH envs via cross-RG RBAC — a second
// instance must NOT be created in prod). If enableKillswitch=true but
// killswitchRbacRoleId='' (role not yet created), the deploy is a clean no-op.
// See: infra/COST_KILLSWITCH_PLAN.md §2/Item3, §3/PR-KS1
// ---------------------------------------------------------------------------

module killswitch 'modules/killswitch.bicep' = if (enableKillswitch && !empty(killswitchRbacRoleId) && envName == 'dev') {
  name: 'killswitch-${envName}'
  params: {
    envName: envName
    location: location
    killswitchRbacRoleId: killswitchRbacRoleId
  }
}

// ---------------------------------------------------------------------------
// Module: webapp (optional — gated on deployWebApp, default false in both envs)
// Container App `teetime-web-<env>` serving `teetime web` (MULTIUSER_PLAN §8/§12 MU-15a).
// See webapp.bicep header for the exact gating rationale and the killswitch-latch coupling.
// ---------------------------------------------------------------------------

module webapp 'modules/webapp.bicep' = if (deployWebApp) {
  name: 'webapp-${envName}'
  params: {
    envName: envName
    location: location
    containerImage: containerImage
    userAssignedIdentityResourceId: identity.outputs.identityResourceId
    keyVaultUri: keyvault.outputs.vaultUri
    acaEnvironmentId: compute.outputs.acaEnvironmentId
    usePublicBootstrapImage: usePublicBootstrapImage
    enableIngress: effectiveEnableSchedules
    webPublicBaseUrl: webPublicBaseUrl
    operatorEmail: operatorEmail
    dryRun: dryRun
  }
  dependsOn: [sharedAcrPull]
}

// ---------------------------------------------------------------------------
// Module: email (optional — gated on deployAcsEmail, default false in both envs)
// ACS Communication Service + Email Service + Azure-managed domain (MULTIUSER_PLAN §10.1).
// See email.bicep header for the operator prerequisites (RP registration, KV RBAC).
// ---------------------------------------------------------------------------

module email 'modules/email.bicep' = if (deployAcsEmail) {
  name: 'email-${envName}'
  params: {
    envName: envName
    keyVaultName: keyvault.outputs.vaultName
  }
}

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------

@description('ACA Job resource name, for manual trigger via az containerapp job start.')
output jobName string = compute.outputs.jobName

@description('Shared ACR login server (in rg-teetime-shared), for image push commands. Derived from sharedAcrName; the ACR is deployed separately, not by this template.')
output acrLoginServer string = '${sharedAcrName}.azurecr.io'

@description('Key Vault URI, for az keyvault secret set commands.')
output keyVaultUri string = keyvault.outputs.vaultUri

@description('User-assigned managed identity principal ID. Used to verify RBAC assignments post-deploy.')
output identityPrincipalId string = identity.outputs.principalId

@description('ARM resource ID of the killswitch Action Group. Empty string when the killswitch module is not deployed (enableKillswitch=false, killswitchRbacRoleId empty, or envName!=dev). Pass to budget.bicep as killswitchActionGroupId to arm the $50 budget threshold (budget.bicep already has the killswitchBudget resource wired, conditional on this param).')
// Use the safe-dereference operator (.?) + null-coalesce (??) rather than an any()-cast: when
// the killswitch module is not deployed, `killswitch.?outputs` is null and we fall back to ''.
// When it IS deployed, this resolves to the actionGroupId STRING (Bicep's normal module-output
// `.value` unwrapping is preserved). The previous any()-cast approach returned the raw
// {value,type} object and failed output evaluation at deploy time (DeploymentOutputEvaluationFailed).
output killswitchActionGroupId string = killswitch.?outputs.actionGroupId ?? ''

@description('Web app Container App FQDN. Empty string when deployWebApp=false. The operator reads this after first enabling the web app to fill in webPublicBaseUrl (see webapp.bicep header).')
output webAppFqdn string = webapp.?outputs.fqdn ?? ''

@description('ACS Email Azure-managed domain sender subdomain. Empty string when deployAcsEmail=false. The operator sets acsEmailSender to "DoNotReply@<this value>" once known (see email.bicep header).')
output acsEmailDomain string = email.?outputs.mailFromSenderDomain ?? ''
