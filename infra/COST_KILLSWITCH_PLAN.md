# Cost Killswitch Plan — Automated ACA Job Schedule Disable on Budget Overrun

> **Status:** IMPLEMENTED + DEPLOYED to dev (PR-KS1 + PR-KS2 merged 2026-05-31). See `infra/AZURE_PLAN.md §9.2`.
> Authoritative Azure infra reference: `infra/AZURE_PLAN.md`. This plan adds a
> second budget tier ($50) with an automated enforcement chain on top of the
> existing $20 notify-only budget. The $20 budget is NOT replaced — it stays
> as the early-warning tier.

---

## Executive summary

When the combined dev+prod bill hits **$50 actual spend** in a calendar month,
an automated chain silences all six ACA Job cron triggers — three jobs in each
of the two environments (dev + prod) — so they never auto-fire again that month,
AND stops any in-flight executions that are running at the moment the alert fires.
Resources stay up and are fully reversible. The chain is:

```
Cost Management budget ($50, Actual >= 100%)
  → Azure Monitor Action Group (logicAppReceiver)
    → Logic App (Consumption, HTTP trigger)
      → LEVER (a): 6 PATCH calls to Microsoft.App/jobs API (3 jobs × 2 envs)
             (triggerType: Manual — stops FUTURE scheduled fires)
      → LEVER (b): 6 POST calls to Microsoft.App/jobs/{name}/stop (3 jobs × 2 envs)
             (stops IN-FLIGHT executions at the moment $50 trips)
```

This is a **slow-runaway backstop**, not real-time per-run protection. Azure
cost data lags hours. See §Limitations.

---

## 1. ARM mechanism — verified API paths

### Lever (a): stop future fires — PATCH triggerType to Manual

**Research result (confirmed, Microsoft Learn):**
`triggerType` IS mutable via `PATCH` on `Microsoft.App/jobs`. It is NOT
immutable and does NOT force a resource replacement. The PATCH uses JSON
Merge Patch semantics.

**API version:** `2024-03-01`
**Endpoint:**
```
PATCH https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.App/jobs/{jobName}?api-version=2024-03-01
```

**Exact request body to disable schedule (Schedule → Manual):**
```json
{
  "properties": {
    "configuration": {
      "triggerType": "Manual",
      "scheduleTriggerConfig": null,
      "manualTriggerConfig": {
        "replicaCompletionCount": 1,
        "parallelism": 1
      }
    }
  }
}
```

**Source:** https://learn.microsoft.com/en-us/rest/api/resource-manager/containerapps/jobs/update?view=rest-resource-manager-containerapps-2024-03-01

The PATCH returns HTTP 202 (async) and eventually 200 with the updated state.
PATCHing Manual → Manual is idempotent (no error, returns 200 with current state).

### Lever (b): stop in-flight executions — POST .../jobs/{name}/stop

**Why this lever is required:** Lever (a) prevents FUTURE scheduled fires. It
does NOT terminate a replica that is ALREADY running at the moment $50 trips.
The watch job fires every 10 minutes; at $50, one or more replicas may be
mid-execution. Those replicas continue to bill compute time until they complete
or time out (`replicaTimeout = 120 s` for watch, `1200 s` for booking jobs).
The killswitch MUST also stop in-flight executions to halt ALL variable spend.

**Research result (confirmed, Microsoft Learn):**
The "Stop Multiple Executions" action (`POST .../jobs/{name}/stop`) terminates
ALL currently-running executions for a given job in **one single call** — no
enumeration of individual execution names is required.

**API version:** `2024-03-01`
**Endpoint (one call per job, stops ALL running executions for that job):**
```
POST https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.App/jobs/{jobName}/stop?api-version=2024-03-01
```
**Request body:** empty (`{}`)
**Response:** HTTP 200 with a list of the executions that were stopped; HTTP 202
if async; HTTP 200 with empty list if no executions were running (no error).

**Source:** https://learn.microsoft.com/en-us/rest/api/resource-manager/containerapps/jobs/stop-multiple-executions?view=rest-resource-manager-containerapps-2024-03-01

**Idempotency:** POST to `/stop` when no executions are running returns HTTP 200
with an empty `value` array — not an error. The Logic App can call this
unconditionally without checking for running executions first.

### Combined action: 12 HTTP calls per killswitch fire (6 + 6)

The Logic App issues **12 calls total** per trigger activation (6 PATCHes + 6 POSTs):

**Dev env (`rg-teetime-dev`):**
- `PATCH .../jobs/teetime-job-dev-edt?api-version=2024-03-01` — set triggerType=Manual
- `PATCH .../jobs/teetime-job-dev-est?api-version=2024-03-01` — set triggerType=Manual
- `PATCH .../jobs/teetime-watch-job-dev?api-version=2024-03-01` — set triggerType=Manual
- `POST .../jobs/teetime-job-dev-edt/stop?api-version=2024-03-01` — stop running replicas
- `POST .../jobs/teetime-job-dev-est/stop?api-version=2024-03-01` — stop running replicas
- `POST .../jobs/teetime-watch-job-dev/stop?api-version=2024-03-01` — stop running replicas

**Prod env (`rg-teetime-prod`):**
- `PATCH .../jobs/teetime-job-prod-edt?api-version=2024-03-01` — set triggerType=Manual
- `PATCH .../jobs/teetime-job-prod-est?api-version=2024-03-01` — set triggerType=Manual
- `PATCH .../jobs/teetime-watch-job-prod?api-version=2024-03-01` — set triggerType=Manual
- `POST .../jobs/teetime-job-prod-edt/stop?api-version=2024-03-01` — stop running replicas
- `POST .../jobs/teetime-job-prod-est/stop?api-version=2024-03-01` — stop running replicas
- `POST .../jobs/teetime-watch-job-prod/stop?api-version=2024-03-01` — stop running replicas

