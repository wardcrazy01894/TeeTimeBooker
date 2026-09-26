"""MULTIUSER_PLAN MU-10b: ``run_tenant_watch`` end to end over ``InMemoryTenantStore`` + fake
adapters (§7.1 run loop, §7.5 snapshot trust + vanish, §7.6 ownership, §7.8 dry-run, §7.9 exit).

Every engine call goes through the UNMODIFIED ``WatchOrchestrator``; these tests observe it only
through the store (rows, ledger, snapshots), the fake adapters' counters and the notifier.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from teetime.core.adapter import AdapterError, AuthError, CaptchaError, RateLimitError
from teetime.core.clock import FakeClock
from teetime.tenant.in_memory_store import InMemoryTenantStore
from teetime.tenant.models import (
    Actor,
    BookingSource,
    BookingState,
    RowStatus,
    RuleId,
    StandingRule,
)
from teetime.tenant.notify import UserEventKind
from teetime.tenant.runner import ExitStatus, WatchReport
from teetime.tenant.watch_runner import run_tenant_watch, watch_exit_status, watch_run_index

from .watch_runner_builders import (
    AFTER_CUTOFF,
    BETTER,
    CUTOFF,
    GRID,
    KEYRING,
    MB,
    POLICIES,
    POLICY_ON,
    TARGET,
    WATCH_NOW,
    WINDOW,
    FakeFactory,
    RecordingNotifier,
    WatchFake,
    new_store,
    now_without_cadence,
    seed,
    stored_row,
    watch_scheduler,
)


async def _watch(
    store: Any,
    factory: FakeFactory,
    *,
    now: datetime = WATCH_NOW,
    dry_run: bool = False,
    notifier: RecordingNotifier | None = None,
) -> WatchReport:
    return await run_tenant_watch(
        policies=POLICIES,
        store=store,
        clock=FakeClock(start=now),
        scheduler=watch_scheduler(),
        booking_policy=POLICY_ON,
        cutoff=CUTOFF,
        keyring=KEYRING,
        adapter_factory=factory,
        notifier=notifier or RecordingNotifier(),
        dry_run=dry_run,
    )


def test_watch_run_index_is_the_ten_minute_bucket() -> None:
    assert watch_run_index(WATCH_NOW + timedelta(minutes=9)) == watch_run_index(WATCH_NOW)
    assert watch_run_index(WATCH_NOW + timedelta(minutes=10)) == watch_run_index(WATCH_NOW) + 1


# --- §7.1 step 2/3: one shared search per group, login only on an opportunity -----------------


async def test_watch_no_login_without_opportunity() -> None:
    """A pending row whose window has no bookable slot, off-cadence: ONE search, ZERO logins."""
    store = new_store()
    s = await seed(store, n=1)
    fake = WatchFake(slots=[])  # sold out
    factory = FakeFactory(adapters={s.account.id: fake})

    report = await _watch(store, factory, now=now_without_cadence(s.account))

    assert report.searches == 1
    assert report.logins == 0
    assert fake.authenticate_call_count == 0
    assert fake.list_reservations_call_count == 0
    assert fake.book_call_count == 0
    assert watch_exit_status(report) is ExitStatus.OK


async def test_watch_one_search_per_group() -> None:
    """Two party-2 rows on the same (course, date) share ONE search; a party-4 row is its own
    group (MB hides slots with fewer open spots, §7.2)."""
    store = new_store()
    a = await seed(store, n=1)
    b = await seed(store, n=2)
    c = await seed(store, n=3, party_size=4)
    factory = FakeFactory(
        adapters={x.account.id: WatchFake(slots=[]) for x in (a, b, c)},
    )

    report = await _watch(store, factory, now=now_without_cadence(a.account, b.account, c.account))

    assert report.searches == 2
    assert factory.total("search_call_count") == 2
    assert report.logins == 0


async def test_watch_books_a_pending_row_when_a_slot_opens() -> None:
    """The recovery booking: pending + an in-window slot -> login, the unmodified engine books
    the best slot, the row becomes BOOKED and the booking is ledgered OWNED (source watch)."""
    store = new_store()
    s = await seed(store, n=1)
    fake = WatchFake()
    factory = FakeFactory(adapters={s.account.id: fake})
    notifier = RecordingNotifier()

    report = await _watch(store, factory, notifier=notifier)

    row = await stored_row(store, s)
    assert row.status is RowStatus.BOOKED
    assert row.booked_raw_id == f"FAKE-{BETTER.slot_id}"
    assert row.lease_owner is None  # the watcher released its lease in the outcome write
    ledger = await store.list_owned_bookings(s.account.id, target_date=TARGET)
    assert [(e.raw_reservation_id, e.state, e.source) for e in ledger] == [
        (f"FAKE-{BETTER.slot_id}", BookingState.HELD, BookingSource.WATCH)
    ]
    assert report.logins == 1
    assert report.booked == (s.row.id,)
    assert fake.book_call_count == 1
    assert UserEventKind.BOOKED in [e.kind for e in notifier.events]
    assert watch_exit_status(report) is ExitStatus.OK


# --- §3.5 leases: the watcher never acts under someone else's lease --------------------------


async def test_watch_respects_booker_lease() -> None:
    store = new_store()
    s = await seed(store, n=1)
    until = WATCH_NOW + timedelta(minutes=15)
    assert await store.claim_rows([s.row.id], owner="booker:mb", until=until, now=WATCH_NOW)
    fake = WatchFake()
    factory = FakeFactory(adapters={s.account.id: fake})

    report = await _watch(store, factory)

    row = await stored_row(store, s)
    assert row.status is RowStatus.PENDING
    assert row.lease_owner == "booker:mb"
    assert fake.book_call_count == 0
    assert report.booked == ()


class _SkipOnLeaseStore:
    """Collaborator spy: the user skips the row (and optionally unskips it again) between the
    watcher's read and its lease acquire (M5)."""

    def __init__(
        self, inner: InMemoryTenantStore, *, user_id: Any, now: datetime, unskip: bool = False
    ) -> None:
        self._inner = inner
        self._user_id = user_id
        self._now = now
        self._unskip = unskip

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def acquire_row_lease(self, row_id: Any, **kwargs: Any) -> bool:
        targets = [RowStatus.SKIPPED, RowStatus.PENDING] if self._unskip else [RowStatus.SKIPPED]
        for to in targets:
            await self._inner.transition_row(
                row_id, user_id=self._user_id, to=to, actor=Actor.WEB, reason=None, now=self._now
            )
        return await self._inner.acquire_row_lease(row_id, **kwargs)


