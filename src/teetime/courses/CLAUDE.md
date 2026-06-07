# CLAUDE.md — course adapters

Scoped notes for working under `src/teetime/courses/`. Loads when you touch an
adapter. The root `CLAUDE.md` has the repo-wide architectural invariants
(including the ForeUP `list_reservations` login-cache behaviour, the TTB: confirmation
prefix, and the cancel-before-book / `prepare_book` protocol) — read those too.

## Adding a course

- **ForeUP course (three steps):**
  1. Drop a sibling file next to `foreup/mangrove_bay.py` (e.g. `twin_brooks.py`).
     Set all four IDs (`course_pk`, `booking_class_id`, `schedule_id`,
     `public_booking_class_id`) and override `booking_page_url`.
  2. Import it in `__main__.py` and add one line to `_ADAPTER_REGISTRY`:
     `"foreup.twin_brooks": TwinBrooksAdapter,`
  3. Add a `[[courses]]` entry in your TOML config and add `"foreup:twin_brooks"`
     to `course_preferences` in the desired priority position.

  No other code needs to change. Adding a course to `[[courses]]` without
  adding it to `course_preferences` is safe — it won't change the RequestId
  or be tried by the orchestrator.

- **TeeItUp course (three steps):**
  1. Drop a sibling file next to `teeitup/sydney_marovitz.py` (e.g. `diversity_golf.py`).
     Set `course_slug`, `gn_facility_id`, `gnc_facility_id`, `kenna_facility_id`,
     `channel_id`, and `timezone`. Subclass `TeeItUpAdapter`. (`advance_booking_days`
     is a documentation-only constant in the course module, not a constructor arg.)
  2. Import it in `__main__.py` and add one line to `_ADAPTER_REGISTRY`:
     `"teeitup.diversity_golf": DiversityGolfAdapter,`
  3. Add a `[[courses]]` entry in your TOML config with `*_env` keys for all card
     credentials, and add the course id to `course_preferences`.

  No other code needs to change.

- **Chronogolf course:** stand up `chronogolf/base.py` first (Spike S2).

## Mangrove Bay specifics (ForeUP)

- Booking URL: `https://foreupsoftware.com/index.php/booking/19671/2149#/teetimes`
- `course_pk = 19671`, `booking_class_id = 2149` (teesheet/URL ID), `schedule_id = 2149`
- `public_booking_class_id = 12239` — the "Public" booking class from the page's `SCHEDULES` JSON; used in the login POST and is distinct from the teesheet URL ID
- Login uses `api_key=""` (empty); search uses `api_key="no_limits"` — confirmed by browser capture
- 7-day window opens 06:00 America/New_York exactly; minimum 2 players required
- **Party size is 4** (configured in `config/example.toml` + `config/local.toml` as 4 `[[request.players]]` entries). The idempotency layer-2 guard (`list_reservations`) matches on `party_size == len(request.players)` exactly — if you change party size between production runs an existing booking with the old party size will NOT block a new attempt. Cancel any conflicting reservation before deploying a party-size change.
- **Schedule books wanted morning days (default Sat+Sun)** (multi-day re-arch). Two ACA Job crons (one per DST half: `teetime-job-<env>-edt` / `-est`) fire DAILY at ~05:50 ET; each run computes `today + target_offsets[0]` (=7) and books it only if its weekday ∈ `target_weekdays` (else fast-exits 0 via `core/booking_day_gate.py`). The watcher checks the next upcoming occurrence of each wanted weekday within the horizon, one reservation per date. For ad-hoc dev runs use `teetime run --no-wait --dry-run true` (or `--fire-time HH:MM:SS` for an on-demand `--wait` check); the `watch` command takes `--date` to pin a specific date. (The old `book.yml` / `workflow_dispatch` path was removed in #43.)
- **Time window is 08:45–10:00 ET** (single morning window). Midpoint is 09:22:30; the slot closest to that midpoint wins (midpoint-distance sort). For mid-week or afternoon bookings, add a second `[[request.time_windows]]` entry in `config/local.toml` for that run only.

## Sydney R. Marovitz specifics (TeeItUp)

- Booking URL: `https://sydney-r-marovitz-golf-course.book.teeitup.com/`
- Platform: TeeItUp (NBC Sports Next / Indigo Sports), operated by Chicago Park District
- `course_slug = "sydney-r-marovitz-golf-course"`, `gn_facility_id = 4014`, `gnc_facility_id = 7218`
- `kenna_facility_id = "54f14cb60c8ad60378b02bfb"`, `channel_id = "20972"`
- **15-day advance booking window** (CPD policy). `advance_booking_days = 15`.
- **9 holes** (`holes = 9`). This is a par-3 course; there is no 18-hole option.
- **Party size** must include at least 2 players (singles must call the shop).
- **Payment flow**: TeeItUp native accounts use direct card entry via `POST https://tr.gnsvc.com/AddReservation` (form-encoded). There is no "card on file" wallet — card credentials are passed each booking call. Required `extra` fields in `CourseCredentials`:
  - `card_number`, `cvv`, `expiry_month`, `expiry_year`, `billing_address`, `billing_postal_code`
  - Optional: `billing_country` (default `"US"`), `name_on_card` (default: first+last from auth)
  - In TOML, use the `*_env` convention (e.g. `card_number_env = "SM_CARD_NUMBER"`) so secrets resolve from env vars and never appear in config files.
- **Cancel returns HTTP 200** (not 404) for already-cancelled reservations — our cancel is idempotent on both (live-confirmed 2026-05-29).
- **`list_reservations()` uses a live GET** (`/reservation/history`), unlike ForeUP's login-cache approach. Re-authentication before calling is not required.
- **`tr.gnsvc.com` response time**: payment endpoint takes ~5-10 s. The adapter sets a 60 s timeout for that specific call.

> **Card-data / PCI note:** the card fields above are real PAN/CVV passed to
> `tr.gnsvc.com` on every TeeItUp booking. They must be dropped by `_redact_payload`
> before any `attempt_log` write (PLAN.md §10.1), and the card POST uses
> `follow_redirects=False`. See the root CLAUDE.md "Credit-card data" invariant.
