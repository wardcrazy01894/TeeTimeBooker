# TeeTimeBooker — Azure Serverless Hosting Plan (v1)

> **Scope.** This document covers the Azure infrastructure that replaces the
> v0 GitHub Actions runner-hosted execution. (The v0 cron workflows `book.yml` /
> `watch-tee-time.yml` were removed in #43; scheduling now runs as ACA Jobs, and the
> M6 runtime wiring — `--wait`, `core/dst_gate.py`, `core/target_date.py` — landed in
> `src/`.) The bot binary is container-packaged and run as an Azure Container Apps Job on a
> cron schedule. All decisions listed in the task brief are treated as settled;
> this document addresses the "anticipate-the-reviewer" items explicitly.

---

## 1. Architecture overview

```
  GitHub Actions CI (azure-iac.yml)
  ┌───────────────────────────────────┐
  │ bicep build → what-if → deploy    │
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

---

## 3. Module layout

**Status: ALL modules implemented (storage module removed — state is in-process only). Cost killswitch (PR-KS1) implemented.**

```
infra/
  AZURE_PLAN.md                # this file
  COST_KILLSWITCH_PLAN.md      # verified design for the $50 automated killswitch chain
  bicep/
    main.bicep                 # entry point; orchestrates all modules; accepts envName + location params
    main.bicepparam.dev        # dev environment parameter values
    main.bicepparam.prod       # prod environment parameter values
    modules/
      identity.bicep           # user-assigned managed identity for the Container Apps Jobs
      registry.bicep           # ACR Basic; grants AcrPull to the job MI
      keyvault.bicep           # Key Vault Standard; grants Key Vault Secrets User to the job MI; soft-delete 90d
      logs.bicep               # Log Analytics Workspace + Application Insights; linked to ACA env
      compute.bicep            # ACA Environment (Consumption) + 2× booking ACA Jobs (DST crons) + watch ACA Job
      budget.bicep             # Cost Management budget ($20/mo, both RGs; Actual 80% + Forecasted 100%); subscription-scoped
                               #   also conditionally deploys budget-teetime-killswitch ($50 actual → killswitch Action Group) when killswitchActionGroupId is supplied
      killswitch.bicep         # Cost killswitch: Logic App (Consumption) + Action Group + RBAC; deployed to rg-teetime-dev only
                               #   12 HTTP actions: 6 PATCH (Schedule→Manual) + 6 POST /stop; all 3 jobs × 2 envs
                               #   gated: enableKillswitch && !empty(killswitchRbacRoleId) && envName=='dev'
                               #   DONE 2026-05-31: custom role created (GUID 3e2d5a14-96bd-4469-9f96-b9c3270aa9e6 set in param files + azure-iac.yml); killswitch live in dev
      killswitch-rbac-prod.bicep  # companion: cross-RG role assignment for rg-teetime-prod
                               #   deployed as nested module by killswitch.bicep with scope: resourceGroup(sub, prodRgName)
.github/workflows/
  azure-iac.yml                # ACTIVE CI: bicep build + what-if on PR; deploy on merge to main (dev) / tag (prod)
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

### Hard-coded (architectural constants, not env-specific)

