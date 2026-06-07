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

from pydantic import BaseModel, Field, model_validator

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
    # Wanted booking weekdays. The booking job books `today + offset` ONLY when that
    # date's weekday is in this set (core/booking_day_gate.py); the watcher checks the
    # next upcoming occurrence of EACH of these within the horizon (core/target_date.py).
    # Default Sat+Sun. Helpers convert to weekday indices via wanted_weekday_indices.
    target_weekdays: list[str] = Field(default_factory=lambda: ["saturday", "sunday"])
    # Deprecated alias: the old singular key. If present (and target_weekdays was not
    # also given) it seeds the set. Setting both is an error. See MULTIDAY_PLAN.md PR1.
    target_weekday: str | None = None

    @model_validator(mode="after")
    def _resolve_weekdays(self) -> RequestConfig:
        # `model_fields_set` holds the keys actually present in the input, so we can
        # tell "user gave target_weekdays" from "default applied".
        fields_set = self.model_fields_set
        if "target_weekday" in fields_set and "target_weekdays" in fields_set:
            raise ValueError("set either target_weekday (deprecated) or target_weekdays, not both")
        if self.target_weekday is not None:
            self.target_weekdays = [self.target_weekday]
        if not self.target_weekdays:
            raise ValueError("target_weekdays must be non-empty")
        for name in self.target_weekdays:
            weekday_from_name(name)  # raises ValueError("invalid weekday ...") on a bad name
        # Normalise: dedupe + sort by weekday index for a deterministic, legible order.
        by_index = {weekday_from_name(n): n.strip().lower() for n in self.target_weekdays}
        self.target_weekdays = [name for _, name in sorted(by_index.items())]
        return self

    @property
    def wanted_weekday_indices(self) -> frozenset[int]:
        """Python weekday() indices (Mon=0..Sun=6) of the wanted set."""
        return frozenset(weekday_from_name(n) for n in self.target_weekdays)


class SchedulerConfig(BaseModel):
    timezone: str = "America/New_York"
    fire_time: time = time(6, 0, 0)
    early_arrival_ms: int = 500
    poll_interval_ms: int = 250
    max_poll_seconds: int = 30
    # Seconds before T0 to start pre-fetching the CAPTCHA token on the race path
    # (Orchestrator with prefetch_book=True). The 2captcha solve takes ~75s and the
    # reCAPTCHA token lives ~120s, so the default starts the solve early enough to
    # finish just before T0 while keeping the token fresh for the post-T0 book POST.
    # See PLAN.md §9 / the 2026-06-07 prod post-mortem.
    captcha_prefetch_lead_s: int = 90


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
    # NOTE: polling_start_hour/polling_end_hour were REMOVED (MULTIDAY PR4) — the watcher
    # now polls on every run; there is no time-of-day gate.

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
