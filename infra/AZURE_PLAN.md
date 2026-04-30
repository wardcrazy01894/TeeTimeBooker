# TeeTimeBooker — Azure Serverless Hosting Plan (v1)

> **Scope.** This document covers the Azure infrastructure that replaces the
> v0 GitHub Actions runner-hosted execution. v0 source code (`src/`, `tests/`,
> `PLAN.md`, `CLAUDE.md`, `.github/workflows/book.yml`) is unchanged. The bot
> binary is container-packaged and run as an Azure Container Apps Job on a
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
  │             └───────────────┼─►│  (Scheduled trigger)   │  │  │
  │                             │  │  parallelism=1          │  │  │
  │                             │  │  replicaCompletion=1    │  │  │
  │                             │  │  2 cron entries (DST)   │  │  │
  │                             │  └────────┬───────────────-┘  │  │
  │                             │           │ user-assigned MI   │  │
  │                             └───────────┼───────────────────┘  │
  │                                         │                       │
  │  ┌──────────────────────┐               │ RBAC: KV Secrets User │
  │  │  Azure Key Vault     │◄──────────────┘                       │
  │  │  (Standard)          │               ┌───────────────────┐   │
  │  │  MB_USERNAME         │               │  Blob Storage     │   │
  │  │  MB_PASSWORD         │               │  (LRS, Hot)       │   │
  │  │  SMTP_HOST/USER/PASS │               │  container:       │   │
  │  │  PLAYER* secrets     │               │    teetime-state  │   │
  │  └──────────────────────┘               │  blob:            │   │
  │                                         │    teetime.db     │   │
  │  ┌──────────────────────┐               │  (blob lease held │   │
  │  │  Log Analytics WS    │               │   during run)     │   │
  │  │  + App Insights      │               └───────────────────┘   │
  │  └──────────────────────┘                                       │
  │                                                                │
  │  ┌──────────────────────┐                                       │
  │  │  Cost Management     │                                       │
  │  │  Budget ($10/mo)     │                                       │
  │  │  (subscription scope)│                                       │
  │  └──────────────────────┘                                       │
  └────────────────────────────────────────────────────────────────┘
                    │
                    │ outbound HTTPS only
                    ▼
         foreupsoftware.com (ForeUP API)
         SMTP relay (email notification)