| Constant | Value | Reason |
|---|---|---|
| KV secret names | `MB-USERNAME`, `MB-PASSWORD`, `PLAYER1-EMAIL`, `PLAYER1-PHONE`, `PLAYER1-MB-MEMBER`, `TWOCAPTCHA-API-KEY` | Bot reads these by name; names are part of the interface contract |
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
500ms)`) carries over unchanged. The ACA jitter is a tighter bound than GH
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
- Use a slim base image (`python:3.12-slim`, not `python:3.12`). Target < 300 MB
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

`compute.bicep` includes the watch job `Microsoft.App/jobs` resource
(implemented as part of M-azure-T1, now DONE).

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

**⚠️ ONE-TIME PRE-DEPLOY STEP (required before PR5 / the bicep change lands).** `compute.bicep`
references this secret via `keyVaultUrl`, and **ACA validates KV secret refs at job-CREATE time**
— so the secret MUST already exist or the deploy fails (`InvalidParameterValueInContainerTemplate`).
Dev **auto-deploys on merge**, so create it in BOTH vaults **before merging**:
```
az keyvault secret set --vault-name <kv-dev>  --name TEETIME-SKIP-DATES --value ""
az keyvault secret set --vault-name <kv-prod> --name TEETIME-SKIP-DATES --value ""
```
(`--value ""` seeds it empty = no skips. These are operator-run; the agent is hard-blocked from
`az keyvault secret set`.)

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
To confirm a JOB sees it, trigger/await a watch run and read its log: the `Watch check: targets=[…]`
line will OMIT a skipped date. (`az containerapp job start` is operator-only — guard-blocked.)

---

## 8. CI validation pipeline (pre-emption item 9)

The file `.github/workflows/azure-iac.yml` is the active deploy workflow (alongside
`ci.yml` for lint/test). The v0 `book.yml` / `watch-tee-time.yml` cron workflows were
removed in #43.

### 8.1 Trigger strategy

| Trigger | Action |
|---|---|
| `pull_request` touching `infra/**` or `.github/workflows/azure-iac.yml` | `bicep build` lint + `az deployment group what-if` (read-only) |
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
4. Save.

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

**Stance:** treat `what-if` output as advisory on PR. The workflow prints the
what-if output and continues; it does not fail the PR on what-if change
detection. Only `bicep build` failures block the PR. The deploy step (`create`)
is the source of truth for idempotency.

`az deployment group create` with identical Bicep and parameters is fully
idempotent for all resources in this plan. ACA Jobs do NOT create new revisions
on redeploy unless the container image tag or environment configuration changes.
To force a new execution of the job with a new image, update `containerImage`
in the parameter file and redeploy — this is the intended release workflow.

---

## 9. Cost estimate (pre-emption item 12)

### 9.1 Per-component breakdown (East US 2, April 2026)

| Component | SKU | Monthly cost | Notes |
|---|---|---|---|
| Container Apps Job compute | Consumption | **$0.00** | Free tier: 180,000 vCPU-s/month, **shared per-subscription across BOTH envs** (not per-env). Booking: ~11-min busy-wait run × 0.25 vCPU ≈ 165 vCPU-s × ~8-9 weekend runs/mo (Sat+Sun) ≈ 1.5k. Watch (every 10 min): ~4,320 runs/env × 0.25 vCPU; billed on the full replica lifetime (cold start + image pull + Python startup), realistically ~45-60 s/run not the ~30 s of actual work ≈ ~50k vCPU-s/env. Both envs combined ≈ **~110k vCPU-s/mo ≈ ~60-65% of the shared free grant** (~35-40% headroom — NOT the ~80% an optimistic 30 s/run implies). The every-10-min watch job, not the booker, is the dominant consumer and the first thing to push past the grant if cadence or course count grows. |
| Container Apps Job memory | Consumption | **$0.00** | Free tier: 360,000 GiB-s/month, shared per-subscription. Same run profile at 0.5 GiB ≈ ~110k GiB-s/env; both envs combined ≈ **~219k GiB-s/mo ≈ ~61% of the shared free grant**. |
| Container Apps Environment | Consumption | **$0.00** | No per-environment fee on Consumption plan. |
| Azure Container Registry | Basic | **~$5.00** | $5.00/mo flat for Basic SKU. Includes 10 GiB storage. Our image is ~300 MB; well within limits. Every merge pushes a new `teetime:<sha>` tag, so a weekly `acr purge` ACR task (`registry.bicep`, keep last 10 tags + reap untagged) caps unbounded storage growth before it can approach the 10 GiB allowance. |
| Key Vault | Standard | **~$0.03** | $0.03/10k operations. ACA caches KV-referenced secrets (~30-min refresh, not per-execution), so ≈ 6 secrets × ~48 refreshes/day × 30 ≈ ~9k reads/month. Negligible — around the 10k mark, well under $0.10. |
| Log Analytics | Pay-per-use | **~$0.00–$0.50** | First 5 GB/month free. Bot produces <10 MB logs/month. |
| Application Insights | Pay-per-use | **~$0.00** | First 5 GB/month free. |
| Network egress | — | **~$0.00** | First 100 GB/month free. Bot does <10 MB/run. |
| **Total (dev or prod)** | | **~$5.01–$5.51/mo per env** | Well within the $20/mo budget ceiling (covers both envs). |

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

Tier 1 (`budget-teetime`, $20, email-only) is UNCHANGED. Tier 2 (`budget-teetime-killswitch`,
$50, killswitch-trigger) is a SEPARATE second budget resource in `budget.bicep` (conditional on
`killswitchActionGroupId`). Both budgets evaluate the same project spend independently. See
`infra/COST_KILLSWITCH_PLAN.md`.

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
1. PR opens → `azure-iac.yml` runs `bicep build` + `what-if`.
2. Merge to `main` → `azure-iac.yml` **auto-deploys to dev** (no reviewer gate; see §8.1).
   Dev always runs in dry-run (`dryRun = true` in parameter file).
3. Tag `infra/v*` → `azure-iac.yml` deploys to prod (requires manual approval).

> **⚠️ Multi-day cutover — manual orphan cleanup required.** The next prod `infra/v*` tag
> renames the booking jobs `teetime-job-prod-edt-sun`/`-est-sun` → `-edt`/`-est`. Deploys run
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
6. **Grant yourself KV access + set the 6 secrets** (§10.1.1 steps 5b–6), then **re-run the
   deploy** (`workflow_dispatch` env=prod, approve) — now job creation succeeds and the 3 jobs
   land. (Done 2026-05-31.)
7. **Monitor** the first prod booking day (Sat or Sun): confirm the `race: busy-wait complete` line, a real
   `BOOKED` outcome (NOT dry_run), and the course's confirmation email. Watch for
   `CAPTCHA_BLOCKED` / `AUTH_FAILED` (operator-action outcomes).
8. **Rollback:** if the first run misbehaves, redeploy prod with `enableSchedules=false` (or
   re-enable dev) to stop further attempts while you investigate.
9. **Multi-day activation deploy (next `infra/v*` tag) — DELETE the orphaned `-sun` jobs.** The
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

---

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
