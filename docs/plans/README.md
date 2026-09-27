# Shipped design plans

Historical design records. Each plan was ratified (most via an architect + adversarial-reviewer
loop), built, and shipped; each carries a one-line status banner at the top. Their bodies are kept
as written, so line numbers and "today" refer to when they were drafted. Current behaviour is in
the root [CLAUDE.md](../../CLAUDE.md); living designs are [PLAN.md](../../PLAN.md),
[MULTIUSER_PLAN.md](../../MULTIUSER_PLAN.md) and [infra/AZURE_PLAN.md](../../infra/AZURE_PLAN.md).

| Plan | What it built | Shipped |
|------|---------------|---------|
| [M6_PLAN.md](./M6_PLAN.md) | First prod cron run: `run --wait`, DST gate, watcher enabled | M6 PRs 1–6 (schedule superseded by MULTIDAY) |
| [COST_KILLSWITCH_PLAN.md](./COST_KILLSWITCH_PLAN.md) | $50 budget → Logic App that silences every job | PR-KS1, PR-KS2 (2026-05-31) |
| [MULTIDAY_PLAN.md](./MULTIDAY_PLAN.md) | Saturday + Sunday booking, daily crons + booking-day gate | PRs #67–#75 |
| [PERDAY_WINDOWS_PLAN.md](./PERDAY_WINDOWS_PLAN.md) | Time windows bound to weekdays | PRs #76, #77 |
| [LEADTIME_SKIP_PLAN.md](./LEADTIME_SKIP_PLAN.md) | 16:00-day-before cutoff + no-redeploy skip-days | PRs #107–#111, `infra/v2.1.0` |
| [RACE_PREWARM_PLAN.md](./RACE_PREWARM_PLAN.md) | Pre-T0 login pre-warm, CAPTCHA pool, search-sleep trim | `infra/v2.4.0` |
| [BLIND_POST_PLAN.md](./BLIND_POST_PLAN.md) | Concurrent blind book POSTs at T0 + crash-net reconcile | PRs #125–#130, `infra/v2.5.0` |
| [RESEARCH_FALLBACK_PLAN.md](./RESEARCH_FALLBACK_PLAN.md) | Fresh search after the re-guard + fallback token reserve | PRs #157–#160, `infra/v2.8.0` |
| [STAGGER_PLAN.md](./STAGGER_PLAN.md) | Blind burst staggered across T0 | PR #199, `infra/v2.14.0` |
| [FRONTEND_PLAN.md](./FRONTEND_PLAN.md) | Single-user web UI (never built) | Superseded by MULTIUSER_PLAN |

When a plan ships, move it here with `git mv`, add the status banner and a row above, and fix every
link to it (`tests/test_docs_consistency.py` fails on a broken relative link).