```

**One-line summary.** Two ACA Jobs (one per DST half) fire 10 minutes before
6:00 AM ET daily in UTC cron. Each job pulls the bot image from ACR using a
user-assigned managed identity, downloads the SQLite state blob from Blob
Storage (acquiring a 60-second renewable blob lease), runs the booking logic,
and uploads the updated blob on exit — mirroring the v0 `actions/cache` pattern.
Secrets flow from Key Vault into the job container as environment variables via
native `keyVaultUrl` secret references, resolved at container start by the ACA
platform using the same user-assigned MI.

---

## 2. Service selection

| Concern | Chosen | Rejected | Rejection reason |
|---|---|---|---|
| Compute | **Azure Container Apps Jobs (Consumption)** | Azure Functions Consumption | Cold-start variance (documented 0–60s) breaks the T0 busy-wait window — see §5.1 |
| Compute | (same) | Azure Functions Premium EP1 | ~$146/mo, 29× cost ceiling; no benefit for single daily 5-min job |
| Compute | (same) | Azure Logic Apps | No native Python; JSON-based workflows can't run the bot code |
| State persistence | **Azure Blob Storage (LRS Hot)** | Azure Cosmos DB | Overkill; $24+/mo minimum RU reservation |
| State persistence | (same) | Azure Table Storage | No ACID multi-row transactions; SQLite gives us that for free |
| State persistence | (same) | Azure Files | SMB mount latency introduces unnecessary complexity for a blob download pattern |
| Secrets | **Azure Key Vault (Standard)** | GitHub Actions secrets in env | v1 is no longer GitHub-runner-hosted; secrets must live in Azure |
| Secrets | (same) | Hardcoded Bicep parameters | Hard no — plaintext secrets in IaC state |
| IaC | **Bicep** | Terraform | Bicep is first-class on Azure; no external state backend needed; less tooling overhead for single-cloud shop |
| Auth (CI→Azure) | **OIDC federated credential** | Service principal client secret | Secrets stored in GitHub = rotation burden; OIDC is credential-free |
| Container registry | **ACR Basic** | Docker Hub | Private registry; managed identity pull avoids credentials; Basic is sufficient for 1-2 image tags |
| Region | **East US 2** | Other regions | Closest to Mangrove Bay / ForeUP origin; lowest egress latency to foreupsoftware.com |

---

## 3. Module layout

```
infra/
  AZURE_PLAN.md                # this file
  bicep/
    main.bicep                 # entry point; orchestrates all modules; accepts envName + location params
    main.bicepparam.dev        # dev environment parameter values
    main.bicepparam.prod       # prod environment parameter values
    modules/
      identity.bicep           # system-assigned managed identity for the Container Apps Job
      registry.bicep           # ACR Basic; grants AcrPull to the job MI
      storage.bicep            # Blob Storage account (LRS Hot) + container 'teetime-state'; soft-delete 7d
      keyvault.bicep           # Key Vault Standard; grants Key Vault Secrets User to the job MI; soft-delete 90d
      logs.bicep               # Log Analytics Workspace + Application Insights; linked to ACA env
      compute.bicep            # Container Apps Environment (Consumption) + Container Apps Job (two cron entries)
      budget.bicep             # Cost Management budget ($10/mo at 80% alert); subscription-scoped
  ci/
    azure-iac.yml              # GH Actions workflow: bicep build + what-if on PR; deploy on tag push
```

**Dependency order for `az deployment group create` (RG-scoped):**
`identity` → `registry` + `storage` + `keyvault` + `logs` → `compute`

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
| `budgetAmountUsd` | int | `10` | `10` | Cost ceiling per env |
| `budgetAlertEmail` | string | operator email | operator email | Cost alert recipient |
| `acrSku` | string | `Basic` | `Basic` | Allow upgrade to Standard later |
| `kvSku` | string | `standard` | `standard` | Allow upgrade if HSM needed |

### Hard-coded (architectural constants, not env-specific)

| Constant | Value | Reason |
|---|---|---|
| Blob container name | `teetime-state` | Code references this name; changing it is a code+infra change |
| Blob name | `teetime.db` | Same |
| KV secret names | `MB-USERNAME`, `MB-PASSWORD`, `SMTP-HOST`, `SMTP-USER`, `SMTP-PASS`, `PLAYER1-EMAIL`, etc. | Bot reads these by name; names are part of the interface contract |
| RBAC role IDs | `Key Vault Secrets User` = `4633458b-17de-408a-b874-0445c86b69e6`; `AcrPull` = `7f951dda-4ed3-4680-a7ca-43fe172d538d` | Stable Azure built-in role GUIDs |
| `parallelism` | `1` | Never run two replicas of the booking job simultaneously — see §6 |
| `replicaCompletionCount` | `1` | Pair with parallelism=1; see §6 |
| `replicaRetryLimit` | `0` | Bot handles its own retry logic; ACA-level retry would re-enter booking without idempotency guard |
| `replicaTimeout` | `900` (15 min) | Matches v0 `timeout-minutes: 15` |
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
The identical two-cron pattern from `book.yml` is required and is implemented
in `compute.bicep`:

| ET target | UTC cron | Description |
|---|---|---|
| 05:50 EDT (UTC-4) | `50 9 * * *` | Fires 10 min before T0 in EDT half of year |
| 05:50 EST (UTC-5) | `50 10 * * *` | Fires 10 min before T0 in EST half of year |

Both crons fire every day year-round. The bot's own DST gate — identical to
the Python `dst` step in `book.yml` — runs as the first statement in the
container entry point:

```python
# Pseudocode — same logic as book.yml:dst step
now = datetime.now(ZoneInfo("America/New_York"))
if now.hour != 5:          # wrong cron half; container exits 0 immediately
    sys.exit(0)
