// webapp.bicep — Container App `teetime-web-<env>` serving `teetime web` (MULTIUSER_PLAN §8,
// MU-12/13/14; the app itself is code-complete but UNWIRED to infra until this module deploys).
//
// Scale-to-zero Container App (minReplicas 0, maxReplicas 1) in the SAME Container Apps
// Environment the ACA Jobs use (compute.bicep's acaEnvironmentId output) — one environment per
// RG keeps this to a single Consumption plan, no extra environment cost.
//
// Gating (MU-15a): this module is deployed by main.bicep ONLY when `deployWebApp = true`
// (default false in BOTH envs' param files). Reason: the Container App's secretRefs point at
// Key Vault secrets (WEB-SESSION-SECRET, OAUTH-GOOGLE-CLIENT-ID, OAUTH-GOOGLE-CLIENT-SECRET)
// that the operator has NOT pre-created — the Google OAuth client does not exist yet. ACA
// validates KV secret refs at container-CREATE time, so deploying this module unconditionally
// would break the dev auto-deploy on the very next merge to main. Flip `deployWebApp = true`
// only after the operator has created those three secrets (see the PR body for the exact
// `az keyvault secret set` commands and the Google Cloud Console steps to obtain the OAuth
// client — never do this from an agent per the deploy-safety rules, infra/CLAUDE.md).
//
// Killswitch coupling (MULTIUSER_PLAN §10.1, SF8): `enableIngress` is wired from main.bicep as
// `effectiveEnableSchedules` (= enableSchedules && !killswitchFired), the SAME latch that gates
// the ACA Jobs' cron schedules. When false, this module still creates the Container App (so a
// redeploy is idempotent) but with ingress DISABLED and minReplicas forced to 0 — no traffic in,
// no replica running, so a CI redeploy after the killswitch fires cannot bring the site back up
// even if `deployWebApp` stays true. See test_webapp_ingress_disabled_when_killswitch_fired.
//
// Known limitation (accepted for MU-15a, tracked for the eventual cutover): `publicBaseUrl` is
// a PARAM, not derived from this resource's own FQDN — the Container App's default hostname
// (`<app>.<hash>.<region>.azurecontainerapps.io`) is only known AFTER first creation, a
// chicken-and-egg Bicep cannot resolve inline without a second deploy pass. The operator sets
// `webPublicBaseUrl` to `https://<observed-fqdn>` (or a custom domain) in a follow-up param
// change once `deployWebApp` is first flipped true — until then TEETIME_PUBLIC_BASE_URL is
// empty and the container fails closed at startup (WebConfigError), which is fine because the
// module is not deployed with traffic-serving intent in this PR.
//
// See: infra/AZURE_PLAN.md §3/§5/§7, MULTIUSER_PLAN.md §10.1/§12 MU-15a.

targetScope = 'resourceGroup'

// ---------------------------------------------------------------------------
// Parameters
// ---------------------------------------------------------------------------

@description('Environment name suffix.')
param envName string

@description('Azure region.')
param location string

@description('Full container image reference. Same image as the ACA Jobs — `teetime web` is a subcommand of the same entrypoint.')
param containerImage string

@description('Resource ID of the user-assigned managed identity (from identity.bicep).')
param userAssignedIdentityResourceId string

@description('Key Vault URI for secret references.')
param keyVaultUri string

@description('Resource ID of the Container Apps Environment (compute.bicep\'s acaEnvironmentId output). The web app shares the SAME environment as the ACA Jobs — one Consumption plan per RG.')
param acaEnvironmentId string

@description('True when containerImage is a PUBLIC bootstrap image (deploy pass 1). Mirrors compute.bicep\'s usePublicBootstrapImage — drops the registries[] auth block so ACA pulls anonymously on the very first deploy of a new environment.')
param usePublicBootstrapImage bool = false

@description('When true (the default toml-mode value from main.bicep, effectiveEnableSchedules), the Container App serves traffic (external ingress, minReplicas up to 1). When false — either enableSchedules=false or the cost killswitch has fired (killswitchFired=true) — ingress is disabled and minReplicas is forced to 0, so no traffic reaches the app and no replica can run. This is the SAME latch that silences the ACA Jobs\' cron schedules (MULTIUSER_PLAN §10.1 SF8).')
param enableIngress bool = true

@description('Public base URL the web app is reachable at, e.g. https://teetime-web-dev.<hash>.eastus2.azurecontainerapps.io. Empty by default — only known after the Container App\'s first deploy (a chicken-and-egg Bicep cannot resolve in one pass); the operator sets it in a follow-up param change. See the module header.')
param webPublicBaseUrl string = ''