The PATCH calls can run in parallel with each other; same for the POSTs.
The PATCHes and POSTs can also run in parallel (disabling future fires and
stopping current ones are independent operations). The Logic App workflow
runs all 12 as parallel branches for speed.

---

## 2. Pre-emption items — resolved answers

### Item 1: ARM mechanism reality check

**Lever (a) — stop future fires:**
`triggerType` CAN be changed in place from `Schedule` to `Manual` via PATCH
(JSON Merge Patch) on `Microsoft.App/jobs@2024-03-01`. The platform returns
HTTP 202 (async) and later 200 with the updated state. No replacement occurs.
The PATCH body is provided in §1 above. Idempotent (Manual → Manual = 200, no error).

**Lever (b) — stop in-flight executions:**
`POST .../jobs/{name}/stop?api-version=2024-03-01` stops ALL running executions
for a named job in ONE CALL — no enumeration of individual execution names needed.
Returns 200 with a list of stopped executions (empty list = no-op, no error).

The Logic App issues **12 calls** (6 PATCH + 6 POST), all authenticating with
the system-assigned managed identity using `audience: https://management.azure.com/`.
All 12 run as parallel Logic App actions.

**Both levers are required.** A killswitch that only disables schedules (lever a)
leaves a watch replica mid-execution billing compute until it times out (120 s).
A killswitch that only stops in-flight replicas (lever b) does not prevent the
NEXT cron fire 10 minutes later from spawning a new replica. Both are needed.

### Item 2: Re-enable / deploy-clobber conflict

This is the most critical correctness risk in the design.

**The problem:** `compute.bicep` always declares jobs with
`triggerType: enableSchedules ? 'Schedule' : 'Manual'`. If
`enableSchedules=true` (the default in both dev and prod param files) and the
Logic App has PATCHed a job to Manual, the NEXT CI deploy (auto-deploys on any
`infra/**` merge to main for dev) will re-declare it as Schedule and silently
RE-ARM the killswitch. This repo requires PRs (per memory rules), but PRs on
branches touching `infra/**` trigger CI and auto-deploy dev on merge — meaning
ANY concurrent infra PR could re-arm before the operator's remediation PR lands.
A pure runbook solution (telling the operator to push a file edit "quickly") is
insufficient against the dev-auto-deploy policy.

**Resolution chosen: `killswitchFired` checked-in safety latch.**

Three-layer protection:

1. **The Logic App PATCHes `triggerType=Manual` + POSTs `/stop`** — stops
   further auto-fires and kills in-flight executions immediately (the instant layer).

2. **`killswitchFired` param in `main.bicep` (the durable CI guard):**
   `main.bicep` computes:
   ```bicep
   var effectiveEnableSchedules = enableSchedules && !killswitchFired
   ```
   and passes `effectiveEnableSchedules` (not `enableSchedules`) to
   `compute.bicep`. When the killswitch fires, the operator sets
   `killswitchFired = true` in BOTH `main.bicepparam.dev` and
   `main.bicepparam.prod` and merges the change. Once merged, EVERY
   subsequent CI deploy will compute `effectiveEnableSchedules = false`
   regardless of the `enableSchedules` value — no concurrent infra PR can
   re-arm. This is the checked-in safety bit that survives across PR merges.
   The param files contain an explicit warning comment:
   ```
   // WARNING: do NOT clear this param until the overspend root cause is resolved.
   ```

3. **The operator runbook (§5)** covers diagnosis, root-cause fix, and the
   deliberate re-enable sequence (clear `killswitchFired`, confirm
   `enableSchedules=true`, push, verify).

**Gap window analysis:** After the Logic App fires, the window during which a
concurrent CI deploy could re-arm is between the killswitch fire and the moment
the operator's `killswitchFired=true` PR merges. This window is the time to
create and merge a one-line PR. In this repo a PR is the ONLY path to main
(branch policy); the operator must create a branch, push, open a PR, wait for
CI to pass, and merge — this takes minutes in the best case, but could be
longer if CI is slow or the operator is unavailable.

**Important — re-arm window can be up to ~24 hours, not minutes.** Budget
alerts re-fire at most once per evaluation cycle, which is typically DAILY
(not hourly or continuous). If a CI deploy re-arms the jobs (because
`enableSchedules=true` is still in the param file) AFTER the killswitch fired
but BEFORE the operator's `killswitchFired=true` PR merges, the re-armed
jobs will stay armed until the NEXT budget evaluation (up to ~24 hours later)
before the Logic App re-fires and re-PATCHes them to Manual. During that
window, the watch job (fires every 10 minutes) could execute up to ~144 times.
This is the residual risk we accept: the $50 killswitch is a slow-runaway
backstop, not a real-time per-run guard.

**Residual risk accepted:** The operator is expected to merge `killswitchFired=true`
promptly (minutes) after being alerted by the $50 budget email. Until that
merge lands, concurrent infra PRs that touch `infra/**` and merge to main will
auto-deploy dev with `enableSchedules=true`, potentially re-arming the watch job.
The mitigation is operator speed: merge the safety-latch PR before any other
infra PR merges. Once `killswitchFired=true` is merged, the CI latch closes
permanently until explicitly cleared. The ~24-hour gap is the known worst-case
only if the operator does NOT merge promptly AND a concurrent infra deploy fires.