```

This gate is already part of the `teetime run` CLI in v0 (the orchestrator
checks wall-clock ET before entering busy-wait). No new code is required for
v1; only the scheduling primitive changes from GH Actions to ACA.

**The gate is not optional.** Without it, both crons would fire the full
booking logic, and the wrong-half run would arrive at T0 ± 1 hour, bypassing
the idempotency check (same RequestId but wrong resolved_date) and potentially
booking the wrong day.

---

## 6. State persistence (pre-emption items 4 & 10)

### 6.1 Blob layout

```
Storage Account: teetime{envName}sa (LRS, Hot)
  Container: teetime-state
    Blob: teetime.db          ← the SQLite file; binary, ~100 KB typical
```

The blob is downloaded at job start into a local ephemeral path
(`/tmp/teetime.db` or the equivalent within the job container), used for the
run, then uploaded back at job end (`if: always()` equivalent = the bot's
`finally:` block in the orchestrator). This is the direct functional
equivalent of v0's `actions/cache/restore` + `actions/cache/save`.

### 6.2 Blob lease semantics (concurrency safety)

The SQLite file is per-blob-download. If two ACA Job replicas ran
simultaneously and both downloaded, modified, and uploaded the blob, the last
writer would silently win and clobber the other's attempt_log entries.

**Primary defense:** `parallelism = 1` and `replicaCompletionCount = 1` are
set in the job's `scheduleTriggerConfig`. This pins each cron execution to
exactly one replica. ACA does not launch a second replica for the same
execution under these settings.

**Secondary defense (belt-and-suspenders):** The bot acquires an exclusive
blob lease at download time and holds it for the duration of the run, releasing
on upload. Azure Blob Storage enforces exclusive write access for lease
holders: any upload without the lease ID is rejected with HTTP 412 Precondition
Failed. This catches the theoretical scenario where a manually triggered
`az containerapp job start` overlaps with the scheduled run.

Lease implementation (60-second finite lease with renewal thread):
- Acquire lease: `BlobLeaseClient.acquire(lease_duration=60)`. 60 seconds is
  the chosen duration. Infinite leases (`-1`) are NOT used — Azure does not
  auto-expire them, so a container crash would strand the lease indefinitely.
- Renew on a background `threading.Thread` every 30 seconds (half the lease
  duration) while the job is running. `threading.Thread` is used, not asyncio,
  because the bot's `busy_wait_until` tight loop (1 ms cadence) would starve
  an asyncio task scheduled on the same event loop. A daemon thread runs
  independently and calls `BlobLeaseClient.renew()` on its own cadence.
- Upload with lease ID: pass `lease` parameter to `upload_blob`.
- Release on exit: stop the renewal thread, then call `BlobLeaseClient.release()`
  in the `finally:` block of the orchestrator.
- If the container dies mid-run (crash, OOM kill): the renewal thread dies with
  the process. Azure auto-expires the lease in at most 60 seconds. The next
  scheduled run acquires a fresh lease cleanly. The blob content is unchanged
  (the upload only happens after successful run completion in the `finally:`
  block), so state integrity is preserved.

**Threading model summary:** The lease renewal is a `threading.Thread(daemon=True)`
started after successful lease acquisition and stopped (via a `threading.Event`)
before the upload. It does not interact with the bot's `busy_wait_until` or the
orchestrator's booking logic. The thread is the sole writer to the lease; the
main thread is the sole writer to the blob content. No cross-thread state sharing
beyond the stop event is required.

The bot's existing `request_lock` (PLAN.md §9 layer 5) continues to serve as
the advisory lock within a single run. It does not protect across replicas
because the SQLite file is local to each replica; the blob lease is the
cross-replica guard.

### 6.3 Blob and container soft-delete (disaster recovery)

Both **blob soft-delete** and **container soft-delete** are enabled with 7-day
retention in `storage.bicep`. These are separate properties:

- `deleteRetentionPolicy` (blob soft-delete): protects individual blobs.
  If `teetime.db` is deleted or corrupted, recover with `az storage blob undelete`.
- `containerDeleteRetentionPolicy` (container soft-delete): protects the
  `teetime-state` container itself. If the container is accidentally deleted,
  blob soft-delete alone cannot recover it — the container must be restored
  first via `az storage container-rm undelete` (or portal), then the blob
  within it can be undeleted. Setting both properties closes this gap.

**Cache-eviction equivalent on Azure.** There is no equivalent to GH Actions'
cache eviction on Blob Storage: the blob persists until explicitly deleted.
PLAN.md §9.2's catastrophic-eviction scenario does not apply to v1. If the
blob is absent (first run, or operator-deleted), the bot initializes a fresh
DB, then §9 layer 2 (`list_reservations`) catches any pre-existing reservations
before re-POSTing. Same behavior as v0.

### 6.4 v1 code changes required (new in round 2)

The v0 `SqliteStore` knows nothing about Azure Blob Storage. The following
code changes are required for v1 to work — they are NOT optional and are NOT
handled by the Bicep IaC. This section names them explicitly so the
implementation agent has a clear task list.

**New Python module: `src/teetime/persistence/blob_state_manager.py`**

Implements the Blob Storage download/upload/lease lifecycle. Key responsibilities:

```
class BlobStateManager:
    def __init__(self, account_name: str, container: str, blob_name: str) -> None
    def __enter__(self) -> Path          # download blob → /tmp/teetime.db, acquire lease, start renewal thread
    def __exit__(self, ...) -> None      # upload blob (with lease), release lease, stop renewal thread
