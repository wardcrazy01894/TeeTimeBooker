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
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import click

from .core.adapter import CourseAdapter
from .core.clock import RealClock
from .core.config import (
    AppConfig,
    MissingEnvVarError,
    SchedulerConfig,
    load,
    redact,
)
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
from .core.watch_orchestrator import WatchOrchestrator
from .courses.foreup.base import ForeUpAdapter
from .courses.foreup.captcha import make_2captcha_provider, make_captcha_provider
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
    "--use-fake-adapter",
    is_flag=True,
    default=False,
    help="Use the in-process FakeAdapter (v0 demo). Real ForeUP adapter "
    "lands in M5 (gated behind Spike S1).",
)
def run_cmd(config_path: Path, dry_run: bool, use_fake_adapter: bool) -> None:
    """Run one BookingRequest end-to-end."""
    try:
        cfg = load(config_path)
    except MissingEnvVarError as e:
        raise click.ClickException(str(e)) from e

    asyncio.run(_run(cfg, dry_run=dry_run, use_fake_adapter=use_fake_adapter))


async def _run(cfg: AppConfig, *, dry_run: bool, use_fake_adapter: bool) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    request = _build_request(cfg, dry_run=dry_run)
    log = logging.getLogger(__name__)
    log.info(
        "Booking run: target=%s dry_run=%s players=%d",
        [str(d) for d in request.target_dates],
        dry_run,
        len(request.players),
    )

    if use_fake_adapter:
        adapters: dict[CourseId, CourseAdapter] = {
            CourseId(c.id): FakeAdapter(course_id=CourseId(c.id)) for c in cfg.courses
        }
        creds: dict[CourseId, CourseCredentials] = {}
    else:
        adapters = _build_adapters(cfg, dry_run=dry_run)
        creds = _resolve_creds(cfg)

    store = InMemoryStore()
    await store.initialize()

    # Local demo: skip the 6 AM ET busy-wait. The real wait only makes sense
    # when invoked by the cron in book.yml.
    scheduler = _local_demo_scheduler(cfg.scheduler)

    orch = Orchestrator(
        adapters=adapters,
        store=store,
        notifier=ConsoleNotifier(),
        clock=RealClock(),
        scheduler=scheduler,
        creds=creds,
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
    help="Date to watch (YYYY-MM-DD). Defaults to today + target_offsets[0] days.",
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

    Designed to be called on a recurring schedule (every ~10 minutes via
    GH Actions cron or ACA Job). Exits 0 whether or not a slot was found.
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

    # Derive target_date: explicit override, or first target_date from the request.
    if target_date_str:
        try:
            target_date: date = date.fromisoformat(target_date_str)
        except ValueError as e:
            raise click.ClickException(f"--date must be YYYY-MM-DD, got {target_date_str!r}") from e
    else:
        target_date = request.target_dates[0]

    log.info("Watch check: target=%s dry_run=%s", target_date, dry_run)

    if use_fake_adapter:
        adapters: dict[CourseId, CourseAdapter] = {
            CourseId(c.id): FakeAdapter(course_id=CourseId(c.id)) for c in cfg.courses
        }
        creds: dict[CourseId, CourseCredentials] = {}
    else:
        adapters = _build_adapters(cfg, dry_run=dry_run)
        creds = _resolve_creds(cfg)

    store = InMemoryStore()
    await store.initialize()

    watch_config = cfg.watcher.to_watch_config()
    watch = WatchOrchestrator(
        adapters=adapters,
        store=store,
        notifier=ConsoleNotifier(),
        clock=RealClock(),
        scheduler=cfg.scheduler,
        watch_config=watch_config,
        creds=creds,
        policy=cfg.one_booking_policy,
    )
    result = await watch.check_once(request, target_date)
    if result is not None:
        log.info(
            "watch result: outcome=%s confirmation=%s", result.outcome, result.confirmation_code
        )


def _build_adapters(cfg: AppConfig, *, dry_run: bool = True) -> dict[CourseId, CourseAdapter]:
    """Build a CourseAdapter for each [[courses]] entry via _ADAPTER_REGISTRY.

    In dry_run mode, captcha_provider is always None (no CAPTCHA solving needed
    because the final booking POST is skipped). In live mode, 2captcha is used
    when TWOCAPTCHA_API_KEY is set; otherwise falls back to the inline solver.

    The booking_page_url on each ForeUpAdapter subclass determines which page
    the captcha provider targets — every course has its own booking page.
    """
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
                if twocaptcha_key:
                    cp = make_2captcha_provider(twocaptcha_key, cls.booking_page_url)
                else:
                    cp = make_captcha_provider(cls.booking_page_url)
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
    target_dates = tuple(today + timedelta(days=o) for o in sorted(cfg.request.target_offsets))

    # Use course_preferences (not cfg.courses) for the fingerprint.
    # cfg.courses may contain standby/disabled courses not in course_preferences;
    # including them would change the RequestId and invalidate idempotency records
    # every time the [[courses]] block is edited. See PLAN.md §13.1.
    course_ids = [CourseId(p) for p in cfg.request.course_preferences]
    windows = [TimeWindow(earliest=w.earliest, latest=w.latest) for w in cfg.request.time_windows]
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
        time_windows=windows,
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


def main() -> int:
    cli(standalone_mode=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
