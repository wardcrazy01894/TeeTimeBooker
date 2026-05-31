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

module registry 'modules/registry.bicep' = {
  name: 'registry-${envName}'
  params: {
    envName: envName
    location: location
    acrSku: acrSku
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
  }
}

// ---------------------------------------------------------------------------
// Module: logs
// Log Analytics Workspace + Application Insights.
// See: infra/AZURE_PLAN.md §11
// ---------------------------------------------------------------------------

module logs 'modules/logs.bicep' = {
  // TODO(M-azure-T5): output workspaceId to compute.bicep for ACA env config
  name: 'logs-${envName}'
  params: {
    envName: envName
    location: location
  }
}

// ---------------------------------------------------------------------------
// Module: compute
// Container Apps Environment (Consumption) + Container Apps Job (four crons: Sat+Sun × DST).
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
  // (Bicep no-unnecessary-dependson). registry IS
  // listed: compute derives the ACR login server from the containerImage string
  // (not a registry output), so there is no implicit edge — but the job's AcrPull
  // role assignment in registry.bicep must exist before the job can pull.
  dependsOn: [registry]
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

@description('ACR login server, for image push commands.')
output acrLoginServer string = registry.outputs.loginServer

@description('Key Vault URI, for az keyvault secret set commands.')
output keyVaultUri string = keyvault.outputs.vaultUri

@description('User-assigned managed identity principal ID. Used to verify RBAC assignments post-deploy.')
output identityPrincipalId string = identity.outputs.principalId

@description('ARM resource ID of the killswitch Action Group. Empty string when the killswitch module is not deployed (enableKillswitch=false, killswitchRbacRoleId empty, or envName!=dev). Pass to budget.bicep as killswitchActionGroupId in PR-KS2 to wire the $50 budget threshold.')
// Use the safe-dereference operator (.?) + null-coalesce (??) rather than an any()-cast: when
// the killswitch module is not deployed, `killswitch.?outputs` is null and we fall back to ''.
// When it IS deployed, this resolves to the actionGroupId STRING (Bicep's normal module-output
// `.value` unwrapping is preserved). The previous any()-cast approach returned the raw
// {value,type} object and failed output evaluation at deploy time (DeploymentOutputEvaluationFailed).
output killswitchActionGroupId string = killswitch.?outputs.actionGroupId ?? ''
