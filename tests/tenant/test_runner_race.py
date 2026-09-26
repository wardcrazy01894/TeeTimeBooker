"""MULTIUSER_PLAN MU-9a: ``run_release_event`` end to end over ``InMemoryTenantStore`` +
fake adapters, every test on a ``VirtualClock`` (§4.4 proof 3).

Named tests (§12 MU-9a + the brief): the race-window call budget (proof 1), the unmodified
orchestrator (proof 2), per-account stagger + distinct rank-0 (proof 3), isolation (proof 4),
the recorder-derived ownership rows (proof 5), overflow/over-cap accounts, the claim, the DST
gate, streamed writes and the self-deadline.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from teetime.core.adapter import AdapterError, AuthError, CancelError, OtpChallengeError
from teetime.core.models import BookingOutcome, CourseId, ExistingReservation
from teetime.core.orchestrator import Orchestrator
from teetime.core.redaction import redact_text
from teetime.courses.foreup.token_pool import SharedCaptchaPool
from teetime.dev.virtual_clock import VirtualClock
from teetime.tenant import runner as runner_module
from teetime.tenant.allocation import draft_order
from teetime.tenant.crypto import Keyring
from teetime.tenant.in_memory_store import InMemoryTenantStore
from teetime.tenant.models import BookingSource, BookingState, RequestRow, RowStatus
from teetime.tenant.recording import RecordingAdapter
from teetime.tenant.runner import RunReport, run_release_event

from .runner_builders import (
    EVENT,
    GRID,
    KEYRING,
    MB,
    POLICIES,
    T0,
    TARGET,
    CountingProvider,
    FakeAdapterNonBlind,
    LandedButUncertainAdapter,
    NullUserNotifier,
    PooledFactory,
    ScriptedFactory,
    Seeded,
    SlowBookAdapter,
    SpyStore,
    TimedBlindAdapter,
    blind_adapter,
    new_store,
    race_clock,
    scheduler,
    seed_account,
)


async def _run(
    store: Any,
    clock: VirtualClock,
    factory: Any,
    *,
    lead_s: int = 30,
    pool_factory: Any = None,
    wait: bool = True,
    replica_timeout_s: float = 1200.0,
    keyring: Keyring = KEYRING,
) -> RunReport:
    return await run_release_event(
        event=EVENT,
        policies=POLICIES,
        store=store,
        clock=clock,
        scheduler=scheduler(lead_s=lead_s),
        keyring=keyring,
        adapter_factory=factory,
        notifier=NullUserNotifier(),
        dry_run=False,
        wait=wait,
        pool_factory=pool_factory,
        replica_timeout_s=replica_timeout_s,
    )


async def _row(store: InMemoryTenantStore, seeded: Seeded) -> RequestRow:
    (row,) = [
        r
        for r in await store.rows_for_account_date(seeded.account.id, TARGET)
        if r.id == seeded.row.id
    ]
    return row


def _outcome(report: RunReport, seeded: Seeded) -> Any:
    (out,) = [o for o in report.outcomes if o.row_id == seeded.row.id]
    return out


def _ordered(a: Seeded, b: Seeded) -> tuple[Seeded, Seeded]:
    """(first, second) in the runner's draft order for TARGET."""
    order = draft_order([a.row.id, b.row.id], target_date=TARGET)
    return (a, b) if order[0] == a.row.id else (b, a)


# --- the happy path + streamed writes -------------------------------------------------------


