"""Config loading. TOML on disk + env-var secret refs. Pydantic validates shape.

Schema decisions documented in PLAN.md "Configuration schema". Secrets are NEVER
inlined in TOML; the file references env vars by name (e.g. password_env = "MB_PASS").
"""

from __future__ import annotations

import os
import tomllib
from datetime import date, time
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, Field, model_validator

from .models import CartPreference, WatchConfig
from .skip_dates import parse_skip_dates
from .target_date import weekday_from_name


class MissingEnvVarError(RuntimeError):
    """A required env-var referenced by the config is unset."""

    def __init__(self, var_name: str, field_path: str) -> None:
        super().__init__(f"required env var {var_name!r} (referenced by {field_path}) is unset")
        self.var_name = var_name
        self.field_path = field_path


class TimeWindowConfig(BaseModel):
    """One acceptable tee-off range on a specific weekday. Times 24h HH:MM, course-local.

    `weekday` binds this window to one day (per-day windows, PERDAY_WINDOWS_PLAN). Multiple
    windows may share a weekday; one-per-day still applies (the best slot across that day's
    windows wins).
    """

    weekday: str
    earliest: time
    latest: time

    @model_validator(mode="after")
    def _validate(self) -> TimeWindowConfig:
        weekday_from_name(self.weekday)  # raises ValueError("invalid weekday ...")
        if self.earliest > self.latest:
            raise ValueError(f"time window earliest {self.earliest} is after latest {self.latest}")
        return self


class CourseConfig(BaseModel):
    """Per-course settings. `id` is the orchestrator-stable CourseId string."""

    id: str
    adapter: str
    username_env: str
    password_env: str
    extra: dict[str, str] = Field(default_factory=dict)


class PlayerConfig(BaseModel):
    """One golfer in the party. After `load()`, `email`/`phone`/`member_number`
    are populated from the corresponding `*_env` env-vars (resolution happens
    once at load time so missing vars fail loudly before the orchestrator runs).
    """

    first_name: str
    last_name: str
    email_env: str | None = None
    email: str | None = None
    phone_env: str | None = None
    phone: str | None = None
    member_number_env: str | None = None
    member_number: str | None = None


class BookingCutoffConfig(BaseModel):
    """Hard cutoff after which a target date is FROZEN — no new book, no upgrade.

    Absolute wall-clock relative to the RESERVATION date (tee-time-independent):
    ``cutoff = datetime(target_date - days_before, time_of_day, tz=scheduler.timezone)``.
    Once wall-clock ``now`` reaches that instant, the target date is frozen: the watcher
    makes no new booking AND no upgrade, so the operator can never be surprised by a
    last-minute booking they don't learn about in time. Whatever is held at the cutoff is
    final (held bookings are never auto-cancelled). Default (shipped): 16:00 ET the day
    before. See LEADTIME_SKIP_PLAN.md §F1.

    ``days_before = 0`` is INTENTIONALLY valid: it places the cutoff at ``time_of_day`` on the
    reservation day itself (e.g. freeze same-day bookings after 16:00). Only a NEGATIVE
    ``days_before`` (which would target a date AFTER the reservation) is rejected.
    """

    days_before: int = 1
    time_of_day: time = time(16, 0, 0)

    @model_validator(mode="after")
    def _validate(self) -> BookingCutoffConfig:
        if self.days_before < 0:
            raise ValueError(f"booking_cutoff.days_before must be >= 0, got {self.days_before}")
        return self


