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

@description('ACR SKU tier.')
@allowed(['Basic', 'Standard', 'Premium'])
param acrSku string = 'Basic'

@description('''Shared-ACR consolidation: the OWNER env (prod) creates the single shared ACR;
every other env (dev) skips ACR creation and instead gets a cross-RG AcrPull on the shared
registry. Owner is derived from envName (prod owns). See AZURE_PLAN.md §2.1 + acr-pull-cross-rg.bicep.''')
param acrOwnerEnv string = 'prod'

@description('Name of the shared ACR (the owner env\'s registry). REQUIRED for a non-owner env (dev) — it references this existing ACR cross-RG for the AcrPull grant + the image login server. Ignored for the owner env (it creates + names its own). E.g. teetimeprod<suffix>.')
param sharedAcrName string = ''

@description('Resource group holding the shared ACR (the owner env\'s RG, e.g. rg-teetime-prod). REQUIRED for a non-owner env (dev); ignored for the owner.')
param sharedAcrResourceGroup string = ''

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

// When the killswitch has fired, force schedules off regardless of enableSchedules.
// This ensures that any CI deploy — even one that does not touch the killswitchFired
// param — cannot silently re-arm the jobs. The enableSchedules param retains its value
// so the intent is preserved; only the effective value passed to compute is changed.
var effectiveEnableSchedules = enableSchedules && !killswitchFired

// Shared-ACR consolidation: the owner env creates the single shared ACR; others pull
// from it cross-RG. Prod owns (it is the stable env; dev is disposable and depends on it).
var isAcrOwner = envName == acrOwnerEnv

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
// Module: registry
// ACR Basic — bot image repository.
// See: infra/AZURE_PLAN.md §2 (service selection), §7.2 (AcrPull role)
// ---------------------------------------------------------------------------

// Owner env (prod) creates + owns the single shared ACR (and grants its own MI AcrPull).
module registry 'modules/registry.bicep' = if (isAcrOwner) {
  name: 'registry-${envName}'
  params: {
    envName: envName
    location: location
    acrSku: acrSku
    jobPrincipalId: identity.outputs.principalId
  }
}

// Non-owner env (dev) does NOT create an ACR. It grants its OWN job MI AcrPull on the
// shared ACR, which lives in the owner env's RG — a cross-RG role assignment (nested
// deployment scoped to sharedAcrResourceGroup), same pattern as killswitch-rbac-prod.bicep.
// The CI SP has User Access Administrator on the owner RG, so this needs no operator step.
module sharedAcrPull 'modules/acr-pull-cross-rg.bicep' = if (!isAcrOwner) {
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
    keyVaultUri: keyvault.outputs.vaultUri
    logAnalyticsWorkspaceId: logs.outputs.workspaceId
    logAnalyticsWorkspaceKey: logs.outputs.workspaceKey
    dryRun: dryRun
    usePublicBootstrapImage: usePublicBootstrapImage
    enableSchedules: effectiveEnableSchedules
  }
  // keyvault and logs are already implicit dependencies via their outputs
  // consumed above (vaultUri, workspaceId/Key), so they are NOT listed here
  // (Bicep no-unnecessary-dependson). The ACR-pull grant IS listed: compute derives the
  // ACR login server from the containerImage string (not a module output), so there is no
  // implicit edge — but the job's AcrPull role assignment must exist before the job can
  // pull. Owner env (prod) waits on its registry module; non-owner (dev) waits on the
  // cross-RG AcrPull grant on the shared ACR.
  dependsOn: isAcrOwner ? [registry] : [sharedAcrPull]
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
// Outputs
// ---------------------------------------------------------------------------

@description('ACA Job resource name, for manual trigger via az containerapp job start.')
output jobName string = compute.outputs.jobName

@description('Shared ACR login server, for image push commands. Owner env (prod) emits its created registry\'s login server; non-owner (dev) emits the shared ACR\'s login server (derived from sharedAcrName). Uses safe-deref so the un-deployed registry module on the non-owner path resolves cleanly.')
output acrLoginServer string = registry.?outputs.loginServer ?? '${sharedAcrName}.azurecr.io'

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