```

- Uses `azure-storage-blob` Python SDK (`BlobServiceClient`, `BlobLeaseClient`).
- Auth: `DefaultAzureCredential` — picks up the user-assigned MI automatically
  via `AZURE_CLIENT_ID` env var (the MI client ID). No connection string or
  account key. Env var `AZURE_STORAGE_ACCOUNT_NAME` is the only config needed.
- Implements the 60-second lease + 30-second renewal thread from §6.2.
- On first run (blob does not exist): initializes a fresh SQLite DB, then §9
  layer 2 (`list_reservations`) checks for pre-existing reservations.
- Used as a context manager wrapping the orchestrator's `run()` call in the
  CLI entrypoint.

**New Python dependency: `azure-storage-blob`**

Add to `pyproject.toml` dependencies. The SDK is ~10 MB installed, well within
the image size budget. `azure-identity` is also required for `DefaultAzureCredential`
(check whether it is already a transitive dep; if not, add it explicitly).

Do NOT use `azure-cli` (`az` CLI) for blob I/O. The CLI is ~150 MB and adds no
benefit over the Python SDK. The container image must not include it.

**Container entrypoint**

The CLI entrypoint (`teetime run`) must be updated to instantiate
`BlobStateManager` using `os.environ["AZURE_STORAGE_ACCOUNT_NAME"]` and
`os.environ.get("AZURE_CLIENT_ID")` and use it as a context manager. When
`AZURE_STORAGE_ACCOUNT_NAME` is absent (local dev, v0 GH Actions), the
entrypoint falls back to a local file path (existing behavior). This makes
the blob manager opt-in via env var, keeping backward compatibility.

**Open milestone task for implementation agent:**
- Write `blob_state_manager.py` stub with `NotImplementedError` (red phase).
- Write tests: lease acquisition, renewal thread, upload-on-exit, crash recovery
  (lease expiry), first-run fresh DB init. Use `pytest` + `unittest.mock` for
  the Azure SDK calls; no live Azure required.
- Implement green phase. TDD loop as per CLAUDE.md.

---

## 7. Secrets & identity (pre-emption items 6, 7, 11)

### 7.1 Key Vault secret tree

| Secret name | Contains | Used by |
|---|---|---|
| `MB-USERNAME` | Mangrove Bay / ForeUP login username | Bot env var `MB_USERNAME` |
| `MB-PASSWORD` | Mangrove Bay / ForeUP login password | Bot env var `MB_PASSWORD` |
| `SMTP-HOST` | SMTP relay hostname | Bot env var `SMTP_HOST` |
| `SMTP-USER` | SMTP login username | Bot env var `SMTP_USER` |
| `SMTP-PASS` | SMTP login password | Bot env var `SMTP_PASS` |
| `PLAYER1-EMAIL` | Player 1 email (PII) | Bot env var `PLAYER1_EMAIL` |
| `PLAYER1-PHONE` | Player 1 phone (PII) | Bot env var `PLAYER1_PHONE` |
| `TWOCAPTCHA-API-KEY` | 2captcha.com API key for CAPTCHA solving | Bot env var `TWOCAPTCHA_API_KEY` |

Additional `PLAYER*` secrets follow the same pattern. The set of secrets is
determined by the config file (`config/local.toml`) which references env var
names; the Key Vault must contain matching secrets.

**`STORAGE-CONN-STR` is NOT stored in Key Vault.** Storage connection strings
contain a shared account key — a long-lived credential with full account access.
The bot uses `Storage Blob Data Contributor` via the user-assigned MI and
`DefaultAzureCredential`. The storage account name is passed as a plain
(non-secret) env var `AZURE_STORAGE_ACCOUNT_NAME`. This is strictly better than
a connection string: the MI credential is short-lived, scoped to the specific
storage account, and cannot be exfiltrated as a static string.

KV secret names use hyphens (Azure KV convention); the bot's config references
the env var names with underscores. The mapping is 1:1 via the `secretRef`
→ env var assignment in `compute.bicep`.

### 7.2 Managed identity and RBAC

The Container Apps Job uses a **user-assigned managed identity** (created by
`identity.bicep`). This is the default and only supported path in v1.

**Why user-assigned, not system-assigned:**
A system-assigned MI's `principalId` is unavailable until after the ACA job
resource is created. This makes it impossible to pre-stage RBAC assignments
for Key Vault, ACR, and Storage in the same Bicep deployment — you need either
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
| `Storage Blob Data Contributor` (ba92f5b4-…) | Storage Account | The storage account in the same RG (needed for blob lease + upload) |

Assignments are declared in `keyvault.bicep`, `registry.bicep`, and
`storage.bicep` respectively, referencing the job's `principalId` via module
output. All assignments use `roleAssignmentCondition: none` (no ABAC
conditions needed). Legacy Key Vault access policies are NOT used.

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
az containerapp job start --name teetime-job-<envName> --resource-group rg-teetime-<envName>
```
This triggers a manual execution that will pick up the new secret.

