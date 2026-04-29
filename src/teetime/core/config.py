"""Config loading. TOML on disk + env-var secret refs. Pydantic validates shape.

Schema decisions documented in PLAN.md "Configuration schema". Secrets are NEVER
inlined in TOML; the file references env vars by name (e.g. password_env = "MB_PASS").
"""

from __future__ import annotations

from datetime import time
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, Field

from .models import CartPreference


class TimeWindowConfig(BaseModel):
    """One acceptable tee-off range. Times in 24h HH:MM, course-local."""

    earliest: time
    latest: time


class CourseConfig(BaseModel):
    """Per-course settings. `id` is the orchestrator-stable CourseId string."""

    id: str
    adapter: str                          # e.g. "foreup.mangrove_bay"
    username_env: str                     # name of env var, NOT the value
    password_env: str
    extra: dict[str, str] = Field(default_factory=dict)


class PlayerConfig(BaseModel):
    """One golfer in the party. Email/phone are pass-through to the adapter; PII is
    redacted from `attempt_log` payloads (see PLAN.md §10 'PII handling').
    """

    first_name: str
    last_name: str
    email_env: str | None = None          # name of env var holding the email
    email: str | None = None              # OR inline (less recommended)
    phone_env: str | None = None
    phone: str | None = None
    member_number_env: str | None = None
    member_number: str | None = None


class RequestConfig(BaseModel):
    """One BookingRequest's static config. CLI/env can override fields at runtime.

    Dates are computed at run time from `target_offsets` (days from today, in the
    scheduler timezone). RequestId is derived from the OFFSETS, never the resolved
    dates. The idempotency key is (RequestId, resolved_date) — see PLAN.md §13.
    """

    target_offsets: list[int]             # e.g. [7] -> today + 7 days
    time_windows: list[TimeWindowConfig]
    players: list[PlayerConfig]           # length defines party size
    holes: int = 18
    max_price_per_player: Decimal | None = None
    cart: CartPreference = CartPreference.EITHER
    course_preferences: list[str]         # ordered list of CourseConfig.id values


class SchedulerConfig(BaseModel):
    """When to fire and how to race the booking window opening."""

    timezone: str = "America/New_York"
    fire_time: time = time(6, 0, 0)       # local wall-clock
    early_arrival_ms: int = 500           # land this far before T0
    poll_interval_ms: int = 250           # between empty-inventory retries
    max_poll_seconds: int = 30            # stop polling after this


class NotifierConfig(BaseModel):
    """Pluggable notifier selection."""

    backend: str = "email"                # "email" | "console" | "noop"
    email_to: str | None = None
    smtp_host_env: str | None = None
    smtp_user_env: str | None = None
    smtp_pass_env: str | None = None


class PersistenceConfig(BaseModel):
    """Where state lives between runs."""

    backend: str = "sqlite"               # "sqlite" | "json"
    path: Path = Path("./state/teetime.db")


class AppConfig(BaseModel):
    """Top-level config. Loaded from TOML, validated, then passed to orchestrator."""

    courses: list[CourseConfig]
    request: RequestConfig
    scheduler: SchedulerConfig = SchedulerConfig()
    notifier: NotifierConfig = NotifierConfig()
    persistence: PersistenceConfig = PersistenceConfig()


def load(path: Path) -> AppConfig:
    """Read TOML at `path` and return validated AppConfig. Stub for M1.T2."""
    raise NotImplementedError