@description('The operator\'s sign-in email (implicitly invited so the first operator can sign in to an empty site). Not a secret — an email address, plain value.')
param operatorEmail string = ''

@description('Permanent dry-run flag for the web app\'s cancel action (MULTIUSER_PLAN §7.8 SF2) — mirrors the ACA Jobs\' dryRun param.')
param dryRun bool = true

// ---------------------------------------------------------------------------
// Variables
// ---------------------------------------------------------------------------

var appName = 'teetime-web-${envName}'

var acrLoginServer = split(containerImage, '/')[0]

var registries = usePublicBootstrapImage ? [] : [
  { server: acrLoginServer, identity: userAssignedIdentityResourceId }
]

// KV secrets this Container App references. All three MUST be pre-created by the operator
// before `deployWebApp` is flipped true (see the module header + the PR body for the exact
// commands): WEB-SESSION-SECRET, OAUTH-GOOGLE-CLIENT-ID, OAUTH-GOOGLE-CLIENT-SECRET.
// GitHub OAuth is deliberately NOT wired (operator decision 2026-09-26: Google only).
var webSecrets = [
  { name: 'web-session-secret',        keyVaultUrl: '${keyVaultUri}secrets/WEB-SESSION-SECRET',        identity: userAssignedIdentityResourceId }
  { name: 'oauth-google-client-id',    keyVaultUrl: '${keyVaultUri}secrets/OAUTH-GOOGLE-CLIENT-ID',    identity: userAssignedIdentityResourceId }
  { name: 'oauth-google-client-secret', keyVaultUrl: '${keyVaultUri}secrets/OAUTH-GOOGLE-CLIENT-SECRET', identity: userAssignedIdentityResourceId }
]

// WEB_ENV_VARS (web/app.py) minus the GitHub pair (unset — Google only). TEETIME_PUBLIC_BASE_URL
// and TEETIME_OPERATOR_EMAIL are plain values (not secrets); an empty publicBaseUrl fails the
// container closed at startup (WebConfigError) rather than serving with a broken OAuth redirect.
var webEnv = [
  { name: 'TEETIME_PUBLIC_BASE_URL',       value: webPublicBaseUrl }
  { name: 'WEB_SESSION_SECRET',            secretRef: 'web-session-secret' }
  { name: 'OAUTH_GOOGLE_CLIENT_ID',        secretRef: 'oauth-google-client-id' }
  { name: 'OAUTH_GOOGLE_CLIENT_SECRET',    secretRef: 'oauth-google-client-secret' }
  { name: 'TEETIME_OPERATOR_EMAIL',        value: operatorEmail }
  { name: 'TEETIME_WEB_DRY_RUN',           value: dryRun ? 'true' : 'false' }
  { name: 'TEETIME_ENV',                   value: envName }
]

var tags = {
  application: 'teetime'
  environment: envName
  managedBy: 'bicep'
  component: 'web'
}

// ---------------------------------------------------------------------------
// Resources
// ---------------------------------------------------------------------------

resource webApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: appName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${userAssignedIdentityResourceId}': {}
    }
  }
  properties: {
    environmentId: acaEnvironmentId
    configuration: {
      // Killswitch latch (SF8): ingress OFF + minReplicas forced to 0 when enableIngress=false,
      // so a redeploy after the killswitch fires cannot bring the site back up.
      ingress: enableIngress ? {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
      } : null
      registries: registries
      secrets: webSecrets
    }
    template: {
      containers: [
        {
          image: containerImage
          name: 'teetime-web'
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
          command: ['teetime']
          args: ['web', '--port', '8000']
          env: webEnv
        }
      ]
      scale: {
        // Scale-to-zero always (minReplicas 0 — no idle cost). maxReplicas is the killswitch
        // latch: 0 when enableIngress=false makes it IMPOSSIBLE for any replica to start, not
        // merely undesirable — a stronger guarantee than ingress-disabled alone.
        minReplicas: 0
        maxReplicas: enableIngress ? 1 : 0
      }
    }
  }
}

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------

@description('Container App resource name, for az containerapp show / stop (killswitch lever c).')
output appName string = appName

@description('Container App default FQDN (only meaningful when ingress is enabled). The operator reads this after first deploy to fill in webPublicBaseUrl — see the module header.')
output fqdn string = webApp.properties.configuration.?ingress.?fqdn ?? ''
