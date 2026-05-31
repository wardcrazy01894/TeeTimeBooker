// budget.bicep — Azure Cost Management budget alerts.
//
// IMPORTANT SCOPE NOTE: Cost Management budgets are subscription-scoped,
// not resource-group-scoped. This file uses targetScope = 'subscription'.
// It must be deployed with a separate az deployment sub create command,
// NOT as a nested module inside the RG-scoped main.bicep.
//
// Deploy command (operator, subscription-scope; the CI service principal is RG-scoped
// only, so the azure-iac budget step warns-and-skips — this must be run manually):
//   az deployment sub create \
//     --location eastus2 \
//     --template-file infra/bicep/modules/budget.bicep \
//     --parameters budgetAmountUsd=20 budgetAlertEmail=<email> \
//     --parameters killswitchBudgetAmountUsd=50 killswitchActionGroupId=<actionGroupId>
//
// TWO-TIER BUDGET DESIGN (two independent Microsoft.Consumption/budgets resources):
//
//   Tier 1 — budget-teetime ($20, email-only, unchanged):
//     Tracks combined dev+prod spend. Email-only notifications:
//       - Actual ≥ 80%  ($16): early warning.
//       - Forecasted ≥ 100% ($20): Azure projects the month will exceed $20.
//     This resource is NEVER changed by the killswitch design. budgetAmountUsd
//     stays $20; the $16/$20 email thresholds are unchanged.
//
//   Tier 2 — budget-teetime-killswitch ($50, killswitch-trigger, added by PR-KS2):
//     A SEPARATE, INDEPENDENT second budget resource on the same two-RG filter.
//     Single notification: Actual ≥ 100% (= $50), wired to the killswitch Action Group
//     (contactGroups references the Action Group resourceId from PR-KS1).
//     Both budgets evaluate the same subscription spend independently — this is by
//     design (standard tiered-alert pattern). Azure allows many budgets per subscription
//     scope as long as each has a distinct name; two overlapping-filter budgets is
//     explicitly supported and is the recommended approach for tiered alerting.
//
// See: infra/AZURE_PLAN.md §9.2 (budget alert), §4 (parameter strategy)
// See: infra/COST_KILLSWITCH_PLAN.md §2/Item10 (tier design rationale)

targetScope = 'subscription'

// ---------------------------------------------------------------------------
// Parameters
// ---------------------------------------------------------------------------

@description('Monthly budget ceiling in USD. Emails an Actual alert at 80% (early warning) and a Forecasted alert at 100% (projected to exceed the ceiling this month).')
@minValue(1)
@maxValue(1000)
param budgetAmountUsd int = 20

@description('Email address for budget alert notifications (used by the $20 email-only budget).')
param budgetAlertEmail string

@description('Monthly ceiling in USD for the separate killswitch budget. Defaults $50. The killswitch Action Group fires when actual spend hits this amount. Set to 0 to deploy without the killswitch budget (killswitchActionGroupId must also be empty).')
@minValue(0)
@maxValue(1000)
param killswitchBudgetAmountUsd int = 50

@description('ARM resource ID of the killswitch Action Group (from killswitch.outputs.actionGroupId). When non-empty, the separate $50 killswitch budget resource is created and wired to this Action Group. Empty string = killswitch budget omitted.')
param killswitchActionGroupId string = ''

@description('''Start date for the budget period. Format: YYYY-MM-01T00:00:00Z (first of a month).
utcNow('yyyy-MM-01T00:00:00Z') dynamically sets the first day of the current deployment month
so the budget starts correctly regardless of when it is deployed.
utcNow() is only valid as a parameter default value (not in resource body or variables) —
this placement satisfies that constraint.
If the Bicep linter rejects the literal characters in the format string, the fallback is:
  param budgetStartDate string = '${utcNow('yyyy-MM')}-01T00:00:00Z'
Both forms produce an identical result; the primary form is preferred for readability.''')
param budgetStartDate string = utcNow('yyyy-MM-01T00:00:00Z')

// ---------------------------------------------------------------------------
// Variables
// ---------------------------------------------------------------------------

// Tier 1: project-wide $20 email-only budget. Covers both teetime RGs (dev + prod).
// Fixed name → idempotent if deployed from either the dev or prod path.
var budgetName = 'budget-teetime'

// Tier 2: separate $50 killswitch-trigger budget. Same two-RG filter as Tier 1.
// Deployed only when killswitchActionGroupId is non-empty.
var killswitchBudgetName = 'budget-teetime-killswitch'

// Early-warning Actual alert at 80% of the Tier-1 ceiling ($16 of $20).
var alertThresholdPercent = 80