Note: `killswitchFired` and `effectiveEnableSchedules` are implemented
in `main.bicep` and both `main.bicepparam.*` files (in main).

**Why not use an ARM tag as the guard instead?** A tag on the ACA jobs would
require the CI workflow to read ARM state before deploying, adding live Azure
SDK calls to CI. The param-file approach is a pure static-file change, auditable
via git history, reviewed like any other code change, and does not require CI
to have read access to ARM beyond what it already has. It is simpler and safer.

### Item 3: Subscription vs RG scope

| Resource | Scope | Deployed by | Notes |
|---|---|---|---|
| Budget ($20, notify-only) | Subscription | Operator, `az deployment sub create` | Existing `budget-teetime` resource; unchanged — CI SP is RG-scoped only, so the budget step warns-and-skips |
| Budget ($50, killswitch) | Subscription | Operator, same manual deploy command | Separate `budget-teetime-killswitch` resource in the same `budget.bicep`; same CI-skip constraint; `contactGroups` wired to killswitch Action Group |
| Action Group | Resource Group (rg-teetime-dev) | CI (RG-scoped SP) or operator | RG resource; CI SP has Contributor on the RG |
| Logic App | Resource Group (rg-teetime-dev) | CI (RG-scoped SP) or operator | RG resource; same RG as Action Group |
| RBAC: Logic App MI → rg-teetime-dev jobs | rg-teetime-dev scope | CI (RG-scoped SP) | Inline in killswitch.bicep |
| RBAC: Logic App MI → rg-teetime-prod jobs | rg-teetime-prod scope | CI (RG-scoped SP) | Nested module killswitch-rbac-prod.bicep; CI SP has User Access Admin on rg-teetime-prod (AZURE_PLAN §10.1.1 step 2, DONE 2026-05-31) |

**Sub-scoped budget → RG-scoped Action Group:** Confirmed by Microsoft docs
(Azure billing and cost management budget scenario, §Create the Budget):
`contactGroups` in a sub-scoped budget notification accepts the full ARM
resource ID of an RG-scoped action group:
```
/subscriptions/{sub}/resourceGroups/{rg}/providers/microsoft.insights/actionGroups/{name}
```
A sub-scoped budget CAN reference an RG-scoped action group by full resourceId.

**One killswitch for both envs:** The Action Group is placed in `rg-teetime-dev`
(one logical killswitch for the whole project, not per-env). The Logic App calls
jobs in BOTH RGs. This is intentional: the $50 is a PROJECT ceiling, not per-env.

**Two independent budgets, same filter (by design):** Both `budget-teetime` ($20)
and `budget-teetime-killswitch` ($50) filter on the same two RGs. Azure evaluates
each budget independently — both see the same spend figures. This is the standard
tiered-alert pattern and is explicitly supported: Azure allows any number of budget
resources per subscription scope as long as each has a distinct name.

**Operator manual steps — DONE 2026-05-31 (verified live):**
- The `az deployment sub create` for `budget.bicep` has been run; the separate $50
  `budget-teetime-killswitch` resource exists and its `contactGroups` notification is
  wired to the `ag-teetime-killswitch-dev` Action Group at 100%-actual. The $20
  `budget-teetime` (email-only) is also deployed and armed. No operator step remains.
- The Action Group and Logic App in `killswitch.bicep` are RG-scoped and were
  deployed by CI via `az deployment group create` targeting `rg-teetime-dev`.

**U3 resolved:** The cross-RG prod role assignment is handled by a nested module
`killswitch-rbac-prod.bicep` with `scope: resourceGroup(subscriptionId, prodRgName)`.
Bicep emits this as a nested ARM deployment targeted at `rg-teetime-prod`.
The CI SP has `User Access Administrator` on `rg-teetime-prod` (AZURE_PLAN §10.1.1
step 2, commands verified and marked DONE 2026-05-31 — NOT §8.2, which only covers
the dev RG assignment), so this is deployable by CI without operator intervention.

### Item 4: Least-privilege RBAC

**What the Logic App needs:** issue PATCH + POST /stop to `Microsoft.App/jobs`
in two RGs (rg-teetime-dev and rg-teetime-prod).

