# CLAUDE.md — Azure infra (v1)

Scoped notes for working under `infra/`. This file loads when you touch the
Azure infrastructure. The root `CLAUDE.md` has the repo-wide rules; the
authoritative Azure design is [`AZURE_PLAN.md`](./AZURE_PLAN.md) — read it before
changing anything here.

The v0 files (`src/`, `tests/`) are v0 territory —
do not modify them as part of Azure infra work. The former v0 booking and watch
workflows (`book.yml`, `watch-tee-time.yml`) have been removed; their schedules
now run as ACA Jobs defined in `compute.bicep`.

## Bicep location

**All modules are implemented (M-azure-T1 through M-azure-T7 DONE; storage module removed — state is in-process only). Cost killswitch (PR-KS1) implemented.**

```
infra/
  AZURE_PLAN.md              # authoritative Azure design doc
  COST_KILLSWITCH_PLAN.md    # verified design for the $50 automated killswitch chain
  bicep/
    main.bicep               # entry point (RG-scoped); dryRun param defaults true
    main.bicepparam.dev      # dev parameter values (dryRun=true, enablePurgeProtection=false)
    main.bicepparam.prod     # prod parameter values (dryRun=false, enablePurgeProtection=true)
    modules/
      identity.bicep         # user-assigned MI for all ACA Jobs
      registry.bicep         # ACR Basic; AcrPull RBAC to job MI
      keyvault.bicep         # Key Vault Standard; Secrets User RBAC to job MI
                             #   dev: enablePurgeProtection=false
                             #   prod: enablePurgeProtection=true
      logs.bicep             # Log Analytics Workspace + App Insights
      compute.bicep          # ACA Environment + 2× booking ACA Jobs (DST crons)
                             #   + 1× watch ACA Job (*/10 * * * *)
                             #   all jobs: --dry-run passed via dryRun param
      budget.bicep           # Cost Management budget (subscription-scoped)
      killswitch.bicep       # Cost killswitch: Logic App (Consumption) + Action Group + RBAC
                             #   DEPLOYED TO rg-teetime-dev ONLY (envName=='dev' gate in main.bicep)
                             #   manages BOTH envs via 12 HTTP actions: 6 PATCH + 6 POST /stop
                             #   cross-RG RBAC for rg-teetime-prod via nested module below
                             #   requires operator to pre-create "ACA Job Schedule Manager" custom role
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

What agents MUST NOT run without explicit user instruction:
- `az deployment group create` / `az deployment sub create`
- `az containerapp job start` (triggers live job execution)
- `az keyvault secret set` / `az keyvault secret delete`
- `az group delete`
- Any `az` command that modifies, creates, or deletes Azure resources

## Pointer to open questions

See AZURE_PLAN.md §12. Most questions are now resolved (tenant ID, subscription ID,
repo identity, Dockerfile, and Q11 — ForeUP does NOT block Azure IPs, observed in dev+prod).
The budget alert email (§12 Q4) is set at deploy time (`budgetAlertEmail` param). No blocking
open questions remain for v1.
