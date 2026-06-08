"""CLI entry point. `python -m teetime` and `teetime` (via project.scripts) land here.

Commands:
- show-config: print the resolved AppConfig with secrets masked.
- run: execute one BookingRequest end-to-end.
- watch: perform one cancellation-availability check (M-feature-1).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from dataclasses import replace as dc_replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import click

from .core.adapter import CourseAdapter, RateLimitError
from .core.booking_day_gate import should_book_today
from .core.clock import RealClock, measure_ntp_offset
from .core.config import (
    SECRET_EXTRA_KEYS,
    AppConfig,
    MissingEnvVarError,
    SchedulerConfig,
    load,
    redact,
)
from .core.dst_gate import should_proceed
from .core.models import (
    BookingRequest,
    CourseCredentials,
    CourseId,
    Player,
    TimeWindow,
    build_request_fingerprint,
    derive_request_id,
)
from .core.orchestrator import Orchestrator
from .core.target_date import (
    next_occurrences_within_horizon,
    weekday_from_name,
)
from .core.watch_orchestrator import WatchOrchestrator
from .courses.foreup.base import ForeUpAdapter
from .courses.foreup.captcha import (
    FOREUP_RECAPTCHA_SITE_KEY,
    make_2captcha_provider,
    resolve_invisible_site_key,
)
from .courses.foreup.mangrove_bay import MangroveBayAdapter
from .courses.teeitup.sydney_marovitz import SydneyMarovitzAdapter
from .dev.fake_adapter import FakeAdapter
from .notifications.notifier import ConsoleNotifier
from .persistence.in_memory_store import InMemoryStore

# Registry mapping TOML adapter names to adapter classes.
# _build_adapters() resolves every [[courses]] entry through this dict.
#
# To add a new ForeUP course:
#   1. Create src/teetime/courses/foreup/<course_name>.py as a sibling of mangrove_bay.py.
#      Set all four IDs and override booking_page_url.
#   2. Import the class here and add one line below, e.g.:
#        "foreup.twin_brooks": TwinBrooksAdapter,
#   3. Add a [[courses]] entry in your TOML config.
#
# To add a new TeeItUp course:
#   1. Create src/teetime/courses/teeitup/<course_name>.py as a sibling of sydney_marovitz.py.
#      Set the course_slug, timezone, and booking_page_url.
#   2. Import the class here and add one line below.
#   3. Add a [[courses]] entry in your TOML config.
#
# type[object] because ForeUpAdapter and TeeItUpAdapter have different base classes.
_ADAPTER_REGISTRY: dict[str, type[object]] = {
    "foreup.mangrove_bay": MangroveBayAdapter,
    "teeitup.sydney_marovitz": SydneyMarovitzAdapter,
}


@click.group()
def cli() -> None:
    """TeeTimeBooker CLI."""


@cli.command(name="show-config")
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Path to a TOML config file (see config/example.toml).",
)
def show_config_cmd(config_path: Path) -> None:
    """Print the loaded AppConfig with secrets masked."""
    try:
        cfg = load(config_path)
    except MissingEnvVarError as e:
        raise click.ClickException(str(e)) from e
    masked = redact(cfg)
    click.echo(masked.model_dump_json(indent=2))


@cli.command(name="run")
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
@click.option(
    "--dry-run",
    type=bool,
    default=True,
    show_default=True,
    help="If true, do everything except the final booking POST.",
)
@click.option(
    "--wait/--no-wait",
    "wait",
    default=None,
    help="Busy-wait until the configured fire_time (the real 06:00 ET race path). "
    "Default (no flag): consult the TEETIME_WAIT env var, else --no-wait (immediate, "
    "local-demo timing). The ACA booking job passes --wait.",
)
@click.option(
    "--fire-time",
    "fire_time_str",
    type=str,
    default="",
    help="DEV/TEST ONLY. Override the scheduler fire_time (HH:MM:SS) so an on-demand "
    "--wait run's busy-wait is satisfiable at any wall-clock hour. REFUSED unless "
    "--dry-run true (it must never be able to shift a real booking). See AZURE_PLAN §6.5.",
)
@click.option(
    "--use-fake-adapter",
    is_flag=True,
    default=False,
    help="Use the in-process FakeAdapter instead of the real adapter (testing/demo "
    "only). Omit it to use the live ForeUP/TeeItUp adapter.",
)
def run_cmd(
    config_path: Path,
    dry_run: bool,
    wait: bool | None,
    fire_time_str: str,
    use_fake_adapter: bool,
) -> None:
    """Run one BookingRequest end-to-end."""
    # Resolve bool|None -> bool here (in the command) so _run takes a plain bool.
    resolved_wait = _resolve_wait_mode(wait)
    # --fire-time is a dev/test escape hatch; hard-refuse it on a live run.
    if fire_time_str and not dry_run:
        raise click.ClickException("--fire-time is dev/test-only and requires --dry-run true.")
    try:
        cfg = load(config_path)
    except MissingEnvVarError as e:
        raise click.ClickException(str(e)) from e

    if fire_time_str:
        cfg = _with_fire_time_override(cfg, fire_time_str)

    asyncio.run(_run(cfg, dry_run=dry_run, wait=resolved_wait, use_fake_adapter=use_fake_adapter))


def _resolve_wait_mode(flag: bool | None) -> bool:
    """Resolve the execution mode.

    Precedence: explicit --wait/--no-wait flag > TEETIME_WAIT env (truthy: "1"/"true"/
    "yes"/"on") > False. True = the real 06:00 ET busy-wait path; False = immediate
    local-demo timing. Called from run_cmd (NOT _run) so _run receives a concrete bool.
    """
    if flag is not None:
        return flag
    return os.getenv("TEETIME_WAIT", "").strip().lower() in {"1", "true", "yes", "on"}


def _with_fire_time_override(cfg: AppConfig, fire_time_str: str) -> AppConfig:
    """Return a copy of cfg with scheduler.fire_time replaced by HH:MM:SS.

    Dev/test only (the caller refuses it unless --dry-run true). Lets the --wait
    busy-wait be exercised on demand at any wall-clock hour. Raises ClickException
    on a malformed time.
    """
    try:
        parsed = time.fromisoformat(fire_time_str)
    except ValueError as e:
        raise click.ClickException(f"--fire-time must be HH:MM:SS, got {fire_time_str!r}.") from e
    new_scheduler = cfg.scheduler.model_copy(update={"fire_time": parsed})
    return cfg.model_copy(update={"scheduler": new_scheduler})


async def _run(cfg: AppConfig, *, dry_run: bool, wait: bool, use_fake_adapter: bool) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    log = logging.getLogger(__name__)

    # One-shot NTP offset for the T0 race. Gated on `wait` (NOT dry_run) so the dev
    # `--wait --dry-run true` run probes UDP:123 reachability before the first real
    # prod booking run; best-effort, degrades to 0 on failure. Skipped for the fake adapter
    # (no network in tests/demo) and off the wait path (offset is meaningless there).
    clock_offset = measure_ntp_offset() if wait and not use_fake_adapter else timedelta(0)
    clock = RealClock(offset=clock_offset)

    # The DST + booking-day gates are pure functions of the clock, so they run FIRST —
    # before any adapter construction or the live `_resolve_site_keys` ForeUP GET. The
    # booking job fires DAILY; on a wrong-season or non-booking-day morning it must
    # fast-exit cheaply WITHOUT touching ForeUP (cost + anti-bot etiquette, PLAN §12).

    # DST-half gate (ONLY on the real cron path). compute.bicep registers both a EDT and
    # an EST cron per day; only one is correct each season. The wrong-season cron would
    # otherwise book ~50 min late (past T0) or busy-wait ~70 min into the replica timeout
    # (future T0). Exit 0 without booking — a wrong-season firing is NOT an error. The
    # --no-wait path (manual/local/on-demand) bypasses the gate, matching the old
    # book.yml workflow_dispatch always-proceed semantics. See core/dst_gate.py.
    if wait and not should_proceed(
        clock, timezone=cfg.scheduler.timezone, fire_time=cfg.scheduler.fire_time
    ):
        log.info(
            "DST-half gate: wrong-season cron (ET hour != %d) — exiting 0 without booking.",
            cfg.scheduler.fire_time.hour - 1,
        )
        return

    # Booking-day gate (MULTIDAY PR2, ONLY on the real cron path). The booking job now
    # fires DAILY; on the 5/7 mornings whose target (today+offset) isn't a wanted weekday
    # it fast-exits here — after the DST gate (so we never decide off a wrong-season clock)
    # and before the busy-wait. The --no-wait path bypasses this (always-proceed), matching
    # the manual/local semantics. See core/booking_day_gate.py.
    offset = cfg.request.target_offsets[0]
    tz = ZoneInfo(cfg.scheduler.timezone)
    if wait and not should_book_today(
        clock,
        timezone=cfg.scheduler.timezone,
        target_offset=offset,
        wanted_weekdays=cfg.request.wanted_weekday_indices,
    ):
        target = clock.now_utc().astimezone(tz).date() + timedelta(days=offset)
        log.info(
            "booking-day gate: today+%d is %s, not a wanted booking day — exiting 0.",
            offset,
            target.strftime("%A %Y-%m-%d"),
        )
        return

    # Past the gates — NOW resolve adapters/credentials (the expensive part: a live
    # ForeUP site-key GET per course in prod). A wrong-season or non-booking-day cron
    # never reaches here, so it pays none of this cost and never touches ForeUP.
    if use_fake_adapter:
        adapters: dict[CourseId, CourseAdapter] = {
            CourseId(c.id): FakeAdapter(course_id=CourseId(c.id)) for c in cfg.courses
        }
        creds: dict[CourseId, CourseCredentials] = {}
    else:
        # Pre-flight (off the race path): resolve the live reCAPTCHA site key so a
        # ForeUP key rotation is detected before T0, not after every solve fails.
        site_keys = await _resolve_site_keys(cfg) if not dry_run else {}
        adapters = _build_adapters(cfg, dry_run=dry_run, site_keys=site_keys)
        creds = _resolve_creds(cfg)

    # State is in-process only (InMemoryStore). The source of truth for existing
    # bookings is the live `list_reservations()` pre-book check, not a durable store.
    store = InMemoryStore()
    await store.initialize()

    # --wait (the ACA booking cron): use cfg.scheduler verbatim so the orchestrator
    # busy-waits to the configured fire_time (06:00:00 ET). --no-wait (local default):
    # T0 = now via _local_demo_scheduler, so busy_wait returns immediately.
    scheduler = cfg.scheduler if wait else _local_demo_scheduler(cfg.scheduler)

    if wait:
        # Verification surface (M6 PR6): confirms the REAL scheduler was selected (not
        # the immediate demo path) and shows the NTP correction the busy-wait applies.
        log.info(
            "run: real-timing path (--wait); fire_time=%s %s, NTP offset_ms=%.1f",
            cfg.scheduler.fire_time,
            cfg.scheduler.timezone,
            clock_offset.total_seconds() * 1000.0,
        )

    # The booking run targets a SINGLE date — the gated today+offset (course-local). A
    # multi-date request would let another day's reservation vacuously satisfy the pre-book
    # list_reservations guard (_first_matching_reservation), so this MUST be one date.
    booking_target = clock.now_utc().astimezone(tz).date() + timedelta(days=offset)
    request = _build_booking_request(cfg, dry_run=dry_run, target_date=booking_target)
    log.info(
        "Booking run: target=%s dry_run=%s players=%d",
        [str(d) for d in request.target_dates],
        dry_run,
        len(request.players),
    )

    orch = Orchestrator(
        adapters=adapters,
        store=store,
        notifier=ConsoleNotifier(),
        clock=clock,
        scheduler=scheduler,
        creds=creds,
        # Pre-solve the CAPTCHA during the busy-wait ONLY on the --wait race path (the
        # ACA booking cron). The watcher / local demo never pre-fetch — a token is only
        # solved when actually booking. See core/orchestrator._prefetch_captcha.
        prefetch_book=wait,
    )
    result = await orch.run(request)
    if result.outcome.value not in {"booked", "dry_run", "already_booked"}:
        raise click.ClickException(f"booking failed: outcome={result.outcome.value}")


@cli.command(name="watch")
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
@click.option(
    "--dry-run",
    type=bool,
    default=True,
    show_default=True,
    help="If true, find available slots but do not book.",
)
@click.option(
    "--date",
    "target_date_str",
    type=str,
    default="",
    help="Date to watch (YYYY-MM-DD). Defaults to the next upcoming occurrence of EACH wanted "
    "weekday within the horizon (the wanted days are derived from the configured time windows). "
    "An explicit date whose weekday has no configured window is rejected.",
)
@click.option(
    "--use-fake-adapter",
    is_flag=True,
    default=False,
    help="Use the in-process FakeAdapter (testing only).",
)
def watch_cmd(
    config_path: Path,
    dry_run: bool,
    target_date_str: str,
    use_fake_adapter: bool,
) -> None:
    """Perform one cancellation-availability check for the target date.

    Designed to be called on a recurring schedule (every ~10 minutes via the
    ACA Job watch cron). Exits 0 whether or not a slot was found.
    Re-exits non-zero only on CaptchaError or AuthError (operator action needed).
    """
    try:
        cfg = load(config_path)
    except MissingEnvVarError as e:
        raise click.ClickException(str(e)) from e

    if not cfg.watcher.enabled:
        # Q2 (resolved): warning log + clean exit when watcher is disabled.
        logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
        log = logging.getLogger(__name__)
        log.warning(
            "Watch job is disabled in config (watcher.enabled = false). Set to true to activate."
        )
        return  # exit 0

    asyncio.run(
        _watch(
            cfg, dry_run=dry_run, target_date_str=target_date_str, use_fake_adapter=use_fake_adapter
        )
    )


async def _watch(
    cfg: AppConfig,
    *,
    dry_run: bool,
    target_date_str: str,
    use_fake_adapter: bool,
) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    log = logging.getLogger(__name__)

    request = _build_request(cfg, dry_run=dry_run)

    # Derive the watch date list (MULTIDAY PR4). Explicit --date overrides to a single date;
    # otherwise watch the next upcoming occurrence of EACH wanted weekday within the bookable
    # horizon (upcoming Sat AND Sun). horizon = max(target_offsets) — single source of truth.
    if target_date_str:
        try:
            target_dates: tuple[date, ...] = (date.fromisoformat(target_date_str),)
        except ValueError as e:
            raise click.ClickException(f"--date must be YYYY-MM-DD, got {target_date_str!r}") from e
    else:
        today = datetime.now(tz=ZoneInfo(cfg.scheduler.timezone)).date()
        target_dates = next_occurrences_within_horizon(
            today,
            cfg.request.wanted_weekday_indices,
            max(cfg.request.target_offsets),
        )

    log.info("Watch check: targets=%s dry_run=%s", [str(d) for d in target_dates], dry_run)

    if use_fake_adapter:
        adapters: dict[CourseId, CourseAdapter] = {
            CourseId(c.id): FakeAdapter(course_id=CourseId(c.id)) for c in cfg.courses
        }
        creds: dict[CourseId, CourseCredentials] = {}
    else:
        site_keys = await _resolve_site_keys(cfg) if not dry_run else {}
        adapters = _build_adapters(cfg, dry_run=dry_run, site_keys=site_keys)
        creds = _resolve_creds(cfg)

    # In-process state only. Existing bookings are detected via the live
    # list_reservations() check inside the orchestrators, not a durable store.
    store = InMemoryStore()
    await store.initialize()

    clock_offset = measure_ntp_offset() if not use_fake_adapter and not dry_run else timedelta(0)

    watch_config = cfg.watcher.to_watch_config()
    watch = WatchOrchestrator(
        adapters=adapters,
        store=store,
        notifier=ConsoleNotifier(),
        clock=RealClock(offset=clock_offset),
        scheduler=cfg.scheduler,
        watch_config=watch_config,
        creds=creds,
        policy=cfg.one_booking_policy,
    )
    # Check EVERY wanted target date this run, whatever weekday we execute on (do NOT break —
    # the upcoming Sat AND Sun are both checked, and either can be booked). Each check_once gets
    # a request scoped to ITS target date + that date's weekday's windows, so the Saturday-target
    # check uses Saturday's windows and the Sunday-target check uses Sunday's. See PERDAY §8.
    try:
        for target_date in target_dates:
            scoped_request = _scope_request_to_date(request, cfg, target_date)
            result = await watch.check_once(scoped_request, target_date)
            if result is not None:
                log.info(
                    "watch result: date=%s outcome=%s confirmation=%s",
                    target_date,
                    result.outcome,
                    result.confirmation_code,
                )
    except RateLimitError as exc:
        # A 429 aborts the whole run (all remaining dates) — re-raised by check_once
        # so we stop hammering the throttled platform. This is a back-off, NOT an
        # operator-action failure, so exit 0 (the 10-min cron is the retry). Non-zero
        # exit stays reserved for Captcha/Auth. PLAN §12.
        log.warning(
            "watch: rate-limited (retry_after=%ss) — backing off; the next cron run is the retry",
            exc.retry_after_s,
        )


async def _resolve_site_keys(cfg: AppConfig) -> dict[CourseId, str]:
    """Pre-flight: resolve the live invisible reCAPTCHA site key per ForeUP course.

    Best-effort per course (degrades to the hardcoded key on any failure). Run this
    BEFORE the T0 busy-wait so a ForeUP key rotation is caught up front rather than
    surfacing as an invalid-key error on every booking solve.
    """
    site_keys: dict[CourseId, str] = {}
    for c in cfg.courses:
        cls = _ADAPTER_REGISTRY.get(c.adapter)
        if cls is not None and issubclass(cls, ForeUpAdapter) and cls.booking_page_url:
            site_keys[CourseId(c.id)] = await resolve_invisible_site_key(cls.booking_page_url)
    return site_keys


def _build_adapters(
    cfg: AppConfig,
    *,
    dry_run: bool = True,
    site_keys: dict[CourseId, str] | None = None,
) -> dict[CourseId, CourseAdapter]:
    """Build a CourseAdapter for each [[courses]] entry via _ADAPTER_REGISTRY.

    In dry_run mode, captcha_provider is always None (no CAPTCHA solving needed
    because the final booking POST is skipped). In live mode a ForeUP course
    requires TWOCAPTCHA_API_KEY (2captcha is the only supported live solver);
    a missing key raises a clear error instead of silently degrading.

    The booking_page_url on each ForeUpAdapter subclass determines which page
    the captcha provider targets — every course has its own booking page.
    `site_keys` (from `_resolve_site_keys`) overrides the hardcoded reCAPTCHA key
    per course when present; absent entries use the hardcoded default.
    """
    site_keys = site_keys or {}
    twocaptcha_key = None if dry_run else os.environ.get("TWOCAPTCHA_API_KEY")

    # Validate: every course_preferences entry must have a [[courses]] entry.
    # Without this, the orchestrator silently skips the missing course and returns
    # NO_INVENTORY — indistinguishable from genuine inventory absence.
    configured_ids = {c.id for c in cfg.courses}
    for pref in cfg.request.course_preferences:
        if pref not in configured_ids:
            raise click.ClickException(
                f"course_preferences has {pref!r} but there is no [[courses]] entry for it. "
                "Add a [[courses]] block with that id, or remove it from course_preferences."
            )

    adapters: dict[CourseId, CourseAdapter] = {}
    for c in cfg.courses:
        cls = _ADAPTER_REGISTRY.get(c.adapter)
        if cls is None:
            raise click.ClickException(
                f"Unknown adapter {c.adapter!r} for course {c.id!r}. "
                f"Register it in __main__._ADAPTER_REGISTRY. "
                f"Known adapters: {sorted(_ADAPTER_REGISTRY)}"
            )
        if issubclass(cls, ForeUpAdapter):
            # ForeUP-specific: CAPTCHA solver and booking_page_url validation.
            if dry_run:
                cp = None
            else:
                # Validate booking_page_url before building the CAPTCHA provider —
                # an empty URL would silently pass bad input to the CAPTCHA service.
                if not cls.booking_page_url:
                    raise click.ClickException(
                        f"Adapter {cls.__name__!r} (for course {c.id!r}) has no booking_page_url. "
                        "Set booking_page_url = <url> in the adapter class before live use."
                    )
                if not twocaptcha_key:
                    raise click.ClickException(
                        f"Live ForeUP booking for course {c.id!r} requires a CAPTCHA solver, "
                        "but TWOCAPTCHA_API_KEY is unset. Set it (the 2captcha API key) in the "
                        "environment, or run with --dry-run true. "
                        "(The Playwright inline solver was removed — the deployed image has no "
                        "browser; 2captcha is the only supported live solver.)"
                    )
                site_key = site_keys.get(CourseId(c.id), FOREUP_RECAPTCHA_SITE_KEY)
                cp = make_2captcha_provider(twocaptcha_key, cls.booking_page_url, site_key)
            adapters[CourseId(c.id)] = cls(captcha_provider=cp)  # type: ignore[call-arg]
        else:
            # Non-ForeUP adapters (TeeItUp, future platforms): no CAPTCHA, simpler construction.
            adapters[CourseId(c.id)] = cls()  # type: ignore[assignment]
    return adapters


def _resolve_creds(cfg: AppConfig) -> dict[CourseId, CourseCredentials]:
    creds: dict[CourseId, CourseCredentials] = {}
    for c in cfg.courses:
        username = os.environ.get(c.username_env)
        password = os.environ.get(c.password_env)
        if username is None:
            raise click.ClickException(
                f"Required env var {c.username_env!r} (course {c.id!r}) is unset. "
                "Run: set -a && source .env && set +a"
            )
        if password is None:
            raise click.ClickException(
                f"Required env var {c.password_env!r} (course {c.id!r}) is unset. "
                "Run: set -a && source .env && set +a"
            )
        # Guard: both `card_number` (literal) and `card_number_env` (env ref) in the
        # same TOML block would produce a silent winner depending on iteration order.
        # Detect this and fail loudly before any credential is resolved.
        for key in c.extra:
            if not key.endswith("_env") and (key + "_env") in c.extra:
                raise click.ClickException(
                    f"course {c.id!r} extra has both {key!r} (literal) and "
                    f"{key + '_env'!r} (env-var ref) — remove one to avoid ambiguity."
                )
            # Guard: a sensitive credential field (card_number/cvv/password/…) MUST be an
            # env-var ref, never a literal — a raw PAN/CVV in a config file is a PCI footgun.
            # Force the `*_env` form. Note: the literal VALUE is never echoed in the error.
            if not key.endswith("_env") and key in SECRET_EXTRA_KEYS:
                raise click.ClickException(
                    f"course {c.id!r} extra has {key!r} as a literal value — sensitive "
                    f"credential fields MUST use the {key + '_env'!r} env-var form so a raw "
                    "secret never lives in a config file."
                )

        # Resolve any extra key ending in _env (e.g. card_number_env → card_number).
        # Non-_env keys are passed through as literal values (e.g. booking_class_id).
        extra: dict[str, str] = {}
        for key, value in c.extra.items():
            if key.endswith("_env"):
                resolved = os.environ.get(value)
                if resolved is None:
                    raise click.ClickException(
                        f"Required env var {value!r} (course {c.id!r} extra.{key}) is unset. "
                        "Run: set -a && source .env && set +a"
                    )
                extra[key[:-4]] = resolved  # strip _env suffix: card_number_env → card_number
            else:
                extra[key] = value
        creds[CourseId(c.id)] = CourseCredentials(username=username, password=password, extra=extra)
    return creds


def _local_demo_scheduler(base: SchedulerConfig) -> SchedulerConfig:
    """Return a scheduler whose T0 is 'now' in the configured tz, so the
    orchestrator's busy_wait returns immediately for local demo runs."""
    tz = ZoneInfo(base.timezone)
    now_local = datetime.now(tz=tz)
    return SchedulerConfig(
        timezone=base.timezone,
        fire_time=time(now_local.hour, now_local.minute, now_local.second),
        early_arrival_ms=0,
        poll_interval_ms=10,
        max_poll_seconds=1,
    )


