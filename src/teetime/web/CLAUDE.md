# Web app invariants (`src/teetime/web/`)

Moved here from the root `CLAUDE.md` on 2026-10-25 so they load only when you work under this
tree. The route table is `web/routes.py::ROUTES` (MULTIUSER_PLAN §8 is its contract); the tenant
rules the pages sit on are [`../tenant/CLAUDE.md`](../tenant/CLAUDE.md). **`tests/web/` does not
load this file: read it first when working there.**

- **The prod site has ONE canonical host** (`https://spicyteetimebooker.com`, 2026-09-28). The OAuth
  `state` lives in the session cookie of the host sign-in started on, so with
  `TEETIME_CANONICAL_HOST_REDIRECT=true` (Bicep sets it iff `webCustomDomain` is set)
  `web/app.py::_CanonicalHostMiddleware` redirects every other host (`www.`, the old
  `*.azurecontainerapps.io` name) to the same path on `TEETIME_PUBLIC_BASE_URL` (301 GET/HEAD, 308
  otherwise; `/healthz` exempt; the target is always the configured origin). The managed
  certificates `mc-<host-dashed>` are created once by AZURE_PLAN §10.9 before any deploy that sets
  the domain, or that deploy fails.
- **The website works with JavaScript off, and never shows a raw course id.** CSP is
  `script-src 'self'; style-src 'self'`: no inline script or style. The only script is the
  same-origin `web/static/app.js`, progressive enhancement over plain forms (the ranked form's
  rows 2-6 live in a `<details>` the server parses whether or not JS ran). Every course name shown
  to a person, on pages, in messages and in emails, comes from
  `courses/names.py::course_display_name`; add a new course there, with its
  `COURSE_SIGNUP_URLS` entry (the course's own booking site, linked from Connect a course as
  where to create the login; no entry = no link) and its `COURSE_TEE_SHEET_HOURS` entry (first
  tee to last, a generous year-round span; no entry = the whole day). **Every time bound on a
  booking form is a `<select>` of quarter hours inside those hours, never `<input type="time">`**
  (operator, 2026-10-02): `web/time_options.py` builds the list (the union of the person's
  courses on the ranked form, where the course is chosen per row; the rule's own course on its
  edit form, with a saved off-grid window kept selectable), `app.js` narrows each row to its
  chosen course's `data-first`/`data-last`, and `_Ctx.check_window` is the guarantee: every
  create/edit path refuses a window outside the course's hours with a 400 that names the course
  and its hours. Pinned by `tests/web/test_web_time_picker.py`. Every release-cycle sentence a page shows
  ("Tee times open 7 days ahead, at 6:00 AM Eastern.") is `web/course_info.py::release_cycle`
  over the course's `ReleasePolicy` + the viewer's own cutoff (site default when unset), never typed into a template, so a
  page cannot state a rule the bot does not run (2026-10-01). The Connect dropdown lists only
  courses the user has NOT connected (`connectable` in `pages.accounts`). Every form is a CSRF-guarded POST
  and every id is resolved through user-scoped reads (a foreign id is the uniform 404). Pinned by
  `tests/web/test_web_ui_polish.py` and the web security tests.
- **Static assets are linked through `static_url(name)`, never a bare `/static/...` path.** It
  appends `?v=<content hash>` (`web/app.py::static_asset_versions`, computed at startup), and
  every static response carries `Cache-Control: no-cache`, so a deploy can never leave browsers on
  stale CSS/JS (dev showed the pre-#268 pickers for hours). Pinned by
  `test_no_template_links_a_static_file_without_its_version`.
- **Adopting reservations is operator-only and confirmed** (MU-16b, `web/adopt.py`): it re-plans
  server-side from a trusted snapshot at most 15 minutes old and writes nothing without the
  confirm box; it cannot tell the bot's bookings from a manual one for the same slot, which is why
  the operator confirms.