class RequestConfig(BaseModel):
    """One BookingRequest's static config."""

    target_offsets: list[int]
    # Per-day windows (PERDAY_WINDOWS_PLAN): each window carries a `weekday`. The wanted
    # booking weekdays are DERIVED from the distinct weekdays here (see wanted_weekday_indices)
    # — there is no separate target_weekdays list. Multiple windows may share a weekday;
    # one reservation per day (best window) still applies. Non-empty.
    time_windows: list[TimeWindowConfig]
    players: list[PlayerConfig]
    holes: int = 18
    max_price_per_player: Decimal | None = None
    cart: CartPreference = CartPreference.EITHER
    course_preferences: list[str]
    # Hard booking cutoff (LEADTIME_SKIP_PLAN F1). Defaulted so existing configs load
    # unchanged. Does NOT feed the RequestId fingerprint (see models.build_request_fingerprint)
    # — it is booking POLICY, not request identity, so in-process idempotency keys are stable.
    booking_cutoff: BookingCutoffConfig = BookingCutoffConfig()
    # No-redeploy "skip this day" control (LEADTIME_SKIP_PLAN F2). `skip_dates_env` is an
    # env-var NAME (never a literal date list in TOML), following the `*_env` convention; in
    # prod it is injected from a Key Vault secret editable in the Portal. It is resolved at
    # load() time (fail-open) into `skip_dates`. NOTE: unlike credential `*_env` fields, an
    # UNSET/empty/malformed skip env is NOT an error (absence = no skips) — so it is hydrated
    # in load(), never via the raising _resolve_env. Like booking_cutoff, neither field feeds
    # the RequestId fingerprint.
    skip_dates_env: str | None = None
    skip_dates: frozenset[date] = Field(default_factory=frozenset)
    # Migration sentinels (PERDAY_WINDOWS_PLAN §7, hard cutover): the multi-day re-arch's
    # target_weekdays / target_weekday were REMOVED — each window now carries its own
    # weekday. These typed-but-forbidden fields exist ONLY so an un-migrated config fails
    # loudly (pydantic's default extra="ignore" would otherwise drop them silently).
    target_weekdays: object | None = None
    target_weekday: object | None = None

    @model_validator(mode="after")
    def _validate_windows(self) -> RequestConfig:
        if self.target_weekdays is not None or self.target_weekday is not None:
            raise ValueError(
                "target_weekdays/target_weekday have been removed; tag each "
                "[[request.time_windows]] with a `weekday` instead (see "
                "PERDAY_WINDOWS_PLAN.md §7)."
            )
        if not self.time_windows:
            raise ValueError("request.time_windows must be non-empty")
        # Normalise window order by (weekday index, earliest) for deterministic ranking +
        # a stable RequestId fingerprint.
        self.time_windows = sorted(
            self.time_windows,
            key=lambda w: (weekday_from_name(w.weekday), w.earliest),
        )
        return self

    @property
    def wanted_weekday_indices(self) -> frozenset[int]:
        """Python weekday() indices (Mon=0..Sun=6) of the days that have windows."""
        return frozenset(weekday_from_name(w.weekday) for w in self.time_windows)

    def windows_for(self, weekday: int) -> tuple[TimeWindowConfig, ...]:
        """The configured windows whose weekday index == `weekday`, in normalised
        (earliest-first) order. Empty tuple if none — callers never pass a windowless
        weekday in normal flow (wanted_weekday_indices is derived from these windows)."""
        return tuple(w for w in self.time_windows if weekday_from_name(w.weekday) == weekday)