async def test_watch_skipped_between_read_and_lock_not_booked() -> None:
    store = new_store()
    s = await seed(store, n=1)
    fake = WatchFake()
    factory = FakeFactory(adapters={s.account.id: fake})
    spy = _SkipOnLeaseStore(store, user_id=s.user.id, now=WATCH_NOW)

    report = await _watch(spy, factory)

    assert (await stored_row(store, s)).status is RowStatus.SKIPPED
    assert fake.book_call_count == 0
    assert report.booked == ()


async def test_watch_row_edited_between_read_and_lock_not_booked() -> None:
    """Still PENDING, but the row CHANGED since the read (skip + unskip bumped its version): the
    fingerprinted lease acquire refuses, so the watcher never acts on what it did not read."""
    store = new_store()
    s = await seed(store, n=1)
    fake = WatchFake()
    factory = FakeFactory(adapters={s.account.id: fake})
    spy = _SkipOnLeaseStore(store, user_id=s.user.id, now=WATCH_NOW, unskip=True)

    report = await _watch(spy, factory)

    row = await stored_row(store, s)
    assert (row.status, row.lease_owner) == (RowStatus.PENDING, None)
    assert fake.book_call_count == 0
    assert report.skipped_leased == (s.row.id,)


# --- finalizer + materializer (§7.1 step 1, §7.7) ---------------------------------------------


async def test_watch_finalizes_lost_once() -> None:
    store = new_store()
    s = await seed(store, n=1)
    factory = FakeFactory(adapters={s.account.id: WatchFake(slots=[])})
    notifier = RecordingNotifier()

    first = await _watch(store, factory, now=AFTER_CUTOFF, notifier=notifier)
    second = await _watch(
        store, factory, now=AFTER_CUTOFF + timedelta(minutes=10), notifier=notifier
    )

    assert (await stored_row(store, s)).status is RowStatus.LOST
    assert first.lost == (s.row.id,)
    assert second.lost == ()
    lost_events = [e for e in notifier.events if e.kind is UserEventKind.LOST]
    assert len(lost_events) == 1
    assert lost_events[0].user_id == s.user.id


async def test_watch_materializes_rules_before_loading_rows() -> None:
    """The tick runs FIRST, so a rule created since the last run is watched (and booked) in the
    same run."""
    store = new_store()
    s = await seed(store, n=1)
    await store.transition_row(
        s.row.id,
        user_id=s.user.id,
        to=RowStatus.WITHDRAWN,
        actor=Actor.WEB,
        reason="user_withdrawn",
        now=WATCH_NOW,
    )
    rule = StandingRule(
        id=RuleId(uuid4()),
        course_account_id=s.account.id,
        weekday=TARGET.weekday(),
        window_earliest=WINDOW[0],
        window_latest=WINDOW[1],
        party_size=2,
        active=True,
        materialized_through=None,
        version=1,
    )
    await store.upsert_rule(rule, user_id=s.user.id)
    factory = FakeFactory(adapters={s.account.id: WatchFake()})

    report = await _watch(store, factory)

    rows = await store.rows_for_account_date(s.account.id, TARGET)
    booked = [r for r in rows if r.status is RowStatus.BOOKED]
    assert len(booked) == 1 and booked[0].rule_id == rule.id
    assert report.booked == (booked[0].id,)


