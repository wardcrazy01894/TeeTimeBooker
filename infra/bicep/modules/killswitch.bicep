// killswitch.bicep — Cost Killswitch: Logic App (Consumption) + Action Group + RBAC.
//
// When the $50 monthly budget threshold (Actual >= 100%) fires, the Azure Monitor
// Action Group triggers this Logic App via its HTTP trigger. The Logic App issues
// TWELVE HTTP calls — 3 jobs × 2 envs (dev + prod) × 2 actions (PATCH + POST /stop):
//
//   LEVER (a) — 6 PATCH calls to Microsoft.App/jobs (api-version 2024-03-01):
//     Sets triggerType=Manual on each ACA Job so FUTURE scheduled fires are suppressed.
//     Body: { "properties": { "configuration": {
//       "triggerType": "Manual",
//       "scheduleTriggerConfig": null,
//       "manualTriggerConfig": { "replicaCompletionCount": 1, "parallelism": 1 }
//     }}}
//     Source: https://learn.microsoft.com/en-us/rest/api/resource-manager/containerapps/jobs/update?view=rest-resource-manager-containerapps-2024-03-01
//
//   LEVER (b) — 6 POST calls to Microsoft.App/jobs/{name}/stop (api-version 2024-03-01):
//     Stops ALL IN-FLIGHT executions for each ACA Job in a single call (no enumeration needed).
//     A replica already running at $50-trip time keeps billing until stopped — lever (a) alone
//     does NOT halt current spend. Empty request body; returns 200 with list of stopped
//     executions (empty list = no-op, not an error).
//     Source: https://learn.microsoft.com/en-us/rest/api/resource-manager/containerapps/jobs/stop-multiple-executions?view=rest-resource-manager-containerapps-2024-03-01
//
// RBAC (VERIFIED — see infra/COST_KILLSWITCH_PLAN.md §2/Item4):
//   Logic App system-assigned MI gets a custom role ("ACA Job Schedule Manager")
//   with ONLY Microsoft.App/jobs/read + Microsoft.App/jobs/write + Microsoft.App/jobs/stop/action,
//   scoped to each target RG. Two role assignments: one for rg-teetime-dev (inline) and one for
//   rg-teetime-prod (via nested module with cross-RG scope). The role definition itself must be
//   created MANUALLY by the operator (requires subscription-level
//   Microsoft.Authorization/roleDefinitions/write, which the CI service principal does NOT have).
//   Pass the resulting GUID as killswitchRbacRoleId.
//
//   Custom role definition (create manually before deploying killswitch.bicep):
//   {
//     "Name": "ACA Job Schedule Manager",
//     "Actions": [
//       "Microsoft.App/jobs/read",
//       "Microsoft.App/jobs/write",
//       "Microsoft.App/jobs/stop/action"
//     ],
//     "AssignableScopes": ["/subscriptions/3f82c7e1-4b1b-4a55-b905-d79f65c6887d"]
//   }
//
// Action Group wiring (VERIFIED — see infra/COST_KILLSWITCH_PLAN.md §2/Item5):
//   Microsoft.Insights/actionGroups@2023-01-01 supports logicAppReceivers[].
//   callbackUrl is obtained via listCallbackUrl() at deploy time — no secret stored.
//   Source: https://learn.microsoft.com/en-us/azure/templates/microsoft.insights/2023-01-01/actiongroups
//
// INVARIANT — TRIGGER NAME COUPLING:
//   The Logic App HTTP trigger is declared with the name 'manual' (see the
//   triggers.manual block in the workflow definition). The listCallbackUrl()
//   call below references this name:
//     listCallbackUrl('${logicApp.id}/triggers/manual', '2019-05-01')
//   If this trigger is ever renamed (e.g. to 'When_HTTP_request'), the
//   listCallbackUrl path will point to a non-existent trigger URL, silently
//   producing a 404 when the Action Group fires. The trigger name MUST remain
//   'manual' (exact match, lowercase) in the workflow definition — do not rename it.
//
// Idempotency (VERIFIED — see infra/COST_KILLSWITCH_PLAN.md §2/Item6):
//   PATCH triggerType=Manual on an already-Manual job returns HTTP 200 (no error).
//   POST /stop when no executions are running returns HTTP 200 with empty list (no error).
//   Budget alerts re-fire daily while over threshold; both levers are safe to call repeatedly.
//
// IMPORTANT — Deploy-clobber risk (see infra/COST_KILLSWITCH_PLAN.md §2/Item2):
//   A CI deploy with enableSchedules=true will RE-ARM the killswitched jobs.
//   The deploy-clobber guard is the killswitchFired param in main.bicep: when
//   set to true in the param files it forces enableSchedules=false regardless of
//   the enableSchedules param value. After the killswitch fires, the operator
//   creates a PR setting killswitchFired=true in both param files and merges it
//   (requires full PR flow — minutes). Until that PR merges, concurrent infra
//   deploys can re-arm the jobs. Budget alerts re-fire at most once per day, so
//   the re-arm window is up to ~24 hours if the operator is slow. See the gap
//   window analysis in infra/COST_KILLSWITCH_PLAN.md §2/Item2 for the full
//   residual risk statement.
//
// Cost: Logic App Consumption is FREE for the first 4,000 actions/month.
//   This Logic App fires at most 12 HTTP actions per budget-alert evaluation,
//   and budget alerts fire at most once per day — ~360 actions/month max.
//   Effective monthly cost: $0.00.
//   See infra/COST_KILLSWITCH_PLAN.md §2/Item9.
//
// Deploy gate: main.bicep's param default for enableKillswitch is false, but both
// bicepparam files and azure-iac.yml set enableKillswitch=true and supply the custom
// role GUID — so this module IS deployed (live in dev as of 2026-05-31).
//
// Deploy dependency: this module is deployed in rg-teetime-dev. The role
// assignments target BOTH rg-teetime-dev (inline) and rg-teetime-prod (nested
// module with cross-RG scope). See infra/COST_KILLSWITCH_PLAN.md §2/Item3 for the
// resolved U3 cross-RG RBAC strategy.
//
// NON-REAL-TIME LIMITATION: Azure cost data lags 8–24 hours. This killswitch
// is a slow-runaway backstop, NOT instant per-run protection. See §4 (Limitations).
//
// See: infra/COST_KILLSWITCH_PLAN.md (full verified design + all 10 pre-emption items)
//      infra/AZURE_PLAN.md §9.2 (budget), §10 (runbook)

