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

- **Release policy (both platforms, MULTIUSER_PLAN §6.1 / MU-1):** give the adapter class a
  `release_policy: ClassVar[ReleasePolicy] = ReleasePolicy(advance_days=…, release_time=time(H, M),
  timezone="Area/City", hosted_booking=…)` (`core/release_policy.py`) stating WHEN the course
  releases inventory. It must pass `validate_release_policy` — v1 only supports release hours
  **04–22** (a midnight release, common on TeeItUp, would fire the cron on D-1 and book a day late
  — see §6.1/§14), the cron lead may not cross midnight, the zone must be a valid IANA name, and
  **the fire time (release − lead) must land in hour `release.hour − 1`**, i.e.
  `release.minute < lead <= release.minute + 60` — that is the reading `dst_gate.should_proceed`
  makes, so a 06:30 release needs a lead in (30, 90], NOT the default 10 (it would fire 06:20:
  never books in summer, and in winter the wrong-season cron passes with T0 70 min out and blows
  the replica timeout). `hosted_booking=False` for any course whose booking path is out of hosted
  scope (TeeItUp: the PAN path) — no ACA job is ever derived from it. Nothing on the TOML
  production path reads the policy yet; the tenant runner (MU-9a) and the `release_events.json`
  parity test (MU-15a) will. Courses sharing `release_key(policy)` = `(timezone, release_time)`
  share ONE EDT/EST job pair; `cron_pair(policy)` gives its UTC crons as a `CronPair` (MB:
  `50 9 * * *` / `50 10 * * *`, pinned equal to `compute.bicep` by `tests/test_release_policy.py`).
  A course in a zone WITHOUT DST gets `CronPair.deduped == True` and `.jobs` of length 1 — deploy
  ONE job for it, never the -edt/-est pair (both would fire at the same instant and both pass the
  gate). Add a `test_<course>_release_policy_classvar` test there.

- **Chronogolf course:** stand up `chronogolf/base.py` first (Spike S2).

## Mangrove Bay specifics (ForeUP)

- Booking URL: `https://foreupsoftware.com/index.php/booking/19671/2149#/teetimes`
- `course_pk = 19671`, `booking_class_id = 2149` (teesheet/URL ID), `schedule_id = 2149`
- `public_booking_class_id = 12239` — the "Public" booking class from the page's `SCHEDULES` JSON; used in the login POST and is distinct from the teesheet URL ID
- Login uses `api_key=""` (empty); search uses `api_key="no_limits"` — confirmed by browser capture
- 7-day window opens 06:00 America/New_York exactly; minimum 2 players required
- **Release policy** (MU-1): `MangroveBayAdapter.release_policy = ReleasePolicy(advance_days=7,
  release_time=06:00, timezone="America/New_York", hosted_booking=True)`. Derived cron pair
  `("50 9 * * *", "50 10 * * *")` — pinned EQUAL to `compute.bicep`'s `cronEdtDaily`/`cronEstDaily`
  by `tests/test_release_policy.py::test_cron_pair_mb_matches_compute_bicep`, and the DST gate's
  `hour == fire_time.hour - 1` reading is reproduced from it. Data only until the MULTIUSER cutover.
