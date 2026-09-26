"""Builders for the MU-10b tenant-watcher runner tests (``tests/tenant/test_watch_runner*.py``).

Reuses the MU-9a runner builders (one fake course ``MB`` in America/New_York, advance 7, a
REAL AES-GCM keyring, an ``InMemoryTenantStore`` seeded through its public Protocol) and adds
what the watcher needs: a watch-time clock inside the horizon, booked rows seeded through the
LEASED ``record_outcomes`` path (never by poking the store's dicts), a scriptable per-account
``FakeAdapter`` factory, and a cadence-aware ``now`` picker so "no login" tests are not flaky
(the reconcile cadence reads the account's random UUID).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from teetime.core.config import BookingCutoffConfig, OneBookingPolicyConfig, SchedulerConfig
from teetime.core.models import (
    BookingOutcome,
    BookingResult,
    CourseId,
    ExistingReservation,
    TeeTimeSlot,
)
from teetime.courses.foreup.token_pool import LeaseKey, SharedCaptchaPool
from teetime.dev.fake_adapter import FakeAdapter
from teetime.tenant.crypto import credential_aad, encrypt_password
from teetime.tenant.in_memory_store import InMemoryTenantStore
from teetime.tenant.models import (
    AccountProvenance,
    AccountStatus,
    Actor,
    BookingSource,
    BookingState,
    CourseAccount,
    OwnedBooking,
    OwnedBookingId,
    RankedWindow,
    RequestRow,
    ReservationSnapshot,
    RowStatus,
    SnapshotEntry,
    User,
    UserId,
    UserRole,
    UserStatus,
    derive_account_id,
)
from teetime.tenant.store import RowOutcome
from teetime.tenant.watch_runner import RECONCILE_EVERY_N_RUNS, watch_run_index

from .runner_builders import GRID, KEYRING, MB, POLICIES, TARGET, WINDOW, slot

# A Monday noon ET, five days before the Saturday TARGET (inside [today, today + 7], well before
# the Fri 16:00 ET cutoff).
WATCH_NOW = datetime(2026, 10, 5, 16, 0, tzinfo=UTC)
AFTER_CUTOFF = datetime(2026, 10, 9, 21, 0, tzinfo=UTC)  # Fri 17:00 EDT: TARGET is frozen
SEED_NOW = WATCH_NOW - timedelta(days=2)
BETTER = slot(8, 15)  # the window midpoint: strictly better than anything else in GRID
HELD_EARLY = slot(7, 30)  # the held booking the upgrade tests start from

__all__ = ["GRID", "KEYRING", "MB", "POLICIES", "TARGET", "WINDOW", "slot"]


def watch_scheduler() -> SchedulerConfig:
    """timezone is deliberately WRONG: the watcher must use each row's course timezone."""
    return SchedulerConfig(timezone="UTC")


POLICY_ON = OneBookingPolicyConfig(enabled=True)
CUTOFF = BookingCutoffConfig()


def new_store() -> InMemoryTenantStore:
    return InMemoryTenantStore(course_timezones={MB: "America/New_York"}, cutoff=CUTOFF)


@dataclass(frozen=True)
class Seeded:
    user: User
    account: CourseAccount
    row: RequestRow
    password: str


async def seed(
    store: InMemoryTenantStore, *, n: int, party_size: int = 2, ciphertext: str | None = None
) -> Seeded:
    """A user + an MB account (real AES-GCM blob, AAD bound) + an explicit PENDING row for
    TARGET in WINDOW with ``party_size``."""
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
    password = f"watch-pass-{n}-{uuid4().hex[:6]}"
    draft = CourseAccount(
        id=derive_account_id(user.id, MB),
        user_id=user.id,
        course_id=MB,
        provenance=AccountProvenance.USER_SUPPLIED,
        username=f"watcher{n}",
        password_ciphertext="placeholder",
        key_id="k1",
        status=AccountStatus.ACTIVE,
    )
    blob = ciphertext or encrypt_password(KEYRING, password, aad=credential_aad(draft))
    account = CourseAccount(
        **{name: getattr(draft, name) for name in draft.__dataclass_fields__}
        | {"password_ciphertext": blob}
    )
    await store.upsert_account(account)
    row = await store.create_explicit_row(
        user_id=user.id,
        account_id=account.id,
        target_date=TARGET,
        options=(RankedWindow(1, WINDOW[0], WINDOW[1]),),
        party_size=party_size,
        now=SEED_NOW,
    )
    return Seeded(user=user, account=account, row=row, password=password)


def ledger_entry(
    row: RequestRow,
    raw_id: str,
    tee: TeeTimeSlot,
    *,
    state: BookingState = BookingState.HELD,
    source: BookingSource = BookingSource.BLIND,
) -> OwnedBooking:
    return OwnedBooking(
        id=OwnedBookingId(uuid4()),
        row_id=row.id,
        course_account_id=row.course_account_id,
        course_id=row.course_id,
        target_date=row.target_date,
        raw_reservation_id=raw_id,
        tee_time=tee.tee_time,
        party_size=row.party_size,
        source=source,
        state=state,
    )