# --- §7.9 exit contract ------------------------------------------------------------------------


async def test_watch_rate_limit_exits_zero() -> None:
    store = new_store()
    s = await seed(store, n=1)
    fake = WatchFake()
    fake.set_search_to_raise(RateLimitError("429", retry_after_s=60))
    factory = FakeFactory(adapters={s.account.id: fake})

    report = await _watch(store, factory)

    assert report.rate_limited is True
    assert report.logins == 0
    assert fake.book_call_count == 0
    assert watch_exit_status(report) is ExitStatus.OK


async def test_watch_rate_limit_mid_account_aborts_the_run_with_exit_zero() -> None:
    store = new_store()
    a = await seed(store, n=1)
    b = await seed(store, n=2)
    first, second = WatchFake(), WatchFake()
    first.set_authenticate_side_effects([RateLimitError("429")])
    second.set_authenticate_side_effects([RateLimitError("429")])
    factory = FakeFactory(adapters={a.account.id: first, b.account.id: second})

    report = await _watch(store, factory)

    assert report.rate_limited is True
    assert report.logins == 1  # the run stopped at the first 429; no second login attempted
    assert first.book_call_count == second.book_call_count == 0
    assert (await stored_row(store, a)).lease_owner is None
    assert (await stored_row(store, b)).lease_owner is None
    assert watch_exit_status(report) is ExitStatus.OK


async def test_watch_captcha_error_exits_nonzero() -> None:
    store = new_store()
    s = await seed(store, n=1)
    fake = WatchFake()
    fake.set_book_to_raise(CaptchaError("solver down"))
    factory = FakeFactory(adapters={s.account.id: fake})

    report = await _watch(store, factory)

    assert report.captcha_error is True
    row = await stored_row(store, s)
    assert row.status is RowStatus.PENDING
    assert row.needs_reconcile is False  # a captcha rejection is not UNCERTAIN: nothing landed
    assert row.lease_owner is None
    assert watch_exit_status(report) is ExitStatus.SYSTEMIC_FAILURE


async def test_watch_uncertain_book_sets_needs_reconcile_and_exits_zero() -> None:
    """§7.9: an UNCERTAIN book is per-account (the next run reconciles it), not systemic."""
    store = new_store()
    s = await seed(store, n=1)
    fake = WatchFake()
    fake.set_book_to_raise(AdapterError("read timeout after POST"))
    factory = FakeFactory(adapters={s.account.id: fake})

    report = await _watch(store, factory)

    row = await stored_row(store, s)
    assert row.status is RowStatus.PENDING
    assert row.needs_reconcile is True
    assert report.uncertain == (s.row.id,)
    assert watch_exit_status(report) is ExitStatus.OK


async def test_watch_auth_error_is_per_account_and_exits_zero() -> None:
    store = new_store()
    a = await seed(store, n=1)
    b = await seed(store, n=2)
    bad, good = WatchFake(), WatchFake()
    bad.set_authenticate_side_effects([AuthError("bad password")])
    factory = FakeFactory(adapters={a.account.id: bad, b.account.id: good})
    notifier = RecordingNotifier()

    report = await _watch(store, factory, notifier=notifier)

    assert report.auth_failed_accounts == (a.account.id,)
    assert (await stored_row(store, b)).status is RowStatus.BOOKED
    assert [e.user_id for e in notifier.events if e.kind is UserEventKind.AUTH_FAILED] == [
        a.user.id
    ]
    assert watch_exit_status(report) is ExitStatus.OK


async def test_watch_decrypt_failure_skips_row_and_exits_nonzero() -> None:
    store = new_store()
    s = await seed(store, n=1, ciphertext="v1:k1:AAAA:BBBB")
    fake = WatchFake()
    factory = FakeFactory(adapters={s.account.id: fake})

    report = await _watch(store, factory)

    assert report.decrypt_failures == (s.account.id,)
    assert fake.authenticate_call_count == 0
    assert watch_exit_status(report) is ExitStatus.SYSTEMIC_FAILURE


async def test_watch_store_failure_is_systemic() -> None:
    class _Broken:
        def __init__(self, inner: InMemoryTenantStore) -> None:
            self._inner = inner

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

        async def load_watch_rows(self, **kwargs: Any) -> Any:
            raise ConnectionError("db down")

    store = new_store()
    await seed(store, n=1)

    report = await _watch(_Broken(store), FakeFactory())

    assert report.systemic_error is not None
    assert watch_exit_status(report) is ExitStatus.SYSTEMIC_FAILURE


def test_grid_constants_sane() -> None:
    assert BETTER in GRID and BETTER.course_id == MB
