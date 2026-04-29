// logs.bicep — Log Analytics Workspace + Application Insights.
// The Log Analytics workspace is linked to the Container Apps Environment
// in compute.bicep, so all job stdout/stderr logs flow to Log Analytics.
// Application Insights is available for structured metrics (v1 extension point).
//
// Retention: 30 days (minimal for cost; first 5 GB/month free).
// Bot uses structlog → JSON → stderr; captured by ACA and forwarded here.
//
// See: infra/AZURE_PLAN.md §10 (observability, from PLAN.md), §9.1 (cost),
//      §11 (security)

targetScope = 'resourceGroup'

// ---------------------------------------------------------------------------
// Parameters
// ---------------------------------------------------------------------------

@description('Environment name suffix.')
param envName string

@description('Azure region.')
param location string

@description('Log retention in days. 30 is the minimum; first 5 GB/month is free.')
@minValue(30)
@maxValue(730)
param retentionDays int = 30

// ---------------------------------------------------------------------------
// Variables
// ---------------------------------------------------------------------------

var workspaceName = 'law-teetime-${envName}'
var appInsightsName = 'ai-teetime-${envName}'

// ---------------------------------------------------------------------------
// Resources
// ---------------------------------------------------------------------------

// TODO(M-azure-T5): implement Log Analytics Workspace resource.
// Resource type: Microsoft.OperationalInsights/workspaces
// Key properties:
//   sku.name: 'PerGB2018'   (pay-per-GB; no commitment tier needed at this volume)
//   retentionInDays: retentionDays
//   publicNetworkAccessForIngestion: 'Enabled'
//   publicNetworkAccessForQuery: 'Enabled'
// Reference: https://learn.microsoft.com/en-us/azure/templates/microsoft.operationalinsights/workspaces

// TODO(M-azure-T5): implement Application Insights resource (linked to workspace).
// Resource type: Microsoft.Insights/components
// Key properties:
//   kind: 'other'           (not web; this is a background job)
//   applicationType: 'other'
//   workspaceResourceId: workspace.id
//   publicNetworkAccessForIngestion: 'Enabled'
//   publicNetworkAccessForQuery: 'Enabled'
// Reference: https://learn.microsoft.com/en-us/azure/templates/microsoft.insights/components
// NOTE: App Insights is an extension point for v1 structured metrics
// (PLAN.md §10 "Metric surface v1"). It is provisioned but not wired into
// the bot code in v1.0 — wire APPLICATIONINSIGHTS_CONNECTION_STRING into the
// job env vars when the bot adds SDK-level telemetry.

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------

@description('Log Analytics Workspace resource ID. Required by compute.bicep for ACA environment diagnostics config.')
output workspaceId string = 'TODO(M-azure-T5): workspace.id'

@description('Log Analytics Workspace primary shared key. Required by ACA environment to send logs.')
// NOTE: the workspace key is a sensitive value. In Bicep, use
// listKeys() to retrieve it and pass as a secureString. Do NOT output it
// as a plain string — Bicep will ERROR (not warn) on insecure outputs
// from listKeys() in recent Bicep versions (linter rule: secure-secrets-in-params).
// Mark output as @secure() as done here.
// See: https://learn.microsoft.com/en-us/azure/azure-resource-manager/bicep/outputs#secure-outputs
@secure()
output workspaceKey string = 'TODO(M-azure-T5): listKeys(workspace.id, workspace.apiVersion).primarySharedKey'

@description('Application Insights connection string. For future bot SDK telemetry integration.')
@secure()
output appInsightsConnectionString string = 'TODO(M-azure-T5): appInsights.properties.ConnectionString'
