// email.bicep — Azure Communication Services Email: Email Service + Azure-managed domain +
// Communication Service, wired to `tenant/acs_email.py`'s `AcsEmailClient` (MULTIUSER_PLAN
// §10.1, MU-11). `listKeys()` on the Communication Service writes the connection string straight
// into the Key Vault secret `ACS-EMAIL-CONNECTION` at deploy time — no operator step, no
// connection string in bicepparam files or CI logs.
//
// Gating (MU-15a): deployed by main.bicep ONLY when `deployAcsEmail = true` (both envs' param
// files set it). A NEW subscription needs `Microsoft.Communication` registered first (operator
// step, subscription-scoped: the CI service principal is RG-scoped and cannot self-register a
// provider; see infra/CLAUDE.md "Register RP before first deploy").
//
// Azure-managed email domain (`AzureManagedDomain`): free, auto-verified, sender
// `DoNotReply@<generated-subdomain>.azurecomm.net`. Prod also has a customer-managed domain
// (`customDomain`, two stages, DNS in Cloudflare) and sends as hello@<customDomain> once linked.
// main.bicep derives the sender from these outputs unless `acsEmailSender` overrides it.
//
// See: MULTIUSER_PLAN.md §10.1/§13 Q3, infra/AZURE_PLAN.md §7.1 (new KV secrets).

targetScope = 'resourceGroup'

// ---------------------------------------------------------------------------
// Parameters
// ---------------------------------------------------------------------------

@description('Environment name suffix.')
param envName string

@description('Customer-managed sender domain (prod: spicyteetimebooker.com; empty = none). Stage 1 (operator request 2026-09-29): the domain is created and its DNS records (the customDomainVerificationRecords output) go into Cloudflare and are verified. Azure refuses to link an unverified domain, so linking waits for customDomainLinked.')
param customDomain string = ''

@description('Stage 2, only AFTER every customDomain DNS record is verified: link the domain to the Communication Service and add the hello@ sender (main.bicep then sends as hello@<customDomain>).')
param customDomainLinked bool = false

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

// Customer-managed domain (operator request 2026-09-29): mail from hello@spicyteetimebooker.com
// instead of the azurecomm.net address. Its DNS records (verification TXT, SPF, 2x DKIM CNAME)
// are the customDomainVerificationRecords output; the operator adds them in Cloudflare (DNS only)
// and verification is initiated per record type (AZURE_PLAN runbook). No charge beyond the
// per-message price the managed domain already pays.
resource customDomainResource 'Microsoft.Communication/emailServices/domains@2023-04-01' = if (!empty(customDomain)) {
  parent: emailService
  name: empty(customDomain) ? 'unused' : customDomain
  location: 'global'
  tags: tags
  properties: {
    domainManagement: 'CustomerManaged'
    userEngagementTracking: 'Disabled'
  }
}

// The sender people see ("Spicy's Tee Time Booker" <hello@...>). Replies go to hello@, which
// Cloudflare Email Routing forwards to the operator. Created only once the domain is linked
// (i.e. verified), like the link itself.
resource helloSender 'Microsoft.Communication/emailServices/domains/senderUsernames@2023-04-01' = if (customDomainLinked && !empty(customDomain)) {
  parent: customDomainResource
  name: 'hello'
  properties: {
    username: 'hello'
    displayName: 'Spicy\'s Tee Time Booker'
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
    linkedDomains: concat(
      [domain.id],
      customDomainLinked && !empty(customDomain) ? [customDomainResource.id] : []
    )
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

@description('The Azure-managed domain\'s verified sender subdomain (e.g. <hash>.azurecomm.net). main.bicep derives the DoNotReply@ sender from it when no customer domain is linked.')
output mailFromSenderDomain string = domain.properties.mailFromSenderDomain

@description('The customer-managed domain\'s DNS records to add in Cloudflare (Domain / SPF / DKIM / DKIM2, each {type, name, value, ttl}); empty when customDomain is empty.')
output customDomainVerificationRecords object = !empty(customDomain) ? customDomainResource!.properties.verificationRecords : {}

@description('The address the site sends from: hello@<customDomain> once it is linked (stage 2), else DoNotReply@<the Azure-managed domain>. main.bicep hands it to compute + webapp, which also orders them after the ACS-EMAIL-CONNECTION secret write.')
// Stage 2 reads the username FROM the hello sender resource, so compute + webapp (which take
// this output) are ordered after it exists and never send as hello@ a moment too early.
output senderAddress string = customDomainLinked && !empty(customDomain) ? '${helloSender!.properties.username}@${customDomain}' : 'DoNotReply@${domain.properties.mailFromSenderDomain}'

@description('Communication Service resource name.')
output communicationServiceName string = communicationServiceName