**Available actions** (from the Azure RBAC permissions reference for Microsoft.App,
confirmed at https://learn.microsoft.com/en-us/azure/role-based-access-control/permissions/compute#microsoftapp):
- `Microsoft.App/jobs/read` — GET the job
- `Microsoft.App/jobs/write` — PUT/PATCH the job (needed for Lever a)
- `Microsoft.App/jobs/delete` — DELETE the job (NOT needed; excluded)
- `Microsoft.App/jobs/start/action` — trigger a manual execution (NOT needed)
- `Microsoft.App/jobs/stop/action` — stop ALL running executions (needed for Lever b)

**Decision: custom role with `Microsoft.App/jobs/read + write + stop/action`.**

```json
{
  "Name": "ACA Job Schedule Manager",
  "IsCustom": true,
  "Description": "Read, PATCH (disable/enable schedule), and stop executions on ACA Jobs. Used by the cost killswitch Logic App.",
  "Actions": [
    "Microsoft.App/jobs/read",
    "Microsoft.App/jobs/write",
    "Microsoft.App/jobs/stop/action"
  ],
  "NotActions": [],
  "AssignableScopes": [
    "/subscriptions/3f82c7e1-4b1b-4a55-b905-d79f65c6887d"
  ]
}
```

`Microsoft.App/jobs/write` covers the PATCH (Lever a).
`Microsoft.App/jobs/stop/action` covers the POST .../stop (Lever b).
`Microsoft.App/jobs/read` is best practice (required for some management-plane
operations and safe to include; it does not add meaningful privilege beyond
the write access).

The custom role definition is subscription-scoped (so it can be assigned on
both RGs) but the RBAC **ASSIGNMENTS** are scoped per-RG:

```
Logic App MI → "ACA Job Schedule Manager" → /subscriptions/.../resourceGroups/rg-teetime-dev
Logic App MI → "ACA Job Schedule Manager" → /subscriptions/.../resourceGroups/rg-teetime-prod
```

**Important caveat:** custom role DEFINITIONS require subscription-level
`Microsoft.Authorization/roleDefinitions/write`. The CI SP does NOT have this
(it is RG-scoped only). The custom role definition must be created manually
by the operator (once, before the killswitch.bicep deploy). The killswitch.bicep
stub references the role by its GUID (passed as `killswitchRbacRoleId` param).
See §Operator manual steps in the runbook.

**Alternative evaluated and rejected: built-in Contributor scoped per-RG.**
Contributor grants write on ALL resource types in the RG, not just
`Microsoft.App/jobs`. For a system that runs in this same RG (with Key Vault,
ACR, ACA environment), Contributor would be significantly over-privileged.
The custom role is the correct choice.

### Item 5: Budget → Action Group → Logic App wiring

**Confirmed by Microsoft docs:**

1. `Microsoft.Consumption/budgets` notification supports `contactGroups`
   (an array of Action Group resource IDs). Example from the official Azure
   billing/budget scenario tutorial:
   ```json
   "contactGroups": [
     "/subscriptions/{sub}/resourceGroups/{rg}/providers/microsoft.insights/actionGroups/{name}"
   ]
   ```

2. `Microsoft.Insights/actionGroups` supports `logicAppReceivers` (confirmed
   via the Bicep template reference, api-version 2023-01-01):
   ```bicep
   logicAppReceivers: [
     {
       name: 'string'
       resourceId: 'string'       // Logic App ARM resource ID
       callbackUrl: 'string'      // Logic App HTTP trigger URL
       useCommonAlertSchema: bool
     }
   ]
   ```

3. The `callbackUrl` (the Logic App HTTP trigger URL) can be obtained from
   Bicep using `listCallbackUrl`:
   ```bicep
   listCallbackUrl('${logicApp.id}/triggers/manual', '2019-05-01').value
   ```
   This is a Bicep deployment-time function call that returns the trigger URL
   without storing a secret in params. The URL contains an embedded SAS token
   (valid ~5 years); redeploy refreshes it automatically.

4. A sub-scoped budget CAN reference an RG-scoped Action Group by its full ARM
   resourceId. Confirmed by the Microsoft scenario doc (the `contactGroups`
   array accepts any valid Action Group ARM ID regardless of where the budget
   is scoped).

### Item 6: Idempotency / repeat-fire

Budget alerts re-fire while spend stays above the threshold (at evaluation
intervals, typically daily). Both Logic App actions are inherently idempotent:

- **PATCH (Lever a):** PATCHing `triggerType=Manual` on a job that is already
  `Manual` returns HTTP 200 with the current state unchanged — not an error.

- **POST /stop (Lever b):** POST to `/stop` when no executions are running
  returns HTTP 200 with an empty `value` array — not an error. The Logic App
  calls `/stop` unconditionally; if nothing is running, the call is a no-op.

The Logic App does not need explicit idempotency guards for either lever.

### Item 7: Non-real-time limitation (mandatory disclosure)

**Azure cost data lags hours.** The budget threshold is evaluated against
billing data that is typically 8–24 hours stale. A job that runs at 06:00 AM
ET on Sunday may not appear in Cost Management until Sunday afternoon or even
Monday. This means the killswitch will not activate DURING a runaway spending
event; it activates hours to a day after the threshold is crossed.

**This is a slow-runaway backstop, NOT instant per-run protection.** Its
purpose is: "if something goes badly wrong (e.g., a bad deploy causes jobs to
spin up repeatedly), stop the bleeding within ~24 hours and alert the
operator." For the expected steady-state spend of ~$5–11/mo, the $50 threshold
provides approximately a 5–10× safety margin before the killswitch fires.

**Per-run cost protection** (e.g., stopping a job that is over-running a time
limit) is handled by `replicaTimeout` in compute.bicep (1200 s for booking
jobs, 120 s for the watch job) — that is an ACA platform control, not a cost
alert.

### Item 8: Testing without spending $50

The chain can be validated end-to-end without real billing spend:

1. **Logic App logic test (fire manually):** After deploying killswitch.bicep,
   use the Azure portal Logic App Designer → "Run Trigger" → POST a synthetic
   budget-alert payload to the HTTP trigger URL. The Logic App executes all 12
   HTTP calls. Verify in the portal that all three jobs now show
   `triggerType: Manual` and that any in-flight executions were stopped.

2. **Synthetic payload format** (use the Common Alert Schema that the Action
   Group sends; set `useCommonAlertSchema: true`):
   ```json
   {
     "schemaId": "azureMonitorCommonAlertSchema",
     "data": {
       "essentials": { "alertId": "test", "alertRule": "test" },
       "alertContext": {
         "SubscriptionName": "teetime",
         "BudgetName": "budget-teetime",
         "SpendingAmount": "50",
         "BudgetStartDate": "2026-06-01",
         "Budget": "50",
         "Unit": "USD",
         "NotificationThresholdAmount": "1.0"
       }
     }
   }
   ```

3. **Verify PATCH calls via Logic App run history:** the Logic App run history
   in the portal shows each HTTP action, its request body, and the response
   (200 OK or 202 Accepted). This confirms the PATCH reached the ACA API.

