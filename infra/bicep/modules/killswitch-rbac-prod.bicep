// killswitch-rbac-prod.bicep — Cross-RG role assignment for rg-teetime-prod.
//
// This module is deployed BY killswitch.bicep (which lives in rg-teetime-dev)
// with `scope: resourceGroup(subscriptionId, prodRgName)`. Bicep emits this as
// a nested ARM deployment targeted at rg-teetime-prod, so the role assignment
// lands in the correct RG even though the parent module is deployed in rg-teetime-dev.
//
// Why a separate file: Bicep does not allow inline resources to target a different
// RG from the current deployment scope. A nested module with a scope expression is
// the required pattern. The targetScope of this file matches the outer deployment
// scope (resourceGroup), which is what Bicep requires for the nested deployment.
//
// Caller in killswitch.bicep:
//   module rbacProd 'killswitch-rbac-prod.bicep' = {
//     name: 'rbac-killswitch-prod'
//     scope: resourceGroup(subscriptionId, prodRgName)  // rg-teetime-prod
//     params: {
//       killswitchRbacRoleId: killswitchRbacRoleId
//       logicAppPrincipalId: logicApp.identity.principalId
//       logicAppId: logicApp.id
//     }
//   }
//
// CI deployability: the CI SP has `User Access Administrator` on rg-teetime-prod
// (AZURE_PLAN.md §10.1.1), so this nested deployment is deployable without
// operator intervention.
//
// Role definition (custom, pre-created by operator):
//   Actions: Microsoft.App/jobs/read + write + stop/action
//   Name: "ACA Job Schedule Manager"
//   See: infra/COST_KILLSWITCH_PLAN.md §2/Item4 for the exact az CLI command.
//
// See: infra/COST_KILLSWITCH_PLAN.md (full design + pre-emption items)

targetScope = 'resourceGroup'

// ---------------------------------------------------------------------------
// Parameters
// ---------------------------------------------------------------------------

@description('GUID of the pre-created "ACA Job Schedule Manager" custom role definition. Passed from killswitch.bicep.')
param killswitchRbacRoleId string

@description('Principal ID of the Logic App system-assigned managed identity. Passed from killswitch.bicep after logicApp resource is created.')
param logicAppPrincipalId string

@description('Resource ID of the Logic App. Used as a stable seed for the role assignment GUID.')
param logicAppId string

// ---------------------------------------------------------------------------
// Resources
// ---------------------------------------------------------------------------

// TODO(PR-KS1): Implement the role assignment resource below.
// The resource is commented out in the stub because killswitch.bicep's
// TODO(PR-KS1) module call is also commented out — both land together in PR-KS1.
//
// resource rbacProd 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
//   // GUID deterministic from deployment-scope inputs: rg().id + roleId + logicAppId.
//   // logicAppPrincipalId is a param (not resourceGroup().id), so it can appear here
//   // without the "run-time value in resource name" restriction — params are resolved
//   // before the name expression is evaluated.
//   name: guid(resourceGroup().id, killswitchRbacRoleId, logicAppId)
//   scope: resourceGroup()   // rg-teetime-prod (set by the outer scope expression)
//   properties: {
//     roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', killswitchRbacRoleId)
//     principalId: logicAppPrincipalId
//     principalType: 'ServicePrincipal'
//   }
// }
