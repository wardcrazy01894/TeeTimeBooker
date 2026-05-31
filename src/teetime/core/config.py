"""Config loading. TOML on disk + env-var secret refs. Pydantic validates shape.

Schema decisions documented in PLAN.md "Configuration schema". Secrets are NEVER
inlined in TOML; the file references env vars by name (e.g. password_env = "MB_PASS").
"""

from __future__ import annotations

import os
import tomllib
from datetime import time
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

from .models import CartPreference, WatchConfig
from .target_date import weekday_from_name


class MissingEnvVarError(RuntimeError):
    """A required env-var referenced by the config is unset."""

    def __init__(self, var_name: str, field_path: str) -> None:
        super().__init__(f"required env var {var_name!r} (referenced by {field_path}) is unset")
        self.var_name = var_name
        self.field_path = field_path


class TimeWindowConfig(BaseModel):
    """One acceptable tee-off range. Times in 24h HH:MM, course-local."""

    earliest: time
    latest: time


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


class RequestConfig(BaseModel):
    """One BookingRequest's static config."""

    target_offsets: list[int]
    time_windows: list[TimeWindowConfig]
    players: list[PlayerConfig]
    holes: int = 18
    max_price_per_player: Decimal | None = None
    cart: CartPreference = CartPreference.EITHER
    course_preferences: list[str]
    # Booking weekday the offsets anchor to. The target is computed as
    # (most-recent <target_weekday>) + offset, so the daily watch job locks onto the
    # upcoming target date all week instead of drifting with `today + offset`. The
    # 6 AM booker (which only runs on this weekday) is unaffected. See target_date.py.
    target_weekday: str = "sunday"

    @field_validator("target_weekday")
    @classmethod
    def _validate_target_weekday(cls, v: str) -> str:
        weekday_from_name(v)  # raises ValueError on an invalid name
        return v


class SchedulerConfig(BaseModel):
    timezone: str = "America/New_York"
    fire_time: time = time(6, 0, 0)
    early_arrival_ms: int = 500
    poll_interval_ms: int = 250
    max_poll_seconds: int = 30


class NotifierConfig(BaseModel):
    # Console is the only notifier in v0. Email/SMTP was dropped — booking
    # confirmations come from the course directly. See PLAN.md §16 (M4 removed).
    backend: str = "console"


class WatcherConfig(BaseModel):
    """Config for the cancellation-monitor job (M-feature-1).

    The watcher job runs on its own cron schedule (every 10 minutes during
    reasonable hours) and polls for newly available slots on the target date.

    All fields are optional so this block may be omitted from TOML configs
    that do not use the watch feature; defaults are applied automatically.

    Use `to_watch_config()` to translate to the frozen `WatchConfig` dataclass
    consumed by `WatchOrchestrator`. The conversion happens in the CLI's
    `teetime watch` command before constructing `WatchOrchestrator`. Note that
    `WatcherConfig` does NOT have `max_watch_duration_s` — that field lives
    only in `WatchConfig` (the dataclass) and is always set to its default
    (518400 s = 6 days) since there is no user-facing knob for it.
    """

    enabled: bool = False
    poll_interval_s: int = 600  # 10 minutes; must be >= 300 (anti-bot floor)
    # Wall-clock hours (course-local) bounding when polling is permitted.
    polling_start_hour: int = 7  # 7 AM
    polling_end_hour: int = 22  # 10 PM

    def to_watch_config(self) -> WatchConfig:
        """Translate pydantic WatcherConfig to the frozen WatchConfig dataclass.

        This conversion is performed in the CLI's `teetime watch` command before
        constructing `WatchOrchestrator`. `WatchConfig.max_watch_duration_s` has no
        corresponding TOML field — it is always the default (518400 s = 6 days). If
        a future need arises to expose it, add `max_watch_duration_s` to this class
        and pass it through here.
        """
        return WatchConfig(
            poll_interval_s=self.poll_interval_s,
            polling_start_hour=self.polling_start_hour,
            polling_end_hour=self.polling_end_hour,
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
    return cfg


_SENSITIVE_EXTRA_KEYS = frozenset(
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


def redact(cfg: AppConfig) -> AppConfig:
    """Return a deep copy of `cfg` with resolved secrets masked.

    Used by `teetime show-config` so the resolved config is inspectable
    without leaking PII or credentials. The `*_env` reference fields stay
    intact (they are env-var names, not values).
    """
    masked = cfg.model_copy(deep=True)
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
        c.extra = {k: ("***" if k in _SENSITIVE_EXTRA_KEYS else v) for k, v in c.extra.items()}
    return masked
