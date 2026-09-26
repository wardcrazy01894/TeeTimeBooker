"""MU-14: the managed cancel of a BOOKED row (MULTIUSER_PLAN §8.5, §7.8).

Service-level, against a real ``InMemoryTenantStore`` + ``FakeClock``; the ForeUP adapter is
faked at the ``AdapterFactory`` boundary. Store spies SUBCLASS the in-memory store only to
observe (or fail) a collaborator call; the service under test is never mocked.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import uuid4

import pytest

from teetime.core.adapter import CancelError
from teetime.core.clock import FakeClock
from teetime.core.models import BookingOutcome, BookingResult
from teetime.tenant.in_memory_store import InMemoryTenantStore
from teetime.tenant.models import (
    Actor,
    BookingSource,
    BookingState,
    CourseAccount,
    OwnedBooking,
    OwnedBookingId,
    RequestRow,
    RowStatus,
    RuleId,
    StandingRule,
    UserId,
)
from teetime.tenant.notify import UserEvent, UserEventKind
from teetime.tenant.store import RowOutcome
from teetime.web import services
from teetime.web.services import ActionRefusedError, CancelRefusedError, RefreshCache

from ..tenant.conformance import COURSE_TIMEZONES, CUTOFF
from .account_builders import KEYRING, ProbeAdapter, ProbeFactory, reservation, stored_account
from .conftest import T0

OCT3 = date(2026, 10, 3)
TEE = datetime(2026, 10, 3, 13, 30, tzinfo=UTC)  # 09:30 EDT
RAW = "9001"


class SpyStore(InMemoryTenantStore):
    """Records ``record_outcomes`` batches; optionally fails a post-commit collaborator."""

    def __init__(self, *, fail: frozenset[str] = frozenset()) -> None:
        super().__init__(course_timezones=COURSE_TIMEZONES, cutoff=CUTOFF)
        self.batches: list[list[RowOutcome]] = []
        self.fail = fail

    async def record_outcomes(self, outcomes: Sequence[RowOutcome]) -> None:
        self.batches.append(list(outcomes))
        if "record_outcomes" in self.fail:
            raise ExceptionGroup("record_outcomes: 1 row(s) not applied", [RuntimeError("db")])
        await super().record_outcomes(outcomes)

    async def append_audit(
        self,
        *,
        user_id: UserId | None,
        action: str,
        row_id: Any,
        detail: Mapping[str, object],
        at: datetime,
    ) -> None:
        if "append_audit" in self.fail:
            raise RuntimeError("global partition unavailable")
        await super().append_audit(
            user_id=user_id, action=action, row_id=row_id, detail=detail, at=at
        )

    async def save_snapshot(self, snapshot: Any) -> None:
        if "save_snapshot" in self.fail:
            raise RuntimeError("snapshot write failed")
        await super().save_snapshot(snapshot)


class RecordingNotifier:
    def __init__(self, *, fail: bool = False) -> None:
        self.events: list[UserEvent] = []
        self.fail = fail

    async def send(self, event: UserEvent) -> None:
        if self.fail:
            raise RuntimeError("mail down")
        self.events.append(event)


@pytest.fixture
def store() -> SpyStore:
    return SpyStore()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=T0)


async def _booked(
    store: InMemoryTenantStore, *, owned: bool = True
) -> tuple[CourseAccount, RequestRow]:
    """An account with a rule row on OCT3 BOOKED through the leased runner path (WRITE #2).
    ``owned`` = the ledger holds the reservation (the bot made it)."""
    account = stored_account(UserId(uuid4()))
    await store.upsert_account(account)
    rule = await store.upsert_rule(
        StandingRule(
            id=RuleId(uuid4()),
            course_account_id=account.id,
            weekday=OCT3.weekday(),
            window_earliest=time(8, 0),
            window_latest=time(10, 0),
            party_size=2,
            active=True,
            materialized_through=None,
            version=1,
        ),
        user_id=account.user_id,
    )
    row = await store.insert_rule_row_if_absent(rule, OCT3, now=T0)
    assert row is not None
    owner = "booker:test"
    assert await store.claim_rows([row.id], owner=owner, until=T0 + timedelta(minutes=5), now=T0)
    booking = OwnedBooking(
        id=OwnedBookingId(uuid4()),
        row_id=row.id,
        course_account_id=account.id,
        course_id=row.course_id,
        target_date=OCT3,
        raw_reservation_id=RAW,
        tee_time=TEE,
        party_size=2,
        source=BookingSource.BLIND,
        state=BookingState.HELD,
    )
    await store.record_outcomes(
        [
            RowOutcome(
                row_id=row.id,
                course_account_id=account.id,
                target_date=OCT3,
                actor=Actor.BOOKING_RUNNER,
                to_status=RowStatus.BOOKED,
                last_outcome="booked",
                at=T0,
                result=BookingResult(
                    request_id=row.request_id,
                    outcome=BookingOutcome.BOOKED,
                    course_id=row.course_id,
                    slot=None,
                    confirmation_code=f"TTB:{RAW}",
                    booked_at=T0,
                    attempts=1,
                ),
                booking=booking if owned else None,
                release_lease_owner=owner,
            )
        ]
    )
    booked = await store.get_row(row.id, user_id=account.user_id)
    assert booked is not None and booked.status is RowStatus.BOOKED
    assert booked.booked_raw_id == RAW and booked.lease_owner is None
    if isinstance(store, SpyStore):
        store.batches.clear()
    return account, booked


def _live_adapter(*raw_ids: str, trusted: bool = True) -> ProbeAdapter:
    adapter = ProbeAdapter(trusted=trusted)
    adapter.set_existing_reservations([reservation(r, TEE) for r in raw_ids])
    return adapter


async def _cancel(
    store: InMemoryTenantStore,
    clock: FakeClock,
    factory: ProbeFactory,
    *,
    account: CourseAccount,
    row: RequestRow,
    confirm_unowned: bool = False,
    dry_run: bool = False,
    notifier: RecordingNotifier | None = None,
    cache: RefreshCache | None = None,
    user_id: UserId | None = None,
) -> RequestRow:
    return await services.cancel_row(
        store,
        user_id=account.user_id if user_id is None else user_id,
        row_id=row.id,
        confirm_unowned=confirm_unowned,
        keyring=KEYRING,
        adapter_factory=factory,
        clock=clock,
        dry_run=dry_run,
        cache=cache,
        notifier=notifier,
    )


async def _row(store: InMemoryTenantStore, account: CourseAccount, row: RequestRow) -> RequestRow:
    got = await store.get_row(row.id, user_id=account.user_id)
    assert got is not None
    return got


async def test_cancel_owned_booking(store: SpyStore, clock: FakeClock) -> None:
    account, row = await _booked(store)
    adapter = _live_adapter(RAW, "5555")
    factory = ProbeFactory(adapter=adapter)
    notifier = RecordingNotifier()
    cache = RefreshCache(ttl_s=120)
    got = await _cancel(
        store, clock, factory, account=account, row=row, notifier=notifier, cache=cache
    )

    assert (got.status, got.status_reason) == (RowStatus.CANCELLED, "user")
    assert got.lease_owner is None  # released
    assert adapter.cancel_call_count == 1
    assert [r.confirmation_code for r in await adapter.list_reservations()] == ["5555"]
    assert store.slot_pointer(account.id, OCT3) is None  # the date is free for a re-request
    (entry,) = await store.list_owned_bookings(account.id, target_date=OCT3)
    assert entry.state is BookingState.CANCELLED_USER
    snap = await store.get_snapshot(account.id)
    assert snap is not None and snap.trusted
    assert [e.raw_id for e in snap.entries] == ["5555"]  # the post-cancel list
    assert cache.get(account.id, now=clock.now_utc()) == snap
    assert [e.action for e in store.audit_log] == ["cancel_user"]
    (event,) = notifier.events
    assert event.kind is UserEventKind.CANCELLED
    assert (event.user_id, event.row_id, event.target_date) == (account.user_id, row.id, OCT3)
    assert factory.adapter.closed == 1


async def test_cancel_writes_row_slot_ledger_in_one_batch(
    store: SpyStore, clock: FakeClock
) -> None:
    """§8.5 step 5: the row -> CANCELLED(user), the slot release and the ledger
    ``cancelled_user`` are ONE ``record_outcomes`` outcome (one account-partition batch),
    written under the web's own lease."""
    account, row = await _booked(store)
    await _cancel(store, clock, ProbeFactory(adapter=_live_adapter(RAW)), account=account, row=row)
    (batch,) = store.batches
    (outcome,) = batch
    assert outcome.row_id == row.id
    assert outcome.actor is Actor.WEB
    assert (outcome.to_status, outcome.status_reason) == (RowStatus.CANCELLED, "user")
    assert outcome.booking is not None
    assert outcome.booking.raw_reservation_id == RAW
    assert outcome.booking.state is BookingState.CANCELLED_USER
    assert outcome.release_lease_owner is not None
    assert outcome.release_lease_owner.startswith("web:")


@pytest.mark.parametrize("failing", ["append_audit", "save_snapshot", "notify"])
async def test_cancel_audit_failure_does_not_undo_cancel(
    clock: FakeClock, caplog: pytest.LogCaptureFixture, failing: str
) -> None:
    """The audit doc (``global`` partition), the snapshot and the email come AFTER the batch
    committed: a failure of any is logged at ERROR and never undoes or blocks the cancel."""
    store = SpyStore(fail=frozenset({failing}))
    account, row = await _booked(store)
    notifier = RecordingNotifier(fail=failing == "notify")
    with caplog.at_level(logging.ERROR):
        got = await _cancel(
            store,
            clock,
            ProbeFactory(adapter=_live_adapter(RAW)),
            account=account,
            row=row,
            notifier=notifier,
        )
    assert (got.status, got.status_reason) == (RowStatus.CANCELLED, "user")
    assert any(r.levelno >= logging.ERROR for r in caplog.records)


async def test_cancel_row_write_failure_after_course_cancel_is_reported(
    clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    """The course cancelled but the batch failed: say so (the watcher's vanish inference
    reconciles the row), log CRITICAL, and never claim nothing happened."""
    store = SpyStore()
    account, row = await _booked(store)
    store.fail = frozenset({"record_outcomes"})  # only the CANCEL's batch fails
    adapter = _live_adapter(RAW)
    with caplog.at_level(logging.CRITICAL), pytest.raises(ActionRefusedError, match="cancelled"):
        await _cancel(store, clock, ProbeFactory(adapter=adapter), account=account, row=row)
    assert adapter.cancel_call_count == 1
    assert any(r.levelno == logging.CRITICAL for r in caplog.records)
    assert (await _row(store, account, row)).lease_owner is None  # the lease is still released


@pytest.mark.parametrize("script", ["untrusted", "soft_fail"])
async def test_cancel_requires_trusted_snapshot(
    store: SpyStore, clock: FakeClock, script: str
) -> None:
    """§8.5 step 2: without an established login AND a trusted reservation list, nothing is
    cancelled (an absent id there would be misread as already gone)."""
    account, row = await _booked(store)
    adapter = _live_adapter(trusted=script != "untrusted")
    if script == "soft_fail":
        adapter.set_auth_soft_fail()
    with pytest.raises(CancelRefusedError, match="nothing was cancelled"):
        await _cancel(store, clock, ProbeFactory(adapter=adapter), account=account, row=row)
    assert adapter.cancel_call_count == 0
    after = await _row(store, account, row)
    assert after.status is RowStatus.BOOKED
    assert after.lease_owner is None
    assert store.batches == []
    assert adapter.closed == 1


async def test_cancel_refused_while_booker_lease(store: SpyStore, clock: FakeClock) -> None:
    account, row = await _booked(store)
    assert await store.acquire_row_lease(
        row.id, owner="booker:x", until=T0 + timedelta(minutes=20), now=T0, expected=None
    )
    factory = ProbeFactory(adapter=_live_adapter(RAW))
    with pytest.raises(CancelRefusedError, match=r"(?i)booking in progress"):
        await _cancel(store, clock, factory, account=account, row=row)
    assert factory.calls == []  # the lease is taken BEFORE any ForeUP login (§8.5 step 1)
    assert (await _row(store, account, row)).lease_owner == "booker:x"


async def test_cancel_refused_in_dry_run(store: SpyStore, clock: FakeClock) -> None:
    """§7.8: a dry-run environment never mutates a reservation: refused before any login."""
    account, row = await _booked(store)
    factory = ProbeFactory(adapter=_live_adapter(RAW))
    with pytest.raises(CancelRefusedError, match="dry-run"):
        await _cancel(store, clock, factory, account=account, row=row, dry_run=True)
    assert factory.calls == []
    assert await _row(store, account, row) == row


async def test_dry_run_web_refuses_cancel(store: SpyStore, clock: FakeClock) -> None:
    """The §12 MU-14 name for the §7.8 rule (SF2): even a confirmed unowned cancel is refused."""
    account, row = await _booked(store, owned=False)
    factory = ProbeFactory(adapter=_live_adapter(RAW))
    with pytest.raises(CancelRefusedError, match="dry-run"):
        await _cancel(
            store, clock, factory, account=account, row=row, dry_run=True, confirm_unowned=True
        )
    assert factory.calls == []


async def test_unowned_cancel_requires_confirm(store: SpyStore, clock: FakeClock) -> None:
    """§8.5: a booking the bot did not make is cancelled only behind an explicit confirm."""
    account, row = await _booked(store, owned=False)
    adapter = _live_adapter(RAW)
    factory = ProbeFactory(adapter=adapter)
    with pytest.raises(CancelRefusedError, match="wasn't made by TeeTimeBooker"):
        await _cancel(store, clock, factory, account=account, row=row)
    assert factory.calls == []
    got = await _cancel(store, clock, factory, account=account, row=row, confirm_unowned=True)
    assert (got.status, got.status_reason) == (RowStatus.CANCELLED, "user")
    assert adapter.cancel_call_count == 1
    assert await store.list_owned_bookings(account.id, target_date=OCT3) == []  # never ledgered


async def test_cancel_already_gone_is_success(store: SpyStore, clock: FakeClock) -> None:
    """§8.5 step 3: the id is absent from a TRUSTED live list -> CANCELLED(already_gone), with
    no cancel call; the reservation's ledger entry is marked vanished."""
    account, row = await _booked(store)
    adapter = _live_adapter()  # trusted, and our reservation is not there
    got = await _cancel(store, clock, ProbeFactory(adapter=adapter), account=account, row=row)
    assert (got.status, got.status_reason) == (RowStatus.CANCELLED, "already_gone")
    assert adapter.cancel_call_count == 0
    (entry,) = await store.list_owned_bookings(account.id, target_date=OCT3)
    assert entry.state is BookingState.VANISHED
    assert store.slot_pointer(account.id, OCT3) is None


async def test_cancel_refused_by_course_changes_nothing(store: SpyStore, clock: FakeClock) -> None:
    account, row = await _booked(store)
    adapter = _live_adapter(RAW)
    adapter.set_cancel_to_raise(CancelError("server refused"))
    with pytest.raises(CancelRefusedError, match="unchanged"):
        await _cancel(store, clock, ProbeFactory(adapter=adapter), account=account, row=row)
    after = await _row(store, account, row)
    assert (after.status, after.lease_owner) == (RowStatus.BOOKED, None)
    assert store.batches == []


async def test_cancel_refuses_a_row_that_is_not_booked(store: SpyStore, clock: FakeClock) -> None:
    account, row = await _booked(store)
    await _cancel(store, clock, ProbeFactory(adapter=_live_adapter(RAW)), account=account, row=row)
    cancelled = await _row(store, account, row)
    factory = ProbeFactory()
    with pytest.raises(ActionRefusedError, match="Only a booked"):
        await _cancel(store, clock, factory, account=account, row=cancelled)
    assert factory.calls == []


async def test_cancel_other_users_row_is_not_found(store: SpyStore, clock: FakeClock) -> None:
    account, row = await _booked(store)
    factory = ProbeFactory(adapter=_live_adapter(RAW))
    with pytest.raises(services.WebNotFoundError):
        await _cancel(store, clock, factory, account=account, row=row, user_id=UserId(uuid4()))
    assert factory.calls == []
    assert await _row(store, account, row) == row


async def test_cancel_login_is_bound_by_the_refresh_rate_limit(
    store: SpyStore, clock: FakeClock
) -> None:
    """Review must-fix (#241): a Cancel does a live ForeUP login, so it spends the SAME per-account
    hourly budget as Refresh. Once that budget is used up the cancel is refused BEFORE any login,
    so repeated clicks after a failed cancel cannot hammer the user's real ForeUP account."""
    account, row = await _booked(store)
    for _ in range(services.ProbeLimits().refreshes_per_account_per_hour):
        await store.record_login_probe(
            user_id=account.user_id,
            course_id=account.course_id,
            username_hash=services.refresh_probe_hash(account.id),
            ok=True,
            at=T0,
        )
    factory = ProbeFactory(adapter=_live_adapter(RAW))
    with pytest.raises(services.RateLimitedError):
        await _cancel(store, clock, factory, account=account, row=row)
    assert factory.calls == []
    assert await _row(store, account, row) == row
