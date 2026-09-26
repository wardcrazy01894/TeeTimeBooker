// MU-15b (MULTIUSER_PLAN §10.2/§10.5): the shared Cosmos DB for NoSQL account that backs the
// multi-user tenant store (`tenant/cosmos/store.py::CosmosTenantStore`).
//
// DEPLOYED STANDALONE into rg-teetime-shared by the OPERATOR, like the shared ACR — never by the
// per-env main.bicep that CI auto-deploys (pinned by tests/test_cosmos_bicep.py). One account, two
// databases (`prod`, `dev`), so both envs share the subscription's single free-tier slot.
//
// Cost/safety invariants (all pinned by tests/test_cosmos_bicep.py):
//   * enableFreeTier: true — 1000 RU/s + 25 GB free. It can ONLY be set at account creation.
//   * capacity.totalThroughputLimit: 1000 — the killswitch cannot stop Cosmos and provisioned
//     RU/s bill hourly, so a mis-edit above the free tier must be IMPOSSIBLE to provision.
//   * two databases at 400 RU/s shared throughput each (the shared-database minimum) = 800.
//   * disableLocalAuth: true — no account keys at all; only an Entra token holding a data-plane
//     role on a database is accepted. Those role assignments are created BY HAND by the operator
//     (§10.5); no Bicep module may declare a Cosmos data-plane role assignment.
//   * Strong consistency, single region.
//   * The index policy includes EXACTLY the paths the store filters on (`QUERIED_PATHS` in
//     tenant/cosmos/store.py) and excludes everything else: Cosmos rejects a filter on an
//     unindexed path, and fewer indexed paths make writes cheaper.

@description('Location of the account (single region).')
param location string = 'eastus2'

@description('Account name (globally unique DNS label).')
param accountName string = 'cosmos-teetime-shared'

// Every path the store's queries filter on, as Cosmos index paths. MUST equal
// `{p + "/?" for p in QUERIED_PATHS}` — tests/test_cosmos_bicep.py fails CI otherwise.
var indexedPaths = [
  '/accountId/?'
  '/active/?'
  '/courseId/?'
  '/groupId/?'
  '/oauthProvider/?'
  '/oauthSubject/?'
  '/rawReservationId/?'
  '/rowId/?'
  '/ruleId/?'
  '/source/?'
  '/status/?'
  '/targetDate/?'
  '/type/?'
  '/userId/?'
  '/usernameHash/?'
]

var includedPaths = [for p in indexedPaths: { path: p }]

var indexingPolicy = {
  indexingMode: 'consistent'
  automatic: true
  includedPaths: includedPaths
  excludedPaths: [
    { path: '/*' }
    { path: '/"_etag"/?' }
  ]
}

// Container shapes (§3.1). `tenant` never expires documents; `global` has TTL ON with no default
// (-1) so only the probe (2 h) and audit (400 d) docs, which carry their own `ttl`, expire.
var containers = [
  { name: 'tenant', partitionKey: '/accountId', defaultTtl: null }
  { name: 'global', partitionKey: '/pk', defaultTtl: -1 }
]

// §10.2 Testing: the dev-only CI twins of the two containers, with a 7-day container default TTL
// (tenant/cosmos/documents.py::CI_CONTAINER_DEFAULT_TTL_S) that sweeps anything a crashed
// integration run left behind. The store selects them only via TENANT_COSMOS_CONTAINER_SUFFIX.
var ciSuffix = '-ci'

resource account 'Microsoft.DocumentDB/databaseAccounts@2024-11-15' = {
  name: accountName
  location: location
  kind: 'GlobalDocumentDB'
  properties: {
    databaseAccountOfferType: 'Standard'
    enableFreeTier: true
    disableLocalAuth: true
    consistencyPolicy: {
      defaultConsistencyLevel: 'Strong'
    }
    capacity: {
      totalThroughputLimit: 1000
    }
    locations: [
      {
        locationName: location
        failoverPriority: 0
        isZoneRedundant: false
      }
    ]
    // ACA consumption has no static egress IP; with local auth off, only an Entra token with a
    // data-plane role on a database gets in. 0.0.0.0 = "accept connections from within public
    // Azure datacenters".
    publicNetworkAccess: 'Enabled'
    ipRules: [
      { ipAddressOrRange: '0.0.0.0' }
    ]
    minimalTlsVersion: 'Tls12'
  }
}

resource prodDb 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-11-15' = {
  parent: account
  name: 'prod'
  properties: {
    resource: { id: 'prod' }
    options: { throughput: 400 }
  }
}

resource devDb 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-11-15' = {
  parent: account
  name: 'dev'
  properties: {
    resource: { id: 'dev' }
    options: { throughput: 400 }
  }
}

resource prodContainers 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-11-15' = [for c in containers: {
  parent: prodDb
  name: c.name
  properties: {
    resource: union(
      {
        id: c.name
        partitionKey: { paths: [c.partitionKey], kind: 'Hash' }
        indexingPolicy: indexingPolicy
      },
      c.defaultTtl == null ? {} : { defaultTtl: c.defaultTtl }
    )
  }
}]

resource devContainers 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-11-15' = [for c in containers: {
  parent: devDb
  name: c.name
  properties: {
    resource: union(
      {
        id: c.name
        partitionKey: { paths: [c.partitionKey], kind: 'Hash' }
        indexingPolicy: indexingPolicy
      },
      c.defaultTtl == null ? {} : { defaultTtl: c.defaultTtl }
    )
  }
}]

resource devCiContainers 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-11-15' = [for c in containers: {
  parent: devDb
  name: '${c.name}${ciSuffix}'
  properties: {
    resource: {
      id: '${c.name}${ciSuffix}'
      partitionKey: { paths: [c.partitionKey], kind: 'Hash' }
      indexingPolicy: indexingPolicy
      defaultTtl: 604800
    }
  }
}]

@description('The account endpoint the tenant jobs and web read as TENANT_COSMOS_ENDPOINT.')
output endpoint string = account.properties.documentEndpoint

@description('The account resource id (scope for the operator\'s hand-made role assignments).')
output accountId string = account.id