class SchedulerConfig(BaseModel):
    timezone: str = "America/New_York"
    fire_time: time = time(6, 0, 0)
    early_arrival_ms: int = 500
    poll_interval_ms: int = 250
    max_poll_seconds: int = 30
    # Seconds before T0 to start pre-fetching the CAPTCHA token on the race path
    # (Orchestrator with prefetch_book=True). Must be >= the provider timeout so the
    # pre-fetch typically finishes before T0: 24 polls x 5s/poll = 120s. The invariant
    # also keeps the token fresh at T0 — age = lead - solve_time <= 120s <= reCAPTCHA
    # validity (~120s). See 2026-06-07/2026-06-14 prod post-mortems in PLAN.md §9.
    captcha_prefetch_lead_s: int = 120
    # Number of CAPTCHA tokens to pre-solve CONCURRENTLY on the race path
    # (Orchestrator with prefetch_book=True), so the first N ranked candidates each
    # fire near-instantly instead of re-solving a fresh single-use token inline. Default
    # 3 balances solve-cost/rate-limit against fallback depth at a competitive drop
    # (RACE_PREWARM_PLAN §4.4). Ignored off the race path (upgrade/inline solve count=1).
    captcha_prefetch_count: int = Field(default=3, ge=1)
    # Blind-POST fan-out cap (BLIND_POST_PLAN.md §5, OQ3). On the race path, for a
    # blind-capable PRIMARY course (Mangrove Bay), the orchestrator fires up to this many
    # concurrent book POSTs for the top-N ranked in-window grid slots at T0, keeps the best,
    # and cancels the rest. DECOUPLED from captcha_prefetch_count (the single-POST race
    # prefetch depth): the CAPTCHA prefetch SCALES to min(blind_post_max_count, in-window
    # grid count) when the primary is blind-capable. The actual burst N is further bounded by
    # the pooled-token count. `0` DISABLES blind fan-out (single-POST race path). The code
    # default is 12 (the superseded "all-in-window" value), but the SHIPPED configs override to
    # 3 — the top 3 slots nearest the window midpoint; the surplus POSTs past the first success
    # bounce on ForeUP's "1 reservation/day" rule (see config/example.toml, RESEARCH_FALLBACK_PLAN).
    # Ignored off the race path and for non-capable or non-primary courses.
    blind_post_max_count: int = Field(default=12, ge=0)
    # Blind-POST 0-booked fallback reserve (RESEARCH_FALLBACK_PLAN §2 Q3). EXTRA CAPTCHA
    # tokens to pre-solve BEYOND the blind burst so the post-reguard FRESH search's book()
    # pops a fresh POOLED token instead of a ~75s inline solve. The burst size is unchanged
    # (synthesize_blind_slots truncates to blind_post_max_count), so these tokens are never
    # fired — they REMAIN pooled for the late fallback (all tokens are solved in one
    # concurrent batch, so the reserve is "present," not "fresher"). Race-critical pool
    # depth → parity-checked across the committed configs. `0` = no reserve (off).
    blind_post_fallback_token_reserve: int = Field(default=2, ge=0)


class NotifierConfig(BaseModel):
    # Console is the only notifier in v0. Email/SMTP was dropped — booking
    # confirmations come from the course directly. See PLAN.md §16 (M4 removed).
    backend: str = "console"


class WatcherConfig(BaseModel):
    """Config for the cancellation-monitor job (M-feature-1).

    The watcher job runs on its own cron schedule (every 10 minutes, year-round; it polls
    on every run — the time-of-day gate was removed) for newly available slots on each
    wanted target date.

    All fields are optional so this block may be omitted from TOML configs
    that do not use the watch feature; defaults are applied automatically.

    Use `to_watch_config()` to translate to the frozen `WatchConfig` dataclass
    consumed by `WatchOrchestrator`. The conversion happens in the CLI's
    `teetime watch` command before constructing `WatchOrchestrator`.
    """

    enabled: bool = False
    poll_interval_s: int = 600  # 10 minutes; must be >= 300 (anti-bot floor)
    # NOTE: polling_start_hour/polling_end_hour were REMOVED (MULTIDAY PR4) — the watcher
    # now polls on every run; there is no time-of-day gate.

    def to_watch_config(self) -> WatchConfig:
        """Translate pydantic WatcherConfig to the frozen WatchConfig dataclass.

        This conversion is performed in the CLI's `teetime watch` command before
        constructing `WatchOrchestrator`.
        """
        return WatchConfig(
            poll_interval_s=self.poll_interval_s,
        )


class PrioritySlotConfig(BaseModel):
    """One entry in the ordered priority list (M-feature-2).

    priority=0 is highest. course_id must appear in [[courses]].
    time_window overrides [request.time_windows] for this specific priority check.
    """

    priority: int
    course_id: str
    time_window_earliest: time
    time_window_latest: time


class OneBookingPolicyConfig(BaseModel):
    """Config for the "one booking" invariant enforcer (M-feature-2).

    When enabled, the watch job also checks whether a higher-priority slot has
    become available. If so, it cancels the current managed booking and books
    the better slot.

    priority_slots is an ordered list (priority=0 wins). If omitted, the policy
    defaults to the courses in [request].course_preferences order with the same
    time_window as [request].time_windows[0].
    """

    enabled: bool = False
    priority_slots: list[PrioritySlotConfig] = Field(default_factory=list)


