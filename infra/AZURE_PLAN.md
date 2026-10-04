# TeeTimeBooker — Azure Serverless Hosting Plan (v1)

> **Scope.** This document covers the Azure infrastructure that replaces the
> v0 GitHub Actions runner-hosted execution. (The v0 cron workflows `book.yml` /
> `watch-tee-time.yml` were removed in #43; scheduling now runs as ACA Jobs, and the
> M6 runtime wiring — `--wait`, `core/dst_gate.py`, `core/target_date.py` — landed in
> `src/`.) The bot binary is container-packaged and run as an Azure Container Apps Job on a
> cron schedule. All decisions listed in the task brief are treated as settled;
> this document addresses the "anticipate-the-reviewer" items explicitly.
>
> **Status:** living doc, implemented. Prod and dev both run the multi-user tenant jobs, web app,
> ACS email and the shared Cosmos account: prod since MU-18 stage B (`infra/v3.0.0`, `dryRun=false`),
> dev since MU-17 (dry-run). The single-user TOML jobs remain prod's rollback. Current prod tag and history: [../docs/RELEASES.md](../docs/RELEASES.md).
> The killswitch design record is [../docs/plans/COST_KILLSWITCH_PLAN.md](../docs/plans/COST_KILLSWITCH_PLAN.md).

---

<!-- toc -->
## Contents