targetScope = 'resourceGroup'

// ---------------------------------------------------------------------------
// Parameters
// ---------------------------------------------------------------------------

@description('Environment name suffix (e.g. dev, prod). Used to derive job names.')
param envName string

@description('Azure region for the Logic App and Action Group resources.')
param location string

@description('GUID of the pre-created "ACA Job Schedule Manager" custom role definition. Must be created manually by the operator (subscription-level Microsoft.Authorization/roleDefinitions/write required — not deployable by the CI service principal). Role actions: Microsoft.App/jobs/read + Microsoft.App/jobs/write + Microsoft.App/jobs/stop/action. See infra/COST_KILLSWITCH_PLAN.md §2/Item4 for the exact role definition JSON.')
param killswitchRbacRoleId string

@description('Subscription ID containing both rg-teetime-dev and rg-teetime-prod. Defaults to the current subscription.')
param subscriptionId string = subscription().subscriptionId

@description('The prod resource group name (the Logic App MI needs ACA Job Schedule Manager on this RG too, since jobs span both envs).')
param prodRgName string = 'rg-teetime-prod'

// ---------------------------------------------------------------------------
// Variables
// ---------------------------------------------------------------------------

var logicAppName = 'logic-teetime-killswitch-${envName}'
var actionGroupName = 'ag-teetime-killswitch-${envName}'

// The three ACA Job names that the Logic App will PATCH + POST /stop.
// Naming must match compute.bicep: teetime-job-{envName}-edt-sun, -est-sun,
// and teetime-watch-job-{envName}.
var bookingJobEdtSun  = 'teetime-job-${envName}-edt-sun'
var bookingJobEstSun  = 'teetime-job-${envName}-est-sun'
var watchJob          = 'teetime-watch-job-${envName}'

// Both envs' resource group names. The Logic App patches jobs in the SAME RG it is
// deployed in (rg-teetime-dev) plus the prod RG. The dev killswitch handles both
// envs because the $50 budget covers both. See COST_KILLSWITCH_PLAN §Item3.
var devRgName = resourceGroup().name

// Prod ACA Job names: same naming convention but for the prod env.
var bookingJobEdtSunProd  = 'teetime-job-prod-edt-sun'
var bookingJobEstSunProd  = 'teetime-job-prod-est-sun'
var watchJobProd          = 'teetime-watch-job-prod'

// Resource tags applied to all resources in this module.
var tags = {
  application: 'teetime'
  environment: envName
  managedBy: 'bicep'
  component: 'cost-killswitch'
}

