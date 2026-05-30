# CLAUDE.md — Azure infra (v1)

Scoped notes for working under `infra/`. This file loads when you touch the
Azure infrastructure. The root `CLAUDE.md` has the repo-wide rules; the
authoritative Azure design is [`AZURE_PLAN.md`](./AZURE_PLAN.md) — read it before
changing anything here.

The v0 files (`src/`, `tests/`, `.github/workflows/book.yml`) are v0 territory —
do not modify them as part of Azure infra work.

## Bicep location

**All modules are implemented (M-azure-T1 through M-azure-T7 DONE).**

```
infra/
  AZURE_PLAN.md              # authoritative Azure design doc
  bicep/
    main.bicep               # entry point (RG-scoped); dryRun param defaults true
    main.bicepparam.dev      # dev parameter values (dryRun=true, enablePurgeProtection=false)
    main.bicepparam.prod     # prod parameter values (dryRun=false, enablePurgeProtection=true)
    modules/
      identity.bicep         # user-assigned MI for all ACA Jobs
      registry.bicep         # ACR Basic; AcrPull RBAC to job MI
      storage.bicep          # Blob Storage + teetime-state container; soft-delete 7d
      keyvault.bicep         # Key Vault Standard; Secrets User RBAC to job MI
                             #   dev: enablePurgeProtection=false
                             #   prod: enablePurgeProtection=true
      logs.bicep             # Log Analytics Workspace + App Insights
      compute.bicep          # ACA Environment + 2× booking ACA Jobs (DST crons)
                             #   + 1× watch ACA Job (*/10 * * * *)
                             #   all jobs: --dry-run passed via dryRun param
      budget.bicep           # Cost Management budget (subscription-scoped)
```

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
repo identity, Dockerfile). Remaining items: ACR/KV/storage name uniqueness
confirmation (§12 Q7–9), budget alert email (§12 Q4), and ForeUP IP allowlist
risk acceptance (§12 Q11). SMTP secrets are deferred (§12 Q10).