class AppConfig(BaseModel):
    courses: list[CourseConfig]
    request: RequestConfig
    scheduler: SchedulerConfig = SchedulerConfig()
    notifier: NotifierConfig = NotifierConfig()
    watcher: WatcherConfig = WatcherConfig()
    one_booking_policy: OneBookingPolicyConfig = OneBookingPolicyConfig()


def _resolve_env(var_name: str, field_path: str) -> str:
    val = os.environ.get(var_name)
    if val is None:
        raise MissingEnvVarError(var_name, field_path)
    return val


def _hydrate_skip(req: RequestConfig) -> frozenset[date]:
    """Resolve `skip_dates_env` -> a frozenset[date], FAIL-OPEN.

    Unlike `_resolve_env` (credentials), an unset / empty / malformed value is NOT an error:
    it yields an empty set (no skips). The job must never crash on a fat-fingered Portal edit
    of TEETIME_SKIP_DATES — a typo that took down the 06:00 booker would be a worse failure
    than missing a skip. See LEADTIME_SKIP_PLAN §F2 / Edge E6.
    """
    if req.skip_dates_env is None:
        return frozenset()
    return parse_skip_dates(os.environ.get(req.skip_dates_env))


def _hydrate_player(p: PlayerConfig, idx: int) -> PlayerConfig:
    base = f"request.players[{idx}]"
    if p.email_env is not None:
        p.email = _resolve_env(p.email_env, f"{base}.email_env")
    if p.phone_env is not None:
        p.phone = _resolve_env(p.phone_env, f"{base}.phone_env")
    if p.member_number_env is not None:
        p.member_number = _resolve_env(p.member_number_env, f"{base}.member_number_env")
    return p


def load(path: Path) -> AppConfig:
    """Read TOML at `path`, validate shape, resolve env-var refs, return AppConfig.

    Raises MissingEnvVarError if any required `*_env` reference is unset.
    """
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    cfg = AppConfig.model_validate(raw)
    cfg.request.players = [_hydrate_player(p, i) for i, p in enumerate(cfg.request.players)]
    cfg.request.skip_dates = _hydrate_skip(cfg.request)
    return cfg


# Public: shared across modules (redact() here + _resolve_creds in __main__). Card +
# credential fields whose VALUES are masked in show-config output.
SENSITIVE_EXTRA_KEYS = frozenset(
    {
        "card_number",
        "cvv",
        "expiry_month",
        "expiry_year",
        "billing_address",
        "billing_postal_code",
        "billing_country",
        "name_on_card",
        "password",
    }
)

# Subset that MUST be provided via the `*_env` form (never a literal in TOML) — a raw
# value here is a real secret/PII. `billing_country` is intentionally EXCLUDED: it is a
# non-secret 2-letter code with a sane "US" default, so forcing an env var for it would be
# pure friction. Used by _resolve_creds (__main__) to reject literal credential keys.
SECRET_EXTRA_KEYS = SENSITIVE_EXTRA_KEYS - frozenset({"billing_country"})


def redact(cfg: AppConfig) -> AppConfig:
    """Return a deep copy of `cfg` with resolved secrets masked.

    Used by `teetime show-config` so the resolved config is inspectable
    without leaking PII or credentials. The `*_env` reference fields stay
    intact (they are env-var names, not values).
    """
    masked = cfg.model_copy(deep=True)
    # `request.skip_dates` (and its `skip_dates_env` NAME) are intentionally left UNMASKED:
    # they are calendar dates, not secrets/PII, and surfacing the active skip set in
    # show-config is a deliberate operator affordance (LEADTIME_SKIP_PLAN Q2).
    for p in masked.request.players:
        if p.email is not None:
            p.email = "***"
        if p.phone is not None:
            p.phone = "***"
        if p.member_number is not None:
            p.member_number = "***"
    # Redact sensitive literal values in course extra dicts.
    # Keys ending in _env are env-var *names* (safe to show); resolved literal
    # values for known sensitive fields are masked.
    for c in masked.courses:
        c.extra = {k: ("***" if k in SENSITIVE_EXTRA_KEYS else v) for k, v in c.extra.items()}
    return masked