**CRITICAL:** purge protection is NOT enabled by default on new Key Vaults
(soft-delete IS enabled by default with 90-day retention). We explicitly set
`enablePurgeProtection: true` in `keyvault.bicep`. This prevents permanent
secret deletion during the purge protection period and is a one-way operation
— once enabled, it cannot be disabled for the vault's lifetime.

---

## 8. CI validation pipeline (pre-emption item 9)

The file `infra/ci/azure-iac.yml` is a new GitHub Actions workflow separate
from `book.yml`. It never touches `book.yml` or any v0 workflow.

### 8.1 Trigger strategy

| Trigger | Action |
|---|---|
| `pull_request` touching `infra/**` | `bicep build` lint + `az deployment group what-if` (read-only) |
| `push` to `main` touching `infra/**` | Same as PR + deploy to `dev` (requires manual approval — see below) |
| `push` tag matching `infra/v*` | Deploy to `prod` (requires manual approval) |
| `workflow_dispatch` | Manual deploy to chosen env (requires manual approval) |

**GitHub Environment protection rules (operator must configure once):**
Both `dev` and `prod` GitHub environments require a manual approval gate.
Auto-deploy-on-merge-to-main without approval is inconsistent with the
CLAUDE.md rule that agents must not run `az deployment ... create` without
explicit user approval. The GitHub UI enforces this outside of code.

