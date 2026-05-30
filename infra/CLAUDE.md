# CLAUDE.md — Azure infra (v1)

Scoped notes for working under `infra/`. This file loads when you touch the
Azure infrastructure. The root `CLAUDE.md` has the repo-wide rules; the
authoritative Azure design is [`AZURE_PLAN.md`](./AZURE_PLAN.md) — read it before
changing anything here.

The v0 files (`src/`, `tests/`, `.github/workflows/book.yml`) are v0 territory —
do not modify them as part of Azure infra work.

## Bicep location

```
infra/
  AZURE_PLAN.md              # authoritative Azure design doc
  bicep/
    main.bicep               # entry point (RG-scoped)
    main.bicepparam.dev      # dev parameter values
    main.bicepparam.prod     # prod parameter values
    modules/
      identity.bicep         # optional user-assigned MI stub
      registry.bicep         # ACR Basic
      storage.bicep          # Blob Storage + teetime-state container
      keyvault.bicep         # Key Vault Standard
      logs.bicep             # Log Analytics + App Insights
      compute.bicep          # ACA Environment + 2x ACA Jobs (DST crons)
      budget.bicep           # Cost Management budget (subscription-scoped)
  ci/
    azure-iac.yml            # IaC validation + deploy workflow
```

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

Before first deploy, answer the 10 questions in AZURE_PLAN.md §12. Key
blockers: Azure AD tenant ID, subscription ID, ACR/KV/storage naming
uniqueness, and whether a Dockerfile exists (AZURE_PLAN.md §12 Q6).