- **Party size is 4** (configured in `config/example.toml` + `config/local.toml` as 4 `[[request.players]]` entries). The idempotency layer-2 guard (`list_reservations`) matches on `party_size == len(request.players)` exactly — if you change party size between production runs an existing booking with the old party size will NOT block a new attempt. Cancel any conflicting reservation before deploying a party-size change.
- **Schedule books wanted morning days (default Sat+Sun)** (multi-day re-arch). Two ACA Job crons (one per DST half: `teetime-job-<env>-edt` / `-est`) fire DAILY at ~05:50 ET; each run computes `today + target_offsets[0]` (=7) and books it only if its weekday has a configured window (else fast-exits 0 via `core/booking_day_gate.py`). The watcher checks the next upcoming occurrence of each wanted weekday within the horizon, one reservation per date. For ad-hoc dev runs use `teetime run --no-wait --dry-run true` (or `--fire-time HH:MM:SS` for an on-demand `--wait` check); the `watch` command takes `--date` to pin a specific date. (The old `book.yml` / `workflow_dispatch` path was removed in #43.)
- **Time windows are per-day** (PERDAY_WINDOWS_PLAN): each `[[request.time_windows]]` carries a `weekday`. Currently Sat+Sun both 08:45–10:00 ET (midpoint 09:22:30; slot closest to the midpoint wins). Multiple windows may share a day — at most ONE reservation that day, booked in the best window (window list order = preference). The wanted booking days are derived from these windows (no separate `target_weekdays`).
- **Book-response shape is a FLAT dict** (not `{"reservation": {...}}`): the reservation id comes back only in `TTID`/`teetime_id`. `book()`'s extraction chain reads those two fields LAST (the SAME two `_parse_reservation` reads — keep in sync), so `confirmation_code` = `TTB:<teetime_id>` on a live MB booking. Before BLIND_POST_PLAN PR0 the chain missed them and returned `None` (cosmetic for upgrade, which gets its id from `list_reservations`; load-bearing for blind-POST cancel-extras).
- **Tournaments block whole DATES, and it looks nothing like losing a race.** MB hosts events
  with shotgun starts (e.g. the [Anniversary Tournament](https://golfstpete.com/mangrove-bay-anniversary-tournament/),
  2026-08-08, 8 AM shotgun, 4-person scramble). A shotgun occupies all 18 holes at once, so
  NO public tee time exists for most of that day: on 2026-08-08 the teesheet was non-empty but
  started at **16:07** — nothing earlier at ANY party size. Signature at the 06:00 drop: every
  blind POST returns `400 {"success":false,"msg":"Time not available."}` with a server `Date`
  inside the open second (so not an early-arrival rejection) — i.e. every POST logs
  `→ gone[unavailable]`, never `gone[daily_limit]` (see the `SlotGoneError.reason` bullet in the
  root CLAUDE.md; a `daily_limit` reject alongside a booked sibling means our own burst WON and is
  not a miss at all, whereas an ALL-`daily_limit` burst means a reservation for that date already
  existed) —
  and the post-reguard fresh search
  returns plenty of raw slots with 0 in-window; expect no morning slot to reappear on later
  watcher cycles either. **This is NOT a slot-race loss and no burst size fixes it** — a
  whole-day block means the synthesized morning grid has no real inventory behind it, so
  widening the burst adds nothing; and a genuine race loss leaves partial-capacity slots and
  later cancellations behind, whereas a block leaves a hard empty edge. `search()`'s 0-match
  diagnostics line (root CLAUDE.md) is the fast discriminator **at the 06:00 drop**:
  `out-of-window=N` plus an available span starting in the afternoon means blocked, not raced.
  (That same shape is expected LATER in the week from an ordinary sold-out morning — it is
  diagnostic only at T0, when inventory has just dropped and is very unlikely to have sold out
  already. Not impossible: the fresh fallback search runs seconds after T0, and a fully raced
  morning would look the same, which is what makes the 2026-07-18 drop (target Sat 7/25) still unexplained.) Check the
  [events calendar](https://golfstpete.com/events/) before treating a Saturday miss as a
  tuning problem.
- **`/times` server-filters on the `players` param.** Every returned slot already satisfies
  `available_spots >= players`, so the adapter's client-side spots filter is a backstop that
  never fires in production. Verified live 2026-08-02: `players=4` returns a SUBSET of
  `players=2` on the same date — every partially-booked slot is dropped, leaving only the
  four-spot ones. (Exact counts drift daily with bookings, so the subset relation is the
  durable form of the claim; on 2026-08-02 it was a STRICT subset, but on a date with no
  partially-booked slots the two are simply equal.)
  Two consequences: (a) searching with the real party size HIDES partially-booked slots, so
  when smoke-testing "is anything left on this date" drop to `players=2` (and note `players=1`
  returns `[]` outright — `allowed_group_sizes` is 2-4); (b) an `insufficient-spots` rejection
  in the 0-match diagnostics line would mean ForeUP changed this contract, which is why that
  leg escalates to WARNING.
- **Blind-POST capable** (BLIND_POST_PLAN.md; `capabilities = AdapterCapabilities(blind_post=True)` — the ONLY course that flips it; the base ForeUP default is `blind_post=False`). `mangrove_bay.py` ships two module constants: `BLIND_POST_TEMPLATE` (the static, card-free search-slot raw shape — only `time` + `start_front` are per-slot) and `BLIND_POST_MORNING_GRID` (explicit HH:MM list, NOT an interval). `synthesize_blind_slots()` intersects the grid with the request window, computes each `start_front` (`f"{YYYY}{month-1:02d}{DD}{HH}{MM}"`, **0-indexed month**; the `time` field uses the real 1-indexed month), feeds each raw through the SAME `_parse_slot` the search uses, and returns slots ranked by `rank_slots_for_request`. The grid is **DERIVED** from the proven 8/hr teesheet cadence (mornings sell out → not directly searchable), so it's validated retroactively: `synthesize_blind_slots` logs the grid span + the firing times and `search()` logs matched morning tee times — diff them after a real drop and fold drift back into `BLIND_POST_MORNING_GRID`.
- **The grid spans the FULL morning, 07:00–12:00 (41 points; MULTIUSER_PLAN §6.5 / E3, MU-3).** It is generated from the cadence minutes `(:00,:07,:15,:22,:30,:37,:45,:52)` over hours 07–11 plus `12:00` — extend the RANGE if needed, never re-cadence. It originally covered only the operator's 08:45–10:00 window (11 points), so any EARLIER window synthesized zero blind slots and fell to the slower search race path. Widening the **grid** (not the window) is behaviour-preserving for every existing window, because synthesize intersects the grid with the request window: the operator's 08:45–10:00 burst emits byte-identical slots in identical rank order (`09:22, 09:15, 09:30`, then `09:37, 09:07, 09:00, 09:45, 09:52, 08:52, 08:45, 10:00`) — pinned literally against the pre-change grid by `test_widened_grid_emits_identical_slots_for_0845_1000_window`, so the STAGGER offset↔rank diagnostic is not confounded. Grid points outside every requested window are NEVER POSTed. The real first tee before 08:45 is **unverified** (Spike S-M7; `07:37` has been observed live post-drop): a grid point earlier than the true first tee is inert unless a window covers it, and then costs only a `400 → SlotGoneError → next candidate`.
- **Blind allowlist hook — `set_blind_allowlist(frozenset[SlotId] | None)` / `blind_allowlist` (MULTIUSER_PLAN §5.4 / E2, MU-3).** `synthesize_blind_slots` applies it to the RANKED in-window candidates **before** truncation to `max_count`, so an account allocated slots that are not its natural top-N still fires a full burst of exactly those slots, in rank order (`test_allowlist_filters_before_truncation`). `None` (the default, and what the single-user TOML path always has) = no filter = today's behaviour; `frozenset()` = "search-only this drop" (returns `[]`). Set ONCE pre-T0 by the tenant runner from `tenant.allocation.allocate_blind_slots` (a pure snake draft over `draft_order`, which sorts rows by id and rotates first pick by `target_date.toordinal()`), so N accounts sharing a window blind-POST DISJOINT grid slots and no account's surplus POST can take another's rank-0; accounts with disjoint windows get exactly their own top-3 (`tests/test_allocation.py`). The firing-grid log line names the allowlist (`N allowed by allowlist of M` vs `no allowlist`) so a per-account burst is auditable from logs. `Orchestrator` is untouched — it still just calls `synthesize_blind_slots`.

## Sydney R. Marovitz specifics (TeeItUp)

- Booking URL: `https://sydney-r-marovitz-golf-course.book.teeitup.com/`
- Platform: TeeItUp (NBC Sports Next / Indigo Sports), operated by Chicago Park District
- `course_slug = "sydney-r-marovitz-golf-course"`, `gn_facility_id = 4014`, `gnc_facility_id = 7218`
- `kenna_facility_id = "54f14cb60c8ad60378b02bfb"`, `channel_id = "20972"`
- **15-day advance booking window** (CPD policy). `advance_booking_days = 15`.
- **Release policy** (MU-1): `SydneyMarovitzAdapter.release_policy = ReleasePolicy(advance_days=15,
  release_time=06:00 **PLACEHOLDER**, timezone="America/Chicago", hosted_booking=False)`. The daily
  release TIME is **UNCONFIRMED** (MULTIUSER_PLAN Spike S-M4) — 06:00 is the plan's §6.1 worked
  example for a Chicago release, not an observation; 00:00 (common on TeeItUp) is not representable
  in v1 (`validate_release_policy` rejects hours outside 04–22). `hosted_booking=False` ⇒ no ACA job
  is ever derived; the policy is data for the parity/allocation tests only. Replace the time when
  S-M4 closes (two probes an hour apart around the 15-days-out inventory appearing, or a pro-shop answer).
- **9 holes** (`holes = 9`). This is a par-3 course; there is no 18-hole option.
- **Party size** must include at least 2 players (singles must call the shop).
- **Payment flow**: TeeItUp native accounts use direct card entry via `POST https://tr.gnsvc.com/AddReservation` (form-encoded). There is no "card on file" wallet — card credentials are passed each booking call. Required `extra` fields in `CourseCredentials`:
  - `card_number`, `cvv`, `expiry_month`, `expiry_year`, `billing_address`, `billing_postal_code`
  - Optional: `billing_country` (default `"US"`), `name_on_card` (default: first+last from auth)
  - In TOML, use the `*_env` convention (e.g. `card_number_env = "SM_CARD_NUMBER"`) so secrets resolve from env vars and never appear in config files. **Enforced:** `_resolve_creds` rejects a literal value for any key in `SECRET_EXTRA_KEYS` (see `core/config.py` for the authoritative list — card/CVV/expiry/billing-address/name/password) and requires the `*_env` form. `billing_country` is exempt (non-secret, defaults to `"US"`).
- **Cancel returns HTTP 200** (not 404) for already-cancelled reservations — our cancel is idempotent on both (live-confirmed 2026-05-29).
- **`list_reservations()` uses a live GET** (`/reservation/history`), unlike ForeUP's login-cache approach. Re-authentication before calling is not required.
- **`tr.gnsvc.com` response time**: payment endpoint takes ~5-10 s. The adapter sets a 60 s timeout for that specific call.

> **Card-data / PCI note:** the card fields above are real PAN/CVV passed to
> `tr.gnsvc.com` on every TeeItUp booking. They are dropped by `redact_payload`, which
> `BookingStore.append_attempt` applies at the store boundary on every `attempt_log` write
> (PLAN.md §10.1), and the card POST uses `follow_redirects=False`. See the root CLAUDE.md
> "Credit-card data" invariant.

> **Deployment scope (local-dev only):** TeeItUp booking is **not in scope for the deployed
> prod/dev ACA Jobs** — the hosted bot books **ForeUP (Mangrove Bay) only**. The `SM_*` card
> credentials therefore have **no Key Vault wiring** (`keyvault.bicep` / `compute.bicep` carry
> only `MB-*` / `PLAYER1-*` / `TWOCAPTCHA-*`); they are sourced from a local `.env` for
> developer/manual runs against Sydney Marovitz. Consequence: a `[[courses]]` entry using a
> TeeItUp adapter must NOT appear in the deployed `container.toml` `course_preferences`, since
> the hosted job has no card secrets to fulfil it. If TeeItUp ever moves into hosted scope,
> wire its card into Key Vault first — and note that storing the CVV (which must be sent on
> every booking, unlike ForeUP's card-on-file) is a deliberate PCI-scope expansion to ratify.
