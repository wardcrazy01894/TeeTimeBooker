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

@description('Start date for the budget period. Format: YYYY-MM-01T00:00:00Z (first of a month). Must be updated before deployment if deploying after 2026-05-01.')
// WARNING: '2026-05-01T00:00:00Z' is a hardcoded date. If deployed after this
// date the budget will silently start mid-period and alert at the wrong threshold
// until the next month boundary. Before deploying, update this value to the first
// day of the current deployment month (e.g. '2026-06-01T00:00:00Z').
// TODO(M-azure-T7): the CI workflow should compute this dynamically:
//   budgetStartDate=$(date -u +"%Y-%m-01T00:00:00Z")
// and pass it via --parameters budgetStartDate=$budgetStartDate.
// Alternatively, use utcNow('yyyy-MM-01') in Bicep — note utcNow() is only
// valid as a parameter default value (not in a resource body):
//   param budgetStartDate string = utcNow('yyyy-MM-01T00:00:00Z')
// The utcNow approach is cleaner and requires no CI-side date computation.
// The implementor should pick one and document the choice in M-azure-T7.
param budgetStartDate string = '2026-05-01T00:00:00Z'

// ---------------------------------------------------------------------------
// Variables
// ---------------------------------------------------------------------------

var budgetName = 'budget-teetime-${envName}'
var alertThresholdPercent = 80  // fire at 80% of budgetAmountUsd

// ---------------------------------------------------------------------------
// Resources
// ---------------------------------------------------------------------------

// TODO(M-azure-T7): implement Cost Management budget resource.
// Resource type: Microsoft.Consumption/budgets
// Key properties:
//   amount: budgetAmountUsd
//   timeGrain: 'Monthly'
//   timePeriod.startDate: budgetStartDate
//   category: 'Cost'
//   filter.dimensions: filter to subscription (no RG filter needed; budget is sub-scoped)
//     NOTE: to scope the budget to a specific resource group, add:
//       filter.dimensions.name: 'ResourceGroupName'
//       filter.dimensions.operator: 'In'
//       filter.dimensions.values: ['rg-teetime-${envName}']
//     This is RECOMMENDED to avoid the budget alerting on unrelated subscription costs.
//   notifications.actual_GreaterThan_alertThresholdPercent:
//     enabled: true
//     operator: 'GreaterThan'
//     threshold: alertThresholdPercent
//     contactEmails: [budgetAlertEmail]
//     thresholdType: 'Actual'
// Reference: https://learn.microsoft.com/en-us/azure/templates/microsoft.consumption/budgets
// See: AZURE_PLAN.md §9.2

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------

@description('Budget resource name.')
output budgetName string = budgetName