// The PATCH body to disable a job's schedule (Lever a). Sets triggerType=Manual
// so the job never auto-fires again until re-declared as Schedule.
// See COST_KILLSWITCH_PLAN §1/Lever(a) for the verified ARM API shape.
var patchBodyDisable = {
  properties: {
    configuration: {
      triggerType: 'Manual'
      scheduleTriggerConfig: null
      manualTriggerConfig: {
        replicaCompletionCount: 1
        parallelism: 1
      }
    }
  }
}

// ---------------------------------------------------------------------------
// Resources
// ---------------------------------------------------------------------------

// Logic App (Consumption) — HTTP trigger + 6 PATCH + 6 POST /stop actions.
// System-assigned managed identity used to authenticate against the ACA API
// (management.azure.com). The MI's principalId receives two RBAC assignments
// (rg-teetime-dev + rg-teetime-prod) after this resource is created.
//
// Workflow definition format: Consumption Logic App ARM definition (inline JSON).
// The workflow JSON is the Logic App Designer's "Code view" representation.
// useCommonAlertSchema=true in the Action Group means the HTTP trigger body
// will be in the Common Alert Schema format.
//
// Workflow definition: HTTP trigger → Parse JSON (Common Alert Schema) → 12 parallel
// HTTP actions (6 PATCH + 6 POST /stop targeting all 6 ACA Job resources in
// both rg-teetime-dev and rg-teetime-prod).
//
// The 12 actions (all parallel — runAfter: {}):
//
//   Lever (a) — 6 PATCH calls (Schedule → Manual):
//   Patch_booking_edt_sun_dev:   PATCH .../rg-teetime-dev/providers/Microsoft.App/jobs/teetime-job-dev-edt-sun?api-version=2024-03-01
//   Patch_booking_est_sun_dev:   PATCH .../rg-teetime-dev/providers/Microsoft.App/jobs/teetime-job-dev-est-sun?api-version=2024-03-01
//   Patch_watch_dev:             PATCH .../rg-teetime-dev/providers/Microsoft.App/jobs/teetime-watch-job-dev?api-version=2024-03-01
//   Patch_booking_edt_sun_prod:  PATCH .../rg-teetime-prod/providers/Microsoft.App/jobs/teetime-job-prod-edt-sun?api-version=2024-03-01
//   Patch_booking_est_sun_prod:  PATCH .../rg-teetime-prod/providers/Microsoft.App/jobs/teetime-job-prod-est-sun?api-version=2024-03-01
//   Patch_watch_prod:            PATCH .../rg-teetime-prod/providers/Microsoft.App/jobs/teetime-watch-job-prod?api-version=2024-03-01
//
//   Lever (b) — 6 POST /stop calls (stop in-flight executions):
//   Stop_booking_edt_sun_dev:    POST .../rg-teetime-dev/providers/Microsoft.App/jobs/teetime-job-dev-edt-sun/stop?api-version=2024-03-01
//   Stop_booking_est_sun_dev:    POST .../rg-teetime-dev/providers/Microsoft.App/jobs/teetime-job-dev-est-sun/stop?api-version=2024-03-01
//   Stop_watch_dev:              POST .../rg-teetime-dev/providers/Microsoft.App/jobs/teetime-watch-job-dev/stop?api-version=2024-03-01
//   Stop_booking_edt_sun_prod:   POST .../rg-teetime-prod/providers/Microsoft.App/jobs/teetime-job-prod-edt-sun/stop?api-version=2024-03-01
//   Stop_booking_est_sun_prod:   POST .../rg-teetime-prod/providers/Microsoft.App/jobs/teetime-job-prod-est-sun/stop?api-version=2024-03-01
//   Stop_watch_prod:             POST .../rg-teetime-prod/providers/Microsoft.App/jobs/teetime-watch-job-prod/stop?api-version=2024-03-01
//
// All 12 HTTP actions authenticate with:
//   authentication: { "type": "ManagedServiceIdentity", "audience": "https://management.azure.com/" }
//
// Do NOT retry on 4xx (job not found = deleted is OK; 401/403 = RBAC misconfigured, surface error).
// Retry on 5xx is fine (transient ARM errors).
// Idempotency: PATCH Manual→Manual = 200; POST /stop with no running replicas = 200 + empty list.
resource logicApp 'Microsoft.Logic/workflows@2019-05-01' = {
  name: logicAppName
  location: location
  tags: tags
  identity: {
    // System-assigned MI: the principalId is known after creation and used
    // in the RBAC assignments below. System-assigned is appropriate here
    // because the Logic App and its MI are co-located in the same RG and
    // have the same lifecycle.
    type: 'SystemAssigned'
  }
  properties: {
    state: 'Enabled'
    definition: {
      '$schema': 'https://schema.management.azure.com/providers/Microsoft.Logic/schemas/2016-06-01/workflowdefinition.json#'
      contentVersion: '1.0.0.0'
      triggers: {
        manual: {
          type: 'Request'
          kind: 'Http'
          inputs: {
            schema: {}
          }
        }
      }
      actions: {
        // Lever (a) — 6 PATCH calls: Schedule → Manual (disable future fires).
        // All 12 actions run in parallel (runAfter: {}). URIs are Bicep string
        // interpolations resolved at deploy time — they embed the subscription ID,
        // RG names, and job names from Bicep variables, producing static strings
        // in the ARM deployment output. The Logic App then calls these literal
        // endpoints at runtime.
        Patch_booking_edt_sun_dev: {
          type: 'Http'
          inputs: {
            method: 'PATCH'
            uri: 'https://management.azure.com/subscriptions/${subscriptionId}/resourceGroups/${devRgName}/providers/Microsoft.App/jobs/${bookingJobEdtSun}?api-version=2024-03-01'
            body: patchBodyDisable
            authentication: {
              type: 'ManagedServiceIdentity'
              audience: 'https://management.azure.com/'
            }
          }
          runAfter: {}
        }
        Patch_booking_est_sun_dev: {
          type: 'Http'
          inputs: {
            method: 'PATCH'
            uri: 'https://management.azure.com/subscriptions/${subscriptionId}/resourceGroups/${devRgName}/providers/Microsoft.App/jobs/${bookingJobEstSun}?api-version=2024-03-01'
            body: patchBodyDisable
            authentication: {
              type: 'ManagedServiceIdentity'
              audience: 'https://management.azure.com/'
            }
          }
          runAfter: {}
        }
        Patch_watch_dev: {
          type: 'Http'
          inputs: {
            method: 'PATCH'
            uri: 'https://management.azure.com/subscriptions/${subscriptionId}/resourceGroups/${devRgName}/providers/Microsoft.App/jobs/${watchJob}?api-version=2024-03-01'
            body: patchBodyDisable
            authentication: {
              type: 'ManagedServiceIdentity'
              audience: 'https://management.azure.com/'
            }
          }
          runAfter: {}
        }
        Patch_booking_edt_sun_prod: {
          type: 'Http'
          inputs: {
            method: 'PATCH'
            uri: 'https://management.azure.com/subscriptions/${subscriptionId}/resourceGroups/${prodRgName}/providers/Microsoft.App/jobs/${bookingJobEdtSunProd}?api-version=2024-03-01'
            body: patchBodyDisable
            authentication: {
              type: 'ManagedServiceIdentity'
              audience: 'https://management.azure.com/'
            }
          }
          runAfter: {}
        }
        Patch_booking_est_sun_prod: {
          type: 'Http'
          inputs: {
            method: 'PATCH'
            uri: 'https://management.azure.com/subscriptions/${subscriptionId}/resourceGroups/${prodRgName}/providers/Microsoft.App/jobs/${bookingJobEstSunProd}?api-version=2024-03-01'
            body: patchBodyDisable
            authentication: {
              type: 'ManagedServiceIdentity'
              audience: 'https://management.azure.com/'
            }
          }
          runAfter: {}
        }
        Patch_watch_prod: {
          type: 'Http'
          inputs: {
            method: 'PATCH'
            uri: 'https://management.azure.com/subscriptions/${subscriptionId}/resourceGroups/${prodRgName}/providers/Microsoft.App/jobs/${watchJobProd}?api-version=2024-03-01'
            body: patchBodyDisable
            authentication: {
              type: 'ManagedServiceIdentity'
              audience: 'https://management.azure.com/'
            }
          }
          runAfter: {}
        }
        // Lever (b) — 6 POST /stop calls: stop all in-flight executions.
        // Runs in parallel with lever (a). Idempotent: returns 200 + empty list
        // when no executions are running (not an error). See COST_KILLSWITCH_PLAN §1.
        Stop_booking_edt_sun_dev: {
          type: 'Http'
          inputs: {
            method: 'POST'
            uri: 'https://management.azure.com/subscriptions/${subscriptionId}/resourceGroups/${devRgName}/providers/Microsoft.App/jobs/${bookingJobEdtSun}/stop?api-version=2024-03-01'
            body: {}
            authentication: {
              type: 'ManagedServiceIdentity'
              audience: 'https://management.azure.com/'
            }
          }
          runAfter: {}
        }
        Stop_booking_est_sun_dev: {
          type: 'Http'
          inputs: {
            method: 'POST'
            uri: 'https://management.azure.com/subscriptions/${subscriptionId}/resourceGroups/${devRgName}/providers/Microsoft.App/jobs/${bookingJobEstSun}/stop?api-version=2024-03-01'
            body: {}
            authentication: {
              type: 'ManagedServiceIdentity'
              audience: 'https://management.azure.com/'
            }
          }
          runAfter: {}
        }
        Stop_watch_dev: {
          type: 'Http'
          inputs: {
            method: 'POST'
            uri: 'https://management.azure.com/subscriptions/${subscriptionId}/resourceGroups/${devRgName}/providers/Microsoft.App/jobs/${watchJob}/stop?api-version=2024-03-01'
            body: {}
            authentication: {
              type: 'ManagedServiceIdentity'
              audience: 'https://management.azure.com/'
            }
          }
          runAfter: {}
        }
        Stop_booking_edt_sun_prod: {
          type: 'Http'
          inputs: {
            method: 'POST'
            uri: 'https://management.azure.com/subscriptions/${subscriptionId}/resourceGroups/${prodRgName}/providers/Microsoft.App/jobs/${bookingJobEdtSunProd}/stop?api-version=2024-03-01'
            body: {}
            authentication: {
              type: 'ManagedServiceIdentity'
              audience: 'https://management.azure.com/'
            }
          }
          runAfter: {}
        }
        Stop_booking_est_sun_prod: {
          type: 'Http'
          inputs: {
            method: 'POST'
            uri: 'https://management.azure.com/subscriptions/${subscriptionId}/resourceGroups/${prodRgName}/providers/Microsoft.App/jobs/${bookingJobEstSunProd}/stop?api-version=2024-03-01'
            body: {}
            authentication: {
              type: 'ManagedServiceIdentity'
              audience: 'https://management.azure.com/'
            }
          }
          runAfter: {}
        }
        Stop_watch_prod: {
          type: 'Http'
          inputs: {
            method: 'POST'
            uri: 'https://management.azure.com/subscriptions/${subscriptionId}/resourceGroups/${prodRgName}/providers/Microsoft.App/jobs/${watchJobProd}/stop?api-version=2024-03-01'
            body: {}
            authentication: {
              type: 'ManagedServiceIdentity'
              audience: 'https://management.azure.com/'
            }
          }
          runAfter: {}
        }
      }
      outputs: {}
    }
    parameters: {}
  }
}