// ---------------------------------------------------------------------------
// Resources
// ---------------------------------------------------------------------------

// Microsoft.Consumption/budgets @ 2023-11-01
// Subscription-scoped; filtered to the rg-teetime-${envName} resource group
// so this budget only counts costs from this project's resources.
// Reference: https://learn.microsoft.com/en-us/azure/templates/microsoft.consumption/budgets
resource budget 'Microsoft.Consumption/budgets@2023-11-01' = {
  name: budgetName
  properties: {
    // Core budget definition
    category: 'Cost'
    amount: budgetAmountUsd
    timeGrain: 'Monthly'
    timePeriod: {
      // startDate must be the first day of a month in ISO-8601 format.
      // budgetStartDate defaults to the first of the current deployment month
      // via utcNow() in the param default above.
      startDate: budgetStartDate
    }

    // Filter to the project resource group only.
    // Without this filter the budget would alert on all subscription costs,
    // including unrelated resources. See: AZURE_PLAN.md §9.2.
    filter: {
      dimensions: {
        name: 'ResourceGroupName'
        operator: 'In'
        values: [
          'rg-teetime-dev'
          'rg-teetime-prod'
        ]
      }
    }

    // Two notifications, both emailing budgetAlertEmail:
    //  - Actual ≥ 80%  ($16): early warning that spend is climbing.
    //  - Forecasted ≥ 100% ($20): Azure projects this month will EXCEED the ceiling —
    //    this is the "email me if the bill is going to be north of $20" alert.
    notifications: {
      'actual_GreaterThan_${alertThresholdPercent}Pct': {
        enabled: true
        operator: 'GreaterThan'
        threshold: alertThresholdPercent
        thresholdType: 'Actual'
        contactEmails: [
          budgetAlertEmail
        ]
      }
      forecasted_GreaterThan_100Pct: {
        enabled: true
        operator: 'GreaterThan'
        threshold: 100
        thresholdType: 'Forecasted'
        contactEmails: [
          budgetAlertEmail
        ]
      }
    }
  }
}

// Tier 2: separate $50 killswitch-trigger budget.
// Only deployed when killswitchActionGroupId is provided (non-empty string).
// This is a SECOND, INDEPENDENT Microsoft.Consumption/budgets resource with a
// DISTINCT name (budget-teetime-killswitch). Azure supports multiple budgets per
// subscription scope with distinct names; both budgets evaluate the same spend
// independently — by design (tiered-alert pattern). No conflict with Tier 1.
//
// Single notification: Actual >= 100% (= $50), contactGroups wired to the
// killswitch Action Group. No contactEmails — the Tier-1 budget handles email.
//
// This resource is implemented (merged in PR-KS2). To arm the $50 tier, the operator
// runs az deployment sub create with killswitchActionGroupId (obtain from killswitch.outputs.actionGroupId
// via: az deployment group show -g rg-teetime-dev -n teetime-dev --query properties.outputs.killswitchActionGroupId.value -o tsv).
// See infra/AZURE_PLAN.md §9.2 for the full deploy command.
resource killswitchBudget 'Microsoft.Consumption/budgets@2023-11-01' = if (!empty(killswitchActionGroupId)) {
  name: killswitchBudgetName
  properties: {
    category: 'Cost'
    amount: killswitchBudgetAmountUsd
    timeGrain: 'Monthly'
    timePeriod: {
      startDate: budgetStartDate
    }
    // Same two-RG filter as Tier 1 — both budgets track the full project spend.
    // Two independent budgets on the same filter is intentional (tiered alerting).
    filter: {
      dimensions: {
        name: 'ResourceGroupName'
        operator: 'In'
        values: [
          'rg-teetime-dev'
          'rg-teetime-prod'
        ]
      }
    }
    notifications: {
      // Single notification: Actual >= 100% of $50 = $50 hard cap.
      // contactGroups wired to the killswitch Action Group (triggers Logic App).
      // contactEmails intentionally empty — Tier-1 budget handles email alerts.
      actual_GreaterThan_100Pct_killswitch: {
        enabled: true
        operator: 'GreaterThan'
        threshold: 100
        thresholdType: 'Actual'
        contactEmails: []
        contactGroups: [
          killswitchActionGroupId
        ]
      }
    }
  }
}

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------

@description('Tier-1 budget resource name ($20, email-only).')
output budgetName string = budget.name

@description('Tier-2 killswitch budget resource name ($50, Action Group wired). Empty string if not deployed.')
output killswitchBudgetName string = !empty(killswitchActionGroupId) ? killswitchBudget.name : ''
