"""Shared builders for the MU-9a booking-runner tests (``tests/tenant/test_runner*.py``).

One release event (``mb0600et``: 06:00 America/New_York, advance 7) over one fake course, an
``InMemoryTenantStore`` seeded through its public Protocol (users, accounts with REAL AES-GCM
ciphertexts, explicit rows), a scriptable per-account adapter factory, and spies for the
store/crypto call-budget proof. Every runner test drives time with a ``VirtualClock``: the
runner's concurrent orchestrators, streamed writer and self-deadline all sleep on it.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from teetime.core.adapter import CourseAdapter
from teetime.core.config import BookingCutoffConfig, SchedulerConfig
from teetime.core.models import CourseId, SlotId, TeeTimeSlot
from teetime.core.release_policy import ReleasePolicy
from teetime.courses.foreup.token_pool import LeaseKey, SharedCaptchaPool
from teetime.dev.blind_fake_adapter import BlindFakeAdapter
from teetime.dev.fake_adapter import FakeAdapter
from teetime.dev.virtual_clock import VirtualClock
from teetime.tenant.crypto import Keyring, credential_aad, encrypt_password
from teetime.tenant.in_memory_store import InMemoryTenantStore
from teetime.tenant.models import (
    AccountProvenance,
    AccountStatus,
    CourseAccount,
    RequestRow,
    User,
    UserId,
    UserRole,
    UserStatus,
    derive_account_id,
)
from teetime.tenant.runner import ReleaseEvent

MB = CourseId("fake:mb")
TZ = "America/New_York"
ZONE = ZoneInfo(TZ)
# The drop: Sat 2026-10-03 06:00 EDT = 10:00 UTC, booking Sat 2026-10-10 (advance 7).
T0 = datetime(2026, 10, 3, 10, 0, tzinfo=UTC)
TARGET = date(2026, 10, 10)
SETUP_NOW = T0 - timedelta(days=1)  # rows are created the day before (well before the cutoff)
EVENT = ReleaseEvent(key="mb0600et", timezone=TZ, release_time=time(6, 0), course_ids=(MB,))
POLICIES = {MB: ReleasePolicy(advance_days=7, release_time=time(6, 0), timezone=TZ)}
WINDOW = (time(7, 0), time(9, 30))  # midpoint 08:15
KEYRING = Keyring(active_kid="k1", keys={"k1": os.urandom(32)})


def scheduler(*, lead_s: int = 30) -> SchedulerConfig:
    """The shipped race knobs (stagger (-500,-250,0), burst 3, reserve 2) with a short lead
    and poll so VirtualClock runs stay small. timezone/fire_time are deliberately WRONG: the
    runner must derive them from the event."""
    return SchedulerConfig(
        timezone="UTC",
        fire_time=time(23, 0),
        early_arrival_ms=500,
        blind_post_stagger_ms=(-500, -250, 0),
        poll_interval_ms=10,
        max_poll_seconds=1,
        captcha_prefetch_lead_s=lead_s,
        captcha_prefetch_count=3,
        blind_post_max_count=3,
        blind_post_fallback_token_reserve=2,
    )


def race_clock(*, before_t0_s: float = 45.0) -> VirtualClock:
    return VirtualClock(start=T0 - timedelta(seconds=before_t0_s))


def slot(hour: int, minute: int, *, course: CourseId = MB) -> TeeTimeSlot:
    return TeeTimeSlot(
        course_id=course,
        slot_id=SlotId(f"s-{hour:02d}{minute:02d}"),
        tee_time=datetime(2026, 10, 10, hour, minute, tzinfo=ZONE),
        holes=18,
        available_spots=4,
        price_per_player=Decimal("45.00"),
        cart_included=True,
    )


# The in-window grid ranked by distance from the 08:15 midpoint: 08:15, 08:00, 08:30, 07:45, ...
GRID = [slot(7, 30), slot(7, 45), slot(8, 0), slot(8, 15), slot(8, 30), slot(8, 45), slot(9, 0)]


def new_store() -> InMemoryTenantStore:
    return InMemoryTenantStore(course_timezones={MB: TZ}, cutoff=BookingCutoffConfig())


@dataclass(frozen=True)
class Seeded:
    user: User
    account: CourseAccount
    row: RequestRow
    password: str


async def seed_account(
    store: InMemoryTenantStore,
    *,
    n: int,
    password: str | None = None,
    ciphertext: str | None = None,
) -> Seeded:
    """A user + an MB account (password encrypted with AAD bound to the account) + an explicit
    PENDING row for TARGET in WINDOW."""
    user = User(
        id=UserId(uuid4()),
        oauth_provider="github",
        oauth_subject=f"gh-{uuid4()}",
        email=f"user{n}@example.test",
        display_name=f"User {n}",
        role=UserRole.MEMBER,
        status=UserStatus.ACTIVE,
    )
    await store.upsert_user(user)
    pw = password if password is not None else f"s3cret-pass-{n}-{uuid4().hex[:6]}"
    draft = CourseAccount(
        id=derive_account_id(user.id, MB),
        user_id=user.id,
        course_id=MB,
        provenance=AccountProvenance.USER_SUPPLIED,
        username=f"golfer{n}",
        password_ciphertext="placeholder",
        key_id="k1",
        status=AccountStatus.ACTIVE,
    )
    blob = ciphertext or encrypt_password(KEYRING, pw, aad=credential_aad(draft))
    account = CourseAccount(**{**_fields(draft), "password_ciphertext": blob})
    await store.upsert_account(account)
    row = await store.create_explicit_row(
        user_id=user.id,
        account_id=account.id,
        target_date=TARGET,
        window_earliest=WINDOW[0],
        window_latest=WINDOW[1],
        party_size=2,
        now=SETUP_NOW,
    )
    return Seeded(user=user, account=account, row=row, password=pw)


def _fields(account: CourseAccount) -> dict[str, Any]:
    return {name: getattr(account, name) for name in account.__dataclass_fields__}


def blind_adapter(slots: list[TeeTimeSlot] | None = None) -> BlindFakeAdapter:
    adapter = BlindFakeAdapter(course_id=MB)
    adapter.set_blind_slots(list(GRID if slots is None else slots))
    return adapter


@dataclass
class FactoryCall:
    course_id: CourseId
    account: CourseAccount
    pool: SharedCaptchaPool | None
    lease_key: LeaseKey | None
    dry_run: bool


@dataclass
class ScriptedFactory:
    """``AdapterFactory`` returning a pre-built adapter per account id (default: a fresh
    ``BlindFakeAdapter`` over GRID). Records every call."""

    adapters: dict[Any, CourseAdapter] = field(default_factory=dict)
    calls: list[FactoryCall] = field(default_factory=list)

    def __call__(
        self,
        *,
        course_id: CourseId,
        account: CourseAccount,
        pool: SharedCaptchaPool | None,
        lease_key: LeaseKey | None,
        dry_run: bool,
    ) -> CourseAdapter:
        self.calls.append(FactoryCall(course_id, account, pool, lease_key, dry_run))
        adapter = self.adapters.get(account.id)
        if adapter is None:
            adapter = blind_adapter()
            self.adapters[account.id] = adapter
        return adapter


class SpyStore:
    """Records (method, clock instant) for EVERY ``TenantStore`` call, then delegates. A spy on
    the collaborator, never on the runner."""

    def __init__(self, inner: InMemoryTenantStore, clock: VirtualClock) -> None:
        self._inner = inner
        self._clock = clock
        self.calls: list[tuple[str, datetime]] = []

    def __getattr__(self, name: str) -> Callable[..., Awaitable[Any]]:
        target = getattr(self._inner, name)

        async def call(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, self._clock.now_utc()))
            return await target(*args, **kwargs)

        return call

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


class NullUserNotifier:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def send(self, event: object) -> None:
        self.events.append(event)


class FakeAdapterNonBlind(FakeAdapter):
    """A plain (non-blind) FakeAdapter bound to MB."""

    def __init__(self) -> None:
        super().__init__(course_id=MB)
