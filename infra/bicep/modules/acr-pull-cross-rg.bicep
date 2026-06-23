// acr-pull-cross-rg.bicep — grant a job's managed identity AcrPull on a SHARED ACR
// that lives in a DIFFERENT resource group.
//
// Shared-ACR consolidation (full-repo-scan cost finding): instead of one ACR per
// environment (~$5/mo each), both envs pull from a single shared ACR. The shared ACR
// is PROD's existing registry (in rg-teetime-prod, the stable env). The DEV env's
// main.bicep no longer creates its own ACR — it deploys THIS module to the shared ACR's
// RG to grant the dev job's user-assigned MI AcrPull on the shared registry.
//
// Why a separate file + nested deployment: Bicep cannot create a resource (the role
// assignment, scoped to the shared ACR) in a different RG from the current deployment
// scope. The caller deploys this module with `scope: resourceGroup(sharedAcrResourceGroup)`,
// so the assignment lands in the shared ACR's RG. Same pattern as killswitch-rbac-prod.bicep.
//
// Caller in main.bicep (dev only — prod OWNS the ACR via the registry module):
//   module sharedAcrPull 'modules/acr-pull-cross-rg.bicep' = if (!isAcrOwner) {
//     name: 'shared-acrpull-${envName}'
//     scope: resourceGroup(sharedAcrResourceGroup)   // rg-teetime-prod
//     params: { acrName: sharedAcrName, jobPrincipalId: identity.outputs.principalId }
//   }
//
// CI deployability: the CI service principal has User Access Administrator on
// rg-teetime-prod (it already creates the killswitch cross-RG role assignment there —
// AZURE_PLAN.md §10.1.1 / COST_KILLSWITCH_PLAN.md), so this nested deployment needs no
// operator step. AcrPull is read-only (pull, not push); the dev image PUSH (`az acr build`)
// uses the CI SP's Contributor on rg-teetime-prod, not this assignment.

targetScope = 'resourceGroup'

// ---------------------------------------------------------------------------
// Parameters
// ---------------------------------------------------------------------------

@description('Name of the shared ACR (in this module\'s target RG) to grant pull on.')
param acrName string

@description('Principal ID of the job user-assigned managed identity receiving AcrPull.')
param jobPrincipalId string

// ---------------------------------------------------------------------------
// Variables / resources
// ---------------------------------------------------------------------------

// AcrPull built-in role GUID — stable across all Azure environments (same id used in
// registry.bicep for the owner-RG assignment).
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'

// Reference the EXISTING shared ACR in this module's (target) resource group.
resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: acrName
}

resource acrPullAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  // Deterministic GUID from (ACR scope, principal, role) so re-deploys are idempotent.
  // jobPrincipalId is a param (resolved before the name expression), so it is allowed here.
  name: guid(acr.id, jobPrincipalId, acrPullRoleId)
  scope: acr
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
    principalId: jobPrincipalId
    principalType: 'ServicePrincipal'
  }
}
