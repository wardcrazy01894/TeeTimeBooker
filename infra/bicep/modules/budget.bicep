// budget.bicep — Azure Cost Management budget alert.
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
//     --parameters budgetAmountUsd=20 budgetAlertEmail=<email>
//
// Single $20/mo budget across both teetime RGs (dev + prod) = the total project bill.
// Two email notifications (notification only — does NOT stop/throttle usage):
//   - Actual ≥ 80%  ($16): early warning.
//   - Forecasted ≥ 100% ($20): Azure projects the month will exceed $20.
//
// See: infra/AZURE_PLAN.md §9.2 (budget alert), §4 (parameter strategy)

targetScope = 'subscription'

// ---------------------------------------------------------------------------
// Parameters
// ---------------------------------------------------------------------------

@description('Monthly budget ceiling in USD. Emails an Actual alert at 80% (early warning) and a Forecasted alert at 100% (projected to exceed the ceiling this month).')
@minValue(1)
@maxValue(1000)
param budgetAmountUsd int = 20

@description('Email address for budget alert notifications.')
param budgetAlertEmail string

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

// Single project-wide budget (covers both teetime RGs across dev + prod) so it tracks the
// TOTAL monthly bill, not one environment. Fixed name → idempotent if deployed from either
// the dev or prod path.
var budgetName = 'budget-teetime'

// Early-warning Actual alert at 80% of the ceiling ($16 of $20).
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

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------

@description('Budget resource name.')
output budgetName string = budget.name