4. **Verify stop calls via run history:** each POST /stop action shows 200 OK
   (or 200 with empty value array if no jobs were running). This confirms the
   stop endpoint was reached.

5. **Re-enable after test:** to restore schedules after the test, either
   re-run the Bicep deploy (it will re-declare `triggerType: Schedule`) or
   issue a matching PATCH with `triggerType: Schedule`, `manualTriggerConfig:
   null`, and the original `scheduleTriggerConfig` value for each job.
   (Easiest: merge a trivial change to `infra/**` to trigger the CI
   auto-deploy for dev.)

6. **`az deployment group what-if`:** validates the Bicep resources (Action
   Group, Logic App, role assignments) without creating them.

### Item 9: Cost of the killswitch itself

**Consumption Logic App billing:** First 4,000 actions/month free. Beyond
that, built-in actions (HTTP) cost $0.000025 each.

The Logic App fires AT MOST once per budget evaluation cycle (typically once
per day while spend is over threshold) and executes **12 HTTP action calls**
per run (6 PATCH + 6 POST). In the absolute worst case (fires daily for a full
30-day month) that is 360 actions — well inside the 4,000-action free tier.

**Action Group billing:** Action groups themselves have no base fee. Logic App
notification delivery via action group costs $0.00 (it is the Logic App
execution that is billed, not the action group delivery). Logic App cost is
effectively $0.00 for this usage pattern.

**Conclusion: the killswitch adds $0.00/month to the bill in normal operation.
It does not itself eat into the $50 ceiling in any material way.**

### Item 10: Budget tier structure

**Decision: TWO-TIER approach — keep the existing $20 budget EXACTLY AS-IS,
add a SEPARATE $50 budget resource for the killswitch notification.**

**Tier 1 (`budget-teetime`, $20, email-only — UNCHANGED):**
The existing resource in `budget.bicep` stays byte-for-byte behaviorally
identical. `budgetAmountUsd` remains `20`. Its two notifications remain:
- Actual >= 80% → email at **$16** (unchanged)
- Forecasted >= 100% → email when projected to exceed **$20** (unchanged)

No text in this plan should say the $16 email threshold moves to $40 or that
`budgetAmountUsd` changes to 50. That approach was evaluated and rejected.

**Tier 2 (`budget-teetime-killswitch`, $50, killswitch-trigger — implemented in PR-KS2):**
A SEPARATE, INDEPENDENT second `Microsoft.Consumption/budgets` resource with a
distinct name (`budget-teetime-killswitch`). Same two-RG filter as Tier 1.
Single notification: Actual >= 100% (= $50), `contactGroups` wired to the
killswitch Action Group resourceId from PR-KS1. `contactEmails: []` — Tier 1
handles all email alerting.

Both budgets evaluate the same subscription spend independently — by design
(standard tiered-alert pattern). Azure allows many budget resources per
subscription scope with distinct names; there is no "one active budget per
scope" restriction that would prevent this. Two budgets tracking the same
spend is fully supported and is the recommended Microsoft pattern for
tiered alerting.

**Resulting alert ladder:**
- $16 actual → Tier-1 email (early warning, ~80% of $20)
- $20 projected → Tier-1 email (forecasted to exceed ceiling)
- $50 actual → Tier-2 killswitch fires (Logic App silences all 6 jobs)

This gives a 10× safety margin between steady-state spend (~$5–11/mo) and
the hard stop, while keeping the early-warning emails at their original
$16/$20 thresholds.

---

## 3. PR-by-PR plan

Two PRs. PR-KS1 deploys the RG-scoped enforcement chain (Logic App, Action
Group, RBAC). PR-KS2 updates budget.bicep to add the $50 action-group
threshold. The two PRs are independent of each other but PR-KS2 requires the
Action Group ID from PR-KS1 (as a param) — so PR-KS1 lands first.

```
PR-KS1 (killswitch.bicep: Logic App + Action Group + RBAC) → PR-KS2 (budget.bicep: $50 threshold)
```

Both PRs are IaC-only (no Python/src changes). Neither affects the bot
behaviour. They can be merged independently of M2.T3.

Note: `main.bicep` and both `main.bicepparam.*` files already have the
`killswitchFired` param wired in main (see `effectiveEnableSchedules`
in `main.bicep`). PR-KS1 added the `enableKillswitch` + `killswitchRbacRoleId`
params and the module call.

---

### PR-KS1 — killswitch.bicep: Logic App + Action Group + RBAC assignment — DONE (merged 2026-05-31)

**Scope.** Complete the `infra/bicep/modules/killswitch.bicep` module. Declares:
1. `Microsoft.Logic/workflows` (Consumption) with system-assigned MI, an HTTP
   trigger, and a workflow definition that issues **12 HTTP calls** (6 PATCH +
   6 POST /stop) across 3 jobs × 2 envs. All 12 run as parallel Logic App
   actions for speed. All 6 jobs must be covered — silencing only dev would
   leave prod running while the $50 budget is exceeded.
2. `Microsoft.Insights/actionGroups` (RG-scoped) with a single
   `logicAppReceiver` pointing at the Logic App.
