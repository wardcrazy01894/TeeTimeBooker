// identity.bicep — user-assigned managed identity (THE default path).
//
// A single user-assigned MI is created first in the dependency chain. Its
// principalId is known before compute.bicep runs, which breaks the
// chicken-and-egg cycle: keyvault/registry/storage can all receive the
// principalId for RBAC assignments without waiting for the ACA job to exist.
// The ACA job then references this identity by resource ID and client ID.
//
// There is NO system-assigned MI path in this plan. System-assigned MI was
// rejected because its principalId is unavailable until after the job resource
// is created, making it impossible to pre-stage RBAC assignments for KV,
// ACR, and Storage in the same Bicep deployment without a two-pass workaround.
//
// See: infra/AZURE_PLAN.md §7.2

targetScope = 'resourceGroup'

// ---------------------------------------------------------------------------
// Parameters
// ---------------------------------------------------------------------------

@description('Environment name suffix.')
param envName string

@description('Azure region.')
param location string

// ---------------------------------------------------------------------------
// Resources
// ---------------------------------------------------------------------------

// TODO(M-azure-T1): implement user-assigned managed identity resource.
// Resource type: Microsoft.ManagedIdentity/userAssignedIdentities
// Name convention: mi-teetime-{envName}
// See: infra/AZURE_PLAN.md §7.2
// Reference: https://learn.microsoft.com/en-us/azure/templates/microsoft.managedidentity/userassignedidentities

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------

@description('Resource ID of the user-assigned managed identity.')
output identityResourceId string = 'TODO(M-azure-T1)'

@description('Principal ID of the user-assigned managed identity (for role assignments).')
output principalId string = 'TODO(M-azure-T1)'

@description('Client ID of the user-assigned managed identity.')
output clientId string = 'TODO(M-azure-T1)'