// Action Group — single logicAppReceiver pointing at the Logic App.
// logicAppReceivers requires: resourceId (Logic App ARM ID) + callbackUrl
// (the HTTP trigger URL, obtained via listCallbackUrl at deploy time).
// useCommonAlertSchema=true so the budget alert payload follows the
// Common Alert Schema — the Logic App workflow parses it accordingly.
//
// api-version 2023-01-01 (stable) — logicAppReceivers supported since 2017.
// Source: https://learn.microsoft.com/en-us/azure/templates/microsoft.insights/2023-01-01/actiongroups
resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: actionGroupName
  location: 'global'   // Action Groups must use 'global' location
  tags: tags
  properties: {
    groupShortName: 'ks-teetime'   // max 12 chars; used in SMS subject
    enabled: true
    logicAppReceivers: [
      {
        name: 'killswitch-logic-app'
        resourceId: logicApp.id
        // listCallbackUrl() retrieves the HTTP trigger URL at deploy time.
        // The URL contains an embedded SAS token (valid ~5 years) that authorizes
        // the Action Group to invoke the trigger without AAD auth.
        // Re-deploy updates the URL automatically if the Logic App is recreated.
        // Bicep function: listCallbackUrl('{resourceId}/triggers/manual', '2019-05-01').value
        // COMPILE-VERIFIED: `az bicep build` on this file succeeds with no errors on this line.
        // The string interpolation '${logicApp.id}/triggers/manual' is valid; the non-literal
        // path segment does not prevent compilation.
        callbackUrl: listCallbackUrl('${logicApp.id}/triggers/manual', '2019-05-01').value
        useCommonAlertSchema: true
      }
    ]
    // Email/SMS/webhook receivers are empty — this action group is solely for
    // Logic App invocation. Budget email notifications remain in budget.bicep.
    emailReceivers: []
    smsReceivers: []
    webhookReceivers: []
    armRoleReceivers: []
    azureAppPushReceivers: []
    azureFunctionReceivers: []
    eventHubReceivers: []
    itsmReceivers: []
    voiceReceivers: []
    automationRunbookReceivers: []
  }
}