async def test_runner_books_one_account_and_writes_its_row() -> None:
    store = new_store()
    a = await seed_account(store, n=1)
    clock = race_clock()
    factory = ScriptedFactory()

    report = await _run(store, clock, factory)

    assert (report.rows_loaded, report.rows_claimed, report.systemic_error) == (1, 1, None)
    out = _outcome(report, a)
    assert out.outcome is BookingOutcome.BOOKED
    assert (out.error, out.uncertain, out.search_only) == (None, False, False)
    row = await _row(store, a)
    assert row.status is RowStatus.BOOKED
    assert row.booked_raw_id == "FAKE-s-0815"  # rank-0 of the 08:15-midpoint window
    assert (row.lease_owner, row.needs_reconcile) == (None, False)
    ledger = await store.list_owned_bookings(a.account.id, target_date=TARGET)
    by_raw = {e.raw_reservation_id: e for e in ledger}
    assert by_raw["FAKE-s-0815"].state is BookingState.HELD
    assert by_raw["FAKE-s-0815"].source is BookingSource.BLIND
    # The burst's two surplus bookings were cancelled in-run: ledgered cancelled_extra.
    assert {k for k, e in by_raw.items() if e.state is BookingState.CANCELLED_EXTRA} == {
        "FAKE-s-0800",
        "FAKE-s-0830",
    }
    # The factory saw the account, no pool (none configured) and no lease key.
    (call,) = factory.calls
    assert (call.account.id, call.pool, call.lease_key, call.dry_run) == (
        a.account.id,
        None,
        None,
        False,
    )


async def test_runner_streams_outcomes_per_row_after_t0_plus_10() -> None:
    """WRITE #2 is ONE ``record_outcomes`` call PER ROW (never a batch, M4), and none before
    T0 + post_burst_quiet_s (10 s), even though both accounts finished right after T0."""
    inner = new_store()
    a = await seed_account(inner, n=1)
    b = await seed_account(inner, n=2)
    clock = race_clock()
    spy = SpyStore(inner, clock)

    await _run(spy, clock, ScriptedFactory())

    writes = [(n, at) for n, at in spy.calls if n == "record_outcomes"]
    assert len(writes) == 2
    assert all(at >= T0 + timedelta(seconds=10) for _, at in writes)
    assert (await _row(inner, a)).status is RowStatus.BOOKED
    assert (await _row(inner, b)).status is RowStatus.BOOKED


async def test_runner_streams_outcomes_after_quiet_window() -> None:
    """Streamed, not all-at-the-end (SF5): a fast account's row is written at T0 + 10 s while a
    slow sibling is still running; the slow one is written when IT returns."""
    inner = new_store()
    fast = await seed_account(inner, n=1)
    slow = await seed_account(inner, n=2)
    clock = race_clock()
    spy = SpyStore(inner, clock)
    factory = ScriptedFactory(adapters={slow.account.id: SlowBookAdapter(clock, delay_s=40.0)})

    await _run(spy, clock, factory)

    writes = [at for n, at in spy.calls if n == "record_outcomes"]
    assert len(writes) == 2
    assert writes[0] == T0 + timedelta(seconds=10)  # the fast row, at the quiet-window edge
    assert writes[1] >= T0 + timedelta(seconds=39)  # the slow row, only once it returned
    assert (await _row(inner, fast)).status is RowStatus.BOOKED
    assert (await _row(inner, slow)).status is RowStatus.BOOKED


# --- §4.4 proof 1: the race window --------------------------------------------------------


async def test_runner_no_store_calls_inside_race_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """NO TenantStore call and NO decrypt in [T0 - lead - 1 s, T0 + 10 s): the claim + decrypt
    happen at ~05:51, the writes from T0 + 10 s. Spies stamp every call with the shared clock."""
    lead_s = 120
    inner = new_store()
    await seed_account(inner, n=1)
    await seed_account(inner, n=2)
    clock = race_clock(before_t0_s=9 * 60)  # the 05:50 cron + container start
    spy = SpyStore(inner, clock)
    decrypts: list[datetime] = []
    real_decrypt = runner_module.decrypt_password

    def decrypt_spy(keyring: Keyring, blob: str, *, aad: bytes) -> str:
        decrypts.append(clock.now_utc())
        return real_decrypt(keyring, blob, aad=aad)

    monkeypatch.setattr(runner_module, "decrypt_password", decrypt_spy)

    report = await _run(spy, clock, ScriptedFactory(), lead_s=lead_s)

    lo = T0 - timedelta(seconds=lead_s + 1)
    hi = T0 + timedelta(seconds=10)
    assert spy.names()[:2] == ["load_event_rows", "claim_rows"]
    assert [n for n, at in spy.calls if lo <= at < hi] == []
    assert len(decrypts) == 2
    assert all(not (lo <= at < hi) for at in decrypts)
    assert all(at < lo for at in decrypts)  # decrypt is pre-T0 work
    assert sum(o.outcome is BookingOutcome.BOOKED for o in report.outcomes) == 2