def _build_request(cfg: AppConfig, *, dry_run: bool) -> BookingRequest:
    tz = ZoneInfo(cfg.scheduler.timezone)
    today = datetime.now(tz=tz).date()
    # `_build_request` produces the RequestId fingerprint + players/windows; its `target_dates`
    # is a PLACEHOLDER (today + offset) that BOTH callers override before use:
    # `_build_booking_request` pins the single gated date, and `_watch` pins each date via
    # `_scope_request_to_date`. Nothing consumes this value directly (it is never the date booked).
    target_dates = (today + timedelta(days=cfg.request.target_offsets[0]),)

    # Use course_preferences (not cfg.courses) for the fingerprint.
    # cfg.courses may contain standby/disabled courses not in course_preferences;
    # including them would change the RequestId and invalidate idempotency records
    # every time the [[courses]] block is edited. See PLAN.md §13.1.
    course_ids = [CourseId(p) for p in cfg.request.course_preferences]
    windows = [TimeWindow(earliest=w.earliest, latest=w.latest) for w in cfg.request.time_windows]
    # (weekday_index, window) pairs for the fingerprint so a window applied to Sat vs Sun is a
    # distinct RequestId (per-day windows, PERDAY_WINDOWS_PLAN §6). request.time_windows itself
    # stays the full flat tuple; per-invocation scoping narrows it (PR3).
    window_pairs = [
        (weekday_from_name(w.weekday), TimeWindow(earliest=w.earliest, latest=w.latest))
        for w in cfg.request.time_windows
    ]
    players = [
        Player(
            first_name=p.first_name,
            last_name=p.last_name,
            email=p.email or "",
            phone=p.phone,
            member_number=p.member_number,
        )
        for p in cfg.request.players
    ]
    fp = build_request_fingerprint(
        course_ids=course_ids,
        target_offsets=cfg.request.target_offsets,
        time_windows=window_pairs,
        players=players,
    )
    rid = derive_request_id(fp)

    return BookingRequest(
        request_id=rid,
        target_dates=target_dates,
        time_windows=tuple(windows),
        players=tuple(players),
        course_preferences=tuple(CourseId(p) for p in cfg.request.course_preferences),
        holes=cfg.request.holes,
        max_price_per_player=cfg.request.max_price_per_player,
        cart=cfg.request.cart,
        dry_run=dry_run,
    )


