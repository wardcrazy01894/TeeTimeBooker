// budget.bicep — Azure Cost Management budget alert.
//
// IMPORTANT SCOPE NOTE: Cost Management budgets are subscription-scoped,
// not resource-group-scoped. This file uses targetScope = 'subscription'.
// It must be deployed with a separate az deployment sub create command,
// NOT as a nested module inside the RG-scoped main.bicep.
//
// Deploy command (from azure-iac.yml, after the RG deployment):
//   az deployment sub create \
//     --location eastus2 \
//     --template-file infra/bicep/modules/budget.bicep \
//     --parameters envName=dev budgetAmountUsd=10 budgetAlertEmail=<email>
//
// The budget alert fires at 80% of the monthly amount (i.e., ~$8 for a $10
// budget). This is a notification only — it does not stop or throttle Azure
// resource usage.
//
// The budget is filtered to the specific resource group rg-teetime-${envName}
// via filter.dimensions so it does not alert on unrelated subscription costs.
//
// See: infra/AZURE_PLAN.md §9.2 (budget alert), §4 (parameter strategy)

targetScope = 'subscription'

// ---------------------------------------------------------------------------
// Parameters
// ---------------------------------------------------------------------------

@description('Environment name. Used to name the budget uniquely per env.')
param envName string

@description('Monthly budget ceiling in USD. Alert fires at 80%.')
@minValue(1)
@maxValue(100)
param budgetAmountUsd int = 10

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

var budgetName = 'budget-teetime-${envName}'

// Alert threshold as a percentage of the monthly budget amount.
// 80% means the alert fires at ~$8 for a $10 budget.
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
          'rg-teetime-${envName}'
        ]
      }
    }

    // Notifications map: key is an arbitrary identifier for the notification rule.
    // 'actual_GreaterThan_${alertThresholdPercent}Pct' is a descriptive name that
    // encodes the trigger condition for human readability in the Azure portal.
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
    }
  }
}

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------

@description('Budget resource name.')
output budgetName string = budget.name