Setup steps:
1. GitHub repo > Settings > Environments > New environment > name: `dev`
2. Under "Deployment protection rules" > enable "Required reviewers"
3. Add the operator GitHub account as required reviewer
4. Save. Repeat for `prod` environment.

Once configured, every merge to `main` that touches `infra/**` will pause at
the `Deploy to dev` step and wait for the operator to click "Approve" in the
GitHub UI before `az deployment group create` runs. This applies to both
human-initiated merges and any agent-initiated PRs.

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
| Container Apps Job compute | Consumption | **$0.00** | Free tier: 180,000 vCPU-seconds/month. One 5-min run/day at 0.25 vCPU = 375 vCPU-s/day × 30 = 11,250 vCPU-s/month. 94% below free tier. |
| Container Apps Job memory | Consumption | **$0.00** | Free tier: 360,000 GiB-seconds/month. One 5-min run at 0.5 GiB = 750 GiB-s/day × 30 = 22,500 GiB-s/month. 94% below free tier. |
| Container Apps Environment | Consumption | **$0.00** | No per-environment fee on Consumption plan. |
| Azure Container Registry | Basic | **~$5.00** | $5.00/mo flat for Basic SKU. Includes 10 GiB storage. Our image is ~300 MB; well within limits. |
| Blob Storage | LRS Hot | **~$0.01** | ~100 KB blob × 30 writes/month = negligible. $0.018/GB storage + $0.004/10k operations. |
| Key Vault | Standard | **~$0.03** | $0.03/10k operations. ~960 secret reads/month (7 secrets × 2 jobs/day × 2 cron-halves × 30 days = 840; rounding to ~960 with overhead). Still negligible — well under 10k operations. |
| Log Analytics | Pay-per-use | **~$0.00–$0.50** | First 5 GB/month free. Bot produces <10 MB logs/month. |
| Application Insights | Pay-per-use | **~$0.00** | First 5 GB/month free. |
| Network egress | — | **~$0.00** | First 100 GB/month free. Bot does <10 MB/run. |
| **Total (dev or prod)** | | **~$5.04–$5.54/mo** | Well within $10/mo ceiling. |

### 9.2 Budget alert

Azure Cost Management budgets are **subscription-scoped**, not resource-group-
scoped. `budget.bicep` is a subscription-scope Bicep module (targetScope =
'subscription') and must be deployed at the subscription level, not as a
nested module in the RG-scoped `main.bicep`.

**Approach:** `main.bicep` is RG-scoped. `budget.bicep` is a separate
deployment invoked from `azure-iac.yml` with `--scope /subscriptions/<id>`
after the RG deployment. Alternatively, create the budget manually once via
the Azure portal (Cost Management > Budgets) and document it in the runbook.

Budget parameters: `$10/mo`, 80% threshold alert, email to `budgetAlertEmail`.
The alert fires at ~$8 in a given month.

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
az keyvault secret set --vault-name kv-teetime-dev --name SMTP-HOST --value "<value>"
az keyvault secret set --vault-name kv-teetime-dev --name SMTP-USER --value "<value>"
az keyvault secret set --vault-name kv-teetime-dev --name SMTP-PASS --value "<value>"
az keyvault secret set --vault-name kv-teetime-dev --name PLAYER1-EMAIL --value "<value>"
az keyvault secret set --vault-name kv-teetime-dev --name PLAYER1-PHONE --value "<value>"
# NOTE: No STORAGE-CONN-STR. Blob Storage access uses DefaultAzureCredential
# via the user-assigned MI. The storage account name is resolved from IaC
# output (az deployment group show --query properties.outputs.storageAccountName).
# See §7.1 and §6.4.

