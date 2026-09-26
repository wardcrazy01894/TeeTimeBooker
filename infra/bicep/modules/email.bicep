// email.bicep — Azure Communication Services Email: Email Service + Azure-managed domain +
// Communication Service, wired to `tenant/acs_email.py`'s `AcsEmailClient` (MULTIUSER_PLAN
// §10.1, MU-11 — code-complete, UNWIRED until this module deploys and ACS_EMAIL_CONNECTION is
// populated). `listKeys()` on the Communication Service writes the connection string straight
// into the Key Vault secret `ACS-EMAIL-CONNECTION` at deploy time — no operator step, no
// connection string in bicepparam files or CI logs.
//
// Gating (MU-15a): deployed by main.bicep ONLY when `deployAcsEmail = true` (default false in
// BOTH envs). Nothing in this module requires a pre-existing secret (it CREATES its own KV
// secret via listKeys()), so it would not itself break a toml-mode auto-deploy — it is gated
// purely per the operator's "don't stand up resources nothing uses yet" instruction (MU-15a
// brief) and because `Microsoft.Communication` must be registered as a resource provider first
// (operator step, subscription-scoped — the CI service principal is RG-scoped and cannot
// self-register a provider; see infra/CLAUDE.md "Register RP before first deploy").
//
// Azure-managed email domain (`AzureManagedDomain`): free, auto-verified, sender address is
// `DoNotReply@<generated-subdomain>.azurecomm.net` — no custom-domain DNS records to manage.
// `acsEmailSender` (compute.bicep param) must be set to that exact address once known (the
// domain's `mailFromSenderDomain` output, below); it is a plain (non-secret) value.
//
// See: MULTIUSER_PLAN.md §10.1/§13 Q3, infra/AZURE_PLAN.md §7.1 (new KV secrets).

targetScope = 'resourceGroup'

// ---------------------------------------------------------------------------
// Parameters
// ---------------------------------------------------------------------------

@description('Environment name suffix.')
param envName string

@description('Key Vault name (not URI — Microsoft.KeyVault/vaults/secrets is a child resource reference, which needs the vault NAME to construct a `resource` symbolic reference in this scope).')
param keyVaultName string

// ---------------------------------------------------------------------------
// Variables
// ---------------------------------------------------------------------------

var emailServiceName = 'acs-email-teetime-${envName}'
var communicationServiceName = 'acs-teetime-${envName}'
var domainName = 'AzureManagedDomain'

var tags = {
  application: 'teetime'
  environment: envName
  managedBy: 'bicep'
  component: 'acs-email'
}

// ---------------------------------------------------------------------------
// Resources
// ---------------------------------------------------------------------------

// Email Communication Service — the Email-specific resource that owns the domain.
// dataLocation 'United States' matches the East US 2 deployment region family.
resource emailService 'Microsoft.Communication/emailServices@2023-04-01' = {
  name: emailServiceName
  location: 'global'
  tags: tags
  properties: {
    dataLocation: 'United States'
  }
}

// Azure-managed domain: free, pre-verified (no DNS TXT/CNAME records to add), sender addresses
// are <local-part>@<generated-subdomain>.azurecomm.net. userEngagementTracking is off (no link/
// open tracking pixels — the emails carry no marketing content, only booking notifications).
resource domain 'Microsoft.Communication/emailServices/domains@2023-04-01' = {
  parent: emailService
  name: domainName
  location: 'global'
  tags: tags
  properties: {
    domainManagement: 'AzureManaged'
    userEngagementTracking: 'Disabled'
  }
}

// Communication Service — the resource `AcsEmailClient` authenticates against. Linked to the
// email domain so its connection string can send from that domain's addresses.
resource communicationService 'Microsoft.Communication/communicationServices@2023-04-01' = {
  name: communicationServiceName
  location: 'global'
  tags: tags
  properties: {
    dataLocation: 'United States'
    linkedDomains: [
      domain.id
    ]
  }
}

// Key Vault secret: the Communication Service connection string, written by the DEPLOY (not the
// operator) via listKeys(). ACA resolves it at container start via the job's managed identity
// exactly like every other KV-backed secret (compute.bicep). Re-running this deploy rotates the
// value only if the underlying key is regenerated — listKeys() always returns the CURRENT
// primary key, so a stable deploy is idempotent.
//
// PREREQUISITE (operator, one-time, before deployAcsEmail=true): the CI deploy identity must
// hold a DATA-PLANE role on the vault to WRITE a secret value — "Key Vault Secrets Officer"
// (b86a8fe4-44ce-4948-aee5-eccb2c155cd7), scoped to this vault. Control-plane rights (even RG
// Owner) do NOT grant data-plane secret write under RBAC-authorization Key Vaults (keyvault.bicep
// sets enableRbacAuthorization=true) — this is intentional Azure isolation, not a bug. Grant with
// (read-only az commands otherwise; this ONE write is an explicit operator action, not agent-run):
//   az role assignment create --role "Key Vault Secrets Officer" \
//     --assignee <ci-service-principal-object-id> --scope <keyVault resource id>
resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

resource acsConnectionSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'ACS-EMAIL-CONNECTION'
  properties: {
    value: communicationService.listKeys().primaryConnectionString
  }
}

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------

@description('The Azure-managed domain\'s verified sender subdomain (e.g. <hash>.azurecomm.net). The operator sets compute.bicep\'s acsEmailSender param to "DoNotReply@<this value>" once known — see the module header.')
output mailFromSenderDomain string = domain.properties.mailFromSenderDomain

@description('Communication Service resource name.')
output communicationServiceName string = communicationServiceName
