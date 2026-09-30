# CLAUDE.md — Azure infra (v1)

Scoped notes for working under `infra/`. This file loads when you touch the
Azure infrastructure. The root `CLAUDE.md` has the repo-wide rules; the
authoritative Azure design is [`AZURE_PLAN.md`](./AZURE_PLAN.md) — read it before
changing anything here.

The v0 files (`src/`, `tests/`) are v0 territory —
do not modify them as part of Azure infra work. The former v0 booking and watch
workflows (`book.yml`, `watch-tee-time.yml`) have been removed; their schedules
now run as ACA Jobs defined in `compute.bicep`.

<!-- toc -->
## Contents

- [Bicep location](#bicep-location)
- [Logging in for local Azure CLI work](#logging-in-for-local-azure-cli-work)
- [Agent rules for Azure deployments](#agent-rules-for-azure-deployments)
- [Pointer to open questions](#pointer-to-open-questions)

<!-- /toc -->

## Bicep location

All modules are implemented (M-azure-T1 to T7, the cost killswitch, and MU-15a/15b/16a/17 for the
multi-user site). The killswitch design record is
[`docs/plans/COST_KILLSWITCH_PLAN.md`](../docs/plans/COST_KILLSWITCH_PLAN.md).

| Param | Dev | Prod |
|-------|-----|------|
| `dryRun` | `true` | `false` |
| `bookingMode` / `watchMode` | `tenant` (MU-17) | `tenant` (MU-18 stage B) |
| `watchCron` | `0 * * * *` | `*/10 * * * *` |
| `tenantCosmosEndpoint` | shared Cosmos account | shared Cosmos account (MU-18 stage A) |
| `deployWebApp` / `deployAcsEmail` | `true` | `true` (MU-18 stage A) |

```
infra/
  AZURE_PLAN.md              # authoritative Azure design doc
  bicep/
    main.bicep               # entry point (RG-scoped); dryRun param defaults true
    main.bicepparam.dev      # dev parameter values (dryRun=true, enablePurgeProtection=false)
    main.bicepparam.prod     # prod parameter values (dryRun=false, enablePurgeProtection=true)
    release_events.json      # MU-15a: release-event table (MULTIUSER_PLAN §6.2) — single source
                             #   of truth for booking-job derivation, read via loadJsonContent by
                             #   BOTH compute.bicep and killswitch.bicep. v1 ships exactly one
                             #   event (mb0600et, Mangrove Bay); Sydney Marovitz is data-only
                             #   (hosted_booking=False, no ACA job). Pinned to
                             #   core/release_policy.py::cron_pair by test_release_events_parity.py.
    modules/
      identity.bicep         # user-assigned MI for all ACA Jobs
      registry.bicep         # ACR Basic; weekly `acr purge` task (keep last 10 tags PER repo: teetime + teetime-dev)
                             #   the SINGLE shared ACR — deployed standalone to rg-teetime-shared (envName=shared);
                             #   NOT created by either env's main.bicep. Both envs use one ACR (~$5/mo saved).
      acr-pull-cross-rg.bicep  # BOTH envs: cross-RG AcrPull on the shared ACR (in rg-teetime-shared) for the job MI
      keyvault.bicep         # Key Vault Standard; Secrets User RBAC to job MI
                             #   dev: enablePurgeProtection=false
                             #   prod: enablePurgeProtection=true
      logs.bicep             # Log Analytics Workspace + App Insights
      compute.bicep          # ACA Environment + booking ACA Jobs derived from ../release_events.json
                             #   (v1: 2 jobs, DST crons, legacy teetime-job-<env>-edt/-est names)
                             #   + 1× watch ACA Job (watchCron param: prod */10 * * * *, dev hourly)
                             #   all jobs: --dry-run passed via dryRun param
                             #   bookingMode/watchMode params (MU-15a; dev 'tenant' since MU-17,
                             #   prod 'tenant' since MU-18 stage B): select `run --config .../container.toml` vs `tenant-run --event
                             #   <key>`/`tenant-watch`; tenant-only secretRefs/env vars are added
                             #   ONLY inside a mode=='tenant' branch, so the default toml mode
                             #   never references a KV secret the operator has not created.
                             #   + (MU-16a, tenant mode only) the Manual teetime-migrate-<env>
                             #   job (`tenant-migrate`, no KV secret, NOT a killswitch target);
                             #   azure-iac.yml starts + awaits it right after deploy pass 2.
      webapp.bicep           # MU-15a: Container App teetime-web-<env> (`teetime web`),
                             #   minReplicas = webMinReplicas (prod 1 always warm, dev 0
                             #   scale-to-zero; latch forces 0), same ACA environment as the jobs. Gated on
                             #   deployWebApp (dev true since MU-17, prod true since MU-18 stage A). Ingress + max-replicas
                             #   latched to effectiveEnableSchedules (killswitch lever (c) target).
                             #   MU-16a: tenant backend env (Cosmos, AZURE_CLIENT_ID, keyring,
                             #   ACS) wired ONLY when tenantCosmosEndpoint is non-empty.
                             #   customDomain (prod spicyteetimebooker.com): apex + www bound
                             #   SNI to managed certs mc-<host-dashed>, created once by the
                             #   AZURE_PLAN §10.9 runbook BEFORE the first deploy that sets it.
                             #   githubIssuesRepo (prod only): GITHUB-ISSUES-TOKEN secretRef +
                             #   GITHUB_ISSUES_* env, site reports -> anonymized GitHub issues.
      email.bicep            # MU-15a: ACS Communication Service + Email Service +
                             #   Azure-managed domain (+ prod's customer-managed
                             #   spicyteetimebooker.com, 2026-09-29: emailCustomDomain creates it,
                             #   emailCustomDomainLinked links it + adds hello@ as the sender;
                             #   AZURE_PLAN §10.10); writes KV secret ACS-EMAIL-CONNECTION via
                             #   listKeys() at deploy time. Gated on deployAcsEmail (true in
                             #   both envs). Requires Microsoft.Communication RP registration +
                             #   "Key Vault Secrets Officer" for the CI deploy identity (operator,
                             #   one-time — see the module header).
      cosmos.bicep           # MU-15b: the shared free-tier Cosmos DB account for the
                             #   multi-user tenant store. DEPLOYED STANDALONE by the operator to
                             #   rg-teetime-shared (like the shared ACR), never by main.bicep/CI.
                             #   prod+dev databases at 400 RU/s, totalThroughputLimit 1000, no account
                             #   keys, index policy = the store's QUERIED_PATHS. Data-plane role
                             #   assignments are created BY HAND (AZURE_PLAN §7.2a); no module may
                             #   declare one.
      budget.bicep           # Cost Management budget (subscription-scoped)
      killswitch.bicep       # Cost killswitch: Logic App (Consumption) + Action Group + RBAC
                             #   DEPLOYED TO rg-teetime-dev ONLY (envName=='dev' gate in main.bicep)
                             #   manages BOTH envs via 14 HTTP actions (MU-15a): 6 PATCH + 6 job
                             #   POST /stop + 2 web-app POST /stop (lever c, teetime-web-<env>)
                             #   cross-RG RBAC for rg-teetime-prod via nested module below
                             #   requires operator to pre-create/update "ACA Job Schedule Manager"
                             #   custom role (MU-15a added containerApps/read + .../stop/action)
                             #   gate: enableKillswitch && !empty(killswitchRbacRoleId) && envName=='dev'
      killswitch-rbac-prod.bicep  # companion: Microsoft.Authorization/roleAssignments in rg-teetime-prod
                             #   deployed as nested module by killswitch.bicep
                             #   scope: resourceGroup(subscriptionId, prodRgName) → nested ARM deployment
```

**Killswitch deploy notes:**
- The killswitch Logic App lives ONLY in `rg-teetime-dev`. It calls ACA Job APIs in BOTH
  `rg-teetime-dev` and `rg-teetime-prod` via cross-RG RBAC. A prod deploy MUST NOT create a
  second Logic App — the `envName == 'dev'` gate in `main.bicep` prevents this.
- The `enableKillswitch = true` param is set in both param files but the `!empty(killswitchRbacRoleId)`
  guard means the deploy is a clean no-op until the operator creates the custom role and fills in
  the GUID. Safe to merge and auto-deploy without the role GUID in place.
- After creating the custom role (see AZURE_PLAN.md §9.2), fill the GUID into both param files
  and merge. The killswitch chain deploys automatically on the next dev auto-deploy.
- RBAC role assignments: Logic App system-assigned MI → "ACA Job Schedule Manager" custom role,
  assigned on BOTH `rg-teetime-dev` (inline resource in killswitch.bicep) and `rg-teetime-prod`
  (via `killswitch-rbac-prod.bicep` nested module).

The IaC validation + deploy workflow lives at `.github/workflows/azure-iac.yml`
(GitHub only runs workflows under `.github/workflows/`). It is the ACTIVE
workflow; there is no copy under `infra/ci/`.

**Dev deploy policy:** merges to `main` that touch `infra/**` or the workflow
file auto-deploy to dev with NO required-reviewer gate (intentional per operator
request). Prod deploys require a manual approval gate on the GitHub `prod`
environment and are triggered by `infra/v*` tag pushes.

**Inline parameters:** `azure-iac.yml` deploys with INLINE parameters, so a value set in a
`.bicepparam` file does nothing until the workflow parses and passes it too (a missed one kept
dev's watcher at `*/10`). `test_every_param_file_value_reaches_every_ci_deploy` enforces this.

**Public repo:** no email address may sit in a param file. The operator email comes from the
`OPERATOR-NOTIFY-EMAIL` Key Vault secret and the ACS sender is derived from the email module
output (`tests/test_webapp_bicep.py`).

Note: compiled ARM JSON (`infra/bicep/**/*.json`) is gitignored — CI deploys from
the `.bicep` sources directly (`az` compiles on the fly). Do not commit build output.

## Logging in for local Azure CLI work

```bash
az login                                      # browser-based login
az account set --subscription <SUBSCRIPTION_ID>
az account show                               # confirm correct subscription
```

For CI, authentication uses OIDC federated credentials (no client secret).
See AZURE_PLAN.md §8.2 for the one-time federated credential setup steps.

## Agent rules for Azure deployments

**CRITICAL: An agent MUST NOT run `az deployment group create` or
`az deployment sub create` without explicit user approval.** These commands
create or modify live Azure resources. This rule is also **mechanically enforced**
by `.claude/hooks/az-deploy-guard.sh` (a PreToolUse hook that hard-blocks the
destructive commands below), but do not rely on the hook — follow the rule.

What agents CAN run autonomously:
- `az bicep build` (lint only; no network calls)
- `az deployment group validate` (validates template; no resource changes)
- `az deployment group what-if` (read-only; shows planned changes)
- `az keyvault secret list` (read-only; lists secret names, not values)
- `az containerapp job list` / `az containerapp job show` (read-only)

What agents MUST NOT run without explicit user instruction (all hard-blocked by
`az-deploy-guard.sh`):
- `az deployment group` / `sub` / `mg` / `tenant` `create` / `delete`
- `az containerapp job start` / `stop` / `create` / `update` / `delete` (live execution / config change / removal — `stop` is the verb the killswitch itself uses)
- `az role assignment` / `role definition` `create` / `delete` (RBAC grant/revoke + custom-role escalation)
- `az keyvault secret set` / `delete` / `purge`; `az keyvault set-policy`
- `az keyvault update` / `network-rule add` / `network-rule remove` (network-ACL / config change — can lock the vault and DoS the jobs); `az keyvault purge` / `delete` (vault-level — destroys/permanently-removes the whole vault)
- `az group delete` / `az resource delete`
- Any `az` command that modifies, creates, or deletes Azure resources

## Pointer to open questions

See AZURE_PLAN.md §12. Most questions are now resolved (tenant ID, subscription ID,
repo identity, Dockerfile, and Q11 — ForeUP does NOT block Azure IPs, observed in dev+prod).
The budget alert email (§12 Q4) is set at deploy time (`budgetAlertEmail` param). No blocking
open questions remain for v1.
