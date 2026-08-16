# BACKLOG — future wants

A running list of things to add when there's time. **Not ratified, not scheduled** —
this is the "ideas that would otherwise live in phone notes" file. Detailed designs
live in their own `*_PLAN.md` docs; this file is the index plus the not-yet-designed
items. Add freely; promote an item to a real plan/milestone when you decide to build it.

---

## Courses to add

- **Moccasin Wallow Golf Course** (Palmetto, FL — near St. Petersburg).
  - First step: identify the booking platform (**ForeUP / TeeItUp / Chronogolf**) —
    that decides which adapter base to reuse (`courses/foreup/`, `courses/teeitup/`,
    or the `courses/chronogolf/` placeholder).
  - Then follow the step-by-step in
    [`src/teetime/courses/CLAUDE.md`](./src/teetime/courses/CLAUDE.md) ("adding a
    course"): per-course IDs + a config entry. No engine changes expected — adding a
    course is data/config, not new orchestration.

---

## Observability / reliability

- **A *persistent* 429 (or repeated CAPTCHA/auth failure) is currently invisible.**
  The watcher now backs off cleanly on a rate-limit and exits 0 (the 10-min cron is the
  retry — correct, since the notifier is `ConsoleNotifier` and M4 email was cut, so there's
  no alert channel anyway). But that means a platform that throttles us for hours makes every
  watch run show "Succeeded" while the bot silently stops booking. If any "ACA run
  Succeeded-vs-Failed" alerting is ever added, repeated 429-backoff (and the booker's
  `NO_INVENTORY` terminals) should surface somewhere. No action while there's no alert sink.

- **A late-landing booking race (CAPTCHA-prefetch lead not honored) is log-only.** When the
  ACA booking cron lands late in hour 5, the orchestrator now logs a `prefetch lead not fully
  honored` WARNING and the `book()` POST may fire after the 06:00 drop (it still prefetches, so
  it's *less* late — but the slot can still be lost). That WARNING is only grep-able, not
  actively surfaced. If/when an alert sink exists, route this WARNING (and the late-POST drift)
  into the `Notifier` so a missed drop is visible, not buried. Same "no alert channel" caveat
  as above. (Full-repo-scan follow-up, deferred from PR #114.)

- **Widen the Saturday/Sunday time window.** On the 2026-08-15 miss (target Sat 8/22), a
  `07:37` slot was bookable at T0+6 s and was correctly rejected as out-of-window
  (`08:45–10:00`). Every candidate the bot could reach that morning was outside the window
  by 22 minutes. Widening to e.g. `07:30–10:00` costs nothing on days we win — ranking is
  midpoint-distance based, so a 09:22-ish slot still wins whenever one exists — and only
  matters when the prime band is gone. **Deliberately NOT bundled with STAGGER_PLAN**: it
  changes which slots `synthesize_blind_slots` emits, which would confound the stagger's
  offset→outcome diagnostic on its very first drops. Ship after the stagger has produced a
  reading.

- **Retry burst across a WIDER post-T0 window**, conditional on the stagger diagnostic
  confirming the pre-open/flip-jitter hypothesis (STAGGER_PLAN §4). Today's stagger spans
  ±500 ms; if the release flip turns out to jitter by seconds, the answer is repeated POSTs
  at T0+0.5 s / +1 s / +2 s. That needs a bigger CAPTCHA pool — each `book()` pops a
  single-use token — so it carries its own cost and rate-limit analysis. **Measure first:**
  the hypothesis currently rests on two 0/3 drops, one of which (2026-08-01) is fully
  explained by a whole-day tournament block.

- **Pin whether ForeUP's "1 online reservation per day" is scoped to the PLAY date or the
  CALENDAR day the booking is made.** Surfaced by the adversarial review of #201 and currently
  UNPINNED — the multi-day design implies play-date scoping and nothing observed contradicts it,
  but no evidence discriminates the two (every drop books a different play date on a different
  calendar day, so the two hypotheses predict identical outcomes). It matters because
  `_rejection_summary` now reports an all-`daily_limit` burst as "we already hold a reservation
  for **this date**": under calendar-day scoping that wording would be wrong, since a Sunday
  burst could be bounced by Saturday's reservation made the previous morning. Cheap-ish
  experiment: from a dev/manual session, attempt a second booking for a DIFFERENT play date on a
  day we already booked, and read which body comes back.

---

## Frontend (single-user web UI)

A full, ratified design already exists: **[FRONTEND_PLAN.md](./FRONTEND_PLAN.md)**
(status: *proposed*, no code yet). Every item below is specced there — this is just
the index back to it.

| Want | Where it's designed |
|------|---------------------|
| A website around the booking engine | FRONTEND_PLAN.md (whole doc) |
| Show all current bookings | Goal 1 / **M-fe-T2** — live `list_reservations()` across courses |
| Cancel button next to each booking | Goal 2 / **M-fe-T3** — `cancel_reservation()`, managed vs. manual |
| Cancel **all** bookings | Goal 3 / **M-fe-T4** — list → per-item cancel |
| Change the time window / day preference | Goal 4 / **M-fe-T5** — edit `[request]` prefs |
| **Re-rank the courses** (change booking priority) | Goal 4 / **M-fe-T5** — edit `course_preferences` order + `[[one_booking_policy.priority_slots]]` |
| Auth on the frontend | FRONTEND_PLAN.md **§7 Q2** (open question) |

**Two things the plan already flags as the real constraints:**

- **Auth is not optional if the UI is exposed.** The API holds course credentials and
  can cancel bookings, so it can't be unauthenticated on a public surface. Resolution
  (local-only / basic auth / behind the Azure perimeter) is open — FRONTEND_PLAN §7 Q2.
- **Editing preferences (windows, day, course rank) needs durable mutable state.**
  M3 (`SqliteStore`) was cut from v0, so there's an open persistence decision —
  FRONTEND_PLAN §7 Q1. List + cancel + cancel-all ship with **no** durable store; only
  preference-editing carries this dependency.

The whole frontend is a "maybe someday" — the v0 cron/ACA-Jobs booking + watch engine
stands on its own without it.