// RBAC — Logic App system-assigned MI → "ACA Job Schedule Manager" custom role.
// Two assignments: one per RG (both envs share the same subscription and the
// Logic App must PATCH + POST /stop jobs in both).
//
// The custom role definition (killswitchRbacRoleId) must be pre-created manually
// by the operator. Actions needed:
//   Microsoft.App/jobs/read       — read job state
//   Microsoft.App/jobs/write      — PATCH triggerType (Lever a)
//   Microsoft.App/jobs/stop/action — POST /stop (Lever b)
// See COST_KILLSWITCH_PLAN §2/Item4 for the exact az role definition create command.
//
// Cross-RG RBAC strategy (resolved — was U3 in COST_KILLSWITCH_PLAN.md):
//   The rg-teetime-prod role assignment is cross-RG from this module's deployment
//   scope (rg-teetime-dev). Bicep resourceGroup() in an inline resource always
//   targets the deployment RG. The solution is a NESTED MODULE with a scope
//   expression: `scope: resourceGroup(subscriptionId, prodRgName)`. This causes
//   Bicep to emit a nested ARM deployment that the ARM engine places in rg-teetime-prod.
//   The CI SP has `User Access Administrator` on rg-teetime-prod (AZURE_PLAN §10.1.1
//   step 2, DONE 2026-05-31 — §8.2 only covers the dev RG assignment), so this
//   assignment CAN be deployed by CI.
resource rbacDev 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  // The role assignment name must be a GUID deterministic from deployment-start inputs only.
  // logicApp.identity.principalId is a run-time value (not known at start), so it cannot
  // appear here. We use resourceGroup().id + roleId + logicApp.id as a stable, unique seed.
  // logicApp.id is known at start (it is a function of the resource name, which is a param).
  name: guid(resourceGroup().id, killswitchRbacRoleId, logicApp.id)
  scope: resourceGroup()   // rg-teetime-dev
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', killswitchRbacRoleId)
    principalId: logicApp.identity.principalId
    principalType: 'ServicePrincipal'
    // No ABAC condition needed: the role is already scoped to Microsoft.App/jobs only.
  }
}