# 4. Build and push container image
az acr build --registry teetimedev --image teetime:dev --file Dockerfile .

# 5. Trigger a manual dry-run to validate
az containerapp job start \
  --name teetime-job-dev \
  --resource-group rg-teetime-dev
# Check logs in Log Analytics; verify email arrives; verify blob created.

# 6. Deploy budget (subscription-scoped, run once)
az deployment sub create \
  --location eastus2 \
  --template-file infra/bicep/modules/budget.bicep \
  --parameters envName=dev budgetAmountUsd=10 budgetAlertEmail=<email>
```

### 10.2 Ongoing deploy (CI-driven)

For image-only updates (new bot code, same IaC):
1. CI builds and pushes `teetime:<tag>` to ACR.
2. Update `containerImage` in `main.bicepparam.dev` (or `prod`).
3. CI workflow runs `az deployment group create` — ACA Job picks up new image
   on next execution. There is no "restart" primitive for scheduled jobs; the
   new image takes effect on the next cron fire.

For IaC changes (Bicep edits):
1. PR opens → `azure-iac.yml` runs `bicep build` + `what-if`.
2. Merge → `azure-iac.yml` deploys to dev.
3. Tag `infra/v*` → `azure-iac.yml` deploys to prod.

### 10.3 v0 → v1 cutover

The v0 `book.yml` cron and the v1 ACA Jobs schedule MUST NOT both be active at
the same time. Running both risks concurrent booking attempts against the same
RequestId from two independent execution environments (GH runner + ACA
container), defeating layer 5 (advisory lock is per-SQLite-file, not
cross-platform).

**Cutover sequence:**
1. Deploy and validate v1 in dev with `--dry-run true`.
2. Confirm v1 dry-run email arrives at correct time with correct content.
3. Disable the v0 cron schedule in `book.yml` by commenting out both `schedule:`
   entries (keep `workflow_dispatch` intact for manual recovery):
   ```yaml
   on:
     # schedule:  ← DISABLED on v1 cutover; v1 uses ACA Jobs
     #   - cron: "50 9 * * *"
     #   - cron: "50 10 * * *"
     workflow_dispatch:
       ...
   ```
   Commit this change as a PR titled "v1 cutover: disable v0 cron schedule".
4. Deploy v1 ACA Jobs in prod with `--dry-run false`.
5. Monitor for 3 consecutive successful daily runs.
6. After 30 days of clean v1 operation, remove the commented schedule entries
   from `book.yml` in a follow-up PR.

**Do NOT auto-delete `book.yml`.** Keep `workflow_dispatch` forever as a
manual recovery path if ACA has an outage. An operator can re-enable the cron
by un-commenting the schedule lines, run a manual booking, then re-disable.

---

## 11. Security checklist

| Item | Status | Detail |
|---|---|---|
| No plaintext secrets in Bicep | Required | All secrets via Key Vault reference; `main.bicepparam.*` files contain no secret values |
| No secrets in container env vars (direct) | Required | All env vars are `secretRef:` pointing to Key Vault references |
| Key Vault soft-delete | On (90 days, default) | Confirmed default for new vaults created since 2019 |
| Key Vault purge protection | **Explicitly enabled in `keyvault.bicep`** | NOT on by default; must be set; irreversible |
| Blob soft-delete | Enabled (7 days) | Set in `storage.bicep` |
| Blob versioning | Disabled | Versioning adds cost and complexity; soft-delete is sufficient |
| ACA Job has no public ingress | By design | ACA Jobs (scheduled trigger type) do NOT expose HTTP ingress — unlike Container Apps services, which can have HTTP listeners. There is no public endpoint, no port binding, and no inbound network surface for the job resources. |
| Outbound-only network | By design | Bot makes outbound HTTPS to ForeUP and SMTP relay; no inbound surface |
| VNet integration | Not required for v0/v1 | ForeUP is a public internet endpoint; VNet adds cost and complexity with no security benefit |
| ACR authentication | Managed identity (AcrPull) | No registry password in job config; admin account disabled on ACR |
| RBAC minimum privilege | Key Vault Secrets User (read only), AcrPull (read only), Storage Blob Data Contributor (read/write/lease) | Each role is scoped to the specific resource, not subscription. All three roles are assigned to the single user-assigned MI (not per-job system-assigned MIs). |
| CI service principal | Contributor + User Access Admin, RG-scoped | Not subscription-level Contributor |
| OIDC auth (no client secrets in GitHub) | Required | GitHub stores only AZURE_CLIENT_ID, AZURE_TENANT_ID, AZURE_SUBSCRIPTION_ID |
| No credit card data | By design (inherited from v0) | ForeUP keeps card on file; bot never sees PAN/CVV |
| PII redaction in logs | Inherited from v0 | PLAN.md §10.1 rules apply; attempt_log is in Blob Storage under the same RBAC |

---

## 12. Open questions for the user

The following items cannot be resolved without operator input. The stubs in
`infra/bicep/` use placeholder values; these must be filled before first deploy.

| # | Question | Where it's needed |
|---|---|---|
| 1 | ~~**Azure AD tenant ID**~~ — **RESOLVED: `5151757e-ef5b-42a5-a09b-6410b40b2186`** | `azure-iac.yml` AZURE_TENANT_ID secret; OIDC setup |
| 2 | ~~**Azure subscription ID**~~ — **RESOLVED: `3f82c7e1-4b1b-4a55-b905-d79f65c6887d`** | `azure-iac.yml` AZURE_SUBSCRIPTION_ID secret; budget.bicep deploy |
| 3 | ~~**Preferred environment names**~~ — **RESOLVED: `dev`/`prod`** confirmed | `main.bicepparam.*` filenames and resource name suffixes |
| 4 | **Budget alert email address** — `alanc3939+claude@gmail.com` from config/container.toml is a reasonable default; confirm or override | `budget.bicep` parameter |
| 5 | ~~**GitHub repo owner/name**~~ — **RESOLVED: `wardcrazy01894/TeeTimeBooker`**. OIDC subject claims updated. | OIDC federated credential `subject` field |
| 6 | ~~**Dockerfile needed?**~~ — **RESOLVED: created at `Dockerfile` + `config/container.toml` + `.dockerignore`**. SMTP backend set to `console` until credentials are wired. SQLite path set to `/tmp/teetime-state/teetime.db` for BlobStateManager. | `registry.bicep` + `azure-iac.yml` build step |
| 7 | **ACR name** must be globally unique in Azure. Proposed: `teetime{envName}{shortId}` where `shortId` is a 4-char hash of the subscription ID. Confirm or override. | `registry.bicep` |
| 8 | **Storage account name** must be globally unique, 3–24 chars, lowercase alphanumeric. Proposed: `teetime{envName}sa{shortId}`. Confirm or override. | `storage.bicep` |
| 9 | **Key Vault name** must be globally unique, 3–24 chars. Proposed: `kv-teetime-{envName}-{shortId}`. Confirm or override. | `keyvault.bicep` |
| 10 | ~~**SMTP credentials**~~ — **DEFERRED.** `config/container.toml` uses `backend = "console"` for now. Switch to `backend = "email"` and provision SMTP_HOST/SMTP_USER/SMTP_PASS in Key Vault when ready. | Key Vault secrets + `SMTP-*` secret names |
| 11 | **ForeUP IP allowlist / bot-detection risk** — **ACCEPTED RISK for now.** Bot can be run locally via `uv run teetime run --config config/local.toml --dry-run true` while v1 ACA is still being built. Test from Azure before v1 cutover (§10.3 step 2b). Mitigations if blocked: NAT Gateway with static IP, or keep v0 GH Actions as primary. | v1 cutover; §10.3 |
