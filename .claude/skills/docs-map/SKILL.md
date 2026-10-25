---
name: docs-map
description: The change→docs map for this repo. MANDATORY before opening or finishing ANY PR (code, infra, docs, deps). Start from what the PR changes and update EVERY doc site the matching row lists; a claim updated in most but not all of its homes is the recurring full-repo-scan finding.
---

# Change→docs map

Moved here from the root `CLAUDE.md` (Documentation standard) on 2026-10-25. The per-area
invariants it refers to as "CLAUDE.md invariants" now live in `src/teetime/core/CLAUDE.md`,
`src/teetime/tenant/CLAUDE.md`, `src/teetime/web/CLAUDE.md` and `src/teetime/courses/CLAUDE.md`;
update the one that owns the claim (and the root `CLAUDE.md` if its status or pointers change).


Start from what your PR changes; each row lists every doc site that claim lives in. The recurring
full-repo-scan finding is a claim updated in most but not all of its homes, so grep and update ALL
of them in the same PR.

| Change | Doc sites to update (all of them) |
|--------|-----------------------------------|
| Prod infra tag bump / deploy | README.md Status + Azure hosting, CLAUDE.md Current status table, PLAN.md (scope note + §16 M6.T3 row), a new section in `docs/RELEASES.md`. Enforced: `tests/test_docs_consistency.py` fails if README/CLAUDE/PLAN name different "latest infra tag" versions |
| Prod mode change (`bookingMode`/`watchMode` in `main.bicepparam.prod`) | Every current-state line that says what prod runs: README Status + Azure hosting table, CLAUDE.md "Two paths" intro + Current status + milestone table, MULTIUSER_PLAN status note + §12, `docs/MULTIUSER_AS_BUILT.md`, AZURE_PLAN status banner, PLAN.md status, BACKLOG. Mechanically enforced: `tests/test_docs_consistency.py::test_no_doc_says_prod_runs_toml_once_prod_runs_the_tenant_path` (added after four review rounds each found another stale wording) |
| Dependency floor bump / dep-comment edit | `pyproject.toml`: bump the floor AND check the comment above it. A dep comment must name NO tracking version. Enforced for `idna` (only the CVE boundary may be named; the floor may not drop below it). Drifted twice (#106, #204) |
| New/changed config key or default | `core/config.py` field comment, `config/example.toml` + `container.toml` + `local.toml`, README Configuration, `tests/test_container_config_parity.py` pin, CLAUDE.md invariant if load-bearing |
| Engine orchestrator/watcher behaviour (`core/*orchestrator*.py`, gates) | CLAUDE.md invariants, PLAN.md §9/§9.1/§12, the owning plan's status banner (and a supersession banner on any plan it retires) |
| T0 race timing: `early_arrival_ms`, `blind_post_stagger_ms`, a blind-POST rejection reason | `core/config.py` defaults + field comments (the TENANT booker uses these defaults); `config/container.toml` + `example.toml` (+ your untracked `local.toml`); CLAUDE.md status line (What is live) + "Blind-POST burst" bullets (stagger, `stagger[0]`, rejection reasons); README how-it-works step 3 + Configuration table + the etiquette "~N ms window"; PLAN.md §6.1 busy-wait pseudo-code, §9 stagger default, §12 window; MULTIUSER_PLAN §4 race timeline + the MU-18 first-drop checklist; AZURE_PLAN §3 busy-wait note; `src/teetime/courses/CLAUDE.md` MB bullets; BACKLOG retry-burst item; a dated banner on STAGGER_PLAN / BLIND_POST_PLAN; the operator-summary reason text (`tenant/notify.py::_REJECTION_TEXT`). Enforced: `test_every_current_state_doc_names_the_shipped_stagger_ladder` + `test_no_current_state_doc_describes_the_retired_ladder_as_current` (a retired ladder may appear only on a line marked as history) — added 2026-09-30 after the -400 change found eight stale sites |
| Adapter capability / course quirk (`courses/**`) | `src/teetime/courses/CLAUDE.md`, CLAUDE.md capability bullet |
| Tenant logic (`src/teetime/tenant/**`) | `docs/MULTIUSER_AS_BUILT.md` (milestone section), MULTIUSER_PLAN.md (§12 row + the section it implements), CLAUDE.md Multi-user invariants and milestone table if state changes |
| Tenant store semantics or schema (`tenant/store.py`, `in_memory_store.py`, `cosmos/**`, `semantics.py`) | `tests/tenant/conformance.py` (the contract), MULTIUSER_PLAN §3, `QUERIED_PATHS` + `cosmos.bicep` index policy if a new filter path, CLAUDE.md Two-stores bullet |
| Web app (`src/teetime/web/**`, templates, routes) | README Multi-user web app section (pages, env vars), MULTIUSER_PLAN §8 (`web/routes.py::ROUTES` is the contract table), `docs/MULTIUSER_AS_BUILT.md` |
| New CLI command, flag or env var | README, CLAUDE.md Common commands, AZURE_PLAN §7.3 env inventory + `compute.bicep`/`webapp.bicep`/`keyvault.bicep` if deployed |
| Key Vault secret added/renamed | `keyvault.bicep` / the consuming module, AZURE_PLAN §7.3 secret inventory, README env-var tables, `tests/test_keyvault_bicep.py` or the module's test |
| ACA job, cron or timeout (`compute.bicep`) | `compute.bicep` comments, CLAUDE.md + README schedule claims, AZURE_PLAN §5, killswitch job-name coupling (`killswitch.bicep` + `tests/test_killswitch_job_parity.py`) |
| Release event (`infra/bicep/release_events.json`, `core/release_policy.py`) | `tests/test_release_events_parity.py`, MULTIUSER_PLAN §6.2, AZURE_PLAN §5, `src/teetime/courses/CLAUDE.md` per-course policy |
| New Bicep module or param (`infra/bicep/**`: `cosmos`, `webapp`, `email`, …) | `infra/CLAUDE.md` module tree, AZURE_PLAN (module + runbook), both `.bicepparam` files, `azure-iac.yml` inline params (pinned by `test_every_param_file_value_reaches_every_ci_deploy`), killswitch levers if it runs compute |
| CI workflow change (`.github/workflows/**`) | CLAUDE.md Required CI checks + branch protection for a new validation job, `.githooks/pre-push` (pinned by `tests/test_prepush_hook.py`) |
| Milestone or feature done or cut | PLAN.md §16 row or MULTIUSER_PLAN §12 row, README status/roadmap, CLAUDE.md Current status, BACKLOG.md if it retires an item |
| Retry behaviour (a new retry, or a call that must never retry) | CLAUDE.md "Retries everywhere else" bullet (the per-call list), `tenant/retry.py` module docstring if the SDK baseline changes, `docs/MULTIUSER_AS_BUILT.md` "Retry audit" section, and a test that pins both the retry AND its bound |
| Doc moved, renamed or retired | Every link to it (grep the whole repo incl. src docstrings, tests, bicep, workflows), the Where-the-docs-live table here, README Documentation list. Enforced: `tests/test_docs_consistency.py` fails on a broken relative markdown link |

When a sweep fixes a stale claim, ask whether a cheap test can pin it; add sibling checks to
`tests/test_docs_consistency.py` when a claim class recurs.