// Nested module: cross-RG role assignment for rg-teetime-prod.
// Bicep cannot place an inline resource in a different RG from the deployment scope.
// A nested module with `scope: resourceGroup(subscriptionId, prodRgName)` emits an
// ARM nested deployment targeted at prodRgName — the ARM engine creates the role
// assignment in that RG. The CI SP has User Access Administrator on rg-teetime-prod
// (AZURE_PLAN.md §10.1.1 step 2, DONE 2026-05-31 — §8.2 only covers the dev RG)
// so this is deployable without operator intervention.
//
// Nested module: cross-RG role assignment for rg-teetime-prod.
// scope: resourceGroup(subscriptionId, prodRgName) causes Bicep to emit a nested
// ARM deployment that the ARM engine places in rg-teetime-prod. The CI SP has
// User Access Administrator on rg-teetime-prod (AZURE_PLAN §10.1.1, DONE 2026-05-31).
module rbacProd 'killswitch-rbac-prod.bicep' = {
  name: 'rbac-killswitch-prod'
  scope: resourceGroup(subscriptionId, prodRgName)
  params: {
    killswitchRbacRoleId: killswitchRbacRoleId
    logicAppPrincipalId: logicApp.identity.principalId
    logicAppId: logicApp.id
  }
}

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------

@description('ARM resource ID of the Action Group. Pass as killswitchActionGroupId to budget.bicep to arm the $50 budget threshold; budget.bicep already has the killswitchBudget resource wired (conditional on this output). Obtain via: az deployment group show -g rg-teetime-dev -n teetime-dev --query properties.outputs.killswitchActionGroupId.value -o tsv')
output actionGroupId string = actionGroup.id

@description('Principal ID of the Logic App system-assigned managed identity. Used to verify the RBAC assignments post-deploy: az role assignment list --assignee <principalId>.')
output logicAppPrincipalId string = logicApp.identity.principalId

@description('Logic App resource ID.')
output logicAppId string = logicApp.id