3. Two RBAC role assignments: Logic App MI → custom role ("ACA Job Schedule
   Manager", pre-created by operator) → `rg-teetime-dev` (inline resource) and
   `rg-teetime-prod` (via `infra/bicep/modules/killswitch-rbac-prod.bicep`
   nested module with `scope: resourceGroup(subscriptionId, prodRgName)`).

Wired into `main.bicep` as an optional module (gated by
`enableKillswitch bool = false` param in main.bicep). Both param files set
`enableKillswitch = true`. The custom role GUID (`3e2d5a14-96bd-4469-9f96-b9c3270aa9e6`) is set
in both param files and in `azure-iac.yml` — the full chain deploys automatically on merge to main
(dev) and on the next `infra/v*` tag push (prod). The killswitch is live in dev.

**Files touched.**
- `infra/bicep/modules/killswitch.bicep` — stub already on branch; implement
  the TODO(PR-KS1) bodies: 6 PATCH + 6 POST workflow actions, Action Group, RBAC.
- `infra/bicep/modules/killswitch-rbac-prod.bicep` — companion module (implemented in main).
- `infra/bicep/main.bicep` — add optional `killswitch` module reference +
  `enableKillswitch` / `killswitchRbacRoleId` params.
- `infra/bicep/main.bicepparam.dev` — add `enableKillswitch = true`,
  `killswitchRbacRoleId = ''` (operator fills in GUID before deploying).
- `infra/bicep/main.bicepparam.prod` — same.
- `tests/test_killswitch_bicep.py` — NEW; static assertions (see below).
- `infra/AZURE_PLAN.md` — §3 module map + §9.2 budget section pointer.
- `infra/CLAUDE.md` — module map update.
- Root `CLAUDE.md` — note new killswitch module in infra.

**Operator pre-step (manual, before deploying PR-KS1):**
Create the custom role definition (requires subscription-level
`Microsoft.Authorization/roleDefinitions/write`, which the CI SP does NOT
have):
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
# Record the role GUID returned; pass it as killswitchRbacRoleId in bicepparam.dev/prod.
```

**Red tests first (static bicep assertions in `tests/test_killswitch_bicep.py`).**
- `test_killswitch_logic_app_resource_type` — assert `killswitch.bicep`
  contains `Microsoft.Logic/workflows` with api-version `2019-05-01`.
- `test_killswitch_action_group_resource_type` — assert
  `Microsoft.Insights/actionGroups` is present with api-version `2023-01-01`.
- `test_killswitch_logic_app_receiver_wired` — assert `logicAppReceivers`
  array is non-empty in the action group definition.
- `test_killswitch_logic_app_has_system_assigned_mi` — assert `identity.type`
  contains `SystemAssigned` for the Logic App.
- `test_killswitch_rbac_uses_custom_role_param` — assert the role assignment
  references `killswitchRbacRoleId` param (not hardcoded Contributor GUID
  `b24988ac-6180-42a0-ab88-20f7382dd24c`).
- `test_killswitch_patches_all_six_jobs` — assert the workflow definition
  references all three job name patterns (`edt`, `est`, `watch-job`)
  for BOTH envs (`dev` and `prod`) in PATCH calls.
- `test_killswitch_stops_all_six_jobs` — assert the workflow definition
  references all three job name patterns with `/stop` for BOTH envs.
- `test_killswitch_enabled_on_deploy` — assert
  `main.bicepparam.dev` and `main.bicepparam.prod` contain
  `enableKillswitch = true` (enabled on next deploy per operator decision
  2026-05-31). GREEN immediately since the param files already contain this value.
- `test_killswitch_rbac_prod_nested_module_present` — assert
  `killswitch.bicep` references `killswitch-rbac-prod.bicep` (the nested
  module for the cross-RG prod role assignment).
- `test_killswitch_fired_param_in_main_bicep` — assert `main.bicep` contains
  `param killswitchFired bool = false` and `effectiveEnableSchedules` variable.
- `test_killswitch_fired_defaults_false_in_param_files` — assert both
  `main.bicepparam.dev` and `main.bicepparam.prod` set `killswitchFired = false`.
- `test_stop_action_in_custom_role` — assert `killswitch.bicep` mentions
  `Microsoft.App/jobs/stop/action` (confirming lever (b) RBAC is present).
- `test_killswitch_rbac_prod_file_exists` — assert
  `infra/bicep/modules/killswitch-rbac-prod.bicep` exists on disk.

**Stub signatures (bicep fragments — what the stubs declare):**

`killswitch.bicep` already exists (see `infra/bicep/modules/killswitch.bicep`).
The stub body has the correct resource types, params, outputs, and TODO markers.
The `killswitch-rbac-prod.bicep` companion file is NEW (see §stub files below).

Main.bicep additions (NOT yet in main.bicep — PR-KS1 adds them):
```bicep
@description('Enable the cost-killswitch Logic App + Action Group. Defaults false in main.bicep; param files set true (enabled on deploy).')
param enableKillswitch bool = false

@description('GUID of the pre-created "ACA Job Schedule Manager" custom role. Required when enableKillswitch=true.')
param killswitchRbacRoleId string = ''

module killswitch 'modules/killswitch.bicep' = if (enableKillswitch) {
  name: 'killswitch-${envName}'
  params: {
    envName: envName
    location: location
    killswitchRbacRoleId: killswitchRbacRoleId
  }
}
```

**CI/parity.** `az bicep build` in `azure-iac.yml` runs on the new file. No
container config change; `test_container_config_parity.py` unaffected.
`what-if` will show new resources in dev if `enableKillswitch=true`; default
false means the dev deploy is a no-op for existing infra.

**Doc updates.** `infra/AZURE_PLAN.md` §3 (add `killswitch.bicep` and
`killswitch-rbac-prod.bicep` to module map), §9.2 (cross-reference the
killswitch plan), §11 (security — note custom role added). Root `CLAUDE.md`
(infra module summary). `infra/CLAUDE.md` (module map, agent rules note).

---

### PR-KS2 — budget.bicep: add separate $50 killswitch budget resource — DONE (merged 2026-05-31)

**Scope.** Add a SEPARATE, INDEPENDENT `Microsoft.Consumption/budgets` resource
(`budget-teetime-killswitch`) at $50 to `budget.bicep`. The existing
`budget-teetime` resource at $20 is NOT touched — `budgetAmountUsd` stays 20,
the $16/$20 email notifications are unchanged. The new resource has a single
notification (Actual >= 100% = $50) wired to the Action Group from PR-KS1.

**Design:** Add a `killswitchActionGroupId` param (optional string, default `''`)
and a `killswitchBudgetAmountUsd` param (default 50). When `killswitchActionGroupId`
is non-empty, deploy the second budget resource:
```bicep
resource killswitchBudget 'Microsoft.Consumption/budgets@2023-11-01' = if (!empty(killswitchActionGroupId)) {
  name: 'budget-teetime-killswitch'
  properties: {
    amount: killswitchBudgetAmountUsd   // default 50
    // ... same two-RG filter as budget-teetime ...
    notifications: {
      actual_GreaterThan_100Pct_killswitch: {
        enabled: true
        operator: 'GreaterThan'
        threshold: 100
        thresholdType: 'Actual'
        contactEmails: []
        contactGroups: [killswitchActionGroupId]
      }
    }
  }
}
```
Both budgets evaluate the same spend independently (by design — standard tiered
alerting). `budget-teetime` is not modified. No `budgetAmountUsd` change.

**Files touched.**
- `infra/bicep/modules/budget.bicep` — add `killswitchActionGroupId` param +
  `killswitchBudgetAmountUsd` param + second `killswitchBudget` resource.
  The existing `budget` resource block is NOT changed.
- `infra/AZURE_PLAN.md` — §9.2 budget alert table (add killswitch row with $50
  amount), §10.1 step 6 (update manual budget deploy command with new params).
- `tests/test_budget_bicep.py` — NEW; static assertions (see below).

**Red tests first (static bicep assertions in `tests/test_budget_bicep.py`).**
- `test_budget_original_amount_unchanged` — assert `budgetAmountUsd int = 20` in
  `budget.bicep` (the $20 budget must NOT change).
- `test_budget_killswitch_resource_name` — assert `budget-teetime-killswitch`
  appears in `budget.bicep` (the separate resource name).
- `test_budget_actual_100pct_notification_uses_contact_groups` — assert the
  killswitch notification has `contactGroups` property and `contactEmails: []`.
- `test_budget_existing_80pct_notification_preserved` — assert the 80% actual
  notification key still present in the original `budget` resource.
- `test_budget_existing_forecasted_notification_preserved` — assert the
  forecasted 100% notification key still present in the original `budget` resource.
- `test_budget_killswitch_notification_key_name` — assert the literal key
  `actual_GreaterThan_100Pct_killswitch` is in `budget.bicep`.
- `test_budget_killswitch_action_group_param_exists` — assert
  `param killswitchActionGroupId string` is in `budget.bicep`.
- `test_budget_killswitch_budget_amount_param_exists` — assert
  `param killswitchBudgetAmountUsd int` is in `budget.bicep`.

**Implemented params in budget.bicep (in main):**
```bicep
@description('ARM resource ID of the killswitch Action Group. When non-empty, the separate $50 killswitch budget resource is created. Get from killswitch.outputs.actionGroupId after PR-KS1 deploys.')
param killswitchActionGroupId string = ''

@description('Monthly ceiling for the separate killswitch budget. Defaults $50.')
param killswitchBudgetAmountUsd int = 50
```

**CI/parity.** Budget is subscription-scoped; CI SP can't deploy it (same as
today). CI runs `az bicep build` only. The manual deploy command in §10.1 and
§9.2 is updated to include the new params.

**Doc updates.** `infra/AZURE_PLAN.md` §9.2 (add killswitch budget row),
§10.1 (updated deploy command). `infra/CLAUDE.md` (module map note).

---

## 4. Limitations (mandatory disclosure)

1. **Not real-time.** Azure cost data lags 8–24 hours. A runaway spending
   event will not be detected and stopped instantly. This is a daily backstop,
   not a per-run guard. The ACA `replicaTimeout` (compute.bicep) is the
   per-run time guard.

2. **Logic App PATCH vs Bicep redeploy conflict.** The Logic App disables
   schedules at the ARM layer. If a CI deploy runs BEFORE the operator
   merges `killswitchFired=true`, the CI deploy re-enables schedules.
   The `killswitchFired` param (already in main.bicep and param files) is the
   durable guard: once the operator merges `killswitchFired=true`, no CI deploy
   can re-arm until it is explicitly cleared.

3. **Cross-RG RBAC.** The Logic App in `rg-teetime-dev` calls the ACA Jobs
   API in BOTH `rg-teetime-dev` and `rg-teetime-prod`. This requires an RBAC
   assignment on `rg-teetime-prod` for the Logic App's MI. If `rg-teetime-prod`
   is ever deleted and recreated, the role assignment must be re-created (via
   the nested module on next deploy).

4. **Custom role definition requires subscription-level permissions** (not
   deployable by the CI SP). This is a one-time operator step.

5. **Logic App HTTP trigger URL is a secret-like value.** The `callbackUrl`
   from `listCallbackUrl()` is embedded in the Action Group at deploy time. If
   the Logic App is redeployed (its trigger URL regenerates), the Action Group
   must also be redeployed to pick up the new URL. This happens automatically
   via Bicep on the next CI deploy.

6. **Stop-in-flight only for CURRENT executions.** Lever (b) stops the
   execution that was running at the moment the alert fired. A new replica
   started BETWEEN the alert firing and the Logic App completing its PATCH
   calls (~2–5 seconds) could slip through. The PATCH (Lever a) closes this
   window for all subsequent cron fires; the watch job's 10-minute interval
   means at most one additional replica can sneak through the ~5 second window.
   This is an acceptable edge case for a backstop mechanism.

7. **Budget mismatch on small overage.** The killswitch fires at $50 actual,
   but the expected monthly bill is ~$10–11. A genuine overage at $50 likely
   indicates a serious runaway issue. The $50 threshold is not meant to be
   triggered under normal operation.

---

## 5. Operator re-enable runbook

When the killswitch fires (budget $50 alert received), follow these steps in
order to re-enable the ACA Jobs and diagnose the runaway:

1. **Acknowledge the budget alert email.** Confirm actual spend in Azure portal
   → Cost Management → Budgets → budget-teetime.

2. **Diagnose the runaway.** Check Log Analytics (`ContainerAppConsoleLogs_CL`)
   for the last 24–48 hours. Look for unusual job execution frequency, errors,
   or unexpected configuration. Check the Logic App run history to confirm
   all 12 calls succeeded.

3. **Set `killswitchFired = true` in both param files** and open a PR as quickly
   as possible. This activates the CI guard (`effectiveEnableSchedules = false`)
   once merged, so no subsequent PR merge can re-arm the jobs.
   ```
   # 1. Create a branch, edit both param files:
   #    infra/bicep/main.bicepparam.dev:  param killswitchFired = true
   #    infra/bicep/main.bicepparam.prod: param killswitchFired = true
   # 2. Commit + push + gh pr create
   # 3. Wait for CI (bicep build + what-if) — this is a one-line safety latch;
   #    no infra change occurs, so you can skip the what-if review step.
   # 4. Merge. The dev auto-deploy fires immediately on merge.
   ```
   **PR delay acknowledgement:** This repo requires PRs for all changes to main
   (branch policy). Creating + merging this PR takes minutes. During that window,
   any concurrent `infra/**` PR that merges to main will auto-deploy dev with the
   un-patched param files, potentially re-arming the watch job. The Logic App will
   re-fire at the NEXT budget evaluation (up to ~24 hours later) and re-PATCH the
   jobs to Manual again. See §Item2 gap window analysis for the full risk statement.
   Priority: merge this PR before any other infra PR lands.
   Warning: do NOT clear this param until the overspend root cause is resolved.

4. **Fix the root cause** before re-enabling. (If unsure, keep jobs Manual
   until the root cause is identified.)

5. **Re-enable when ready.** Set `killswitchFired = false` in both param
   files, confirm `enableSchedules = true` in both param files, commit and
   push. Both envs will return to their cron schedules on next CI deploy.

6. **Verify jobs are Schedule.** After the CI deploy completes, confirm in the
   Azure portal that all three jobs show `triggerType: Schedule` and that the
   cron expressions are correct.

7. **Reset budget month.** The budget alert stops re-firing once the new
   calendar month begins (spend resets). No manual action needed on the budget.

**Additional manual step if this is the first killswitch event and the custom
role was not yet created:** create the custom role definition (one-time):
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
# Record the GUID; set param killswitchRbacRoleId = '<GUID>' in both param files.
```

---

## 6. Module-to-doc mapping (what each PR updates)

| Document | Updated by PR | Change |
|---|---|---|
| `infra/AZURE_PLAN.md` | PR-KS1, PR-KS2 | §3 module map, §9.2 budget, §10.1 runbook, §11 security |
| `infra/CLAUDE.md` | PR-KS1 | Module map, agent rules pointer |
| Root `CLAUDE.md` | PR-KS1 | Infra module summary; budget amounts |
| `README.md` | PR-KS2 | Cost/budget section if present |
| `PLAN.md` | Neither (no Python changes) | N/A |
| `infra/COST_KILLSWITCH_PLAN.md` | This file | PR-by-PR plan + operator runbook |

---

## 7. Open questions for the user

| # | Question | Blocking | Status |
|---|---|---|---|
| U1 | **Budget tier design.** Should the existing $20 budget be raised to $50 (moving the $16 email to $40), or should a SEPARATE second $50 budget resource be added while keeping the $20 budget unchanged? | PR-KS2 | **Resolved 2026-05-31:** Keep existing `budget-teetime` at $20 EXACTLY AS-IS. Add a SEPARATE `budget-teetime-killswitch` resource at $50. The $16/$20 email thresholds are unchanged. See Item 10 and PR-KS2 for the design. |
| U2 | **enableKillswitch default.** The plan defaulted `enableKillswitch=false` in both param files (staged opt-in). Should it instead be enabled on next deploy? | PR-KS1 | **Resolved 2026-05-31:** Enable on next deploy. Both `main.bicepparam.dev` and `main.bicepparam.prod` set `enableKillswitch = true`. The full chain (Logic App + Action Group + RBAC) deploys automatically on the next merge to main (dev) and on the next `infra/v*` tag push (prod). Operator must supply `killswitchRbacRoleId` before deploying. |
| U3 | **Cross-RG RBAC strategy.** RESOLVED: nested module `killswitch-rbac-prod.bicep` with `scope: resourceGroup(subscriptionId, prodRgName)` — the CI SP has `User Access Administrator` on `rg-teetime-prod` (AZURE_PLAN §10.1.1 step 2, DONE 2026-05-31), so this is deployable by CI. | PR-KS1 | Resolved |
