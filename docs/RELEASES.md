# Release history

> **Status:** living log of every prod infra tag, newest first. The CURRENT prod tag is stated in
> README.md, CLAUDE.md and PLAN.md (CI checks they agree: `tests/test_docs_consistency.py`); add a
> section here on every prod deploy.

Moved out of the root CLAUDE.md Status section on 2026-09-26, where each release was repeated two
or three times. Facts are unchanged; only the repetition is gone. Dev auto-deploys from `main` on
every merge and is not tagged.

<!-- toc -->
## Contents

- [Summary](#summary)
- [infra/v3.10.0: 2026-10-04 (`main`@`08c6fe8`)](#infrav3100-2026-10-04-main08c6fe8)
- [infra/v3.9.0: 2026-10-02 (`main`@`68c4b8c`)](#infrav390-2026-10-02-main68c4b8c)
- [infra/v3.8.0: 2026-10-02 (`main`@`f38f481`)](#infrav380-2026-10-02-mainf38f481)
- [infra/v3.7.0: 2026-10-02 (`main`@`b1610f8`)](#infrav370-2026-10-02-mainb1610f8)
- [infra/v3.6.0: 2026-09-30 (`main`@`69abc3a`)](#infrav360-2026-09-30-main69abc3a)
- [infra/v3.5.0: 2026-09-29 (`main`@`8466ad1`)](#infrav350-2026-09-29-main8466ad1)
- [infra/v3.4.0: 2026-09-29 (`main`@`f149142`)](#infrav340-2026-09-29-mainf149142)
- [infra/v3.3.0: 2026-09-29 (`main`@`be66b5a`)](#infrav330-2026-09-29-mainbe66b5a)
- [infra/v3.2.0: 2026-09-29 (`main`@`fbb6b5e`)](#infrav320-2026-09-29-mainfbb6b5e)
- [infra/v3.1.0: 2026-09-28 (`main`@`4421fb0`)](#infrav310-2026-09-28-main4421fb0)
- [infra/v3.0.2: 2026-09-27](#infrav302-2026-09-27)
- [infra/v3.0.1: 2026-09-27](#infrav301-2026-09-27)
- [infra/v3.0.0: 2026-09-27](#infrav300-2026-09-27)
- [infra/v2.17.0: 2026-09-27 (`main`@`6b8122b`)](#infrav2170-2026-09-27-main6b8122b)
- [infra/v2.16.0: 2026-08-24 (`main`@`4462f56`)](#infrav2160-2026-08-24-main4462f56)
- [infra/v2.15.0: 2026-08-16 (`main`@`8342b67`)](#infrav2150-2026-08-16-main8342b67)
- [infra/v2.14.0: 2026-08-15 (`main`@`e6a8abb`)](#infrav2140-2026-08-15-maine6a8abb)
- [infra/v2.13.0: 2026-08-09 (`main`@`de90316`)](#infrav2130-2026-08-09-mainde90316)
- [infra/v2.12.0: 2026-08-02 (`main`@`a7a1a6c`)](#infrav2120-2026-08-02-maina7a1a6c)
- [infra/v2.11.0: 2026-07-18 (`main`@`cdf5618`)](#infrav2110-2026-07-18-maincdf5618)
- [infra/v2.10.0: 2026-07-15 (`main`@`fb2a133`)](#infrav2100-2026-07-15-mainfb2a133)
- [infra/v2.9.0: 2026-07-10](#infrav290-2026-07-10)
- [infra/v2.8.0: 2026-06-29 (`main`@`21b24cf`)](#infrav280-2026-06-29-main21b24cf)
- [infra/v2.7.0: 2026-06-23 (`main`@`b2c0051`)](#infrav270-2026-06-23-mainb2c0051)
- [infra/v2.6.0: 2026-06-23](#infrav260-2026-06-23)
- [infra/v2.5.0: 2026-06-22 (`main`@`ddf0112`)](#infrav250-2026-06-22-mainddf0112)
- [infra/v2.4.0](#infrav240)
- [infra/v2.2.0](#infrav220)
- [infra/v2.1.0: 2026-06-10](#infrav210-2026-06-10)
- [Before v2.1.0](#before-v210)

<!-- /toc -->

## Summary

| Tag | Deployed | `main` | Booking behaviour | Headline |
|-----|----------|--------|-------------------|----------|
| `infra/v3.10.0` | 2026-10-04 | `08c6fe8` | **changed** (race timing) | Blind-POST first rung `-400` → `-369` ms, per-POST round trip logged, dev sends no email unless its booker fails (#317–#319) |
| `infra/v3.9.0` | 2026-10-02 | `68c4b8c` | unchanged (email, web) | The miss email, Course username hint, operator page per person, Your account (own name), sign-in note for non-Gmail addresses (#307, #309–#314) |
| `infra/v3.8.0` | 2026-10-02 | `f38f481` | **changed** (race timing) | Blind burst 3 → 2, ladder `-400/0`; time pickers bounded to each course's tee-sheet hours (#305, #306) |
| `infra/v3.7.0` | 2026-10-02 | `b1610f8` | unchanged (watcher email, web) | Operator email per watcher booking, cancel email off the request path, Connected Courses UX after the first new user (#301, #302) |
| `infra/v3.6.0` | 2026-09-30 | `69abc3a` | **changed** (race timing) | Blind-POST ladder -400/-250/0 + `too_early` reason, ranking explainer, full-repo-scan fix batch, prod deploys only code on main (#292–#299) |
| `infra/v3.5.0` | 2026-09-29 | `8466ad1` | unchanged (web) | Invite + feedback respond at once, every button shows a click, operator Uninvite (#288, #289) |
| `infra/v3.4.0` | 2026-09-29 | `f149142` | unchanged (web, email, CI) | Mail from hello@spicyteetimebooker.com, Report a bug + Request a course (also filed as anonymized GitHub issues), migrate-start retry (#279–#285) |
| `infra/v3.3.0` | 2026-09-29 | `be66b5a` | unchanged (web, email, infra) | Connect-a-course CTA, "Connected courses", invitation emails + Resend, dashboard Cancel button, prod email domain stage 1 (#275–#278) |
| `infra/v3.2.0` | 2026-09-29 | `fbb6b5e` | unchanged (web, email, CI) | Dashboard cancel, friendlier emails, admin user list + uninvited sign-ins, static cache-busting, faster deploys (#267–#273) |
| `infra/v3.1.0` | 2026-09-28 | `4421fb0` | unchanged (reporting only) | Operator summary v2, prod web always warm, prod on spicyteetimebooker.com (#262, #263, #265) |
| `infra/v3.0.2` | 2026-09-27 | `18ba1ca` | **changed** (retries) | Retry audit: bounded transient-only retries on the tenant path (#260) |
| `infra/v3.0.1` | 2026-09-27 | `3bba87c` | **changed** (watcher) | Tenant watcher search fix: ForeUP `search()` needs no login (#258) |
| `infra/v3.0.0` | 2026-09-27 | | **changed** | MU-18 stage B: prod booking + watch jobs run the multi-user tenant path (#257) |
| `infra/v2.17.0` | 2026-09-27 | `6b8122b` | unchanged (jobs stay TOML) | MU-18 stage A: prod web app, ACS email, tenant store; all multi-user code in the image (#256) |
| `infra/v2.16.0` | 2026-08-24 | `4462f56` | unchanged | Dependency refresh + dep-comment drift guard (#203–#206) |
| `infra/v2.15.0` | 2026-08-16 | `8342b67` | unchanged | Blind-POST rejection reason tagging (#201) |
| `infra/v2.14.0` | 2026-08-15 | `e6a8abb` | **changed** | T0 blind-POST stagger (#199) |
| `infra/v2.13.0` | 2026-08-09 | `de90316` | unchanged | Dependency + toolchain refresh |
| `infra/v2.12.0` | 2026-08-02 | `a7a1a6c` | unchanged | Log-redaction filter + 0-match search diagnostics |
| `infra/v2.11.0` | 2026-07-18 | `cdf5618` | **changed** | Burst reverted to 3 + server-`Date` logging (#181, #182) |
| `infra/v2.10.0` | 2026-07-15 | `fb2a133` | **changed** | Email-OTP response batch, burst-of-one (#177–#179) |
| `infra/v2.9.0` | 2026-07-10 | `a67eccd` | **changed** | 2026-07-09 scan fix batch + python 3.14 base |
| `infra/v2.8.0` | 2026-06-29 | `21b24cf` | **changed** | Blind-POST fallback rework + scan hardening (#157–#164) |
| `infra/v2.7.0` | 2026-06-23 | `b2c0051` | unchanged | Observability + redaction hardening (#153, #154) |
| `infra/v2.6.0` | 2026-06-23 | | unchanged | Shared-ACR consolidation (infra only) |
| `infra/v2.5.0` | 2026-06-22 | `ddf0112` | **changed** | Blind-POST race feature (#125–#130, #131) |
| `infra/v2.4.0` | | | **changed** | Race pre-warm bundle |
| `infra/v2.2.0` | | | **changed** | Within-window upgrade |
| `infra/v2.1.0` | 2026-06-10 | | **changed** | Multi-day Sat+Sun, cutoff + skip-days live |

## infra/v3.10.0: 2026-10-04 (`main`@`08c6fe8`)

**Booking behaviour CHANGED: the early blind POST fires at -369 ms.** ForeUP's clock lead has
been shrinking: a -500 ms POST was accepted until about 2026-09-28, -400 was accepted Oct 1–3, and
on Oct 4 two POSTs 1 ms apart straddled the flip: -396 → `gone[too_early]` (server `Date`
09:59:59), -395 → BOOKED (10:00:00). That `too_early` cost one row (window 07:00–08:00, Sun Oct 11)
its rank-0 slot; its T0 POST then found 07:37 "Time not available." and the fallback search saw
nothing before 08:30 four seconds after open, while a week earlier an 08:07 was still free 27 s
after open. The early block was most likely not online at all (the Oct 3 Flamingo Swing Classic
morning showed the same shape: nothing before 14:00 at open), so the better rung would probably not
have saved that row, but the clock problem is real and recurs. Operator: -369.

- **First rung `-400` → `-369`** (#318). `early_arrival_ms = 369`, `blind_post_stagger_ms =
  [-369, 0]` in the code defaults (the tenant booker reads these) and both shipped configs; burst
  stays 2. The docs-consistency sweep now treats `-400/0` as a retired ladder. The lead has been
  moving ~100 ms a week; if -369 logs `gone[too_early]`, move it later again, or build the pre-T0
  clock probe (BACKLOG).
- **Per-POST round trip logged** (#319). The diagnostic line ends with `(answered in <n>ms)`:
  whether the T0 rung could ever be skipped once the early rung has booked, without waiting,
  depends on ForeUP answering inside the stagger gap, and nothing measured that (a laptop GET
  sees ~0.6–0.9 s to first byte). Over the last 60 days 7 of 17 drops booked two or three and
  cancelled the extras, which the operator wants to avoid without giving up the hedge's latency;
  the next drops decide.
- **Dev sends no email unless its booker fails** (#317). A dry-run environment logs every user
  event (kind + row id) and mails no one, operator included; the booker's summary goes out only on
  a non-zero exit; a feedback report on a dry-run site files its issue but logs the email. The
  invitation email is the one exception. Replaces the one-day redirect-to-operator from the
  2026-10-03 fix.

**Deploy:** tag pushed 14:08 ET; approved by Claude at 14:50 ET on the operator's "you can approve
it" (the first approval attempt was blocked by the permission layer and handed to the operator);
pass 1 skipped, pass 2 + migrations green, done 14:57 ET. **Verified:** both booking jobs and the
watch job run `teetime:08c6fe8…`; `/healthz` 200 on https://spicyteetimebooker.com;
`teetime-web-prod` revision `--0000018` on the same image; the first watch run on the new image
(15:00 ET, `-29852340`) Succeeded. **First exercise:** Monday's 05:50 run (for Mon Oct 12) shows the -369 rung
and the round trip on the per-POST lines; the first contested morning is Saturday Oct 10 (for
Sat Oct 17).

## infra/v3.9.0: 2026-10-02 (`main`@`68c4b8c`)

**Booking behaviour unchanged.** The afternoon's operator requests, all web and email.

- **The miss email** (#307). When the 06:00 run cannot book the release-day date the person now
  gets "No tee time yet": why it usually happens (the course blocked the morning for an event or
  an outing), what the watcher will do until the real cutoff ("before 4 PM on Friday, October 9"),
  honest odds, a consoling one-liner, signed "— Spicy's helper". A booking-service failure says
  "This one was on our side". No more `Details: no_inventory` in user mail.
- **"Course username" explained** on the Connect form (#309): your login at the course, not
  necessarily the email you use here.
- **An operator page per person** (#310, #312): the email in `/admin/users` links to
  `/admin/users/{id}`: sign-in, Connected Courses (status, login, snapshot age, default price),
  weekly bookings, and the next 21 days with what they asked for, status and the booked tee time.
  Read-only, never a course login.
- **Your account** (#311, #314): your name next to Sign out opens `/me`, where you set your own
  name (the invite's or the sign-in's name is only the default); it is how the emails greet you and
  how the operator sees you.
- **Sign-in page note for non-Gmail addresses** (#313): make a Google account on the invited
  address ("Use my current email address instead"), then Continue with Google.

**Deploy:** tag pushed 13:27 ET, approved by Claude on the operator's request ("ok go ahead and
cut v3.9.0 to prod"); pass 1 skipped, pass 2 + migrations green, done 13:34 ET. **Verified:** both
booking jobs, the watch job and `teetime-web-prod` (revision `--0000017`) run `teetime:68c4b8c…`;
`/healthz` 200 on https://spicyteetimebooker.com; the first watch run on the new image (13:40 ET, `-29849380`) Succeeded. **First exercise of the
miss email:** the next release-day miss (a `MISSED_DROP` from the 05:50 run).

## infra/v3.8.0: 2026-10-02 (`main`@`f38f481`)

**Booking behaviour CHANGED: the T0 blind burst is two POSTs.** Since the 2026-09-30 ladder change
the -400 ms POST (the rank-0, best slot) won consistently, so the second early POST at -250 ms
bought nothing and only produced a surplus reservation to cancel. Operator directive 2026-10-02:
"one 400 ms early and one right at the time window."

- **Burst 3 → 2, ladder `-400/-250/0` → `-400/0`** (#305). `blind_post_max_count = 2` and
  `blind_post_stagger_ms = [-400, 0]` in the code defaults (the tenant booker reads these) and both
  shipped configs; `early_arrival_ms` stays 400. A one-account drop now pre-solves 4 CAPTCHA tokens
  (burst 2 + reserve 2, was 5) and the per-course blind-account cap `C // burst` rises (C = 6 → 3
  accounts, was 2). Trade-off: the hedge is two slots, not three, and if ForeUP's clock drifts and
  -400 goes `too_early` again the T0 POST is the only hedge, so watch the per-POST line on the first
  drops. The docs-consistency sweep now treats `-400/-250/0` as a retired ladder.
- **Time pickers list only the course's tee-sheet hours** (#306; operator: "no reason to show 4 AM
  or 9 PM; per course"). Every From/To bound on the booking forms is a list of quarter hours inside
  `courses/names.py::COURSE_TEE_SHEET_HOURS` (Mangrove Bay 6:30 AM–7:00 PM from a live look at the
  October sheet plus summer headroom; Sydney Marovitz 6:00 AM–7:00 PM, an unobserved assumption; no
  entry = the whole day). With script each option row follows the course chosen on that row; without
  it the list spans the person's courses and the server refuses a window outside the chosen course's
  hours with a 400 naming the course, its hours and the picked times (every create/edit path,
  checked before any write).
- Also in: ruff 0.16.9 (#304).

**Deploy:** tag pushed 11:45 ET, approved by Claude on the operator's request ("ok yes, it's ready
to go to prod"); pass 1 skipped, pass 2 + migrations green, done 11:53 ET. **Verified:** both booking
jobs, the watch job and `teetime-web-prod` (revision `--0000016`) run `teetime:f38f481…`;
`/healthz` 200 on https://spicyteetimebooker.com; the first watch run on the new image (12:00 ET, `-29849280`) Succeeded; the 11:50 ET run (`-29849270`) was the last on `b1610f8`. **First exercise of the
two-POST burst:** the next release drop (Sat 2026-10-03 05:50 ET job for 10/10); look for exactly
two `blind-POST sent` lines (`-400` and `+0`) and no `gone[too_early]` on the -400 POST.

## infra/v3.7.0: 2026-10-02 (`main`@`b1610f8`)

**Booking behaviour unchanged.** Everything here came from watching the first new user connect
Mangrove Bay, book a date inside the 7-day window and cancel it (2026-10-01).

- **The operator hears about every booking** (#301). The 06:00 run's summary already listed its
  bookings, but a tee time the WATCHER booked in the afternoon reached only the user. `tenant-watch`
  now sends one short operator email per booking or upgrade (`[TeeTimeBooker · PROD] Booked:
  <name> · Mangrove Bay Sat Oct 4 at 9:07 AM`), after the user's own mail and even when the user
  cannot be mailed; the booker stays copy-free (pinned by an AST test).
- **The cancel email no longer holds the page** (#301). Prod logs for 9/28–10/01 put the ForeUP
  `DELETE` and the ACS `202 Accepted` within one second on every cancel and ACS `Succeeded` 2–6 s
  later, so the "minutes" the user saw are downstream of ACS; the page used to wait for that poll
  before redirecting and now spawns the send as a background job. Every ACS success line logs its
  send-to-Succeeded duration.
- **Connected Courses** (#302): a connected course leaves the Connect dropdown (with every course
  connected the form gives way to a pointer to Re-verify); the form says it uses the login you
  ALREADY have at the course (it creates no account) and links the course's own booking site to
  create one first; every course states its release cycle, derived from its `ReleasePolicy` and
  the configured cutoff ("Tee times open 7 days ahead, at 6:00 AM Eastern. For first pick of the
  tee sheet, book a date 7 or more days out …"), on its card, under the Connect form and above
  both booking forms.

**Deploy:** tag pushed 00:05 ET, approved by Claude on the operator's request ("please merge to
prod yes"); pass 1 skipped, pass 2 + migrations green, done 00:10 ET. **Verified:** both booking
jobs, the watch job and `teetime-web-prod` (revision `--0000015`) run `teetime:b1610f8…`;
`/healthz` 200 on https://spicyteetimebooker.com; the first watch run on the new image (00:20 ET,
`-29848580`) Succeeded (`dry_run=False`, Cosmos `prod`, 3 rows, 2 searches, no errors). **First
exercise of the operator copy:** the next time the watcher books between runs (the 06:00 run keeps
its summary).

## infra/v3.6.0: 2026-09-30 (`main`@`69abc3a`)

**Booking behaviour CHANGED: the T0 blind-POST ladder.** On 2026-09-29 and 2026-09-30 the -500 ms
POST (the rank-0, best slot) was refused before the release: `Booking for <date> starts at <date>
6:00am (EDT)`, server `Date` 09:59:59, logged `gone[unknown]`, and the booking went to the
second choice. Through 9/28 every POST, -500 included, reached ForeUP at 10:00:00 by its clock
(it had run ~0.5 s fast; our NTP offset and fire drift stayed within a few ms). Changes:

- **Ladder -500/-250/0 → -400/-250/0**, `early_arrival_ms` 500 → 400 (operator decision; the
  `SchedulerConfig` defaults the tenant booker uses, plus `container.toml` / `example.toml`).
  Watch the per-POST line: `blind-POST sent -39Xms (planned -400ms) … → BOOKED` means -400 now
  lands after the open; `→ gone[too_early]` means move it later again (#298).
- **`too_early` rejection reason:** that ForeUP body is tagged `too_early`, and the operator
  summary email says "rejected: too early (before the booking window opened)" instead of
  "reason unknown" (#298).
- **Website:** "How the bot picks your tee time" on both booking forms: a timeline + worked
  8:00-10:00 example (9:00, 9:07, 8:52, ...) computed from the real tee grid and pinned to the
  engine's order; the cutoff is worded from the config (#299).
- **Full repo scan fix batch** (#292–#297): one invite per address, the Enable notice, a throttled
  audit write for rejected sign-ins, the Uninvite vs first-sign-in races (claim-first Cosmos bind);
  every ACS / GitHub-issue / OAuth / background-job failure logged with its reason, public issue
  diagnostics without the login-failure count; stale stubs, docstrings, Bicep comments; CI token
  read-only, web env ↔ `webapp.bicep` parity test, Python 3.14 floor; **Log Analytics daily cap
  0.5 GB/day** (normal ingestion < 10 MB/day); urllib3 2.8.0 (3 CVEs) with `pip-audit` in the
  pre-push hook (#293).
- **Deploy hygiene** (#296 + a repo setting): the `prod` environment accepts only `main` and
  `infra/v*` tags, and `deploy-prod`'s first step refuses a commit that is not on `main`; deploy
  jobs have 60-min timeouts and one concurrency group per env. This release is the first deploy
  through that guard (it passed).

**Deploy:** tag pushed 13:19 ET, approved by Claude on the operator's request, pass 1 skipped
(AcrPull grant exists), pass 2 + migrations green. **Verified:** both booking jobs, the watch
job and `teetime-web-prod` (revision `--0000014`) run `teetime:69abc3a…`; `/healthz` 200 on
https://spicyteetimebooker.com, which serves the new `base.css`; the prod workspace's
`dailyQuotaGb` reads 0.5; the first watch run on the new image (13:30 ET, `-29846490`) Succeeded
(`dry_run=False`, Cosmos `prod`, 1 row, 1 search, no errors). **First exercise of the new ladder:**
the next 06:00 ET booking run (Thu 2026-10-01; Saturday's drop is 2026-10-03).

## infra/v3.5.0: 2026-09-29 (`main`@`8466ad1`)

Booking behaviour is unchanged: website only.

- **Instant responses + click feedback** (#288). Invite, Resend invite and Report a bug / Request a
  course respond at once; the email (and the GitHub issue) are sent by a background job after the
  response (`web/background.py`), because awaiting ACS's delivery polling hung the page 5-20 s.
  Every button answers a click: a submitted form's buttons disable and the clicked one shows a
  spinner, button-styled links get the spinner, everything has a pressed look without script; the
  busy state clears on any return to the page and after a 30 s safety timeout. The notices say what
  to do if an email does not arrive; a report that reached no one logs an ERROR. Fixed on the way:
  the audit key `emailed` was always stored as `***` (redaction masks any key containing "mail");
  it is now `sent`.
- **Uninvite** (#289). A still-Invited person on `/admin/users` gets Uninvite (a confirm
  disclosure); `TenantStore.delete_invited_user` removes only a never-bound invite (Cosmos: IfMatch,
  so a racing first sign-in wins); a signed-in person is Disabled instead.

Verified after the deploy (run 36632022361, 6 min 46 s, pass 1 skipped): both booking jobs, the
watcher and the web on `teetime:8466ad1`; the web's sender still `hello@spicyteetimebooker.com`;
`/healthz` 200; the live `/static/app.js` (versioned link) carries the button safety timeout; the
first watch run fully after the deploy (17:30 ET) succeeded on the new image.

## infra/v3.4.0: 2026-09-29 (`main`@`f149142`)

Booking behaviour is unchanged: everything here is the website, user email or the deploy pipeline.

- **Dashboard "Checked" column dropped** (#279).
- **Prod mail comes from "Spicy's Tee Time Booker" <hello@spicyteetimebooker.com>** (#280). The
  ACS customer-managed domain is linked and the `hello` sender added: stage 2 of AZURE_PLAN
  §10.10.
- **Report a bug + Request a course** (#281): both email the operator. The accounts page is now
  "Connected Courses".
- **Deploy retries starting the migrate job** (#282). It follows an already-Running execution
  instead of starting a second one, and a failed status poll costs one poll, not the deploy.
- **Report a bug moves to the top bar**, next to Sign out (#283).
- **Bug reports carry diagnostics** (#284), plus a CI guard against committed merge-conflict
  markers.
- **Site reports are also filed as anonymized issues in this public repo** (#285).

Verified after the deploy (run 36622553035, 5 min 38 s, pass 1 skipped): both booking jobs, the
watcher and the web app on image `teetime:f149142`; the web app's env has
`ACS_EMAIL_SENDER=hello@spicyteetimebooker.com` and
`GITHUB_ISSUES_REPO=wardcrazy01894/TeeTimeBooker`, with the token from the `GITHUB-ISSUES-TOKEN`
secret; the ACS domain shows Domain, SPF and DKIM Verified and the `hello` sender "Spicy's Tee
Time Booker" is present; `/healthz` 200.

## infra/v3.3.0: 2026-09-29 (`main`@`be66b5a`)

Booking behaviour is unchanged. (This section was written late, with v3.4.0's; the facts are from
the deploy run and the verification done at the time.)

- **Dashboard polish** (#275). The header no longer shows the sign-in subject ID (operators see it
  on `/admin/users`); a user with no course gets a "Start here" card with a large **Connect a
  course** button; the `/accounts` page is named "Connected courses" (capitalized to "Connected
  Courses" in v3.4.0).
- **Invitation email + Resend invite** (#276). Invite on `/admin/users` emails "You're invited to
  Spicy's Tee Time Booker!" (operator-approved wording); still-invited people get a Resend button.
- **Prod customer-managed email domain, stage 1** (#277). `spicyteetimebooker.com` created on the
  prod ACS email service (not yet linked; mail still from the azurecomm.net address). Its DNS
  records then went into Cloudflare and verified the same day (AZURE_PLAN §10.10); stage 2 shipped
  in v3.4.0.
- **Dashboard Cancel button** (#278) looks like the Dates page's, and still confirms.

Verified after the deploy (run 36611229843, 6 min 26 s, pass 1 skipped): both booking jobs, the
watcher and the web on `teetime:be66b5a`; `/healthz` 200; the web's sender still
`DoNotReply@<…>.azurecomm.net` (unchanged, as intended); the `spicyteetimebooker.com` domain present
as `CustomerManaged` with its Domain/SPF/DKIM/DKIM2 records to add.

## infra/v3.2.0: 2026-09-29 (`main`@`fbb6b5e`)

Booking behaviour is unchanged: everything here is the website, user email or the deploy pipeline.

- **Emails show the course's wall clock** (#267). Stored tee times (Cosmos returns UTC) were
  mailed raw, e.g. a cancel email said 12:30 PM for an 8:30 AM EDT booking.
- **Booking form pickers** (#268). Players is a 1-4 button row; the date is a month calendar paged
  side to side, over the native date input (works with script off).
- **Cancel from the dashboard + friendlier emails** (#269). A booked tee time has a "Cancel…"
  disclosure on the dashboard. The internal `TTB:` confirmation id is gone from the dashboard and
  user email. The booked email lays out course / date / tee time and signs off with a random golf
  one-liner; the cancel email closes with "Hope to see you back on the course soon."
- **Static cache-busting** (#270). CSS/JS links carry a content hash and static files are served
  `Cache-Control: no-cache`, so a deploy can no longer leave browsers on stale files.
- **Faster deploys** (#271). Deploy pass 1 (the bootstrap image) is skipped once the environment's
  AcrPull grant exists (fail-safe); dev deploys went ~11 → ~6 min, and the web app and jobs no
  longer sit on the placeholder image during a deploy. This is the first prod deploy with it.
- **Admin user list** (#272) and **uninvited sign-in attempts** (#273) on `/admin/users`:
  everyone invited with status, provider, courses, weekly and upcoming bookings and a per-row
  Disable/Enable; and who tried to sign in uninvited (verified emails, attempts, first/last try,
  kept 90 days after the last try) with an Invite button. New Cosmos `global` doc type
  `rejected_signin` (per-item TTL); no index or Bicep change.

Verified after the deploy (run 36592990239, 6 min 5 s): `Detect bootstrap need` found the prod
MI's AcrPull grant and pass 1 was skipped; both booking jobs, the watcher and the web app (min
replicas 1) on image `teetime:fbb6b5e`; `/healthz` 200 on the apex; `/login` links
`/static/base.css?v=…` and `/static/app.js?v=…`, and static files carry `Cache-Control:
no-cache`; the first watch run fully after the deploy (12:00 ET) succeeded on the new image.

## infra/v3.1.0: 2026-09-28 (`main`@`4421fb0`)

- **Operator summary v2** (#262). The booking run's operator email names each person, shows every
  POST of the burst (tee time, send offset from T0; kept / cancelled extra / rejected with reason /
  UNCERTAIN / unconfirmed), lists PROBLEMS first and counts them in the subject, and tags the
  subject with the environment (`[TeeTimeBooker · PROD]`). Booking behaviour is unchanged: the
  recorder now also keeps `SlotGoneError` rejections, for the report only.
- **Prod web always warm** (#263). `webMinReplicas = 1` in prod (dev 0): no more ~30 s
  scale-from-zero cold start; ~$5.83/month idle (AZURE_PLAN §9). The killswitch still forces 0.
- **Prod on https://spicyteetimebooker.com** (#265; #264 auto-closed when its stacked base was
  deleted). Apex + `www` bound SNI to ACA managed certificates (created by the AZURE_PLAN §10.9
  runbook the same day); `www` and the old `teetime-web-prod.wittydesert-02f9f0cd…` host 301 to the
  apex (canonical-host redirect, so OAuth state lives on one host). Google OAuth client lists the
  new callback; the old one is kept.

Verified after the deploy: web `minReplicas 1` with one replica running; `/healthz` 200 on the
apex; `www` and the old host 301 to the apex; certificate `CN=spicyteetimebooker.com` (DigiCert
GeoTrust, valid to 2027-03-28); `/login/google` sends `redirect_uri=https://spicyteetimebooker.com/auth/google/callback`;
both booking jobs and the watcher on image `teetime:4421fb0`.

## infra/v3.0.2: 2026-09-27

**Retry audit** (#260, operator request). Two real bugs fixed: (1) the booker's WRITE #2 retry never
fired against Cosmos, because `CosmosTenantStore.record_outcomes` wraps even a transient 503 in an
`ExceptionGroup` and the runner (and the watcher's `_write`) treated any group as a refusal, so one
blip lost a booked row's ownership record; both now retry when every error in the group is
transient; (2) the web connect / re-verify login probe was being replayed by the adapter's
transport retry despite §8.4, and is now single-attempt (`set_transport_retries(0)` on the probe
adapter only). Added bounded, transient-only retries (`tenant/retry.py`): the booker's READ #1 and
claim (only while the retry finishes before the race window; never inside it), the watcher's reads,
materializer tick and outcome writes, one retry for a group's shared search (a 429 still aborts),
2captcha submit (poll budget unchanged, key never in messages) and `tenant-migrate`'s
`initialize()`. Never retried: ForeUP `book()`, the login probe, leases, snapshot writes.

## infra/v3.0.1: 2026-09-27

**Tenant watcher search fix** (#258). Since the tenant watcher went live (dev at MU-17, prod at
`infra/v3.0.0`) every one of its shared, unauthenticated searches failed with `RuntimeError`:
`ForeUpAdapter` built its HTTP client only inside `authenticate()`. The watcher therefore could not
see open tee times to book a missed date or upgrade (its reconcile check-ins still ran); the 05:50
booker and the web log in first and were unaffected. `search()` now creates the client on first
use; `book()` still raises `AuthError` without a login, and `list_reservations()` still refuses on
a client that only a search built (a new `_search_only_client` flag), so the layer-2 pre-book guard
can never read an empty cache as "no bookings".

## infra/v3.0.0: 2026-09-27

**MU-18 stage B** (#257, MULTIUSER_PLAN §11 step 7, AZURE_PLAN §10.8). **Booking-behaviour change:**
prod's two booking jobs run `tenant-run --event mb0600et --wait --dry-run false` and the watch job
runs `tenant-watch --dry-run false` every 10 minutes, over the shared Cosmos `prod` database, with the
Manual `teetime-migrate-prod` job created and run by CI after deploy pass 2. Crons and timeouts are
unchanged (`50 9 * * *` / `50 10 * * *`, 1200 s; `*/10 * * * *`, 300 s). The operator connected the
Mangrove Bay account, saved the Sat + Sun weekly booking and adopted the TOML bot's live
reservations as owned (MU-16b) before the tag, and again right after the deploy. The one-account
tenant run fires the same slots at the same offsets with the same token budget as the TOML `run`
(`test_single_account_run_matches_todays_burst`). Rollback: set both modes back to `toml` and tag.

## infra/v2.17.0: 2026-09-27 (`main`@`6b8122b`)

**MU-18 stage A** (#256, MULTIUSER_PLAN §11 step 5, AZURE_PLAN §10.8). Prod gets the web app
(`teetime-web-prod`, Google sign-in), ACS email and the shared Cosmos endpoint (`prod` database),
while the booking and watch jobs stay on the TOML path (`run --wait` / `watch`). The image also
carries every multi-user change to the shared engine since `infra/v2.16.0` (the SharedCaptchaPool
refactor MU-2, the widened Mangrove Bay grid + allowlist hook MU-3, the E5/E6/E7 hooks MU-4), each
built behaviour-preserving for the TOML path and pinned by tests. Operator prerequisites (the five
prod Key Vault secrets, Key Vault Secrets Officer for the CI SP, the prod redirect URI on the
Google client) were in place before the tag.

## infra/v2.16.0: 2026-08-24 (`main`@`4462f56`)

The **dependency refresh + dep-comment drift guard** (#203/#204/#205/#206). **NO booking-behavior
change** — nothing touches slot selection, burst size, stagger offsets, timing, or any T0 decision
path (same risk class as `infra/v2.13.0`, the previous dependency refresh).

**Runtime dep IN the image:** `idna` 3.18→3.19 (#204) — transitive via httpx/anyio, never imported
directly, and NOT security-driven: the CVE-2026-45409 boundary is **3.15**, which prod cleared two
releases ago. **NOT in the image** (`uv sync --no-dev` excludes the PEP 735 dev group; setup-uv is a
CI runner action only): `astral-sh/setup-uv` 9.0.0→10.0.1 (#203 — a MAJOR bump; v10 disables the
cache on sensitive events, and it is used only in `ci.yml`), `ruff` 0.16.2→0.16.3 and `mypy`
2.3.0→2.3.1 (#205).

**Docs/tests only (#206):** the `idna` comment in `pyproject.toml` asserted "Floor now tracks the
locked version (3.18)" while sitting above `idna>=3.19` — false the moment it merged, and the
**second** drift of that same claim (cleaned in #106, re-drifted by #197/#204). Dependabot bumps
that floor on every idna release, so ANY version literal in the comment is stale by construction;
the comment now names **no** tracking version and two guards in `tests/test_docs_consistency.py` pin
it — the comment may name no version but the CVE boundary, and the floor may never drop below that
boundary. CLAUDE.md's change→docs map gained a row for the class. Known scope limit: the guard is
**idna-only**, so a NEW tracking comment on another dep would not be caught (no other dep carries
one today — the `ruff`/`mypy` comments state facts that survive bumps).

**Why deployed at all:** only `idna` reaches the image and it is not security-driven, so this was
initially recommended for batching — but with no further changes expected for a while there is no
next tag to batch into, and prod would otherwise sit indefinitely behind. Operator decision
2026-08-24.

Verification: 761 tests, ruff + ruff format + mypy strict clean, `uv lock --locked` ok, two
adversarial review rounds on #206 (APPROVE, APPROVE), dev auto-deploy green on this commit. Prod
param latch verified pre-tag (`dryRun=false`, `killswitchFired=false`, `enableSchedules=true`). All
three prod jobs verified live on `teetime:4462f561b771c8f1dfaaf0f856edb5b39dac9fd8` with crons +
timeouts + `dryRun=false` UNCHANGED from the v2.15.0 baseline: `teetime-job-prod-edt` `50 9 * * *` /
1200 s, `teetime-job-prod-est` `50 10 * * *` / 1200 s, `teetime-watch-job-prod` `*/10 * * * *` / 300
s; post-deploy watch cycle at 16:30 UTC ran clean (authenticated, saw both held reservations,
checked both target dates). First exercise: the Sat 2026-08-29 05:50 ET drop (books 9/5).

## infra/v2.15.0: 2026-08-16 (`main`@`8342b67`)

Blind-POST rejection **reason tagging** (#201). **NO booking-behavior change** — nothing touches
slot selection, burst size, timing, or any T0 decision path (same risk class as
`infra/v2.7.0`/`v2.12.0`). ForeUP returns HTTP 400 for two rejections carrying OPPOSITE evidential
weight, with no machine-readable discriminator — only the `msg` prose differs: `"Time not
available."` (the slot was not bookable — claimed first, OR our POST beat the release flip; the ONLY
one bearing on the pre-open-vs-race question) versus `"...1 online reservation per day."` (ForeUP
bouncing the SURPLUS POSTs of a burst we ALREADY WON). The burst collapsed both into `gone`, and the
aggregate line asserted "claimed pre-book" for every 4xx — so the 2026-08-16 drop (1 booked / 2
`daily_limit`) reported two lost races that never happened, polluting exactly the offset→outcome
signal `v2.14.0`'s stagger exists to produce, immediately before the Sat 8/22 reading.
`SlotGoneError` now carries `reason` (`unavailable` | `daily_limit` | `conflict` | `unknown`),
tagged by `ForeUpAdapter._classify_book_rejection`; `_blind_outcome_label` logs `gone[<reason>]` and
the aggregate becomes `N of M slot(s) rejected (<reason>=<count>, …)`.

**Three deliberate design points:** (1) an ALL-`daily_limit` sweep with nothing booked is NOT
reported as a race wipeout — it means a reservation for that date ALREADY EXISTED which this burst
did not make, and `_reguard_before_fallback` then short-circuits to `ALREADY_BOOKED` without the
fallback search the old text promised (it stays WARNING; a reservation the pre-T0 layer-2 guard
could not see warrants attention); (2) markers cover only wordings OBSERVED live — a 35-day prod Log
Analytics sweep found exactly TWO distinct book-rejection bodies, so the speculative "no longer
available" marker was dropped and anything unmatched surfaces as `gone[unknown]` rather than being
misfiled as race evidence; (3) `reason` is DIAGNOSTIC ONLY — every value routes identically
(`SlotGoneError` → try-next-slot), verified against every `except SlotGoneError` site.

**Evidential caution (do not lose this before Sat 8/22):** the 1-booked/2-`daily_limit` shape is NOT
established as a stagger effect. It was the first non-uniform outcome in the LOG RETENTION WINDOW,
but the pre-stagger 2026-07-11 drop produced the same shape from a SIMULTANEOUS burst (see the
`infra/v2.9.0` paragraph), so a simultaneous burst can also serialize behind ForeUP's 1/day counter.
Treat the shape as uninformative about timing until more drops land.

**Known open question (BACKLOG):** whether ForeUP's 1/day rule is scoped to the PLAY date or the
CALENDAR day the booking is made. The multi-day design implies play-date and nothing observed
contradicts it, but it is unpinned — if it were booking-day scoped, an all-`daily_limit` Sunday
burst could be caused by Saturday's reservation and the "we already hold a reservation for this
date" wording would be wrong.

Verification: 759 tests, ruff + ruff format + mypy strict clean, TWO adversarial review rounds
(APPROVE, APPROVE — round 2 caught the causal over-claim above). All three prod jobs verified live
on `teetime:8342b67f2113657b45786191b72a859df25ac4c5` with crons + timeouts + `dryRun=false`
UNCHANGED from the v2.14.0 baseline: `teetime-job-prod-edt` `50 9 * * *` / 1200 s,
`teetime-job-prod-est` `50 10 * * *` / 1200 s, `teetime-watch-job-prod` `*/10 * * * *` / 300 s.
First exercise: the Sat 2026-08-22 05:50 ET drop (books 8/29) — which is also the first real
diagnostic reading of the stagger itself.

## infra/v2.14.0: 2026-08-15 (`main`@`e6a8abb`)

The **T0 blind-POST stagger** (docs/plans/STAGGER_PLAN.md, #199). **BOOKING-BEHAVIOR CHANGE**, race
path only (`--wait` + blind-capable primary) — the first since `infra/v2.11.0`. The T0 blind burst
no longer fires all N POSTs at one instant: each sleeps to its own offset from
`scheduler.blind_post_stagger_ms` (default `(-500, -250, 0)` ms relative to T0), paired positionally
with the RANK-ordered slots, so the burst SPANS ForeUP's release boundary instead of point-sampling
it.

**Why:** every drop in the Log Analytics retention window came back 3/3 or 0/3, NEVER mixed — a
shape a genuine slot race cannot produce, since our POSTs land within ~100 ms of the open and nobody
books three specific tee times in 100 ms. A burst arriving before the release flip gets the SAME
`400 {"success":false,"msg":"Time not available."}` a claimed slot returns, and the server `Date`
header's 1-second resolution (added in `infra/v2.11.0` for exactly this question) cannot separate
them. Staggering is both a HEDGE (one POST is always SENT no earlier than T0, so a wipeout can't
take the burst as a unit) and a DIAGNOSTIC (outcomes become ordered by offset). Motivated by the
2026-08-15 miss (target Sat 8/22) and the still-unexplained 2026-07-18 miss. **Non-regression is the
binding constraint and is pinned mechanically:** `stagger[0] == -early_arrival_ms`, so the rank-0
(nearest-midpoint) slot fires at EXACTLY its pre-stagger instant and every drop already won is
unchanged — only the surplus POSTs move, and those are already 400'd by the 1/day rule when rank-0
wins. `tests/test_container_config_parity.py` asserts both `stagger[0] == -early_arrival_ms` and
`min(stagger) == -early_arrival_ms` (operator directive 2026-08-15: nothing may be scheduled EARLIER
than today's fire instant). The tail offset is `0` — SENT at 06:00:00.000, carried past the open by
network latency on ARRIVAL — chosen over `+250` to give up the least ground in a genuine race.

**Three supporting changes:** (1) the burst RE-RANKS with `rank_slots_for_request` before pairing
offsets — ranked order had been only an adapter convention the simultaneous burst never depended on,
and is now a safety property, since a worse slot POSTing first would let the 1/day rule reject the
better one; (2) a `field_validator` rejects a DESCENDING offset list (`(-500, 0, -250)` passes every
parity assertion while doing exactly that); (3) the per-POST diagnostic reports the **MEASURED**
send offset, not the planned one — on a run starting past T0 every delay is non-positive and all
POSTs go out simultaneously, so logging the planned ladder would show instants that never happened
and an operator reading a 0/N would wrongly conclude "unordered ⇒ not the boundary". Known
limitation (accepted, BACKLOG.md): offsets correlate with slot rank, so the offset→outcome signal is
confounded — a control POST (same slot at two offsets) and CAPTCHA-token recycling are deferred.

Verification: 749 tests, ruff + ruff format + mypy strict clean, TWO adversarial review rounds
(BLOCK → APPROVE), dev deploy green on this commit. All three prod jobs verified live on
`teetime:e6a8abbe72d4846099864e5d032720ad3017470a` with crons + timeouts + `dryRun=false` UNCHANGED
from the v2.13.0 baseline: `teetime-job-prod-edt` `50 9 * * *` / 1200 s, `teetime-job-prod-est` `50
10 * * *` / 1200 s, `teetime-watch-job-prod` `*/10 * * * *` / 300 s. First exercise: the Sun
2026-08-16 05:50 ET drop (books 8/23) — a non-regression check only, since both 0/3 misses (one
explained, one not) fell on SATURDAYS and every Sunday in the retention window booked cleanly. First
real diagnostic reading: the Sat 2026-08-22 drop.

## infra/v2.13.0: 2026-08-09 (`main`@`de90316`)

The dependency + toolchain refresh. **No booking-behavior change** — nothing touches slot selection,
burst size, timing, or any T0 decision path (same risk class as `infra/v2.7.0`/`v2.12.0`). Runtime
deps IN the image: `click` 8.3.3→8.4.2, `idna` 3.17→3.18 (`httpx` 0.28.1 and `pydantic` 2.13.4 were
already current). Dev toolchain — NOT in the image, `uv sync --no-dev` excludes it: `ruff`
0.15.15→0.16.2, `mypy` 2.1.0→2.3.0, `pytest` 9.0.3→9.1.1, `pytest-asyncio` 1.3.0→1.4.0, `pip-audit`
2.10.0→2.10.1. Floors in `pyproject.toml` were raised to match; they had drifted badly (`ruff>=0.5`
against a locked 0.15.15), advertising support for untested versions.

**One application-code change, behavior-preserving (#197):** `UpgradeOrchestrator. _persist_upgrade`
and `._cancel_and_book_slot` are now KEYWORD-ONLY past their leading args. `_persist_upgrade` takes
`current_booking` AND `new_result`, **both `BookingResult`** — a positional transposition
type-checked clean and would have silently persisted the OLD booking as the upgrade result. mypy
cannot catch same-typed adjacent params; the `*` can. Surfaced by ruff 0.16 promoting `PLR0917` to
stable. Verified argument-by-argument at both call sites and covered by
`test_upgrade_deletes_then_reinserts_terminal_under_lock`, which fails on a swap. `PLR0917` is
deliberately NOT ignored globally — only per-file for the three orchestrator collaborator-injection
ctors (all-distinct types, so mypy catches a swap) and `tests/**` date-builders; it stays live
everywhere else, verified non-vacuous.

**Two ruff-0.16 config consequences:** (1) `PLR0917` as above; (2) ruff 0.16 began formatting python
code blocks INSIDE Markdown, and our plan docs quote FRAGMENTS (bare ctor params) which it parses as
standalone statements and rewrites WRONGLY — turning `x: T | None = None,` into `x: T | None =
(None,)`, a TUPLE. Unnoticed, `ruff format .` would have silently corrupted 8 ratified design docs
into describing something other than what shipped. Markdown is now excluded (`extend-exclude =
["*.md"]` in `[tool.ruff]`).

**Supporting CI/config changes, no image effect:** Dependabot now manages Python via the `uv`
ecosystem (#192) with a WORKING prod/dev split — the first attempt silently collapsed because dev
deps were a `[project.optional-dependencies]` extra, which Dependabot classifies as production, so
#193 put pytest/ruff/mypy in the "python-runtime" group; #195 moved them to a PEP 735
`[dependency-groups]`, which ALSO made the Dockerfile's `uv sync --no-dev` load-bearing (it excludes
GROUPS, not extras — previously the image stayed lean only because extras are opt-in). `pip`
26.1.1→26.1.2 (#191) let the `PYSEC-2026-196` pip-audit suppression be retired (#194), so the CVE
gate runs unsuppressed. Dependabot alerts + security updates are ENABLED.

Verification: ruff clean, ruff format clean, mypy strict clean, 731 tests, pip-audit clean
unsuppressed, `uv lock --locked` passes, docker build + smoke green, dev deploy green on this
commit, three adversarial review rounds all APPROVE. Deployed while holding Sat 8/15 09:22 and Sun
8/16 09:22; first exercise is the Sat 2026-08-15 05:50 ET drop (books 8/22).

## infra/v2.12.0: 2026-08-02 (`main`@`a7a1a6c`)

The log-redaction filter + 0-match search diagnostics. **No booking-behavior change** — nothing
touches slot selection, burst size, timing, or any T0 decision path (same risk class as
`infra/v2.7.0`). Security (#187): the 2captcha API key no longer reaches stdout / Log Analytics.
httpx logs every request at INFO and the 2captcha result-poll URL carries the key as a query param —
71 such lines in the 2026-08-01 prod run. `core.redaction.RedactingLogFilter` +
`install_log_redaction()` attach to the root logger's HANDLERS (a logger-level filter does NOT see
records propagating up from `httpx`) and scrub the rendered message, `%`-args, `exc_info` tracebacks
and `stack_info`. The leaked key was **rotated** the same day (2026-08-02); both Key Vaults hold the
new value and the local `.env` was updated. Known gap (accepted): a traceback printed by Python's
default excepthook bypasses logging entirely. Observability (#188): a search that returns inventory
but matches NOTHING now logs a per-filter rejection tally + the span of tee times actually on offer,
against the requested window — which distinguishes a course-level block from a lost slot race. That
ambiguity cost real diagnosis time after the 2026-08-01 miss, whose cause turned out to be Mangrove
Bay's 8 AM shotgun Anniversary Tournament on the 8/8 TARGET date (no public tee time before ~16:07).
INFO when purely out-of-window (the routine sell-out), WARNING when any other leg fires. Also in
this tag: Dependabot action bumps (setup-python v7, setup-uv v9, checkout 7.0.1), SHA-pins verified
against upstream tags. All three prod jobs verified on the `teetime:a7a1a6c…` image with
crons/timeouts/`dryRun=false` unchanged. First exercise: the Sat 2026-08-08 05:50 ET drop (books
8/15).

## infra/v2.11.0: 2026-07-18 (`main`@`cdf5618`)

The T0-hedge restore + early-arrival diagnostic. Booking-behavior: the blind burst is reverted to
**3** (`blind_post_max_count=3`, default + all shipped configs) — burst-of-one (v2.10.0) bet the
whole drop on the single nearest-midpoint slot and lost that race with nothing else in flight (the
2026-07-18 Sat 7/25 miss, terminal `no_inventory`); the top-3 concurrent POSTs restore the slot-race
hedge, and ForeUP's 1/day rule 400-rejects the surplus once the first lands (`_cancel_extras` keeps
the best, proven live 2026-07-11 which booked the 3rd-ranked sibling). Observability:
`ForeUpAdapter.book()` logs ForeUP's server `Date` response header on every book POST — the server
clock at processing time disambiguates a **pre-open rejection** (the 500 ms `early_arrival_ms` fire
landing before the 06:00 ET open → 400 stamped 05:59:59) from a **genuine slot-race loss**
(06:00:00), which are byte-identical by body (the 2026-07-18 miss couldn't tell them apart). All
three prod jobs verified on the `teetime:cdf5618` image. First exercise: the Sun 2026-07-19 05:50 ET
drop (books 7/26) — the first burst-3 + Date-log drop.

## infra/v2.10.0: 2026-07-15 (`main`@`fb2a133`)

The email-OTP response batch (#177/#178/#179), shipped the same day MB's email-OTP gate went live.
Booking-behavior: the blind burst is **burst-of-one** (`blind_post_max_count=1`, default + all
shipped configs — ForeUP's "1 online reservation per day" rule 400-rejects surplus POSTs, so a wider
burst made the winner first-processed rather than best-ranked; a miss falls to the sequential
center-out fallback with the 2 pooled reserve tokens; **superseded 2026-07-18 by `infra/v2.11.0` —
burst reverted to 3** after burst-of-one's single-slot race loss caused the 2026-07-18 Sat 7/25
miss, see the v2.11.0 paragraph above) — and a cancel-DELETE 400 "We can't find that teetime" is
treated as already-cancelled (ForeUP uses it, not 404, for a missing/expired reservation — observed
live). OTP posture: the 2026-07-15 live recon showed the email-OTP gate is **UI-only** (the bot's
direct API book POST books unchallenged, HTTP 200 + instant confirmation), so the OtpSource stays
off the critical path; `_guard_otp_challenge` → `OtpChallengeError` (CaptchaError subclass) is the
loud observation signal if ForeUP ever extends enforcement to the API. All three prod jobs verified
on the `teetime:fb2a133` image. First drop on this image: Sat 2026-07-18 05:50 ET (books 7/25) — the
first OTP-era drop.

## infra/v2.9.0: 2026-07-10

The 2026-07-09 full-repo-scan fix batch + the python 3.14 base. Booking-behavior: a SURPLUS-cancel
failure (429/captcha/transport blip while cancelling a blind-POST extra) can no longer discard the
kept booking — `_cancel_extras` catches `Exception` broadly, and the watcher reconcile got the same
broadening with the watch-contract errors re-raised (#166). Security: the 2captcha result-poll no
longer leaks the API key into Log Analytics on a non-2xx (sanitized RuntimeError; `redact_text` also
masks credential-named URL query params) (#167); the container runs as a non-root user on a
digest-pinned `python:3.14-slim` base, all workflow actions are SHA-pinned, and Dependabot keeps the
pins fresh (#168, #174 — dev venvs already ran 3.14, the image was the lagging environment).
Observability: the blind-burst captured-`BaseException` branch now logs, the reguard-reauth-fail
WARNING + reconcile CRITICAL are test-pinned, and lock-defer logs are visible at INFO (#173).
Config/docs: `blind_post_max_count` code default aligned to **3**, the `tests/
test_docs_consistency.py` tag-agreement CI guard + the CLAUDE.md change→docs map exist (#172). The
prod jobs were rebuilt + redeployed on the `infra/v2.9.0` image (`teetime:a67eccd`, verified live on
all three jobs); its first real booking exercise (and CPython 3.14's) was the 2026-07-11 drop —
booked Sat 7/18 09:30 (3rd-ranked; the two blind-burst siblings were 400-rejected by the
1-reservation/day rule, the observation that motivated v2.10.0's burst-of-one).

## infra/v2.8.0: 2026-06-29 (`main`@`21b24cf`)

The blind-POST fallback rework + scan hardening (a booking-behaviour change): the booking jobs run
`blind_post_max_count=3` + `blind_post_fallback_token_reserve=2`, the concurrent hedge search is
dropped, and the 0-booked path fires a FRESH search strictly after the re-guard
([RESEARCH_FALLBACK_PLAN.md](./plans/RESEARCH_FALLBACK_PLAN.md), PRs #157–#160), plus the
robustness fixes #161–#164 (book() 429 → `RateLimitError`, re-guard skip-on-reauth-fail, a
blind-burst `BaseException` secures a booked sibling).

## infra/v2.7.0: 2026-06-23 (`main`@`b2c0051`)

Observability + redaction hardening, no booking-behaviour change: the booking job's `_run` logs a
traceback before exit on a failed run (#154), and `redact_text` Luhn-masks PANs in free-text logs
(#153).

## infra/v2.6.0: 2026-06-23

Infra-only shared-ACR consolidation: the ACR moved to a dedicated `rg-teetime-shared` with both envs
as non-owners (AZURE_PLAN §2.1/§10.6).

## infra/v2.5.0: 2026-06-22 (`main`@`ddf0112`)

The runtime feature set, `dryRun=false`: the **blind-POST race feature**
([BLIND_POST_PLAN.md](./plans/BLIND_POST_PLAN.md), PRs #125–#130: Mangrove-Bay-only concurrent
blind book POSTs at T0 for the top-N synthesized in-window slots, keep-best + cancel-extras in-run,
re-guard before the search fallback, watcher >1-reservation reconcile crash-net) and the #131
soft-login-skip fix (record the post-T0 re-auth skip only on a session-established login). Also
active from earlier tags: multi-day Sat+Sun booking, the 4 PM-day-before booking cutoff, the
Portal-editable skip-days (`TEETIME-SKIP-DATES` Key Vault secret, present in both vaults), the
watcher today+7 horizon (#119) and CAPTCHA `TimeoutError` recovery (`book()` / `prepare_book()` →
`CaptchaError`, lead 120 s; #120).

## infra/v2.4.0

The **race pre-warm bundle** ([RACE_PREWARM_PLAN.md](./plans/RACE_PREWARM_PLAN.md)): pre-T0
ForeUP login pre-warm + layer-2 guard, a multi-token concurrent CAPTCHA pool, and the race-path
leading-search-sleep trim.

## infra/v2.2.0

Within-window upgrade: a strictly-closer-to-midpoint slot in the same tier triggers
cancel-before-book.

## infra/v2.1.0: 2026-06-10

Multi-day re-architecture live ([MULTIDAY_PLAN.md](./plans/MULTIDAY_PLAN.md),
[PERDAY_WINDOWS_PLAN.md](./plans/PERDAY_WINDOWS_PLAN.md),
[LEADTIME_SKIP_PLAN.md](./plans/LEADTIME_SKIP_PLAN.md)): daily booking crons + booking-day gate,
Saturday and Sunday booked (one reservation per day), per-day windows, the booking cutoff and
skip-days. The renamed `-edt`/`-est` jobs are deployed in both envs and the old
`-edt-sun`/`-est-sun` orphans were deleted per the AZURE_PLAN §10.2 runbook.

## Before v2.1.0

M6 took prod live (`dryRun=false`; [M6_PLAN.md](./plans/M6_PLAN.md)). A real booking race ran
2026-06-07 and lost on CAPTCHA latency, which led to the race-path CAPTCHA pre-fetch (#68) and the
book-POST 4xx → next-slot fallback (#67).