def _build_booking_request(cfg: AppConfig, *, dry_run: bool, target_date: date) -> BookingRequest:
    """Build the booking request pinned to a SINGLE target date (the gated today+offset).

    Wraps _build_request and overrides target_dates=(target_date,). The booking run MUST
    target exactly one date: _first_matching_reservation matches r.tee_time.date() in
    request.target_dates, so a multi-date request would let another wanted day's existing
    reservation vacuously satisfy this date's pre-book guard. RequestId is unaffected (the
    fingerprint excludes dates). See MULTIDAY_PLAN.md PR2.
    """
    base = _build_request(cfg, dry_run=dry_run)
    return dc_replace(
        base,
        target_dates=(target_date,),
        time_windows=_windows_for_date(cfg, target_date),
    )


def _windows_for_date(cfg: AppConfig, target_date: date) -> tuple[TimeWindow, ...]:
    """Domain TimeWindows configured for target_date's weekday (per-day windows).

    Asserts non-empty: the booking-day gate / watcher only ever produce dates whose weekday
    has windows (wanted_weekday_indices is derived from the windows), so an empty result means
    a caller passed a windowless weekday (e.g. `watch --date` on an unwanted day) — fail loudly.
    See PERDAY_WINDOWS_PLAN §5/§8.
    """
    cfgs = cfg.request.windows_for(target_date.weekday())
    if not cfgs:
        raise click.ClickException(
            f"no time window configured for {target_date} ({target_date.strftime('%A')}); "
            f"add a [[request.time_windows]] with that weekday or pick a different --date."
        )
    return tuple(TimeWindow(earliest=w.earliest, latest=w.latest) for w in cfgs)


def _scope_request_to_date(
    request: BookingRequest, cfg: AppConfig, target_date: date
) -> BookingRequest:
    """Return a copy of `request` scoped to a single date AND that date's weekday's windows.

    Called once per TARGET DATE by `_watch` (which checks every wanted upcoming date each run,
    regardless of execution day). Scoping pairs each target date with its own weekday's windows:
    the check for a Saturday-dated target ranks Saturday's windows, the Sunday-dated target uses
    Sunday's. Keeps the domain TimeWindow weekday-free (narrowing lives in the CLI). See PERDAY §8.
    """
    return dc_replace(
        request,
        target_dates=(target_date,),
        time_windows=_windows_for_date(cfg, target_date),
    )


def main() -> int:
    cli(standalone_mode=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