async def book_row(
    store: InMemoryTenantStore,
    seeded: Seeded,
    *,
    raw_id: str,
    tee: TeeTimeSlot,
    owned: bool = True,
    extras: tuple[OwnedBooking, ...] = (),
) -> RequestRow:
    """Drive the row PENDING -> BOOKED the way the booking runner does (claim + WRITE #2). An
    ``owned`` booking is ledgered ``held`` (confirmation ``TTB:<raw>``); an unowned one (a
    reservation a guard found, §4.6) carries the RAW confirmation and no ledger entry."""
    owner = f"booker:seed:{uuid4().hex[:6]}"
    until = SEED_NOW + timedelta(minutes=20)
    claimed = await store.claim_rows([seeded.row.id], owner=owner, until=until, now=SEED_NOW)
    assert claimed == {seeded.row.id}
    conf = f"TTB:{raw_id}" if owned else raw_id
    result = BookingResult(
        request_id=seeded.row.request_id,
        outcome=BookingOutcome.BOOKED if owned else BookingOutcome.ALREADY_BOOKED,
        course_id=MB,
        slot=tee,
        confirmation_code=conf,
        booked_at=SEED_NOW,
        attempts=1,
    )
    await store.record_outcomes(
        [
            RowOutcome(
                row_id=seeded.row.id,
                course_account_id=seeded.account.id,
                target_date=TARGET,
                actor=Actor.BOOKING_RUNNER,
                to_status=RowStatus.BOOKED,
                last_outcome=result.outcome.value,
                at=SEED_NOW,
                result=result,
                booking=ledger_entry(seeded.row, raw_id, tee) if owned else None,
                held_extras=extras,
                release_lease_owner=owner,
            )
        ]
    )
    return await stored_row(store, seeded)


async def stored_row(store: InMemoryTenantStore, seeded: Seeded) -> RequestRow:
    row = await store.get_row(seeded.row.id, user_id=seeded.user.id)
    assert row is not None
    return row


def reservation(raw_id: str, tee: TeeTimeSlot, *, party_size: int = 2) -> ExistingReservation:
    """Server-sourced: RAW id (no ``TTB:``), as ``list_reservations`` returns it."""
    return ExistingReservation(
        course_id=MB, confirmation_code=raw_id, tee_time=tee.tee_time, party_size=party_size
    )


def trusted_snapshot(
    seeded: Seeded, *, at: datetime, entries: tuple[tuple[str, TeeTimeSlot], ...]
) -> ReservationSnapshot:
    return ReservationSnapshot(
        course_account_id=seeded.account.id,
        observed_at=at,
        source="watcher",
        trusted=True,
        entries=tuple(
            SnapshotEntry(raw_id=raw, tee_time=s.tee_time, party_size=seeded.row.party_size)
            for raw, s in entries
        ),
    )


def cadence_hits(account: CourseAccount, now: datetime) -> bool:
    return (account.id.int + watch_run_index(now)) % RECONCILE_EVERY_N_RUNS == 0


def now_without_cadence(*accounts: CourseAccount, base: datetime = WATCH_NOW) -> datetime:
    """The first 10-minute run at/after ``base`` where no account's reconcile cadence fires."""
    for k in range(RECONCILE_EVERY_N_RUNS * 2):
        now = base + timedelta(minutes=10 * k)
        if not any(cadence_hits(a, now) for a in accounts):
            return now
    raise AssertionError("no cadence-free run found")  # pragma: no cover


def now_with_cadence(account: CourseAccount, *, base: datetime = WATCH_NOW) -> datetime:
    for k in range(RECONCILE_EVERY_N_RUNS):
        now = base + timedelta(minutes=10 * k)
        if cadence_hits(account, now):
            return now
    raise AssertionError("cadence never fires")  # pragma: no cover


class WatchFake(FakeAdapter):
    """An MB ``FakeAdapter`` (``AuthStateReportable``, not snapshot-health) serving GRID."""

    def __init__(self, *, slots: list[TeeTimeSlot] | None = None) -> None:
        super().__init__(course_id=MB)
        self.set_search_response(list(GRID if slots is None else slots))
        self.closed = 0

    async def aclose(self) -> None:
        self.closed += 1


class UntrustedSnapshotFake(WatchFake):
    """ForeUP's non-JSON / missing-``reservations`` login (§7.5 b/c): logged in, cache stale."""

    @property
    def snapshot_trusted(self) -> bool:
        return False


@dataclass
class FakeFactory:
    """``AdapterFactory``: one pre-built adapter per account id (default a fresh ``WatchFake``);
    records every call so a test can count the shared search client builds."""

    adapters: dict[Any, FakeAdapter] = field(default_factory=dict)
    calls: list[tuple[CourseId, Any]] = field(default_factory=list)

    def __call__(
        self,
        *,
        course_id: CourseId,
        account: CourseAccount,
        pool: SharedCaptchaPool | None,
        lease_key: LeaseKey | None,
        dry_run: bool,
    ) -> FakeAdapter:
        assert pool is None and lease_key is None  # the watcher never uses the T0 pool
        self.calls.append((course_id, account.id))
        adapter = self.adapters.get(account.id)
        if adapter is None:
            adapter = WatchFake()
            self.adapters[account.id] = adapter
        return adapter

    def total(self, attr: str) -> int:
        return sum(int(getattr(a, attr)) for a in self.adapters.values())


class RecordingNotifier:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def send(self, event: Any) -> None:
        self.events.append(event)

    def kinds(self) -> list[str]:
        return [str(e.kind) for e in self.events]
