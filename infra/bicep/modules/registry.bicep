// registry.bicep — Azure Container Registry (ACR) Basic SKU.
// Hosts the bot container image. The Container Apps Job pulls from this
// registry using a system-assigned managed identity with the AcrPull role.
// ACR admin account is disabled; no password stored anywhere.
//
// Cost: ~$5.00/mo flat for Basic SKU (East US 2, April 2026).
// See: infra/AZURE_PLAN.md §2 (service selection), §7.2 (AcrPull role),
//      §9.1 (cost estimate)

targetScope = 'resourceGroup'

// ---------------------------------------------------------------------------
// Parameters
// ---------------------------------------------------------------------------

@description('Environment name suffix.')
param envName string

@description('Azure region.')
param location string

@description('ACR SKU tier.')
@allowed(['Basic', 'Standard', 'Premium'])
param acrSku string = 'Basic'

@description('Principal ID of the user-assigned managed identity (from identity.bicep) for AcrPull role assignment.')
// Wired from identity.outputs.principalId in main.bicep. Because identity.bicep
// deploys before this module, the principalId is known at registry deploy time.
// See: infra/AZURE_PLAN.md §7.2
param jobPrincipalId string

@description('Create the scheduled image-purge ACR task (keeps unbounded SHA-tag growth in check).')
param enablePurgeTask bool = true

@description('Cron schedule (UTC) for the image-purge task. Default: 04:00 UTC every Sunday.')
param purgeSchedule string = '0 4 * * Sun'

@description('Number of most-recent matching tags to KEEP per repository when purging.')
@minValue(1)
param purgeKeepCount int = 10

// ---------------------------------------------------------------------------
// Variables
// ---------------------------------------------------------------------------

// ACR name is globally unique, 5–50 chars, alphanumeric.
// Pattern: teetime{envName}{uniqueSuffix} where uniqueSuffix is derived from
// resourceGroup().id via uniqueString() for a deterministic suffix. See AZURE_PLAN.md §12 Q7.
var acrName = 'teetime${envName}${uniqueString(resourceGroup().id)}'

// AcrPull built-in role GUID — stable across all Azure environments.
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'

// ---------------------------------------------------------------------------
// Resources
// ---------------------------------------------------------------------------

// acr purge command: keep the `purgeKeepCount` most-recent tags PER REPOSITORY, delete
// older ones plus any untagged (dangling) manifests. `--ago 0d` = count-based purge (no
// age floor). Single quotes around each filter are escaped (\') for the Bicep string; the
// whole thing is base64-wrapped as an EncodedTask.
//
// SHARED-ACR ISOLATION (critical): on the shared registry, prod images live in the
// `teetime` repo and dev images in a SEPARATE `teetime-dev` repo (dev auto-deploys many
// times/day; prod's tag is static between rare infra/v* releases). `--keep` is applied
// PER REPOSITORY, and — decisively — prod's `teetime` repo is written ONLY by prod, so dev's
// high-frequency pushes can NEVER evict prod's pinned image (the 06:00 booking pull). Both
// repos are listed so dev's repo is also pruned and never grows unbounded. A bare per-env ACR
// (dev-owned, not the shared one) only ever holds `teetime:.*`; the extra filter is a harmless
// no-op there.
var purgeCmd = 'acr purge --filter \'teetime:.*\' --filter \'teetime-dev:.*\' --keep ${purgeKeepCount} --ago 0d --untagged'
var purgeTaskYaml = 'version: v1.1.0\nsteps:\n  - cmd: ${purgeCmd}\n    disableWorkingDirectoryOverride: true\n    timeout: 3600\n'

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: acrName
  location: location
  tags: {
    environment: envName
    managedBy: 'bicep'
  }
  sku: {
    name: acrSku
  }
  properties: {
    adminUserEnabled: false          // use managed identity (AcrPull), not admin creds
    publicNetworkAccess: 'Enabled'   // ACA pulls over public endpoint; no VNet in v1
    zoneRedundancy: 'Disabled'       // Basic SKU does not support zone redundancy
  }
}

// AcrPull role assignment for the Container Apps Job user-assigned MI.
// Conditional: only created when jobPrincipalId is provided, so registry.bicep
// can be deployed standalone (e.g. before identity.bicep runs) without error.
// The name is a deterministic GUID so re-deploys are idempotent.
// See: infra/AZURE_PLAN.md §7.2
resource acrPullAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(jobPrincipalId)) {
  // guid() args: resource the role is scoped to + principal receiving the role + role being assigned.
  name: guid(acr.id, jobPrincipalId, acrPullRoleId)
  scope: acr
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
    principalId: jobPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// Scheduled image-purge task. Every merge to main pushes a new teetime:<sha>
// image; nothing ever deletes old tags, so ACR Basic's 10 GiB allowance would
// creep toward overage over time. This timer-triggered ACR task runs `acr purge`
// weekly, keeping the most-recent `purgeKeepCount` tags. The task runs in the
// registry's own context and needs no extra identity/RBAC for the delete.
// See: infra/AZURE_PLAN.md §9.1 (ACR cost).
resource purgeTask 'Microsoft.ContainerRegistry/registries/tasks@2019-06-01-preview' = if (enablePurgeTask) {
  parent: acr
  name: 'purge-old-images'
  location: location
  properties: {
    status: 'Enabled'
    platform: {
      os: 'Linux'
      architecture: 'amd64'
    }
    agentConfiguration: {
      cpu: 2
    }
    timeout: 3600
    step: {
      type: 'EncodedTask'
      encodedTaskContent: base64(purgeTaskYaml)
    }
    trigger: {
      timerTriggers: [
        {
          name: 'weekly'
          schedule: purgeSchedule
          status: 'Enabled'
        }
      ]
    }
  }
}

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------

@description('ACR login server (e.g. teetimedev<suffix>.azurecr.io). Used in containerImage parameter.')
output loginServer string = acr.properties.loginServer

@description('ACR resource name.')
output acrName string = acrName
