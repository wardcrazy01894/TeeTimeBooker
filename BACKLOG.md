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
