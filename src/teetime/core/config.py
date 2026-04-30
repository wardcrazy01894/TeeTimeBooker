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

from pydantic import BaseModel, Field

from .models import CartPreference


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


class SchedulerConfig(BaseModel):
    timezone: str = "America/New_York"
    fire_time: time = time(6, 0, 0)
    early_arrival_ms: int = 500
    poll_interval_ms: int = 250
    max_poll_seconds: int = 30


class NotifierConfig(BaseModel):
    backend: str = "email"
    email_to: str | None = None
    smtp_host_env: str | None = None
    smtp_user_env: str | None = None
    smtp_pass_env: str | None = None


class PersistenceConfig(BaseModel):
    backend: str = "sqlite"
    path: Path = Path("./state/teetime.db")


class AppConfig(BaseModel):
    courses: list[CourseConfig]
    request: RequestConfig
    scheduler: SchedulerConfig = SchedulerConfig()
    notifier: NotifierConfig = NotifierConfig()
    persistence: PersistenceConfig = PersistenceConfig()


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
    return masked