- [1. Architecture overview](#1-architecture-overview)
- [2. Service selection](#2-service-selection)
  - [2.1 Shared ACR (one registry for both envs)](#21-shared-acr-one-registry-for-both-envs)
- [3. Module layout](#3-module-layout)
- [4. Parameter strategy](#4-parameter-strategy)
  - [Parameterized (vary by env or operator)](#parameterized-vary-by-env-or-operator)
  - [Hard-coded (architectural constants, not env-specific)](#hard-coded-architectural-constants-not-env-specific)
- [5. The 6:00 AM ET race on ACA (pre-emption items 1–3)](#5-the-600-am-et-race-on-aca-pre-emption-items-13)
  - [5.1 ACA Jobs scheduled trigger jitter](#51-aca-jobs-scheduled-trigger-jitter)
  - [5.2 Container cold-start](#52-container-cold-start)
  - [5.3 DST handling on ACA](#53-dst-handling-on-aca)
  - [5.4 Watch job ACA Job (M-feature-1)](#54-watch-job-aca-job-m-feature-1)
  - [5.5 Migrate job (MU-16a, tenant mode only)](#55-migrate-job-mu-16a-tenant-mode-only)
- [6. State persistence (pre-emption items 4 & 10)](#6-state-persistence-pre-emption-items-4--10)
- [7. Secrets & identity (pre-emption items 6, 7, 11)](#7-secrets--identity-pre-emption-items-6-7-11)
  - [7.1 Key Vault secret tree](#71-key-vault-secret-tree)
  - [7.2 Managed identity and RBAC](#72-managed-identity-and-rbac)
  - [7.2a Cosmos DB tenant store (MU-15b) — deploy + data-plane roles, by hand](#72a-cosmos-db-tenant-store-mu-15b--deploy--data-plane-roles-by-hand)
  - [7.3 Key Vault secret injection pattern](#73-key-vault-secret-injection-pattern)
  - [7.4 Secret rotation](#74-secret-rotation)
  - [7.5 Skip dates — no-redeploy "don't book this day" (LEADTIME_SKIP_PLAN F2)](#75-skip-dates--no-redeploy-dont-book-this-day-leadtime_skip_plan-f2)
- [8. CI validation pipeline (pre-emption item 9)](#8-ci-validation-pipeline-pre-emption-item-9)
  - [8.1 Trigger strategy](#81-trigger-strategy)
  - [8.2 OIDC federated credential setup (one-time, operator)](#82-oidc-federated-credential-setup-one-time-operator)
  - [8.3 what-if known issue](#83-what-if-known-issue)
- [9. Cost estimate (pre-emption item 12)](#9-cost-estimate-pre-emption-item-12)
  - [9.1 Per-component breakdown (East US 2, April 2026)](#91-per-component-breakdown-east-us-2-april-2026)
  - [9.2 Budget alert](#92-budget-alert)
- [10. Deploy & cutover runbook (pre-emption items 8 & 13)](#10-deploy--cutover-runbook-pre-emption-items-8--13)
  - [10.1 First-time setup (operator steps, run once)](#101-first-time-setup-operator-steps-run-once)
  - [10.1.1 Prod first-time bootstrap (run once, before the first `infra/v*` tag)](#1011-prod-first-time-bootstrap-run-once-before-the-first-infrav-tag)
  - [10.2 Ongoing deploy (CI-driven)](#102-ongoing-deploy-ci-driven)
  - [10.3 v0 → v1 cutover (DONE)](#103-v0--v1-cutover-done)
  - [10.4 M6 verification (dev, dry-run) — proving both jobs work before prod](#104-m6-verification-dev-dry-run--proving-both-jobs-work-before-prod)
  - [10.5 Prod cutover checklist (in order)](#105-prod-cutover-checklist-in-order)
  - [10.6 Shared-ACR cutover (one-time — dedicated rg-teetime-shared, see §2.1)](#106-shared-acr-cutover-one-time--dedicated-rg-teetime-shared-see-21)
  - [10.7 Multi-user DEV cutover (MU-17, MULTIUSER_PLAN §11/§12)](#107-multi-user-dev-cutover-mu-17-multiuser_plan-1112)
- [11. Security checklist](#11-security-checklist)
- [12. Open questions for the user](#12-open-questions-for-the-user)

<!-- /toc -->

## 1. Architecture overview

```
  GitHub Actions CI (azure-iac.yml)
  ┌───────────────────────────────────┐
  │ bicep build → deploy              │
  │ (OIDC federated credential)       │
  └─────────────────┬─────────────────┘
                    │ az deployment group create
                    │
  Azure Subscription / Resource Group (per env)
  ┌────────────────────────────────────────────────────────────────┐
  │                                                                │
  │  ┌─────────────────────┐    ┌──────────────────────────────┐  │
  │  │  Azure Container    │    │  Azure Container Apps        │  │
  │  │  Registry (Basic)   │    │  Environment (Consumption)   │  │
  │  │  teetime.azurecr.io │    │                              │  │
  │  └──────────┬──────────┘    │  ┌────────────────────────┐  │  │
  │             │ image pull    │  │  Container Apps Job     │  │  │
  │             └───────────────┼─►│  Booking Jobs (×2)      │  │  │
  │                             │  │  2 daily crons (EDT/EST) │  │  │
  │                             │  ├────────────────────────┤  │  │
  │                             │  │  Watch Job (×1)         │  │  │
  │                             │  │  cron: */10 * * * *     │  │  │
  │                             │  └────────┬───────────────-┘  │  │
  │                             │           │ user-assigned MI   │  │
  │                             └───────────┼───────────────────┘  │
  │                                         │                       │
  │  ┌──────────────────────┐               │ RBAC: KV Secrets User │
  │  │  Azure Key Vault     │◄──────────────┘                       │
  │  │  (Standard)          │                                       │
  │  │  MB_USERNAME         │                                       │
  │  │  MB_PASSWORD         │                                       │
  │  │  PLAYER* secrets     │                                       │
  │  │  TWOCAPTCHA-API-KEY  │                                       │
  │  └──────────────────────┘                                       │
  │                                                                 │
  │  ┌──────────────────────┐                                       │
  │  │  Log Analytics WS    │                                       │
  │  │  + App Insights      │                                       │
  │  └──────────────────────┘                                       │
  │                                                                │
  │  ┌──────────────────────┐                                       │
  │  │  Cost Management     │                                       │
  │  │  Budget ($20/mo)     │                                       │
  │  │  (subscription scope)│                                       │
  │  └──────────────────────┘                                       │
  └────────────────────────────────────────────────────────────────┘
                    │
                    │ outbound HTTPS only
                    ▼
         foreupsoftware.com (ForeUP API)
```

**One-line summary.** Three ACA Jobs: two booking jobs (one per DST half) fire 10 minutes before
6:00 AM ET **daily** (the booking-day gate then fast-exits any non-wanted weekday, so they book
only the wanted days — default Sat+Sun); one watch job fires every 10 minutes year-round to
monitor for cancellation slots. Each job is fully stateless — it pulls the bot image from ACR using a
user-assigned managed identity, runs the booking or watch logic entirely in process memory
(`InMemoryStore`), and exits. There is no durable state blob; the live `list_reservations()`
pre-book check is the cross-run source of truth for existing reservations. Secrets flow from Key
Vault into the job container as environment variables via native `keyVaultUrl` secret references,
resolved at container start by the ACA platform using the same user-assigned MI.

---

## 2. Service selection

| Concern | Chosen | Rejected | Rejection reason |
|---|---|---|---|
| Compute | **Azure Container Apps Jobs (Consumption)** | Azure Functions Consumption | Cold-start variance (documented 0–60s) breaks the T0 busy-wait window — see §5.1 |
| Compute | (same) | Azure Functions Premium EP1 | ~$146/mo, 29× cost ceiling; no benefit for twice-weekly 5-min job |
| Compute | (same) | Azure Logic Apps | No native Python; JSON-based workflows can't run the bot code |
| State persistence | **None (in-process InMemoryStore)** | Azure Blob Storage / Cosmos DB / Table Storage | Single-user, low-frequency bot; durable store deliberately dropped. `list_reservations()` pre-book check is the cross-run source of truth. |
| Secrets | **Azure Key Vault (Standard)** | GitHub Actions secrets in env | v1 is no longer GitHub-runner-hosted; secrets must live in Azure |
| Secrets | (same) | Hardcoded Bicep parameters | Hard no — plaintext secrets in IaC state |
| IaC | **Bicep** | Terraform | Bicep is first-class on Azure; no external state backend needed; less tooling overhead for single-cloud shop |
| Auth (CI→Azure) | **OIDC federated credential** | Service principal client secret | Secrets stored in GitHub = rotation burden; OIDC is credential-free |
| Container registry | **ACR Basic** | Docker Hub | Private registry; managed identity pull avoids credentials; Basic is sufficient for 1-2 image tags |
| Region | **East US 2** | Other regions | Closest to Mangrove Bay / ForeUP origin; lowest egress latency to foreupsoftware.com |

### 2.1 Shared ACR (one registry for both envs)

The two environments share a **single** ACR instead of one per env (~$5/mo saved; full-repo-scan
cost finding). The shared ACR lives in a **dedicated `rg-teetime-shared`** resource group and is
deployed separately (`registry.bicep`, `envName=shared`) — **neither** env's `main.bicep` creates
an ACR. Each env grants its own job MI a cross-RG **AcrPull** on the shared registry via
`acr-pull-cross-rg.bicep` (a nested deployment scoped to `rg-teetime-shared` — same pattern as
`killswitch-rbac-prod.bicep`), and pushes/pulls its own image there.

Why a dedicated RG: the shared ACR is a resource neither env owns, so it gets its own
lifecycle-independent RG — a teardown of either env's RG never touches it. The CI service principal
is granted **Contributor + User Access Administrator** on `rg-teetime-shared` (one-time operator
step) so CI can build/push images there and create each env's cross-RG AcrPull grant.

**Repo isolation (load-bearing — do NOT collapse).** Dev's CI build pushes into a SEPARATE repo
`teetime-dev:<sha>`, while prod pushes `teetime:<sha>`. This is not cosmetic: dev auto-deploys
many times/day and prod's tag is static between rare `infra/v*` releases, so if both shared one
repo under the weekly `--keep N` purge, dev churn would evict prod's pinned image within days →
`ImagePullBackOff` on the next 06:00 cron → silent missed booking. With separate repos, prod's
`teetime` repo is written ONLY by prod, so dev can never evict it; the purge task (on the shared
ACR) lists both repos (`--filter 'teetime:.*' --filter 'teetime-dev:.*'`, keep-N per repository)
so dev's repo is still pruned.

**Accepted couplings:** (1) BOTH env deploys now hard-depend on the shared ACR being resolvable —
if `rg-teetime-shared`'s ACR is absent, the "Resolve SHARED ACR" step fails fast. The shared ACR is
long-lived, so stable in practice. (2) An env RG teardown+rebuild gives that env's MI a new
principalId, leaving a stale AcrPull assignment on the shared ACR pointing at the deleted principal
(harmless — Azure GCs it; same as the existing killswitch cross-RG RBAC). Clean up if desired with
`az role assignment list --assignee <oldPrincipalId>`.

This SUPERSEDES the earlier "prod owns the shared ACR" interim design — the ACR was moved out of
`rg-teetime-prod` into the dedicated `rg-teetime-shared`, and prod became a non-owner like dev.
Cutover runbook: §10.6.

---

## 3. Module layout

**Status: ALL modules implemented (storage module removed — state is in-process only). Cost killswitch (PR-KS1) implemented.**

```
infra/
  AZURE_PLAN.md                # this file
  docs/plans/COST_KILLSWITCH_PLAN.md      # verified design for the $50 automated killswitch chain
  bicep/
    main.bicep                 # entry point; orchestrates all modules; accepts envName + location params
    main.bicepparam.dev        # dev environment parameter values
    main.bicepparam.prod       # prod environment parameter values
    release_events.json        # MU-15a: release-event table (MULTIUSER_PLAN §6.2), loadJsonContent'd
                               #   by compute.bicep + killswitch.bicep. v1: one event (mb0600et)
    modules/
      identity.bicep           # user-assigned managed identity for the Container Apps Jobs
      registry.bicep           # ACR Basic; grants AcrPull to the job MI. SHARED: deployed standalone to the dedicated rg-teetime-shared (envName=shared), NOT by either env's main.bicep — see §2.1
      acr-pull-cross-rg.bicep  # BOTH envs (non-owners): cross-RG AcrPull on the shared ACR in rg-teetime-shared for the job MI (§2.1)
      keyvault.bicep           # Key Vault Standard; grants Key Vault Secrets User to the job MI; soft-delete 90d
      logs.bicep               # Log Analytics Workspace + Application Insights; linked to ACA env
      compute.bicep            # ACA Environment (Consumption) + booking ACA Jobs derived from
                               #   ../release_events.json (v1: 2 jobs, DST crons) + watch ACA Job.
                               #   bookingMode/watchMode params (MU-15a, default 'toml' both envs)
                               #   select toml vs tenant CLI args/env/secrets per job.
      webapp.bicep             # NEW (MU-15a): Container App teetime-web-<env> (`teetime web`).
                               #   Gated on deployWebApp (default false, both envs).
      email.bicep              # NEW (MU-15a): ACS Communication Service + Email Service +
                               #   Azure-managed domain. Gated on deployAcsEmail (default false).
      budget.bicep             # Cost Management budget ($20/mo, both RGs; Actual 80% + Forecasted 100%); subscription-scoped
                               #   also conditionally deploys budget-teetime-killswitch ($50 actual → killswitch Action Group) when killswitchActionGroupId is supplied
      killswitch.bicep         # Cost killswitch: Logic App (Consumption) + Action Group + RBAC; deployed to rg-teetime-dev only
                               #   14 HTTP actions (MU-15a): 6 PATCH (Schedule→Manual) + 6 job POST /stop
                               #   + 2 web-app POST /stop (lever c); all 3 ACA jobs × 2 envs + both web apps
                               #   gated: enableKillswitch && !empty(killswitchRbacRoleId) && envName=='dev'
                               #   DONE 2026-05-31: custom role created (GUID 3e2d5a14-96bd-4469-9f96-b9c3270aa9e6 set in param files + azure-iac.yml); killswitch live in dev
      killswitch-rbac-prod.bicep  # companion: cross-RG role assignment for rg-teetime-prod
                               #   deployed as nested module by killswitch.bicep with scope: resourceGroup(sub, prodRgName)
.github/workflows/
  azure-iac.yml                # ACTIVE CI: bicep build on PR; deploy on merge to main (dev) / tag (prod)
```

**Dependency order for `az deployment group create` (RG-scoped):**
`identity` → `registry` + `keyvault` + `logs` → `compute` → `killswitch` (optional; dev only)

`budget` is subscription-scoped and is NOT part of this dependency chain. It is
deployed in a separate `az deployment sub create` call from `azure-iac.yml`
after the RG deployment completes. See §9.2 for the command and rationale.

```
Subscription-scope (separate deploy):
  budget  (no dependency on RG resources; standalone alert)
```

All resource-level modules are referenced from `main.bicep` as nested module
calls. Bicep's `dependsOn` is implicit via symbolic reference; explicit
`dependsOn` is only needed where a role assignment in module A must complete
before module B references the resource.

---

## 4. Parameter strategy

### Parameterized (vary by env or operator)

| Parameter | Type | Example dev | Example prod | Reason |
|---|---|---|---|---|
| `envName` | string | `dev` | `prod` | Resource name suffix; also tags |
| `location` | string | `eastus2` | `eastus2` | Allow future multi-region |
| `containerImage` | string | `teetime.azurecr.io/teetime:dev` | `teetime.azurecr.io/teetime:v1.0.0` | Decoupled from IaC |
| `budgetAmountUsd` | int | `20` | `20` | Monthly cost ceiling (project-wide, both RGs) |
| `budgetAlertEmail` | string | operator email | operator email | Cost alert recipient |
| `acrSku` | string | `Basic` | `Basic` | Allow upgrade to Standard later |
| `kvSku` | string | `standard` | `standard` | Allow upgrade if HSM needed |
| `bookingMode` / `watchMode` | string (`toml`\|`tenant`) | `toml` | `toml` | MU-15a: which code path the ACA jobs run. Default in BOTH envs — flipped only at the MULTIUSER_PLAN §11 cutover steps |
| `watchCron` | string | `0 * * * *` (hourly) | `*/10 * * * *` (unchanged) | MU-15a operator directive: dev watch cadence cut to hourly; prod untouched |
| `deployWebApp` / `deployAcsEmail` | bool | `false` | `false` | MU-15a: gate the web app + ACS resources off until the operator has pre-created their KV secrets |

### Hard-coded (architectural constants, not env-specific)

| Constant | Value | Reason |
|---|---|---|
| KV secret names | `MB-USERNAME`, `MB-PASSWORD`, `PLAYER1-EMAIL`, `PLAYER1-PHONE`, `PLAYER1-MB-MEMBER`, `TWOCAPTCHA-API-KEY`, `TEETIME-SKIP-DATES` | Bot reads these by name; names are part of the interface contract (7 secrets — `TEETIME-SKIP-DATES` added in PR #111) |
| RBAC role IDs | `Key Vault Secrets User` = `4633458b-17de-408a-b874-0445c86b69e6`; `AcrPull` = `7f951dda-4ed3-4680-a7ca-43fe172d538d` | Stable Azure built-in role GUIDs |
| `parallelism` | `1` | Never run two replicas of the booking job simultaneously — see §6 |
| `replicaCompletionCount` | `1` | Pair with parallelism=1; see §6 |
| `replicaRetryLimit` | `0` | Bot handles its own retry logic; ACA-level retry would re-enter booking without idempotency guard. NOTE: in-replica retry of *idempotent* ForeUP calls (warm-up/login/search/cancel) is handled by the adapter (`base.py _send_with_retry`); `book()` is never retried. |
| `bookingReplicaTimeout` | `1200` (20 min) | Booking job busy-waits up to ~12 min to 06:00 ET INSIDE the replica (`run --wait`, M6 PR3); timeout covers lead + busy-wait + post-T0 poll/book with ~330s slack. The DST gate caps the busy-wait by skipping the wrong-season cron. |
| `watchReplicaTimeout` | `300` (5 min) | Normal watch run is one HTTP round-trip (~30s), but the adapter retries transient transport failures on idempotent calls; 300s gives headroom so a slow-upstream run that retries never hits the replica cap (which would turn a recovered run into a Failure). |
| Log Analytics retention | `30` days | Minimal for cost; structured logs are the primary debug surface |

---

## 5. The 6:00 AM ET race on ACA (pre-emption items 1–3)

### 5.1 ACA Jobs scheduled trigger jitter

ACA Jobs cron triggers are evaluated in UTC. Microsoft documentation states
"wait up to a minute for the scheduled job execution to start." Community
reports and GitHub issues (microsoft/azure-container-apps) indicate observed
latency of 0–60 seconds from cron-fire time to container running, which is
**substantially better** than GitHub Actions' documented 1–15 minutes.

This means the v0 10-minute-early strategy is more than sufficient on ACA:
the bot only needs ~1 minute of slack (not 15) for the cron trigger to land
the container, leaving ~9 minutes of busy-wait headroom before T0.

**Bottom line:** the busy-wait pattern from PLAN.md §6.1 (`busy_wait_until(T0 -
early_arrival_ms)`, 369 ms since 2026-10-04, 400 from 2026-09-30) carries over unchanged. The ACA jitter is a tighter bound than GH
Actions, not a wider one.

No ACA-specific SLA document commits to sub-minute scheduling for the
Consumption plan. This is treated as an improvement over v0 but not a
guaranteed hard bound. If Microsoft degrades this in future, the mitigation
is identical to v0: schedule earlier (e.g., 15 min before T0 instead of 10).

### 5.2 Container cold-start

Even if the cron fires on time, the container must pull its image from ACR
and start the Python process before the bot can begin its busy-wait. Observed
cold-starts for a "hello world" image on ACA Consumption are approximately
20–30 seconds (GitHub issue #997: 22 s observed). For a full Python image,
expect 30–60 seconds depending on image size.

**Mitigation strategy (image warming).**
The job's cron schedule is set 10 minutes before T0 (matching v0), and the
bot's busy-wait handles the gap. With a 10-minute window and ~60 s worst-case
cold-start, the bot has ~9 minutes of busy-wait buffer, which is sufficient.

**Mandatory image hygiene:**
- Use a slim base image (`python:3.14-slim`, not the full `python` tag — 3.14 since
  2026-07-10, digest-pinned in the Dockerfile). Target < 300 MB
  compressed. This keeps pull time under 10 seconds from ACR in the same
  region (same Azure backbone, no internet egress).
- Pin ACR to East US 2 (same region as the ACA environment) to eliminate
  cross-region pull latency.
- Use multi-stage builds: builder stage installs deps; final stage copies only
  the venv + src. No dev tools in the production image.

**There is no pre-warming step defined here.** The 10-minute schedule slack is
the warm-up window. If empirical testing after deployment shows the combined
trigger + pull + start time exceeds 8 minutes (leaving less than 2 minutes for
busy-wait), revisit with a dedicated warm-up cron at T0 - 15 min, or consider
ACR geo-replication.

### 5.3 DST handling on ACA

ACA cron expressions are UTC-only (identical constraint to GitHub Actions).
Two DAILY crons (one per DST half; multi-day re-arch — the jobs are `teetime-job-<env>-edt`
and `-est`, the `-sun` suffix was dropped) are implemented in `compute.bicep`:

| ET target | UTC cron | Description |
|---|---|---|
| 05:50 EDT, every day (UTC-4)   | `50 9 * * *` | Fires 10 min before T0, EDT |
| 05:50 EST, every day (UTC-5)   | `50 10 * * *` | Fires 10 min before T0, EST |

Both crons fire every morning year-round. The bot's DST gate selects the correct season half,
and the booking-day gate (`core/booking_day_gate.py`) fast-exits 0 on mornings whose
`today+offset` weekday has no configured window (the wanted days are derived from
`[[request.time_windows]]`; default Sat+Sun) — so the daily crons book only the wanted days.
The bot's own DST gate — re-homed
from the deleted `book.yml` `dst` step into `core/dst_gate.py` (`should_proceed`, M6 PR2) —
is evaluated in `_run` on the `--wait` path, BEFORE the busy-wait:

```python
# core/dst_gate.py — should_proceed(clock, timezone, fire_time)
et_hour = clock.now_utc().astimezone(ZoneInfo("America/New_York")).hour
return et_hour == fire_time.hour - 1   # 5 for a 06:00 drop; wrong-season cron -> exit 0
```

It is a pure function (clock-injectable, FakeClock-tested). `_run` calls it only on the
real-timing `--wait` path (the ACA booking job passes `--wait`, M6 PR3); `--no-wait`
(manual/local) bypasses it, matching the old `workflow_dispatch` always-proceed.

**The gate is not optional.** Without it, both same-day crons would fire the full
booking logic, and the wrong-half run would arrive at T0 ± 1 hour, bypassing
the idempotency check (same RequestId but wrong resolved_date) and potentially
booking the wrong day.

### 5.4 Watch job ACA Job (M-feature-1)

The cancellation-monitor (`teetime watch`) runs as a **third ACA Job** with a
single cron `*/10 * * * *` (every 10 minutes, UTC, no DST gate needed).

Key differences from the booking jobs:

| Property | Booking jobs (×2) | Watch job (×1) |
|---|---|---|
| Cron | 2 entries (daily, one per DST half; booking-day gate restricts to wanted weekdays) | `*/10 * * * *` (single, year-round) |
| DST gate | Required (races a wall-clock moment) | Not required (watcher polls on every run; only the past-deadline gate skips) |
| `replicaTimeout` | 1200 s (20 min — covers the in-replica busy-wait to 06:00 ET) | 300 s (5 min — one HTTP round-trip plus headroom for idempotent-call retries) |
| Command | `teetime run --config ...` | `teetime watch --config ...` |
| Enabled | Always | `watcher.enabled = true` in v1 configs (M6 PR4); look-but-don't-book under `--dry-run true`. Uses the SAME `MB-*`/`PLAYER1-*` KV secrets — no new secrets. |
| Concurrency | Serialized at the ACA-Job level (one execution per job) | Separate job; a watch+book overlap is safe because the in-process advisory lock handles it |

The watch job is fully stateless (same as the booking jobs — `InMemoryStore`).
It acquires `request_lock` only for the booking phase (if a cancellation slot is
found), matching the booking jobs' in-process lock discipline. The
`WatchOrchestrator.check_once` module docstring is the canonical reference for
lock ownership rules.

**Dev cadence change (MU-15a, operator directive):** dev's watch job runs `0 * * * *`
(hourly) instead of `*/10 * * * *`, via `compute.bicep`'s `watchCron` param — a per-env
value, so prod is untouched (`main.bicepparam.prod` keeps the original `*/10 * * * *`).
Rationale: cuts dev Log Analytics/compute noise now that the watcher polls on every run
regardless of time of day; dev is `dryRun=true` so this has no booking-behavior effect.

`compute.bicep` includes the watch job `Microsoft.App/jobs` resource
(implemented as part of M-azure-T1, now DONE).

### 5.5 Migrate job (MU-16a, tenant mode only)

`teetime-migrate-<env>` runs `teetime tenant-migrate` (MULTIUSER_PLAN §10.1/§10.2): connect to the
Cosmos tenant store, run the ordered idempotent data-migration list (v1: empty), exit 0 (non-zero
on any failure). It exists ONLY when `bookingMode` or `watchMode` is `tenant`, is ALWAYS a
**Manual** trigger (never scheduled, so it is not a killswitch target and ignores
`enableSchedules`), has a 600 s replica timeout and no Key Vault secret (env: the three Cosmos
values + `TEETIME_ENV`). `azure-iac.yml` starts it and polls the execution to `Succeeded` (15 min
cap; `Failed`/`Stopped`/timeout fails the deploy job) **right after deploy pass 2**, in a tenant
env only. Not before the jobs switch image: pass 1 (when it runs; see §8) puts every job, this
one included, on the public bootstrap image, so the real migration code only exists once pass 2
has also moved the booking/watch jobs and the web. That is safe because document readers accept `schemaVersion` N
and N−1. An operator can re-run it by hand (`az containerapp job start -n teetime-migrate-<env>`);
agents may not (deploy guard).

---

## 6. State persistence (pre-emption items 4 & 10)

**v1 state is in-process only.** Each ACA Job run uses `InMemoryStore` — state
lives in process memory for the duration of a single execution and is discarded
on exit. There is no durable persistence: no SQLite file, no blob download/upload,
no blob lease.

**Why this is safe.** The bot is single-user and low-frequency (at most a handful
of runs per week). A durable store would guard against double-bookings across runs,
but the same protection is provided more simply by the `list_reservations()` pre-book
check (PLAN.md §9 layer 2): at the start of every run the bot calls the live ForeUP
API to check for existing reservations before posting a new one. The live
`list_reservations()` result is the authoritative cross-run source of truth.

**Why the durable store was dropped.** The blob download/upload/lease cycle (plus
`azure-storage-blob` + `azure-identity` SDK deps and the `BlobStateManager` Python
module) was deliberate infrastructure for a single-user, twice-weekly job. The
pre-book `list_reservations()` guard is a simpler and equally correct substitute.
This was a deliberate scope reduction — not a compromise on correctness.

**Concurrency.** `parallelism = 1` and `replicaCompletionCount = 1` on each ACA Job
ensure at most one replica runs per cron execution. The in-process `request_lock`
(PLAN.md §9 layer 5) serializes any within-run concurrent paths. No blob lease is
involved.

---

## 7. Secrets & identity (pre-emption items 6, 7, 11)

### 7.1 Key Vault secret tree

**Active secrets (current scope — notifications backend = console):**

| Secret name | Contains | Used by |
|---|---|---|
| `MB-USERNAME` | Mangrove Bay / ForeUP login username | Bot env var `MB_USERNAME` |
| `MB-PASSWORD` | Mangrove Bay / ForeUP login password | Bot env var `MB_PASSWORD` |
| `PLAYER1-EMAIL` | Player 1 (account holder) email (PII) | Bot env var `PLAYER1_EMAIL` |
| `PLAYER1-PHONE` | Player 1 (account holder) phone (PII) | Bot env var `PLAYER1_PHONE` |
| `PLAYER1-MB-MEMBER` | Player 1 Mangrove Bay member number | Bot env var `PLAYER1_MB_MEMBER` |
| `TWOCAPTCHA-API-KEY` | 2captcha.com API key for CAPTCHA solving | Bot env var `TWOCAPTCHA_API_KEY` |
| `TEETIME-SKIP-DATES` | Comma/space ISO date list of days to NOT book (LEADTIME_SKIP_PLAN F2); `""` = no skips | Bot env var `TEETIME_SKIP_DATES` |

**Tenant-mode-only secrets (MU-15a, MULTIUSER_PLAN §10.1) — referenced by compute.bicep ONLY
inside a `bookingMode`/`watchMode == 'tenant'` branch, so the default `toml` mode never needs
them to exist:**

| Secret name | Contains | Used by |
|---|---|---|
| `TENANT-CREDS-KEYRING` | JSON keyring for AES-GCM account-password decryption (`tenant/crypto.py`) | Bot env var `TENANT_CREDS_KEYRING` |
| `ACS-EMAIL-CONNECTION` | ACS Communication Service connection string — written by `email.bicep`'s `listKeys()` at deploy time, never an operator-typed value | Bot env var `ACS_EMAIL_CONNECTION` |
| `OPERATOR-NOTIFY-EMAIL` | Operator's address: the booker's run summary and the watcher's per-booking copy (2026-10-01) | Bot env var `OPERATOR_NOTIFY_EMAIL` |
| `GITHUB-ISSUES-TOKEN` | Fine-grained GitHub token, Issues read+write on `wardcrazy01894/TeeTimeBooker` only (created by the operator in PROD's vault, 2026-09-29) | Web env var `GITHUB_ISSUES_TOKEN`, only where `githubIssuesRepo` is set (prod): site reports become anonymized issues |

**Web-app-only secrets (MU-15a) — referenced by `webapp.bicep` ONLY when `deployWebApp=true`
(default false, both envs); the operator must pre-create all three (see the PR body for the
exact Google Cloud Console + `az keyvault secret set` steps) before flipping that param:**

| Secret name | Contains | Used by |
|---|---|---|
| `WEB-SESSION-SECRET` | Random signing key for session cookies (>= 32 chars) | Bot env var `WEB_SESSION_SECRET` |
| `OAUTH-GOOGLE-CLIENT-ID` | Google OAuth 2.0 client ID | Bot env var `OAUTH_GOOGLE_CLIENT_ID` |
| `OAUTH-GOOGLE-CLIENT-SECRET` | Google OAuth 2.0 client secret | Bot env var `OAUTH_GOOGLE_CLIENT_SECRET` |

GitHub OAuth (`OAUTH-GITHUB-CLIENT-ID`/`SECRET`) is deliberately NOT provisioned — operator
decision 2026-09-26: Google only for v1.

**The web app's tenant backend (MU-16a):** once `tenantCosmosEndpoint` is non-empty, `webapp.bicep`
ALSO references `TENANT-CREDS-KEYRING` and `ACS-EMAIL-CONNECTION` (the same secrets the
tenant-mode jobs use), so both must exist before the web is deployed with an endpoint set. With
the endpoint empty the web references neither and runs on its in-memory store.

**Only Player 1 needs secrets — guests do not.** The bot books a full foursome
(4 player slots), but ForeUP's booking POST transmits only the player *count*,
not per-guest name/email/phone (verified in `courses/foreup/base.py` `book()`;
matches the website, which never collects guest emails). So guests 2–4 in
`config/container.toml` are name-only and require **no** `PLAYER2/3/4-EMAIL`
secrets. `tests/test_container_config_parity.py` enforces that every `*_env`
referenced by `container.toml` is wired in `compute.bicep`, so this set can't
silently drift.

**No SMTP secrets.** `config/container.toml` uses `backend = "console"` —
notifications are written to stdout/Log Analytics only. The golf course sends
booking confirmation emails directly to the player. No `SMTP-*` secrets are
needed and none are provisioned in Key Vault.

The set of secrets is determined by `config/container.toml` (the image's runtime
config), which references env var names; the Key Vault must contain matching
secrets. The parity test in §7.1 keeps this in sync automatically.

KV secret names use hyphens (Azure KV convention); the bot's config references
the env var names with underscores. The mapping is 1:1 via the `secretRef`
→ env var assignment in `compute.bicep`.

### 7.2 Managed identity and RBAC

The Container Apps Job uses a **user-assigned managed identity** (created by
`identity.bicep`). This is the default and only supported path in v1.

**Why user-assigned, not system-assigned:**
A system-assigned MI's `principalId` is unavailable until after the ACA job
resource is created. This makes it impossible to pre-stage RBAC assignments
for Key Vault and ACR in the same Bicep deployment — you need either
a two-pass deployment or separate role-assignment runs. A user-assigned MI is
created first (one Bicep module call), its `principalId` is known immediately,
and all RBAC modules receive it in the same deployment. Both ACA job resources
(EDT + EST) reference the SAME user-assigned MI, so a single set of RBAC
assignments covers both.

RBAC assignments granted to the job's MI:

| Role | Scope | Resource |
|---|---|---|
| `Key Vault Secrets User` (4633458b-…) | Key Vault | The vault in the same RG |
| `AcrPull` (7f951dda-…) | Registry | The ACR in the same RG |

The MI is used only for ACR image pulls and Key Vault secret resolution — the bot
makes no authenticated Azure SDK calls at runtime (no blob storage, no
`DefaultAzureCredential` usage). Assignments are declared in `keyvault.bicep` and
`registry.bicep` respectively, referencing the job's `principalId` via module
output. All assignments use `roleAssignmentCondition: none` (no ABAC conditions
needed). Legacy Key Vault access policies are NOT used.

### 7.2a Cosmos DB tenant store (MU-15b) — deploy + data-plane roles, by hand

The multi-user tenant store is one free-tier Cosmos DB for NoSQL account,
`cosmos-teetime-shared` in `rg-teetime-shared`, defined by `bicep/modules/cosmos.bicep`
(MULTIUSER_PLAN §10.2). Like the shared ACR it is deployed **standalone by the operator**; neither
env's `main.bicep` creates it, so CI never touches it. Invariants (pinned by
`tests/test_cosmos_bicep.py`): free tier, `totalThroughputLimit: 1000` (a mis-edit above the free
tier cannot be provisioned), databases `prod` and `dev` at 400 RU/s shared each, containers
`tenant` (`/accountId`) and `global` (`/pk`, TTL on with no default), dev-only `tenant-ci` /
`global-ci` (7-day TTL), an index policy equal to the store's `QUERIED_PATHS`, and
`disableLocalAuth: true` (no account keys).

**This retires "no authenticated Azure SDK calls at runtime" for the tenant path only.** The tenant
jobs and web authenticate to Cosmos with their env's user-assigned MI (a token from the ACA
identity endpoint, acquired at ~05:51 and cached, never in the race window). The TOML path keeps
the no-SDK property until it is retired.

Operator steps, once (agents must not run them):
1. Deploy the account (the `Microsoft.DocumentDB` provider is already registered):
   `az deployment group create -g rg-teetime-shared --name cosmos-teetime-shared --template-file infra/bicep/modules/cosmos.bicep`
2. Create the four data-plane role assignments (`az cosmosdb sql role assignment create
   --account-name cosmos-teetime-shared --resource-group rg-teetime-shared --role-definition-id
   <id> --principal-id <objectId> --scope <scope>`). Built-in ids: Data **Contributor**
   `00000000-0000-0000-0000-000000000002`, Data **Reader** `00000000-0000-0000-0000-000000000001`.

| # | Principal | Role | Scope |
|---|-----------|------|-------|
| 1 | prod MI `mi-teetime-prod` | Contributor | `/dbs/prod` |
| 2 | dev MI `mi-teetime-dev` | Contributor | `/dbs/dev` |
| 3 | developer principal(s) for the integration suite | Contributor | `/dbs/dev/colls/tenant-ci` and `/dbs/dev/colls/global-ci` |
| 4 | operator's own user (Portal Data Explorer) | **Reader** | `/dbs/prod` and `/dbs/dev` |

No Bicep module may declare a Cosmos data-plane role assignment (pinned). Verification is
read-only: `az cosmosdb sql role assignment list --account-name cosmos-teetime-shared
--resource-group rg-teetime-shared`, and later a dev `tenant-watch --dry-run true` run must not log
a 403 at startup. The account endpoint (`https://cosmos-teetime-shared.documents.azure.com:443/`)
becomes each env's `tenantCosmosEndpoint` param at cutover (MU-17 dev, MU-18 prod).

### 7.3 Key Vault secret injection pattern

ACA supports native Key Vault secret references via `keyVaultUrl` in the job's
`secrets` configuration. The platform resolves the secret value using the job's
managed identity **at container start** and makes it available as an
environment variable inside the container. The bot reads it from `os.environ`
exactly as it reads GitHub Actions secrets in v0 — no SDK changes required.

Example pattern (Bicep ARM body, not working code — see `compute.bicep`):
```
secrets: [
  { name: 'mb-password', keyVaultUrl: 'https://<kv>.vault.azure.net/secrets/MB-PASSWORD', identity: 'system' }
]
env: [
  { name: 'MB_PASSWORD', secretRef: 'mb-password' }
]
```

**Important:** not specifying a version in the `keyVaultUrl` causes ACA to
always fetch the **latest** version of the secret. This is the desired
behavior for secret rotation (see §7.4).

If the managed identity does not have `Key Vault Secrets User` on the vault
at container start time, ACA fails the job execution with a configuration
error before the container runs. This is a fast-fail, not a silent failure.

**Tenant-mode env inventory (MU-15a wiring, read by MU-16a's `tenant/wiring.py`).** Which env var
each tenant container gets. Plain values come from Bicep params; secrets are `keyVaultUrl` refs
as above. Nothing here exists in the default `toml` mode.

| Env var | Kind | Booking + watch jobs (tenant mode) | Migrate job (`teetime-migrate-<env>`) | Web (`teetime-web-<env>`, endpoint set) |
|---|---|---|---|---|
| `TENANT_COSMOS_ENDPOINT` | plain (`tenantCosmosEndpoint`) | yes | yes | yes |
| `TENANT_COSMOS_DATABASE` | plain (= `envName`) | yes | yes | yes |
| `AZURE_CLIENT_ID` | plain (the MI's client id) | yes | yes | yes |
| `TENANT_CREDS_KEYRING` | secret `TENANT-CREDS-KEYRING` | yes | no | yes |
| `ACS_EMAIL_CONNECTION` | secret `ACS-EMAIL-CONNECTION` | yes | no | yes |
| `ACS_EMAIL_SENDER` | plain (`acsEmailSender`) | yes | no | yes |
| `OPERATOR_NOTIFY_EMAIL` | secret `OPERATOR-NOTIFY-EMAIL` | yes (the booker's run summary; the watcher's per-booking operator copy, 2026-10-01) | no | no |
| `GITHUB_ISSUES_TOKEN` / `GITHUB_ISSUES_REPO` | secret `GITHUB-ISSUES-TOKEN` / plain (`githubIssuesRepo`) | no | no | only where `githubIssuesRepo` is set (prod) |
| `TWOCAPTCHA_API_KEY` | secret `TWOCAPTCHA-API-KEY` | yes (shared with toml) | no | no |

**Web-only env** (`teetime-web-<env>`, always set when `deployWebApp`; README's web env table is
the per-variable reference):

| Env var | Kind | Notes |
|---|---|---|
| `TEETIME_PUBLIC_BASE_URL` | plain (`webPublicBaseUrl`) | OAuth redirect base; empty = fail closed at startup |
| `WEB_SESSION_SECRET` | secret `WEB-SESSION-SECRET` | signs the session cookie |
| `OAUTH_GOOGLE_CLIENT_ID` / `OAUTH_GOOGLE_CLIENT_SECRET` | secrets `OAUTH-GOOGLE-CLIENT-ID` / `-SECRET` | Google sign-in (GitHub sign-in is not deployed) |
| `TEETIME_OPERATOR_EMAIL` | secret `OPERATOR-NOTIFY-EMAIL` | the operator's address (the web's name for the jobs' `OPERATOR_NOTIFY_EMAIL`) |
| `TEETIME_WEB_DRY_RUN` | plain (`dryRun`) | refuses cancel in a dry-run env |
| `TEETIME_ENV` / `TEETIME_BUILD` | plain (`envName` / the image tag) | shown in bug-report diagnostics |
| `TEETIME_CANONICAL_HOST_REDIRECT` | plain (true iff `webCustomDomain` is set) | one canonical host (CLAUDE.md) |

The store builder fails closed on a half-configured env: `TENANT_COSMOS_DATABASE` is always set
in tenant mode, so a tenant job whose `tenantCosmosEndpoint` param was left empty refuses to start
instead of running over an empty in-memory store and exiting 0. `TENANT_COSMOS_DATABASE` is never
defaulted in code (`dev` would be the wrong database for prod). The migrate job deliberately gets
no Key Vault secret at all, so it cannot fail on one the operator has not created.

### 7.4 Secret rotation

When an operator rotates a credential (e.g., `MB_PASSWORD`):

1. `az keyvault secret set --vault-name <kv> --name MB-PASSWORD --value <new>`
2. The Key Vault reference in ACA does NOT use a pinned version, so the NEXT
   job execution automatically picks up the new value at container start.
   No ACA resource update is required.
3. There is no running container to notify — scheduled jobs start fresh each
   run. The rotation takes effect on the next scheduled execution.

If an operator wants to force immediate pickup (e.g., to test a rotated
password before the next scheduled run), use:
```
az containerapp job start --name teetime-job-<envName>-edt --resource-group rg-teetime-<envName>
```
This triggers a manual execution that will pick up the new secret.

**`TENANT-CREDS-KEYRING` is different:** it encrypts the stored course passwords. Adding a key
and making it `active` is safe, but NEVER remove a key id from it: there is no `tenant-rekey`
command yet (BACKLOG), so a stored password still encrypted under the removed key could never be
decrypted again (MULTIUSER_PLAN §9.2).

**CRITICAL:** purge protection is NOT enabled by default on new Key Vaults
(soft-delete IS enabled by default with 90-day retention).

**Dev vs prod purge protection policy:**
- `dev`: `enablePurgeProtection: false` — allows vault deletion/recreation
  during iteration without waiting out the soft-delete period. Safe because
  dev runs in permanent dry-run and holds no production credentials.
- `prod`: `enablePurgeProtection: true` — prevents permanent secret deletion
  during the soft-delete period. This is a one-way operation; once enabled on
  a vault it cannot be disabled for that vault's lifetime. The prod param file
  sets this explicitly.

### 7.5 Skip dates — no-redeploy "don't book this day" (LEADTIME_SKIP_PLAN F2)

`TEETIME-SKIP-DATES` is a Key Vault secret whose value is a comma/space-separated ISO date
list (e.g. `2026-06-14, 2026-06-21`). The booking job and the watcher skip those dates (and
won't upgrade a held booking on them). Empty/unset/malformed = no skips (fail-open — a typo can
never crash the 06:00 booker). It does NOT feed the RequestId, so editing it never disturbs
idempotency.

**⚠️ Accepted format is strict `YYYY-MM-DD` (`date.fromisoformat`).** Fail-open cuts both ways:
a token that is NOT a bare ISO date — e.g. `2026-06-14T06:00` (time suffix), `2026/06/14`
(slashes), or `06/14/2026` (US order) — is **silently dropped**, so the date you meant to block
is NOT skipped and the bot will book it. The warning lands in Log Analytics, not in the Portal,
so it's invisible at edit time. **Always verify after a Portal edit** that the value parses to
the dates you intend — the fastest agent-safe check is to run the value through the loader
locally: `TEETIME_SKIP_DATES="<the value>" uv run teetime show-config --config config/local.toml`
prints the resolved `skip_dates` (unmasked — calendar dates aren't secrets). A date you expect
that's missing from that list means the token was rejected.

**ONE-TIME PRE-DEPLOY STEP (DONE 2026-06-10 — both vaults confirmed; see §10.1.1).** `compute.bicep`
references this secret via `keyVaultUrl`, and **ACA validates KV secret refs at job-CREATE time**
— so the secret MUST already exist or the deploy fails (`InvalidParameterValueInContainerTemplate`).
Dev **auto-deploys on merge**, so create it in BOTH vaults **before merging**:
```
az keyvault secret set --vault-name <kv-dev>  --name TEETIME-SKIP-DATES --value " "
az keyvault secret set --vault-name <kv-prod> --name TEETIME-SKIP-DATES --value " "
```
Seed it with a single SPACE (`--value " "`) — Azure rejects a truly empty value (`--value ""`
errors `[Required] --value`). A whitespace-only value parses to **no skips** (`parse_skip_dates`
treats whitespace as empty; covered by `test_parse_empty_and_none_is_empty`) with no log noise.
These are operator-run; the agent is hard-blocked from `az keyvault secret set`. The current vault
names are `kv-teetime-dev-s66g` and `kv-teetime-prod-4jte`.

**Editing later (no redeploy):** Portal → the Key Vault → Secrets → `TEETIME-SKIP-DATES` →
**+ New Version** → set the value (e.g. `2026-06-14`) → Create. No new job revision, no redeploy.

**When it takes effect:** the ACA KV reference is NOT version-pinned, so the next job execution
re-resolves it at container start (same mechanism as secret rotation, §7.4). The watch cron fires
every 10 min and the booking cron at 05:50 ET, so a Portal edit is normally in effect by the next
run. **Conservative guidance: make the edit the night before** the day you want skipped — correct
regardless of any platform-side refresh latency. NOTE the cutoff (§F1) does NOT un-book: if a skip
edit lands too late and the bot already booked that day, you must cancel manually on the course
site (with the date now skipped, the watcher won't re-book it).

**Verify (read-only, agent-safe):**
```
az keyvault secret show --vault-name <kv> --name TEETIME-SKIP-DATES --query value -o tsv   # source value
```
To confirm a JOB sees it, trigger/await a watch run and read its log: the watch run's
`targets=[…]` line (message wording is illustrative — match on the targets list, not the exact
string) will OMIT a skipped date. (`az containerapp job start` is operator-only — guard-blocked.)

---

## 8. CI validation pipeline (pre-emption item 9)

The file `.github/workflows/azure-iac.yml` is the active deploy workflow (alongside
`ci.yml` for lint/test). The v0 `book.yml` / `watch-tee-time.yml` cron workflows were
removed in #43.

### 8.1 Trigger strategy

| Trigger | Action |
|---|---|
| `pull_request` touching `infra/**` or `.github/workflows/azure-iac.yml` | `bicep build` lint (the PR what-if was removed 2026-09-30, §8.3) |
| `push` to `main` touching `infra/**` or `.github/workflows/azure-iac.yml` | Same as PR + **auto-deploy to `dev` (no required-reviewer gate — intentional; see below)** |
| `push` tag matching `infra/v*` | Deploy to `prod` (requires manual approval) |
| `workflow_dispatch` | Manual deploy to chosen env |

**GitHub Environment protection rules — dev vs prod:**
`dev` auto-deploys on merge to main with NO required-reviewer gate. This is a
deliberate relaxation of the general CLAUDE.md agent rule for the dev
environment only, per operator request — iteration speed matters more than a
gate when no real credentials or bookings are at stake (`dryRun` defaults
`true`; see §8.1a below). `prod` retains a manual approval gate.

Setup steps for prod environment (one-time):
1. GitHub repo > Settings > Environments > New environment > name: `prod`
2. Under "Deployment protection rules" > enable "Required reviewers"
3. Add the operator GitHub account as required reviewer
4. Under "Deployment branches and tags" > "Selected branches and tags": add branch `main` and
   tag `infra/v*` (done 2026-09-30). `deploy-prod` also refuses a commit that is not on `main`.
5. Save.

The `dev` environment (if configured in GitHub) should have NO required
reviewers — any push to `main` that touches `infra/**` deploys automatically.

**§8.1a `dryRun` Bicep parameter:** `main.bicep` accepts a `dryRun` bool
parameter that defaults to `true`. The dev parameter file sets `dryRun = true`
explicitly — all ACA Job container commands in dev include `--dry-run true`,
so no real bookings ever fire in dev. To go live on prod, set `dryRun = false`
in `main.bicepparam.prod` and deploy via a prod tag push.

### 8.2 OIDC federated credential setup (one-time, operator)

```bash
# 0. Pre-flight: log in and set subscription context.
az login --tenant 5151757e-ef5b-42a5-a09b-6410b40b2186
az account set --subscription 3f82c7e1-4b1b-4a55-b905-d79f65c6887d

# 1. DONE — app registration already created.
#    appId (= AZURE_CLIENT_ID for GitHub secrets): 7a9c17a4-b65b-4028-99db-6a099d2b9524
#    Object ID (used in --id for federated-credential commands):
#                                                  d24e6af8-90cf-4883-afe9-3c68c4bb28c7
#    Note: appId and object ID are different fields. appId is the "client ID" used
#    by azure/login and GitHub secrets. Object ID is used only in az CLI --id args below.

# 2. Create a service principal for the app (needed for RBAC assignments).
az ad sp create --id 7a9c17a4-b65b-4028-99db-6a099d2b9524

# 3. Create the federated credential for GitHub Actions OIDC.
#    IMPORTANT: audiences must be "api://AzureADTokenExchange" (not "AzureADApplications").
#    Using the wrong audience causes AADSTS70021 at runtime.
#    NOTE: --id here takes the app OBJECT ID, not the appId.
az ad app federated-credential create \
  --id d24e6af8-90cf-4883-afe9-3c68c4bb28c7 \
  --parameters '{
    "name": "gh-main",
    "issuer": "https://token.actions.githubusercontent.com",
    "subject": "repo:wardcrazy01894/TeeTimeBooker:ref:refs/heads/main",
    "audiences": ["api://AzureADTokenExchange"]
  }'

# 4. Add a separate credential for tag pushes (prod deploys).
az ad app federated-credential create \
  --id d24e6af8-90cf-4883-afe9-3c68c4bb28c7 \
  --parameters '{
    "name": "gh-tags",
    "issuer": "https://token.actions.githubusercontent.com",
    "subject": "repo:wardcrazy01894/TeeTimeBooker:ref:refs/tags/infra/*",
    "audiences": ["api://AzureADTokenExchange"]
  }'

# 5. Grant the service principal Contributor + User Access Administrator on each RG.
#    (Run once per env — dev first, then prod when ready.)
az role assignment create \
  --assignee 7a9c17a4-b65b-4028-99db-6a099d2b9524 \
  --role "Contributor" \
  --scope "/subscriptions/3f82c7e1-4b1b-4a55-b905-d79f65c6887d/resourceGroups/rg-teetime-dev"

az role assignment create \
  --assignee 7a9c17a4-b65b-4028-99db-6a099d2b9524 \
  --role "User Access Administrator" \
  --scope "/subscriptions/3f82c7e1-4b1b-4a55-b905-d79f65c6887d/resourceGroups/rg-teetime-dev"

# 6. Add GitHub secrets (Settings → Secrets → Actions → New repository secret):
#    AZURE_CLIENT_ID       = 7a9c17a4-b65b-4028-99db-6a099d2b9524
#    AZURE_TENANT_ID       = 5151757e-ef5b-42a5-a09b-6410b40b2186
#    AZURE_SUBSCRIPTION_ID = 3f82c7e1-4b1b-4a55-b905-d79f65c6887d
```

GitHub repository secrets required: `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`,
`AZURE_SUBSCRIPTION_ID`. No `AZURE_CLIENT_SECRET` — OIDC is credential-free.

**Live federated-credential reality (verified 2026-05-31).** Because the
`deploy-dev`/`deploy-prod` jobs set `environment: dev|prod` and `validate` runs on
`pull_request`, GitHub's OIDC `sub` claim is environment-/PR-scoped, NOT ref-scoped.
The app registration therefore carries the credentials below, which are what the
workflow actually consumes — the `gh-main`/`gh-tags` ref-based creds in steps 3–4
above are legacy and NOT used by the current env-scoped jobs:

| Name | Subject | Used by |
|------|---------|---------|
| `gh-env-dev` | `…:environment:dev` | `deploy-dev` (push to main / dispatch) |
| `gh-env-prod` | `…:environment:prod` | `deploy-prod` (tag `infra/v*` OR dispatch) |
| `gh-pull-request` | `…:pull_request` | `validate` |

The `gh-tags` credential is NOT required: `deploy-prod`'s `environment: prod` makes the
`sub` claim `environment:prod` even on a tag push, so `gh-env-prod` covers it.

The CI service principal needs `Contributor` on the target resource group plus
`User Access Administrator` scoped to the resource group (to create role
assignments in the Bicep modules). The `User Access Administrator` scope is
RG-scoped, not subscription-scoped, minimizing blast radius.

### 8.3 what-if known issue

`az deployment group what-if` has documented false-positive drift reports for
Container Apps revision configurations. Specifically, it sometimes reports a
"modify" change on the `configuration.secrets` block even when no change
occurred, because the platform redacts secret values in the GET response.

**Stance:** `what-if` is advisory and run by hand. The PR workflow no longer runs it
(2026-09-30): it passed only the old core params, not the param-file values the deploy jobs
parse, so it previewed template defaults rather than what dev deploys. Run it by hand with the
deploy job's parameters when a preview matters (read-only). Only `bicep build` failures block a
PR; the deploy step (`create`) is the source of truth for idempotency.

`az deployment group create` with identical Bicep and parameters is fully
idempotent for all resources in this plan. ACA Jobs do NOT create new revisions
on redeploy unless the container image tag or environment configuration changes.
To force a new execution of the job with a new image, update `containerImage`
in the parameter file and redeploy — this is the intended release workflow.

---


### Deploy pass 1 runs only for a new environment (2026-09-29)

Each deploy job is two Bicep deployment passes: pass 1 with the public bootstrap image (so a NEW
env's managed identity gets AcrPull on the shared ACR before anything pulls), then the image
build, then pass 2 with the real image. Pass 1 used to run on every deploy, which doubled the
Bicep time (dev deploys went ~4 to ~11 min after MU-17 added the web app, ACS email and the
migrate job) and put every job AND the web app on the placeholder image for a pass (the prod web
is always warm, so the site served the placeholder). The **Detect bootstrap need** step now reads
the env MI's principal id, the shared ACR id and the MI's AcrPull assignment there
(`--assignee-object-id`, no Graph lookup) and skips pass 1 when the assignment exists. It is
fail-safe: no identity, an `az` error or no assignment runs pass 1 as before; the step logs
`bootstrap pass 1 needed: <bool>` as a notice. Pinned by `tests/test_azure_iac_bootstrap_skip.py`,
which runs the step's script against a fake `az`.

## 9. Cost estimate (pre-emption item 12)

### 9.1 Per-component breakdown (East US 2, April 2026)

| Component | SKU | Monthly cost | Notes |
|---|---|---|---|
| Container Apps Job compute | Consumption | **$0.00** | Free tier: 180,000 vCPU-s/month, **shared per-subscription across BOTH envs** (not per-env). Booking: ~11-min busy-wait run × 0.25 vCPU ≈ 165 vCPU-s × ~8-9 weekend runs/mo (Sat+Sun) ≈ 1.5k. Watch (every 10 min): ~4,320 runs/env × 0.25 vCPU; billed on the full replica lifetime (cold start + image pull + Python startup), realistically ~45-60 s/run not the ~30 s of actual work ≈ ~50k vCPU-s/env. Both envs combined ≈ **~110k vCPU-s/mo ≈ ~60-65% of the shared free grant** (~35-40% headroom — NOT the ~80% an optimistic 30 s/run implies). The every-10-min watch job, not the booker, is the dominant consumer and the first thing to push past the grant if cadence or course count grows. |
| Container Apps Job memory | Consumption | **$0.00** | Free tier: 360,000 GiB-s/month, shared per-subscription. Same run profile at 0.5 GiB ≈ ~110k GiB-s/env; both envs combined ≈ **~219k GiB-s/mo ≈ ~61% of the shared free grant**. |
| Container Apps Environment | Consumption | **$0.00** | No per-environment fee on Consumption plan. |
| Azure Container Registry | Basic | **~$5.00** | $5.00/mo flat for Basic SKU. Includes 10 GiB storage. Our image is ~300 MB; well within limits. Every merge pushes a new `teetime:<sha>` tag, so a weekly `acr purge` ACR task (`registry.bicep`, keep last 10 tags + reap untagged) caps unbounded storage growth before it can approach the 10 GiB allowance. |
| Key Vault | Standard | **~$0.03** | $0.03/10k operations. ACA caches KV-referenced secrets (~30-min refresh, not per-execution), so ≈ 7 secrets × ~48 refreshes/day × 30 ≈ ~10k reads/month. Negligible — around the 10k mark, well under $0.10. |
| Log Analytics | Pay-per-use | **~$0.00–$0.50** | First 5 GB/month free. Bot produces <10 MB logs/month. |
| Application Insights | Pay-per-use | **~$0.00** | First 5 GB/month free. |
| Network egress | — | **~$0.00** | First 100 GB/month free. Bot does <10 MB/run. |
| **Total (dev or prod)** | | **~$5.01–$5.51/mo per env** | Well within the $20/mo budget ceiling (covers both envs). |

**MU-15a additions (webapp.bicep, email.bicep): $0.00 while gated off.** Both modules are
deployed only when `deployWebApp`/`deployAcsEmail` are `true` — default `false` in both envs'
param files (see §3, §7.1). A scale-to-zero Container App with no traffic and an ACS Email
service with no messages sent both cost nothing; this PR ships the modules but does not flip
either gate, so the estimate above is unchanged. When eventually enabled: the web Container App
is the same Consumption-plan free-tier math as the ACA Jobs above (near-zero for the low request
volume of an invite-only site), and ACS Email's free tier covers 100 emails/month before
per-message billing.

**Prod web always warm (2026-09-28): up to ~$5.83/month.** Scale-from-zero took ~30 s (replica
assignment + image pull + Python start on 0.25 vCPU), so prod's param file sets
`webMinReplicas = 1`; dev stays 0. Idle billing applies only with `minReplicas > 0` (a
scale-to-zero replica bills at the ACTIVE rate for its whole lifetime, cooldown included, so a
longer cooldown or a keep-warm ping costs more for less). East US 2 retail prices (Azure Retail
Prices API, 2026-09-28): idle vCPU $0.000003/s, memory $0.000003/GiB-s. One idle replica for a
30-day month: 0.25 × 2,592,000 × $0.000003 = $1.94 + 0.5 × 2,592,000 × $0.000003 = $3.89 =
**$5.83**, before whatever free grant the jobs leave (the idle replica alone, 648k vCPU-s, is
past the whole 180k grant, so treat it as real spend). Request-time active seconds are cents.
The killswitch latch still forces `minReplicas` to 0.

### 9.2 Budget alert

Azure Cost Management budgets are **subscription-scoped**, not resource-group-
scoped. `budget.bicep` is a subscription-scope Bicep module (targetScope =
'subscription') and must be deployed at the subscription level, not as a
nested module in the RG-scoped `main.bicep`.

**Approach:** `main.bicep` is RG-scoped. `budget.bicep` is a separate
subscription-scope deployment (`az deployment sub create`), run **manually by
the operator** — the CI service principal is RG-scoped by design and cannot
deploy it. `azure-iac.yml` emits a `::notice::` reminder in the deploy jobs
rather than attempting (and failing) the deploy. The runbook command is below;
the Azure portal (Cost Management > Budgets) is an equivalent path that also
sidesteps a known `az deployment sub create` budget-PUT bug.

**Two-tier alert ladder (as of PR-KS1):**

| Tier | Budget resource | Amount | Threshold | Alert type | Action |
|---|---|---|---|---|---|
| 1 | `budget-teetime` | $20 | 80% actual ($16) | Email only | Early warning |
| 1 | `budget-teetime` | $20 | 100% forecast ($20) | Email only | Projected overage warning |
| 2 | `budget-teetime-killswitch` | $50 | 100% actual ($50) | Action Group → Logic App | Silences all 6 ACA Job crons + stops in-flight |

**Headroom with prod's always-warm web replica (2026-09-28):** steady spend is ~$5–5.50/mo
(ACR Basic; the jobs sit in the free grant) + ~$5.83/mo for the warm replica ≈ **~$11.30/mo**,
under Tier 1's $16 (80%) early warning with ~$4.70 to spare and far below Tier 2's $50. A
genuine anomaly still trips Tier 1 first. The killswitch's `stop` of the prod web app holds it at
zero replicas despite `minReplicas = 1` (see the comment on `Stop_webapp_prod` in
`killswitch.bicep`).

Tier 1 (`budget-teetime`, $20, email-only) is UNCHANGED. Tier 2 (`budget-teetime-killswitch`,
$50, killswitch-trigger) is a SEPARATE second budget resource in `budget.bicep` (conditional on
`killswitchActionGroupId`). Both budgets evaluate the same project spend independently. See
`docs/plans/COST_KILLSWITCH_PLAN.md`.

**Deploy note:** `azure-iac.yml` does **not** attempt the budget deploy — the CI service
principal is RG-scoped only (a subscription-scoped budget needs subscription-level permission),
so the deploy jobs just emit a `::notice::` reminder. The budget is deployed manually by the
operator. **DONE 2026-05-31** — both `budget-teetime` ($20) and
`budget-teetime-killswitch` ($50, wired to the Action Group) are deployed; the killswitch is
fully armed end-to-end across dev + prod.

⚠️ **Two non-obvious prerequisites when (re)deploying the killswitch budget** (both caused a
`RBACAccessDenied` on the first attempt — see Microsoft's Cost Management error-codes doc):
1. **Monitoring Reader on the Action Group's RG.** A budget whose notification references an
   Action Group (`contactGroups`) triggers a *separate* `Microsoft.Insights/actionGroups/read`
   authorization check in the Cost Management PUT path. **Subscription Owner is NOT sufficient**
   (the inherited grant isn't honored by that backend check). The deploying principal must have an
   explicit `Monitoring Reader` (or higher) assignment on `rg-teetime-dev`:
   `az role assignment create --assignee <objectId> --role "Monitoring Reader" --scope /subscriptions/<sub>/resourceGroups/rg-teetime-dev` (wait ~1-2 min to propagate). Granted to the operator 2026-05-31.
2. **Use the canonical Action Group resource ID, exact casing.** Get it from
   `az monitor action-group show -g rg-teetime-dev -n ag-teetime-killswitch-dev --query id -o tsv`
   (note `microsoft.insights` is lowercase in the canonical ID). A mis-cased `contactGroups` ID
   independently triggers `RBACAccessDenied`.

```bash
# 1) Obtain the canonical Action Group ID:
az monitor action-group show -g rg-teetime-dev -n ag-teetime-killswitch-dev --query id -o tsv
# → /subscriptions/3F82C7E1-.../resourceGroups/rg-teetime-dev/providers/microsoft.insights/actionGroups/ag-teetime-killswitch-dev

# 2) Deploy both budget tiers (Tier-1 $20 + Tier-2 $50 killswitch):
az deployment sub create --location eastus2 \
  --template-file infra/bicep/modules/budget.bicep \
  --parameters budgetAmountUsd=20 budgetAlertEmail=<email> \
               killswitchActionGroupId=<canonical id from above> \
               killswitchBudgetAmountUsd=50
```
The Tier-2 `killswitchBudget` resource is conditional on `killswitchActionGroupId`: omit that
param and the manual deploy only creates/updates the Tier-1 $20 budget (Tier 2 is a clean no-op).
Fallback if `RBACAccessDenied` persists after the Monitoring Reader grant propagates: create the
budget in the Azure portal (the portal path bypasses a known `az deployment sub create` bug —
azure-cli issue #23648).

**Killswitch custom role — DONE 2026-05-31:**
The "ACA Job Schedule Manager" custom role (GUID `3e2d5a14-96bd-4469-9f96-b9c3270aa9e6`) has been
created by the operator. The GUID is set in both param files (`main.bicepparam.dev` /
`main.bicepparam.prod`) and in `azure-iac.yml` (dev job env `KILLSWITCH_RBAC_ROLE_ID`). The
killswitch chain (Logic App + Action Group + cross-RG RBAC) arms automatically on every dev
auto-deploy. For reference, the role was created with:
```bash
az role definition create --role-definition '{
  "Name": "ACA Job Schedule Manager",
  "Description": "Read, PATCH (disable/enable schedule), and stop executions on ACA Jobs. Used by cost killswitch Logic App.",
  "Actions": [
    "Microsoft.App/jobs/read",
    "Microsoft.App/jobs/write",
    "Microsoft.App/jobs/stop/action"
  ],
  "AssignableScopes": ["/subscriptions/3f82c7e1-4b1b-4a55-b905-d79f65c6887d"]
}'
# GUID returned: 3e2d5a14-96bd-4469-9f96-b9c3270aa9e6 — already set in both param files.
```

---

## 10. Deploy & cutover runbook (pre-emption items 8 & 13)

### 10.1 First-time setup (operator steps, run once)

```bash
# 1. Create resource group (dev example)
az group create --name rg-teetime-dev --location eastus2

# 2. Deploy IaC (bootstraps all resources)
az deployment group create \
  --resource-group rg-teetime-dev \
  --template-file infra/bicep/main.bicep \
  --parameters @infra/bicep/main.bicepparam.dev

# 3. Populate Key Vault secrets (operator, NOT in automation)
az keyvault secret set --vault-name kv-teetime-dev --name MB-USERNAME --value "<value>"
az keyvault secret set --vault-name kv-teetime-dev --name MB-PASSWORD --value "<value>"
az keyvault secret set --vault-name kv-teetime-dev --name PLAYER1-EMAIL --value "<value>"
az keyvault secret set --vault-name kv-teetime-dev --name PLAYER1-PHONE --value "<value>"
az keyvault secret set --vault-name kv-teetime-dev --name PLAYER1-MB-MEMBER --value "<value>"
az keyvault secret set --vault-name kv-teetime-dev --name TWOCAPTCHA-API-KEY --value "<value>"
# TEETIME-SKIP-DATES is the 7th secret. ACA validates its keyVaultUrl ref at job-create like
# every other, so it MUST exist or the deploy fails — even though it's fail-open at runtime.
# Seed " " = no skips; edit later in the Portal with no redeploy. See §7.5.
az keyvault secret set --vault-name kv-teetime-dev --name TEETIME-SKIP-DATES --value " "
# Guests 2-4 need NO secrets — ForeUP books by player count only. See §7.1.
# No SMTP-* secrets needed — notifications use console (stdout) only. See §7.1.
# No storage secrets needed — the bot makes no Azure SDK calls at runtime. See §6.

# 4. Build and push container image
az acr build --registry teetimedev --image teetime:dev --file Dockerfile .

# 5. Trigger a manual dry-run to validate
az containerapp job start \
  --name teetime-job-dev-edt \
  --resource-group rg-teetime-dev
# Check logs in Log Analytics; verify dry-run output is correct.

# 6. Deploy budget (subscription-scoped, run once)
az deployment sub create \
  --location eastus2 \
  --template-file infra/bicep/modules/budget.bicep \
  --parameters budgetAmountUsd=20 budgetAlertEmail=<email>
```

### 10.1.1 Prod first-time bootstrap (run once, before the first `infra/v*` tag)

The per-env bootstrap that §8.2 step 5 defers ("dev first, then prod when ready").
The prod deploy will FAIL on the first try without these — the CI service principal
starts with permissions on `rg-teetime-dev` only. Status flags reflect 2026-05-31.

```bash
# 1. Resource group.  (DONE 2026-05-31)
az group create -n rg-teetime-prod -l eastus2

# 2. Grant the CI service principal Contributor + User Access Administrator on the
#    prod RG. RG-scoped is sufficient — the CI `az group create` step then no-ops on
#    the existing RG, exactly as it does for dev (the SP need NOT have subscription
#    scope).  (DONE 2026-05-31)
az role assignment create --assignee 7a9c17a4-b65b-4028-99db-6a099d2b9524 \
  --role "Contributor" \
  --scope /subscriptions/3f82c7e1-4b1b-4a55-b905-d79f65c6887d/resourceGroups/rg-teetime-prod
az role assignment create --assignee 7a9c17a4-b65b-4028-99db-6a099d2b9524 \
  --role "User Access Administrator" \
  --scope /subscriptions/3f82c7e1-4b1b-4a55-b905-d79f65c6887d/resourceGroups/rg-teetime-prod

# 3. OIDC federated credential for prod.  (DONE — `gh-env-prod` exists; see §8.2.)
# 4. GitHub `prod` environment with required reviewers.  (DONE — verified present.)

# 5. First prod deploy: push a tag matching infra/v* (e.g. infra/v1.0.0), or
#    workflow_dispatch with environment=prod. This creates ACR, Key Vault
#    (kv-teetime-prod-<suffix>), Log Analytics, identity, and the ACA environment.
#    ⚠️ This deploy is EXPECTED TO FAIL at the compute/jobs step — ACA validates the
#    jobs' keyVaultUrl secret references at CREATION time, and the vault is still empty,
#    so job creation errors ("InvalidParameterValueInContainerTemplate ... Unable to get
#    value ... for secret 'mb-username'..."). The vault IS created before the failure, so
#    you can populate it and redeploy. (Done 2026-05-31.)

# 5b. Grant the OPERATOR (you) write access to the prod vault. keyvault.bicep grants only
#     the bot's managed identity "Key Vault Secrets User" (read); the RBAC vault gives the
#     human no data-plane access, so secret-set would 403 without this. (Done 2026-05-31.)
OBJ=$(az ad signed-in-user show --query id -o tsv)
KVID=$(az keyvault show -n <kv-teetime-prod-suffix> -g rg-teetime-prod --query id -o tsv)
az role assignment create --assignee-object-id "$OBJ" --assignee-principal-type User \
  --role "Key Vault Secrets Officer" --scope "$KVID"   # wait ~1-2 min to propagate

# 6. Populate the prod Key Vault secrets (operator, REAL prod values — NOT in CI).
az keyvault secret set --vault-name <kv-teetime-prod-suffix> --name MB-USERNAME       --value "<value>"
az keyvault secret set --vault-name <kv-teetime-prod-suffix> --name MB-PASSWORD       --value "<value>"
az keyvault secret set --vault-name <kv-teetime-prod-suffix> --name PLAYER1-EMAIL     --value "<value>"
az keyvault secret set --vault-name <kv-teetime-prod-suffix> --name PLAYER1-PHONE     --value "<value>"
az keyvault secret set --vault-name <kv-teetime-prod-suffix> --name PLAYER1-MB-MEMBER --value "<value>"
az keyvault secret set --vault-name <kv-teetime-prod-suffix> --name TWOCAPTCHA-API-KEY --value "<value>"
# TEETIME-SKIP-DATES is the 7th secret (added with the LEADTIME_SKIP feature, PR #111). ACA
# validates its keyVaultUrl ref at job-create like every other, so it MUST exist or the deploy
# fails. Seed " " = no skips; edit later in the Portal with no redeploy. See §7.5.
# (Done 2026-06-10: confirmed present in BOTH vaults before the infra/v2.1.0 prod deploy,
# which deployed successfully.)
az keyvault secret set --vault-name <kv-teetime-prod-suffix> --name TEETIME-SKIP-DATES --value " "

# 7. RE-RUN the deploy (workflow_dispatch environment=prod, approve the gate). Now the
#    secrets resolve, so job creation succeeds and the 3 jobs land. (Done 2026-05-31.)
```

**Prerequisites for a successful prod RUN (not just a successful deploy):**
- A **funded** 2captcha key in `TWOCAPTCHA-API-KEY` — prod runs `dryRun=false`, which
  performs the live CAPTCHA solve (dev dry-run skips it).
- Valid ForeUP / Mangrove Bay credentials in `MB-USERNAME` / `MB-PASSWORD`.
- **M6 implemented and verified in dev** (the `--wait` real-timing path, the DST gate, and
  watcher enablement). The prod cutover is the LAST step, after a clean dev dry-run on a wanted booking day (Sat or Sun).

**Secret ordering (corrected — this bit us on the first prod deploy):** ACA validates a job's
`keyVaultUrl` secret references at DEPLOY (job-creation) time, NOT lazily at run time. So a
fresh-vault deploy hard-FAILS at the compute step until the secrets exist. The vault is created
in the same deploy (before compute), so the working sequence is: **deploy (creates vault, fails
at jobs) → grant operator KV access (5b) → set secrets (6) → re-deploy (7)**. (An earlier draft
of this runbook wrongly said the deploy succeeds and only runs fail — it does not.) Follow-up
idea: have the IaC auto-grant a named `operatorObjectId` the `Key Vault Secrets Officer` role so
step 5b isn't manual.

### 10.2 Ongoing deploy (CI-driven)

The active CI workflow is `.github/workflows/azure-iac.yml`.

For image-only updates (new bot code, same IaC):
1. CI builds and pushes `teetime:<tag>` to ACR.
2. Update `containerImage` in `main.bicepparam.dev` (or `prod`).
3. CI workflow runs `az deployment group create` — ACA Job picks up new image
   on next execution. There is no "restart" primitive for scheduled jobs; the
   new image takes effect on the next cron fire.

For IaC changes (Bicep edits):
1. PR opens → `azure-iac.yml` runs `bicep build`.
2. Merge to `main` → `azure-iac.yml` **auto-deploys to dev** (no reviewer gate; see §8.1).
   Dev always runs in dry-run (`dryRun = true` in parameter file).
3. Tag `infra/v*` → `azure-iac.yml` deploys to prod (requires manual approval).

> **Multi-day cutover — manual orphan cleanup (DONE — orphans deleted; verified again at the
> `infra/v2.1.0` deploy 2026-06-10: only `-edt`, `-est`, and the watch job exist in prod).**
> Kept for reference: the prod `infra/v*` tag that first shipped the multi-day re-arch
> renamed the booking jobs `teetime-job-prod-edt-sun`/`-est-sun` → `-edt`/`-est`. Deploys run
> in ARM **incremental** mode (`az deployment group create`, no `--mode Complete`), which
> CREATES the new jobs but does **not** delete the old ones. The orphaned `-edt-sun`/`-est-sun`
> jobs would keep firing their old Sunday-only cron AND are NOT covered by the killswitch (it
> targets the new names). After the prod tag deploy, MANUALLY delete them (operator-approved):
> ```
> az containerapp job delete -n teetime-job-prod-edt-sun -g rg-teetime-prod --yes
> az containerapp job delete -n teetime-job-prod-est-sun -g rg-teetime-prod --yes
> ```
> Confirm with `az containerapp job list -g rg-teetime-prod` that only `-edt`, `-est`, and the
> watch job remain. (The dev orphans were already cleaned up on the dev auto-deploy.)

### 10.3 v0 → v1 cutover (DONE)

The v0 GitHub Actions cron workflows (`book.yml`, `watch-tee-time.yml`) were **removed in
#43** — the booking and watch schedules now run exclusively as ACA Jobs (daily booking crons
+ booking-day gate; `compute.bicep`). There is therefore no longer a v0/v1 dual-run hazard: no GitHub Actions
schedule exists to conflict with the ACA Jobs. The only remaining GitHub Actions workflows
are `ci.yml` (lint/test on PRs) and `azure-iac.yml` (deploy).

For ad-hoc recovery (e.g. a missed drop), trigger an ACA job execution directly
(`az containerapp job start …`) or run the `teetime` CLI locally — see §10.4. The first
real production run is gated on the M6 cutover checklist (§10.5), not on this section.

### 10.4 M6 verification (dev, dry-run) — proving both jobs work before prod

With `dryRun=true` the final POST never fires, so **logs are the only proof**. Query
`ContainerAppConsoleLogs_CL` (or `az containerapp job logs show`) for the job execution.

**(a) Booking job fired at the 6:00:00 ET drop** — look for, in order:
- `Booking run: target=['<next-target-day>'] dry_run=True players=4` (quoted list)
- `run: real-timing path (--wait); fire_time=06:00:00 America/New_York, NTP offset_ms=…`
  (confirms the REAL scheduler was selected, not the immediate demo path)
- `race: busy-wait complete; firing at <ts> (target=<ts>, drift_ms=…)` — the load-bearing
  line: it fired within a few ms of T0. (Emitted by `orchestrator.run` after `busy_wait_until`.)
- Then a `DRY_RUN` outcome (no booking POST).
A wrong-season cron instead logs `DST-half gate: wrong-season cron (ET hour != 5) — exiting 0`.
On a NON-booking day (multi-day re-arch: the cron fires daily) a correct-season run logs
`booking-day gate: today+7 is <Weekday> <date>, not a wanted booking day — exiting 0.` and
exits without auth/search/busy-wait (sub-cent, free-tier). 5/7 mornings this is the expected
fast-exit; a wanted day (Sat/Sun) proceeds to the busy-wait + race lines above.

**(b) Watch job actually polled** — look for: `Watch check: targets=['<sat>', '<sun>'] dry_run=True`
(plural; the watcher checks the next occurrence of each wanted weekday and polls EVERY run —
multi-day re-arch), a ranked-slots line, and a `DRY_RUN` result. A run that logs
`Watch job is disabled` means `watcher.enabled` is false — not what we want in v1.

**On-demand check (no need to wait for a booking day)** — the `--fire-time` hatch makes the
`--wait` busy-wait + DST gate reachable at any hour, refused unless `--dry-run true`:
```bash
az containerapp job start -n teetime-job-dev-edt -g rg-teetime-dev \
  --command "teetime" --args "run --config /app/config/container.toml --dry-run true --wait --fire-time HH:MM:SS"
```
(pick `HH` = current ET hour + 1 so the gate's `hour == fire_hour-1` passes and the busy-wait
is short). NOTE: `az containerapp job start` is an agent-guarded command — operator runs it.

**Exit criterion (accepted):** production-identical timing (the real cron landing on T0) is
observable ONLY on a live cron landing on a wanted booking day. So M6's go/no-go is: green
FakeClock tests + a clean `--fire-time` on-demand dev run + **one clean dev dry-run on a wanted
booking day (Sat or Sun)** (the cron-driven race).

**(c) Pre-weekend live API-drift canary (manual; do not skip before a real drop).** The respx
unit tests only assert what the bot *sends* — they cannot catch ForeUP changing its login /
reservation / slot response shape or `BLIND_POST_TEMPLATE` drift. The ONLY guard for that is
`tests/test_foreup_canary.py`, which is `integration`-marked (excluded from CI) and skipped
unless MB creds are present, so it **never runs automatically**. Before a booking weekend, run
it locally with live MB creds (it does NOT book):
```bash
MB_USERNAME=… MB_PASSWORD=… uv run pytest -m integration tests/test_foreup_canary.py -v
```
A failure here means ForeUP drifted — fix the adapter/template BEFORE the 06:00 cron, not during
it. (Tracked as full-repo-scan finding L5: the canary had no operator-runbook home, only a
docstring.)

### 10.5 Prod cutover checklist (in order)

1. **M6 verified in dev** (§10.4) — incl. one clean dev dry-run on a wanted booking day (Sat or Sun).
2. **Credential isolation between dev and prod.** Dev and prod must NOT log into the same
   ForeUP account — concurrent logins (especially at the weekend Sat/Sun 6 AM race) can invalidate
   each other's session. **Resolved by giving dev its own ForeUP account** (set
   `MB-USERNAME`/`MB-PASSWORD` in the dev vault `kv-teetime-dev-s66g` to a separate account;
   done 2026-05-31). With distinct accounts, dev (dry-run) and prod (live) never share a
   session, so dev can keep running — no need to silence it.
   - **NOTE — ACA caches Key Vault secrets.** Updating a KV secret value does NOT
     immediately reach a running job; the job serves the cached value until it is
     redeployed. Each `azure-iac` deploy stamps a new image tag (`teetime:<sha>`), which
     updates the job and re-resolves `keyVaultUrl` secrets to "latest". So after rotating a
     dev/prod credential, trigger a deploy (`workflow_dispatch` environment=dev, or any merge
     touching `infra/**` / `src/**` / `config/**`) and confirm the new value via the next
     run's `logging in as <account>` log line.
   - The `enableSchedules=false` param remains available as an explicit kill-switch (jobs go
     Manual-trigger, never auto-fire) if you ever DO need to fully silence an environment.
3. **Prod bootstrap done** (§10.1.1): `rg-teetime-prod` + SP roles (DONE 2026-05-31).
4. **Prerequisites ready:** a FUNDED 2captcha key + valid Mangrove Bay creds. (The ForeUP
   IP-allowlist risk, §12 Q11, is RESOLVED — Azure IPs are not blocked.) (DONE 2026-05-31.)
5. **First deploy:** push tag `infra/v1.0.0` (manual-approval `prod` environment). It creates the
   ACR/KV/identity/env but **FAILS at the jobs step** because the vault is empty (ACA validates
   `keyVaultUrl` secrets at job creation — see §10.1.1). Expected; the vault is created. (DONE
   2026-05-31 — failed-then-recovered exactly as described.)
6. **Grant yourself KV access + set the 7 secrets** (§10.1.1 steps 5b–6) — the 6 credential
   secrets PLUS `TEETIME-SKIP-DATES` (seed `" "`; ACA validates its KV ref at job-create too) —
   then **re-run the deploy** (`workflow_dispatch` env=prod, approve) — now job creation succeeds
   and the 3 jobs land. (Done 2026-05-31.)
7. **Monitor** the first prod booking day (Sat or Sun): confirm the `race: busy-wait complete` line, a real
   `BOOKED` outcome (NOT dry_run), and the course's confirmation email. Watch for
   `CAPTCHA_BLOCKED` / `AUTH_FAILED` (operator-action outcomes).
8. **Rollback:** if the first run misbehaves, redeploy prod with `enableSchedules=false` (or
   re-enable dev) to stop further attempts while you investigate.
9. **Multi-day activation deploy — DELETE the orphaned `-sun` jobs (DONE — deleted; re-verified
   at the `infra/v2.1.0` deploy 2026-06-10).** The
   prod deploy that first ships the multi-day re-arch creates the renamed `-edt`/`-est` jobs but,
   under ARM **incremental** mode, leaves the old `teetime-job-prod-edt-sun`/`-est-sun` jobs in
   place — they keep firing the old Sunday-only cron and are NOT covered by the killswitch. Run
   the manual orphan-cleanup + verification runbook in **§10.2** immediately after that deploy.

**Notes on two non-blocking observations:**
- **Budget deploy is skipped** by design here: the `Deploy budget` step (`az deployment sub
  create`) is **subscription-scoped**, but the CI service principal is **RG-scoped only**
  (least-privilege), so it fails and the step swallows it as a `::warning::`. So the $20/mo
  budget (`budget.bicep` — both RGs, Actual 80% + Forecasted 100%, §9.2) is NOT auto-created;
  deploy it ONCE manually as the operator (command in §9.2 / budget.bicep header). This is a
  notification only — it does NOT affect the bot. Real spend is ~$5/mo per ACR + free-tier
  compute.
- **"Application Insights Smart Detection"** (a Failure-Anomalies smart-detector alert rule) is
  **auto-created by the Azure platform** alongside App Insights — it is NOT in our Bicep. It
  appears on its own shortly after the App Insights resource sees telemetry; prod will get it
  too. Nothing to add to IaC.

### 10.6 Shared-ACR cutover (one-time — dedicated rg-teetime-shared, see §2.1)

Consolidating to ONE shared ACR in a dedicated `rg-teetime-shared`, with BOTH envs as non-owners.
The prod re-point has a short cutover window (run off-peak, away from 05:50 ET). Order:

1. **One-time setup (operator, done):** `az group create -n rg-teetime-shared`; grant the CI SP
   **Contributor + User Access Administrator** on it; deploy the shared ACR
   (`az deployment group create -g rg-teetime-shared --template-file modules/registry.bicep
   --parameters envName=shared acrSku=Basic jobPrincipalId=''`). Note the ACR name
   (`teetimeshared<suffix>`) → it's the `sharedAcrName` in both param files.
2. **Merge the migration PR.** Dev auto-deploys: re-points to the shared ACR, grants its cross-RG
   AcrPull, builds `teetime-dev:<sha>` there. Verify:
   `az containerapp job show -g rg-teetime-dev -n teetime-job-dev-edt --query "properties.configuration.registries[0].server" -o tsv`
   → the shared ACR login server.
3. **Tag `infra/v*` → prod deploys** (manual-approval gate). Prod re-points to the shared ACR,
   grants its cross-RG AcrPull, builds `teetime:<sha>` there. **Short window:** during the
   two-pass deploy a watch cron firing mid-deploy loses one ~10-min cycle (benign; since
   2026-09-29 later deploys skip pass 1, see §8). Verify the
   prod jobs' `registries[0].server` is the shared ACR + the image exists + the prod MI has
   AcrPull on the shared ACR.
4. **Delete the old prod-resident ACR** (the $5/mo saving): `az acr delete -n teetimeprod<suffix>
   -g rg-teetime-prod --yes` (guard-allowed). The old dev ACR was already deleted in the interim step.
5. **Clean orphans:** stale cross-RG AcrPull assignments (from any RG rebuild) on the shared ACR;
   confirm no leftover per-env ACRs. Done — one ACR in `rg-teetime-shared`, ~$5/mo saved.
   Rollback: revert the PR; each env's next deploy still pulls from the shared ACR (it persists).

---

### 10.7 Multi-user DEV cutover (MU-17, MULTIUSER_PLAN §11/§12)

A params-only change to `main.bicepparam.dev` (plus two small main/webapp wiring changes): dev's
booking jobs run `tenant-run --event mb0600et`, the watcher runs `tenant-watch` (still hourly), the
web app and ACS email are deployed, and the tenant store is the shared Cosmos account's `dev`
database. **Dev stays `dryRun = true`.** (Prod followed at MU-18, §10.8.)

- **Prerequisites (operator, all done 2026-09-26):** `cosmos.bicep` deployed to `rg-teetime-shared`
  + the §7.2a data-plane role assignments; dev KV secrets `TENANT-CREDS-KEYRING`,
  `WEB-SESSION-SECRET`, `OAUTH-GOOGLE-CLIENT-ID`, `OAUTH-GOOGLE-CLIENT-SECRET`,
  `OPERATOR-NOTIFY-EMAIL`; Key Vault Secrets Officer for the CI SP on the dev vault (email.bicep
  writes `ACS-EMAIL-CONNECTION`); `Microsoft.Communication` registered; the Google OAuth client
  (project `teetimebooker`, Testing mode) with redirect URI
  `https://teetime-web-dev.kindwave-5d7c992b.eastus2.azurecontainerapps.io/auth/google/callback`.
- **No two-step for the ACS sender:** with `acsEmailSender = ''` main.bicep derives
  `DoNotReply@<managed domain>` from the email module output (which also orders the jobs and web
  after the module that writes `ACS-EMAIL-CONNECTION`).
- **No email address in the (public) param files:** with `operatorEmail = ''` the web reads
  `TEETIME_OPERATOR_EMAIL` from the `OPERATOR-NOTIFY-EMAIL` secret. Prod needs that secret before
  its own `deployWebApp = true` (MU-18).
- **Verify after the auto-deploy:** the migrate job ran (CI step after pass 2); the web answers
  `/healthz`; the operator signs in with Google, connects the separate dev Mangrove Bay account on
  `/accounts`, saves a ranked weekly booking; the next hourly `teetime-watch-job-dev` run and the
  next 05:50 ET `teetime-job-dev-*` run log the tenant path (dry-run: no booking POST) with exit 0.
- **Rollback:** set `bookingMode`/`watchMode` back to `'toml'` (and `deployWebApp = false` to stop
  the site) and merge; the jobs return to `run`/`watch` on the TOML config.

### 10.8 Multi-user PROD cutover (MU-18, MULTIUSER_PLAN §11 steps 5-7)

Operator-driven. The plan's gate is a Mon-Thu, 09:00-20:00 ET flip, away from the 05:50 ET
drops; prod keeps running the TOML bot until step 3. Two PRs, each shipped by an `infra/v*` tag
(prod requires the manual approval in `azure-iac.yml`).

**0. Operator prerequisites (prod vault `kv-teetime-prod-4jte`, verified missing 2026-09-26):**

| Secret / grant | Value |
|---|---|
| `TENANT-CREDS-KEYRING` | a NEW keyring (never reuse dev's): `{"active":"k1","keys":{"k1":"<openssl rand -base64 32>"}}` |
| `WEB-SESSION-SECRET` | `openssl rand -base64 48` |
| `OPERATOR-NOTIFY-EMAIL` | the operator's address |
| `OAUTH-GOOGLE-CLIENT-ID` / `-SECRET` | the SAME Google client as dev, after adding the prod redirect URI `https://teetime-web-prod.wittydesert-02f9f0cd.eastus2.azurecontainerapps.io/auth/google/callback` |
| Key Vault Secrets Officer on the prod vault | for the CI SP `teetime-iac-ci` (object id `4c27be56-ac00-4026-ac98-8d6d2675160e`), so `email.bicep` can write `ACS-EMAIL-CONNECTION` |

The prod MI already holds its Cosmos data-plane role on `/dbs/prod` (§7.2a).

**1. PR A: infra with the modes still `toml` (plan step 5).** `main.bicepparam.prod`:
`deployWebApp = true`, `deployAcsEmail = true`, `tenantCosmosEndpoint =
'https://cosmos-teetime-shared.documents.azure.com:443/'`, `webPublicBaseUrl =
'https://teetime-web-prod.wittydesert-02f9f0cd.eastus2.azurecontainerapps.io'`; `operatorEmail`
and `acsEmailSender` stay `''`. Tag + approve. Gate: the deploy is green, `/healthz` answers, and
the three TOML jobs are unchanged (`run`/`watch`, same crons).

**2. Seed (plan step 6).** In the PROD site: sign in, connect the operator's Mangrove Bay account
on **Accounts**, save the weekly booking (Sat and Sun 08:45-10:00, party of 4) on **Rules**, then
**Refresh from course** and **Adopt existing bookings** (operator-only, MU-16b, `web/adopt.py`):
it lists the live reservations that match a row's date, party size and time window, and records
them as OWNED (`adopted_owned`) once the confirm box is ticked. Gate: the dashboard shows the next
three weeks correctly and `teetime tenant-plan --event mb0600et` prints what you expect.

**3. PR B: the flip (plan step 7).** `bookingMode = 'tenant'`, `watchMode = 'tenant'`. Tag +
approve on a Mon-Thu. **Immediately after the deploy:** Refresh from course and Adopt again, so an
upgrade the TOML watcher made during the deploy window is recorded as owned (a re-pointed row
shows as `repoint`). Gate: the next watcher run is clean; the first tenant drop passes the §11.2
log-line checklist.

**Rollback:** set both modes back to `'toml'` and tag. TOML still has the MB credentials and
`TEETIME-SKIP-DATES`, and the tenant path's bookings are visible to the TOML pre-book guard, so a
rollback cannot double-book. Diff the web's rules and one-off dates against `container.toml` first
(TOML can express only the Sat/Sun windows).

### 10.9 Prod custom domain (spicyteetimebooker.com, 2026-09-28)

The prod web app answers on `https://spicyteetimebooker.com` and `https://www.spicyteetimebooker.com`
(registered at Cloudflare Registrar, DNS at Cloudflare). ACA managed certificates are free. Bicep
(`webCustomDomain` → `webapp.bicep customDomain`) binds both hosts SNI to certificates named
`mc-<host with dots as dashes>`; a managed certificate cannot be issued until its hostname is on
the app, so those certificates are created ONCE, by hand, BEFORE the first deploy that sets
`webCustomDomain` (that deploy fails loudly if they are missing).

**1. DNS (operator, Cloudflare, every record "DNS only" / grey cloud; proxying breaks issuance):**

| Type | Name | Value |
|---|---|---|
| `A` | `@` | the prod environment's static IP (`az containerapp env show -g rg-teetime-prod -n cae-teetime-prod --query properties.staticIp`) |
| `TXT` | `asuid` | the app's `customDomainVerificationId` (`az containerapp show -g rg-teetime-prod -n teetime-web-prod --query properties.customDomainVerificationId`) |
| `CNAME` | `www` | `teetime-web-prod.<env default domain>` |
| `TXT` | `asuid.www` | the same verification id |

**2. Certificates (once; add the hosts, issue the certs, wait for `Succeeded`):**

The order matters: `hostname add` registers each host on the app (binding `Disabled`) so the
HTTP / CNAME validation of `certificate create` can reach it. Done 2026-09-28; both certificates
are reused by every later deploy.

```bash
RG=rg-teetime-prod; APP=teetime-web-prod; ENV=cae-teetime-prod
az containerapp hostname add -g $RG -n $APP --hostname spicyteetimebooker.com
az containerapp hostname add -g $RG -n $APP --hostname www.spicyteetimebooker.com
az containerapp env certificate create -g $RG -n $ENV --hostname spicyteetimebooker.com \
  --certificate-name mc-spicyteetimebooker-com --validation-method HTTP
az containerapp env certificate create -g $RG -n $ENV --hostname www.spicyteetimebooker.com \
  --certificate-name mc-www-spicyteetimebooker-com --validation-method CNAME
az containerapp env certificate list -g $RG -n $ENV --managed-certificates-only -o table
```

**3. Google OAuth (operator):** add `https://spicyteetimebooker.com/auth/google/callback` to the
Google client's authorized redirect URIs (keep the old one until the deploy is verified).

**4. Deploy:** the `infra/v*` tag carrying `webCustomDomain = 'spicyteetimebooker.com'` and
`webPublicBaseUrl = 'https://spicyteetimebooker.com'`. It binds both hosts and turns on the
canonical-host redirect (www and the old `teetime-web-prod.wittydesert-02f9f0cd.eastus2.azurecontainerapps.io`
host → the apex). Gate: `curl -sI https://spicyteetimebooker.com/healthz` is 200,
`curl -sI https://www.spicyteetimebooker.com/` is a 301 to the apex, and Google sign-in works.

**Renewal:** ACA renews managed certificates automatically while the DNS records stay in place.
**Rollback:** set `webCustomDomain = ''` and `webPublicBaseUrl` back to the azurecontainerapps.io
URL, then tag; the certificates can stay.

### 10.10 Prod mail from hello@spicyteetimebooker.com (2026-09-29)

Two stages, because Azure refuses to link a customer-managed email domain before its DNS records
verify. Cost: none beyond the per-message price the managed domain already pays; Cloudflare DNS
and Email Routing are free.

1. **Stage 1 (Bicep, prod release).** `emailCustomDomain = 'spicyteetimebooker.com'`,
   `emailCustomDomainLinked = false` (prod bicepparam). The deploy creates the
   `CustomerManaged` domain on `acs-email-teetime-prod`; the site still sends as
   `DoNotReply@<…>.azurecomm.net`.
2. **Read the records** (read-only; the `az communication` CLI extension is needed): the
   `emailCustomDomainRecords` deployment output, or `az communication email domain show -g rg-teetime-prod --email-service-name
   acs-email-teetime-prod -n spicyteetimebooker.com --query properties.verificationRecords`.
   Domain (TXT at `@`), SPF (TXT at `@`, exactly `v=spf1 include:spf.protection.outlook.com
   -all`), DKIM and DKIM2 (CNAMEs `selector1/2-azurecomm-prod-net._domainkey`).
3. **Cloudflare DNS** (operator-approved, all **DNS only**): add those four records, plus DMARC
   `_dmarc` TXT `v=DMARC1; p=none; rua=mailto:hello@spicyteetimebooker.com`. SPF must be the
   EXACT Azure value at verification time (Azure rejects extra includes).
4. **Verify** (operator-approved Azure write): once DNS resolves, `az communication email
   domain initiate-verification … --verification-type Domain` (then SPF, DKIM, DKIM2); poll
   `properties.verificationStates` until each is `Verified`.
5. **Cloudflare Email Routing** for `hello@` → the operator's Gmail (operator verifies the
   destination by clicking Cloudflare's email). It adds MX records and wants its own SPF include:
   merge it into the ONE apex SPF record only AFTER Azure shows SPF `Verified`:
   `v=spf1 include:spf.protection.outlook.com include:_spf.mx.cloudflare.net -all`.
6. **Stage 2 (Bicep, prod release).** `emailCustomDomainLinked = true`: the domain is linked to
   `acs-teetime-prod`, the `hello` sender ("Spicy's Tee Time Booker") is created, and
   `email.bicep`'s `senderAddress` output makes every prod email come from
   `hello@spicyteetimebooker.com`. Send a test (an invitation to yourself) to confirm.

## 11. Security checklist

| Item | Status | Detail |
|---|---|---|
| No plaintext secrets in Bicep | Required | All secrets via Key Vault reference; `main.bicepparam.*` files contain no secret values |
| No secrets in container env vars (direct) | Required | All env vars are `secretRef:` pointing to Key Vault references |
| Key Vault soft-delete | On (90 days, default) | Confirmed default for new vaults created since 2019 |
| Key Vault purge protection | **Dev: disabled (fast iteration); Prod: enabled** | NOT on by default; must be set for prod; irreversible once enabled |
| Key Vault audit logging | On (both envs) | `keyvault.bicep` ships a `diagnosticSettings` (categoryGroup `audit` = AuditEvent) to the Log Analytics workspace — the forensic record of who/what read which secret. Audit volume is tiny (well under the 5 GB/mo free tier). |
| ACA Job has no public ingress | By design | ACA Jobs (scheduled trigger type) do NOT expose HTTP ingress — unlike Container Apps services, which can have HTTP listeners. There is no public endpoint, no port binding, and no inbound network surface for the job resources. |
| Outbound-only network | By design | Bot makes outbound HTTPS to ForeUP only; no inbound surface |
| VNet integration | Not required for v0/v1 | ForeUP is a public internet endpoint; VNet adds cost and complexity with no security benefit |
| ACR authentication | Managed identity (AcrPull) | No registry password in job config; admin account disabled on ACR |
| RBAC minimum privilege | Key Vault Secrets User (read only), AcrPull (read only), custom "ACA Job Schedule Manager" (killswitch Logic App MI) | Each role is scoped to the specific resource or RG. The killswitch custom role grants only Microsoft.App/jobs/read + write + stop/action — NOT Contributor. No storage RBAC needed — bot makes no Azure SDK calls at runtime. |
| Killswitch custom role | "ACA Job Schedule Manager" (operator creates, subscription-scoped) | Actions: Microsoft.App/jobs/read + write + stop/action. Assigned to Logic App system-assigned MI on rg-teetime-dev + rg-teetime-prod. See §9.2 for the az CLI command. |
| CI service principal | Contributor + User Access Admin, RG-scoped | Not subscription-level Contributor |
| OIDC auth (no client secrets in GitHub) | Required | GitHub stores only AZURE_CLIENT_ID, AZURE_TENANT_ID, AZURE_SUBSCRIPTION_ID |
| Credit-card data | Platform-specific | ForeUP keeps card on file → bot never sends PAN/CVV. **TeeItUp has no wallet → the TeeItUp adapter DOES POST PAN/CVV/expiry/billing to tr.gnsvc.com** (from env vars, never committed); card fields are dropped by `redact_payload` at the `append_attempt` store boundary on every attempt_log write (PLAN.md §10.1), and the card POST uses `follow_redirects=False`. |
| PII redaction in logs | Inherited from v0 | PLAN.md §10.1 rules apply; attempt_log is in Log Analytics (stdout) |

---

## 12. Open questions for the user

The following items cannot be resolved without operator input. The stubs in
`infra/bicep/` use placeholder values; these must be filled before first deploy.

| # | Question | Where it's needed |
|---|---|---|
| 1 | ~~**Azure AD tenant ID**~~ — **RESOLVED: `5151757e-ef5b-42a5-a09b-6410b40b2186`** | `azure-iac.yml` AZURE_TENANT_ID secret; OIDC setup |
| 2 | ~~**Azure subscription ID**~~ — **RESOLVED: `3f82c7e1-4b1b-4a55-b905-d79f65c6887d`** | `azure-iac.yml` AZURE_SUBSCRIPTION_ID secret; budget.bicep deploy |
| 3 | ~~**Preferred environment names**~~ — **RESOLVED: `dev`/`prod`** confirmed | `main.bicepparam.*` filenames and resource name suffixes |
| 4 | **Budget alert email address** — set to the operator's real email at deploy time (passed as a `budget.bicep` parameter / bicepparam value, not committed in plaintext); confirm or override | `budget.bicep` parameter |
| 5 | ~~**GitHub repo owner/name**~~ — **RESOLVED: `wardcrazy01894/TeeTimeBooker`**. OIDC subject claims updated. | OIDC federated credential `subject` field |
| 6 | ~~**Dockerfile needed?**~~ — **RESOLVED: created at `Dockerfile` + `config/container.toml` + `.dockerignore`**. Notifications backend is `console` (stdout only). No blob state manager. | `registry.bicep` + `azure-iac.yml` build step |
| 7 | **ACR name** must be globally unique in Azure. Proposed: `teetime{envName}{shortId}` where `shortId` is a 4-char hash of the subscription ID. Confirm or override. | `registry.bicep` |
| 8 | ~~**Storage account name**~~ — **MOOT (storage module removed).** No storage account is provisioned. State is in-process only. | N/A |
| 9 | **Key Vault name** must be globally unique, 3–24 chars. Proposed: `kv-teetime-{envName}-{shortId}`. Confirm or override. | `keyvault.bicep` |
| 10 | ~~**SMTP credentials**~~ — **CUT.** Email notifications removed from scope. Console (stdout) is the only notifier. The golf course sends booking confirmations directly to the player. | N/A |
| ~~11~~ | ~~**ForeUP IP allowlist / bot-detection risk**~~ — **RESOLVED / OBSERVED (2026-05-31): ForeUP does NOT block the Azure (East US 2) egress IPs.** Both the dev and prod watch jobs log into ForeUP from ACA every 10 min and succeed (`POST .../login "HTTP/1.1 200 OK"`, `ForeUP: login successful`, tee-time fetch returns slots). No 403 / block / challenge observed. Residual: sustained-polling rate-limit over many days is still worth a passive eye (Spike S5), but the IP-block concern is empirically cleared. Fallback if it ever changes: NAT Gateway with a static egress IP. | Resolved (observed in dev + prod) |
| 12 | **Spike S1 — ACA *Job* KV-secret refresh latency** (LEADTIME_SKIP_PLAN F2). §7.5 ships the conservative "edit the night before" guidance and cites §7.4 (KV refs are not version-pinned → next job execution re-resolves). The exact propagation latency for a Portal edit of `TEETIME-SKIP-DATES` into a *Job* execution is documented for Container *Apps* (~30 min) but not freshly verified for *Jobs*. Confirm on live Azure (edit the secret, then time how soon a triggered watch run's `targets=[…]` reflects it) before committing a latency NUMBER in §7.5. The feature ships either way; only the documented number depends on this. | `AZURE_PLAN.md §7.5` runbook latency claim |