async def test_runner_registers_decrypted_passwords_as_secret_literals() -> None:
    store = new_store()
    a = await seed_account(store, n=1)
    b = await seed_account(store, n=2)
    assert a.password in redact_text(a.password)

    await _run(store, race_clock(), ScriptedFactory())

    assert a.password not in redact_text(f"login failed for pw={a.password}")
    assert b.password not in redact_text(f"login failed for pw={b.password}")


# --- §4.4 proof 2: the same coroutine path ---------------------------------------------------


async def test_runner_uses_unmodified_orchestrator_per_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runner constructs ``core.orchestrator.Orchestrator`` itself (not a subclass), one per
    account, with ``prefetch_book=True``, the event's fire time/zone, and the account's adapter
    wrapped in a recording decorator."""
    store = new_store()
    a = await seed_account(store, n=1)
    b = await seed_account(store, n=2)
    built: list[tuple[Orchestrator, dict[str, Any]]] = []

    def constructing(*args: Any, **kwargs: Any) -> Orchestrator:
        assert not args  # keyword-only construction
        orch = Orchestrator(**kwargs)
        built.append((orch, kwargs))
        return orch

    monkeypatch.setattr(runner_module, "Orchestrator", constructing)
    factory = ScriptedFactory()

    await _run(store, race_clock(), factory)

    assert len(built) == 2
    assert all(type(orch) is Orchestrator for orch, _ in built)
    inners = set()
    for _, kwargs in built:
        assert kwargs["prefetch_book"] is True
        sched = kwargs["scheduler"]
        assert (sched.timezone, sched.fire_time) == (EVENT.timezone, EVENT.release_time)
        (adapter,) = kwargs["adapters"].values()
        assert isinstance(adapter, RecordingAdapter)
        inners.add(id(adapter.inner))
    assert inners == {id(factory.adapters[a.account.id]), id(factory.adapters[b.account.id])}


# --- §4.4 proof 3: per-account stagger + distinct rank-0 ------------------------------------


async def test_runner_two_accounts_each_get_stagger_and_rank0_first() -> None:
    """Two accounts wanting the SAME window: each fires its OWN burst at exactly -500/-250/0 ms
    (VirtualClock), its rank-0 first, over DISJOINT allowlisted slots from the snake draft —
    the first drafter holds the 08:15 midpoint slot, the second the next best (08:00)."""
    store = new_store()
    a = await seed_account(store, n=1)
    b = await seed_account(store, n=2)
    first, second = _ordered(a, b)
    clock = race_clock()
    adapters = {
        first.account.id: TimedBlindAdapter(clock),
        second.account.id: TimedBlindAdapter(clock),
    }

    await _run(store, clock, ScriptedFactory(adapters=dict(adapters)))

    one, two = adapters[first.account.id], adapters[second.account.id]
    assert one.send_offsets_ms() == [-500, -250, 0]
    assert two.send_offsets_ms() == [-500, -250, 0]
    # Snake draft over (first, second): round 0 -> 08:15, 08:00; round 1 (reversed) -> 08:30,
    # 07:45; round 2 -> 08:45, 07:30. Each burst POSTs its own picks in rank order.
    assert one.book_slot_ids == ["s-0815", "s-0745", "s-0845"]
    assert two.book_slot_ids == ["s-0800", "s-0830", "s-0730"]
    assert not set(one.book_slot_ids) & set(two.book_slot_ids)
    assert (await _row(store, first)).booked_raw_id == "FAKE-s-0815"
    assert (await _row(store, second)).booked_raw_id == "FAKE-s-0800"


async def test_runner_computes_candidates_with_allowlist_cleared() -> None:
    """MU-3 review follow-up: the allocator's input is each account's UNFILTERED ranked list, so
    a stale allowlist left on an adapter can never shrink its candidates."""
    store = new_store()
    a = await seed_account(store, n=1)
    clock = race_clock()
    adapter = TimedBlindAdapter(clock)
    adapter.set_blind_allowlist(frozenset())  # stale "search-only" state from elsewhere

    await _run(store, clock, ScriptedFactory(adapters={a.account.id: adapter}))

    assert adapter.book_slot_ids == ["s-0815", "s-0800", "s-0830"]
    assert adapter.blind_allowlist == frozenset({"s-0815", "s-0800", "s-0830"})


# --- overflow / over-cap (§5.3) ---------------------------------------------------------------


async def test_runner_overflow_account_runs_search_path() -> None:
    """With C = 3 and burst 3 only ONE account per course gets a blind burst; the other runs the
    search race path (blind_post_max_count=0: zero blind POSTs, a search at T0)."""
    store = new_store()
    a = await seed_account(store, n=1)
    b = await seed_account(store, n=2)
    first, second = _ordered(a, b)
    clock = race_clock()
    provider = CountingProvider(clock)
    factory = PooledFactory(clock)

    report = await _run(
        store,
        clock,
        factory,
        pool_factory=lambda course: SharedCaptchaPool(
            provider=provider, clock=clock, max_concurrent_solves=3, course_id=course
        ),
    )

    blind, overflow = factory.built[first.account.id], factory.built[second.account.id]
    assert blind.search_call_count == 0 and blind.send_offsets_ms() == [-500, -250, 0]
    assert overflow.synthesize_blind_slots_call_count <= 1  # allocation input only
    assert overflow.search_call_count >= 1
    assert overflow.book_slot_ids == [f"fake-slot-{TARGET.isoformat()}"]  # the searched slot
    assert _outcome(report, first).search_only is False
    assert _outcome(report, second).search_only is True
    assert _outcome(report, second).outcome is BookingOutcome.BOOKED


async def test_runner_overcap_account_registered_k0_solves_nothing() -> None:
    """The over-cap account IS registered with the coordinated pool at k = 0 (§5.3), so its
    ``prepare_book(count=3)`` joins the ONE fill and receives nothing: total solves = 3 (the
    blind account's lease) + 2 (shared reserve) = 5, never 3 more outside the C bound."""
    store = new_store()
    a = await seed_account(store, n=1)
    b = await seed_account(store, n=2)
    first, second = _ordered(a, b)
    clock = race_clock()
    provider = CountingProvider(clock)
    pools: list[SharedCaptchaPool] = []

    def pool_factory(course: CourseId) -> SharedCaptchaPool:
        pool = SharedCaptchaPool(
            provider=provider, clock=clock, max_concurrent_solves=3, course_id=course
        )
        pools.append(pool)
        return pool

    factory = PooledFactory(clock)
    await _run(store, clock, factory, pool_factory=pool_factory)

    (pool,) = pools
    report = pool.report()
    assert report is not None
    blind_key = factory.built[first.account.id].key
    over_key = factory.built[second.account.id].key
    assert report.granted == {blind_key: 3}
    assert over_key not in report.granted
    assert report.demanded == 5
    over = factory.built[second.account.id]
    assert over.prepare_book_call_count == 1
    assert over.prefetch_errors == []  # joined the coordinated fill (an unregistered key raises)
    # 3 lease + 2 reserve, and the over-cap account's fallback book used a reserve token.
    assert provider.calls == 5
    assert over.tokens_used and all(t.startswith("tok-") for t in over.tokens_used)


# --- §4.4 proof 4: isolation ------------------------------------------------------------------


async def test_runner_one_account_auth_error_does_not_affect_other() -> None:
    store = new_store()
    bad = await seed_account(store, n=1)
    good = await seed_account(store, n=2)
    clock = race_clock()
    failing = blind_adapter()
    failing.set_authenticate_side_effects([AuthError("bad password"), AuthError("bad password")])

    report = await _run(store, clock, ScriptedFactory(adapters={bad.account.id: failing}))

    out = _outcome(report, bad)
    assert (out.outcome, out.error, out.uncertain) == (None, "AuthError", False)
    assert _outcome(report, good).outcome is BookingOutcome.BOOKED
    bad_row = await _row(store, bad)
    assert bad_row.status is RowStatus.PENDING
    assert bad_row.last_outcome == "error:AuthError"
    assert (bad_row.lease_owner, bad_row.needs_reconcile) == (None, False)
    assert (await _row(store, good)).status is RowStatus.BOOKED
    assert failing.book_call_count == 0


async def test_runner_one_account_uncertain_does_not_cancel_siblings() -> None:
    """An account whose every POST is UNCERTAIN (raises out of ``orch.run``) is isolated by
    ``gather(return_exceptions=True)``: the sibling books, cancels only ITS OWN extras, and the
    uncertain row stays PENDING with ``needs_reconcile``."""
    store = new_store()
    unsure = await seed_account(store, n=1)
    sibling = await seed_account(store, n=2)
    clock = race_clock()
    flaky = blind_adapter()
    flaky.set_book_side_effects([AdapterError("timeout")] * 4)
    ok = TimedBlindAdapter(clock)
    factory = ScriptedFactory(adapters={unsure.account.id: flaky, sibling.account.id: ok})

    report = await _run(store, clock, factory)

    out = _outcome(report, unsure)
    assert (out.outcome, out.error, out.uncertain) == (None, "AdapterError", True)
    row = await _row(store, unsure)
    assert (row.status, row.needs_reconcile, row.lease_owner) == (RowStatus.PENDING, True, None)
    assert flaky.cancel_call_count == 0
    assert _outcome(report, sibling).outcome is BookingOutcome.BOOKED
    assert ok.cancel_call_count == 2  # its own two surplus bookings, nothing else
    assert (await _row(store, sibling)).status is RowStatus.BOOKED


# --- §4.4 proof 5: what the recorder saw -> ownership ---------------------------------------


async def test_runner_records_cancel_extras_failure_as_held_extra() -> None:
    store = new_store()
    a = await seed_account(store, n=1)
    clock = race_clock()
    adapter = TimedBlindAdapter(clock)
    adapter.set_cancel_to_raise(CancelError("server refused"))

    report = await _run(store, clock, ScriptedFactory(adapters={a.account.id: adapter}))

    out = _outcome(report, a)
    assert out.outcome is BookingOutcome.BOOKED
    assert set(out.held_extra_raw_ids) == {"FAKE-s-0800", "FAKE-s-0830"}
    states = {
        e.raw_reservation_id: e.state
        for e in await store.list_owned_bookings(a.account.id, target_date=TARGET)
    }
    assert states == {
        "FAKE-s-0815": BookingState.HELD,
        "FAKE-s-0800": BookingState.HELD_EXTRA,
        "FAKE-s-0830": BookingState.HELD_EXTRA,
    }
    assert (await _row(store, a)).booked_raw_id == "FAKE-s-0815"


async def test_runner_uncertain_blind_post_sets_needs_reconcile() -> None:
    """A sibling books but one blind POST raised a non-SlotGone error that the orchestrator
    swallowed: only the recorder saw it, and the row is written BOOKED + needs_reconcile."""
    store = new_store()
    a = await seed_account(store, n=1)
    clock = race_clock()
    adapter = TimedBlindAdapter(clock)
    adapter.set_book_side_effects(
        [BookingOutcome.BOOKED, AdapterError("timeout"), BookingOutcome.BOOKED]
    )

    report = await _run(store, clock, ScriptedFactory(adapters={a.account.id: adapter}))

    out = _outcome(report, a)
    assert (out.outcome, out.uncertain) == (BookingOutcome.BOOKED, True)
    row = await _row(store, a)
    assert (row.status, row.needs_reconcile) == (RowStatus.BOOKED, True)


async def test_runner_prewarm_already_booked_is_unowned() -> None:
    """The pre-T0 guard found an existing reservation (a manual booking): row -> booked, but
    NO ownership ledger entry — the bot never upgrades or cancels what it did not make."""
    store = new_store()
    a = await seed_account(store, n=1)
    clock = race_clock()
    adapter = TimedBlindAdapter(clock)
    adapter.set_existing_reservations(
        [
            ExistingReservation(
                course_id=MB,
                confirmation_code="MANUAL-1",
                tee_time=GRID[3].tee_time,
                party_size=2,
            )
        ]
    )

    report = await _run(store, clock, ScriptedFactory(adapters={a.account.id: adapter}))

    assert _outcome(report, a).outcome is BookingOutcome.ALREADY_BOOKED
    assert adapter.book_call_count == 0
    row = await _row(store, a)
    assert (row.status, row.booked_raw_id, row.needs_reconcile) == (
        RowStatus.BOOKED,
        "MANUAL-1",
        False,
    )
    assert await store.list_owned_bookings(a.account.id, target_date=TARGET) == []


async def test_runner_reguard_already_booked_after_uncertain_is_owned() -> None:
    """Every blind POST was UNCERTAIN but one LANDED; the re-guard finds it -> ALREADY_BOOKED.
    The runner cannot tell it from a manual booking by id alone (ALREADY_BOOKED carries no
    slot), so it writes the row BOOKED + ``needs_reconcile`` with NO unowned-forever claim:
    the watcher then adopts it OWNED by exact tee-time match with the recorded UNCERTAIN
    slots (§4.6)."""
    store = new_store()
    a = await seed_account(store, n=1)
    clock = race_clock()

    report = await _run(
        store, clock, ScriptedFactory(adapters={a.account.id: LandedButUncertainAdapter()})
    )

    out = _outcome(report, a)
    assert (out.outcome, out.uncertain) == (BookingOutcome.ALREADY_BOOKED, True)
    row = await _row(store, a)
    assert (row.status, row.booked_raw_id, row.needs_reconcile) == (
        RowStatus.BOOKED,
        "LANDED-s-0815",
        True,
    )


async def test_runner_flags_swallowed_blind_captcha_error() -> None:
    store = new_store()
    a = await seed_account(store, n=1)
    clock = race_clock()
    adapter = TimedBlindAdapter(clock)
    adapter.set_book_side_effects(
        [BookingOutcome.BOOKED, OtpChallengeError("code required"), BookingOutcome.BOOKED]
    )

    report = await _run(store, clock, ScriptedFactory(adapters={a.account.id: adapter}))

    out = _outcome(report, a)
    assert (out.outcome, out.swallowed_captcha_error) == (BookingOutcome.BOOKED, True)


async def test_runner_refuses_blind_adapter_missing_blind_methods() -> None:
    """Round-2 SF1: a blind-capable adapter without the blind members is refused at ~05:51 —
    systemic error, no orchestrator runs, and the claimed lease is released pre-T0."""
    store = new_store()
    a = await seed_account(store, n=1)
    clock = race_clock()
    broken = FakeAdapterNonBlind()
    broken.capabilities = type(broken.capabilities)(blind_post=True)  # promises blind, no hook
    factory = ScriptedFactory(adapters={a.account.id: broken})

    report = await _run(store, clock, factory)

    assert report.systemic_error is not None and "TypeError" in report.systemic_error
    assert report.outcomes == ()
    assert broken.authenticate_call_count == 0 and broken.book_call_count == 0
    row = await _row(store, a)
    assert (row.status, row.lease_owner) == (RowStatus.PENDING, None)
    assert clock.now_utc() < T0  # refused before the race, not at it


async def test_runner_pool_factory_failure_is_systemic_and_releases_leases() -> None:
    """A pool that cannot be built (e.g. the MU-9b site-key pre-flight failed) is a systemic
    pre-T0 failure: nothing races and the claimed rows are handed back, not held to T0+1200 s."""
    store = new_store()
    a = await seed_account(store, n=1)

    def pool_factory(course: CourseId) -> SharedCaptchaPool:
        raise RuntimeError("site key pre-flight failed")

    report = await _run(store, race_clock(), ScriptedFactory(), pool_factory=pool_factory)

    assert report.systemic_error == "prepare: RuntimeError"
    assert report.outcomes == ()
    assert (await _row(store, a)).lease_owner is None


# --- decrypt failure, claim, DST gate, self-deadline ------------------------------------------


async def test_runner_decrypt_failure_skips_only_that_row() -> None:
    store = new_store()
    bad = await seed_account(store, n=1, ciphertext="v1:k1:AAAA:BBBB")
    good = await seed_account(store, n=2)
    factory = ScriptedFactory()

    report = await _run(store, race_clock(), factory)

    out = _outcome(report, bad)
    assert (out.decrypt_failed, out.outcome) == (True, None)
    assert [c.account.id for c in factory.calls] == [good.account.id]
    bad_row = await _row(store, bad)
    assert (bad_row.status, bad_row.lease_owner, bad_row.last_outcome) == (
        RowStatus.PENDING,
        None,
        "decrypt_failed",
    )
    assert _outcome(report, good).outcome is BookingOutcome.BOOKED


async def test_runner_claim_skips_leased_row() -> None:
    """A row leased by the watcher is retried every 15 s until T0 - 150 s, then skipped with a
    WARNING: it is never booked by this run and its adapter is never built."""
    store = new_store()
    free = await seed_account(store, n=1)
    busy = await seed_account(store, n=2)
    clock = race_clock(before_t0_s=9 * 60)
    assert await store.acquire_row_lease(
        busy.row.id,
        owner="watcher:run-9",
        until=T0 + timedelta(hours=1),
        now=clock.now_utc(),
        expected=None,
    )
    inner_spy = SpyStore(store, clock)
    factory = ScriptedFactory()

    report = await _run(inner_spy, clock, factory)

    assert (report.rows_loaded, report.rows_claimed) == (2, 1)
    assert [c.account.id for c in factory.calls] == [free.account.id]
    claims = [at for n, at in inner_spy.calls if n == "claim_rows"]
    assert len(claims) > 2  # retried
    assert all(at <= T0 - timedelta(seconds=150) for at in claims)
    busy_row = await _row(store, busy)
    assert (busy_row.status, busy_row.lease_owner) == (RowStatus.PENDING, "watcher:run-9")


async def test_runner_claim_retry_picks_up_a_lease_released_before_the_give_up() -> None:
    store = new_store()
    await seed_account(store, n=1)
    late = await seed_account(store, n=2)
    clock = race_clock(before_t0_s=9 * 60)
    assert await store.acquire_row_lease(
        late.row.id,
        owner="watcher:run-9",
        until=T0 - timedelta(minutes=8),  # expires one minute into the runner's wait
        now=clock.now_utc(),
        expected=None,
    )

    report = await _run(store, clock, ScriptedFactory())

    assert report.rows_claimed == 2
    assert (await _row(store, late)).status is RowStatus.BOOKED


async def test_runner_dst_gate_before_db_read() -> None:
    """A wrong-season cron (04:51 ET for a 06:00 release) exits before ANY store call."""
    inner = new_store()
    await seed_account(inner, n=1)
    clock = VirtualClock(start=T0 - timedelta(hours=1, minutes=9))
    spy = SpyStore(inner, clock)
    factory = ScriptedFactory()

    report = await _run(spy, clock, factory)

    assert spy.calls == []
    assert factory.calls == []
    assert (report.rows_loaded, report.outcomes, report.systemic_error) == (0, (), None)


async def test_runner_self_deadline_marks_unfinished_needs_reconcile() -> None:
    """An account still running at the self-deadline (start + replicaTimeout - 90 s) is stopped,
    its row written ``needs_reconcile`` + lease released, and the report flags the deadline;
    a sibling that finished is unaffected (SF5)."""
    store = new_store()
    stuck = await seed_account(store, n=1)
    done = await seed_account(store, n=2)
    clock = race_clock()  # start = T0 - 45 s
    factory = ScriptedFactory(adapters={stuck.account.id: SlowBookAdapter(clock, delay_s=10_000)})

    report = await _run(store, clock, factory, replica_timeout_s=300.0)

    assert report.self_deadline_hit is True
    deadline = T0 - timedelta(seconds=45) + timedelta(seconds=300 - 90)
    assert clock.now_utc() < deadline + timedelta(seconds=5)
    out = _outcome(report, stuck)
    assert (out.outcome, out.uncertain) == (None, True)
    row = await _row(store, stuck)
    assert (row.status, row.needs_reconcile, row.lease_owner) == (RowStatus.PENDING, True, None)
    assert (await _row(store, done)).status is RowStatus.BOOKED


async def test_runner_no_pending_rows_is_a_clean_empty_report() -> None:
    report = await _run(new_store(), race_clock(), ScriptedFactory())
    assert (report.rows_loaded, report.rows_claimed, report.outcomes) == (0, 0, ())
    assert report.systemic_error is None
