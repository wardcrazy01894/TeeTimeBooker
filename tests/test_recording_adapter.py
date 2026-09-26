"""MULTIUSER_PLAN §4.6 / MU-9a0: ``tenant.recording`` — the in-memory recording adapter.

``Orchestrator.run`` returns only the kept ``best``: surplus bookings, ``_cancel_extras``
failures and swallowed blind-POST errors never reach the caller. The recorder wraps an
account's adapter, records every ``book()``/``cancel_reservation()`` outcome and every
non-``SlotGoneError`` raise with NO I/O, and passes everything else through unchanged.

Capability fidelity (SF1): on Python >= 3.12 ``runtime_checkable`` ``isinstance`` uses
``inspect.getattr_static``, so a ``__getattr__`` proxy would FAIL
``isinstance(rec, ReservationCacheRefreshable)`` and ``_reguard_before_fallback`` would read a
stale snapshot and could double-book. The factory therefore returns a CONCRETE class per inner
capability set, pinned here for every real adapter and every runtime_checkable Protocol.
"""

from __future__ import annotations

import inspect
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from teetime.core import adapter as adapter_module
from teetime.core.adapter import (
    AuthStateReportable,
    BlindPostCapable,
    CancelError,
    CaptchaError,
    CourseAdapter,
    OtpChallengeError,
    RateLimitError,
    ReservationCacheRefreshable,
    ReservationSnapshotHealth,
    SlotGoneError,
)
from teetime.core.clock import FakeClock
from teetime.core.config import SchedulerConfig
from teetime.core.models import (
    BookingOutcome,
    BookingRequest,
    BookingResult,
    CourseCredentials,
    CourseId,
    ExistingReservation,
    Player,
    RequestId,
    SlotId,
    TeeTimeSlot,
    TimeWindow,
)
from teetime.core.orchestrator import Orchestrator
from teetime.courses.foreup.base import ForeUpAdapter
from teetime.courses.foreup.mangrove_bay import MangroveBayAdapter
from teetime.courses.teeitup.sydney_marovitz import SydneyMarovitzAdapter
from teetime.dev.blind_fake_adapter import BlindFakeAdapter
from teetime.dev.fake_adapter import FakeAdapter
from teetime.dev.virtual_clock import VirtualClock
from teetime.notifications.notifier import NoopNotifier
from teetime.persistence.in_memory_store import InMemoryStore
from teetime.tenant.recording import (
    BlindCapableRecordingAdapter,
    RecordingAdapter,
    make_recording_adapter,
)

CID = CourseId("fake:mb")
TARGET = date(2026, 5, 13)
WINDOW = TimeWindow(earliest=time(7, 0), latest=time(9, 30))  # midpoint 08:15
# T0 = 06:00 ET on 2026-05-06 = 10:00 UTC (EDT); the run targets TARGET (a week out).
T0 = datetime(2026, 5, 6, 10, 0, 0, tzinfo=UTC)
CREDS = CourseCredentials(username="u", password="p")


def _request(*, dry_run: bool = False) -> BookingRequest:
    return BookingRequest(
        request_id=RequestId(uuid4()),
        target_dates=(TARGET,),
        time_windows=(WINDOW,),
        players=(Player(first_name="A", last_name="L", email="a@x.test"),),
        course_preferences=(CID,),
        dry_run=dry_run,
    )


def _slot(hour: int, minute: int) -> TeeTimeSlot:
    return TeeTimeSlot(
        course_id=CID,
        slot_id=SlotId(f"s-{hour:02d}{minute:02d}"),
        tee_time=datetime(2026, 5, 13, hour, minute, tzinfo=UTC),
        holes=18,
        available_spots=4,
        price_per_player=Decimal("45.00"),
        cart_included=True,
    )


def _scheduler() -> SchedulerConfig:
    return SchedulerConfig(
        timezone="America/New_York",
        fire_time=time(6, 0, 0),
        early_arrival_ms=500,
        blind_post_stagger_ms=(-500, -250, 0),
        poll_interval_ms=10,
        max_poll_seconds=1,
        captcha_prefetch_lead_s=30,
        captcha_prefetch_count=3,
        blind_post_max_count=3,
        blind_post_fallback_token_reserve=2,
    )


def _race_clock() -> VirtualClock:
    """A VirtualClock that starts before the CAPTCHA-prefetch point of the 06:00 ET drop."""
    return VirtualClock(start=T0 - timedelta(seconds=_scheduler().captcha_prefetch_lead_s + 2))


def _race_orchestrator(adapter: CourseAdapter, clock: VirtualClock) -> Orchestrator:
    """A full race-path Orchestrator (``prefetch_book=True``) over ONE recorded adapter. The
    recorder must stamp instants from the SAME clock the orchestrator runs on."""
    return Orchestrator(
        adapters={CID: adapter},
        store=InMemoryStore(),
        notifier=NoopNotifier(),
        clock=clock,
        scheduler=_scheduler(),
        creds={CID: CREDS},
        prefetch_book=True,
    )


def _runtime_protocols() -> list[type]:
    """Every ``runtime_checkable`` Protocol defined in ``core/adapter.py`` — discovered, not
    listed, so a new capability Protocol is covered the day it lands."""
    found = [
        obj
        for obj in vars(adapter_module).values()
        if inspect.isclass(obj)
        and obj.__module__ == adapter_module.__name__
        and getattr(obj, "_is_runtime_protocol", False)
    ]
    assert CourseAdapter in found
    assert ReservationCacheRefreshable in found
    return found


def _bare_foreup() -> ForeUpAdapter:
    return ForeUpAdapter(
        course_id=CourseId("foreup:bare"),
        course_pk=1,
        booking_class_id=1,
        schedule_id=1,
        timezone="America/New_York",
    )


class _RefreshableFake(FakeAdapter):
    """FakeAdapter + the two ForeUP-only opt-ins, so their pass-through is testable with no
    network. A collaborator fake, not the SUT."""

    def __init__(self) -> None:
        super().__init__(course_id=CID)
        self.refresh_calls = 0
        self._trusted = False

    async def refresh_reservations(self, creds: CourseCredentials) -> None:
        self.refresh_calls += 1
        self._trusted = True
        await self.authenticate(creds)

    @property
    def snapshot_trusted(self) -> bool:
        return self._trusted


# --- capability fidelity (SF1) --------------------------------------------


@pytest.mark.parametrize(
    "inner",
    [
        FakeAdapter(course_id=CID),
        BlindFakeAdapter(course_id=CID),
        _bare_foreup(),
        MangroveBayAdapter(),
        SydneyMarovitzAdapter(),
        _RefreshableFake(),
    ],
    ids=["fake", "blind-fake", "foreup-bare", "mangrove-bay", "teeitup", "refreshable-fake"],
)
def test_recording_adapter_isinstance_mirrors_inner_for_each_capability(
    inner: CourseAdapter,
) -> None:
    """For EVERY runtime_checkable Protocol in core/adapter.py the recorder must answer
    ``isinstance`` exactly as its inner does — presence-checked via getattr_static, which is
    why a ``__getattr__`` proxy cannot satisfy this."""
    rec = make_recording_adapter(inner, clock=FakeClock(start=T0))
    assert isinstance(rec, RecordingAdapter)
    for proto in _runtime_protocols():
        assert isinstance(rec, proto) == isinstance(inner, proto), proto.__name__
    assert rec.capabilities == inner.capabilities
    assert rec.course_id == inner.course_id
    assert rec.inner is inner
    # Blind members exist on the recorder iff the flag promises them (the orchestrator CASTS,
    # it does not isinstance-check, so presence is the only guard).
    blind = inner.capabilities.blind_post
    assert isinstance(rec, BlindCapableRecordingAdapter) is blind
    for name in ("captcha_pool_size", "synthesize_blind_slots", "set_blind_allowlist"):
        present = inspect.getattr_static(rec, name, None) is not None
        assert present is blind, name


def test_recording_adapter_never_uses_getattr_forwarding() -> None:
    """The whole point of SF1: no variant may rely on ``__getattr__``."""
    for inner in (FakeAdapter(course_id=CID), MangroveBayAdapter(), _RefreshableFake()):
        rec = make_recording_adapter(inner, clock=FakeClock(start=T0))
        assert inspect.getattr_static(type(rec), "__getattr__", None) is None


def test_recording_adapter_factory_reuses_one_class_per_capability_set() -> None:
    a = make_recording_adapter(FakeAdapter(course_id=CID), clock=FakeClock(start=T0))
    b = make_recording_adapter(FakeAdapter(course_id=CID), clock=FakeClock(start=T0))
    c = make_recording_adapter(_RefreshableFake(), clock=FakeClock(start=T0))
    assert type(a) is type(b)
    assert type(a) is not type(c)


def test_recording_adapter_passthrough_signatures_match_protocol() -> None:
    """Every pass-through has the SAME signature as the Protocol member it stands in for —
    names, kinds, defaults, annotations — so a caller typed against the Protocol cannot tell
    the recorder from the inner (and a Protocol signature change fails here, not at T0)."""
    mb = MangroveBayAdapter()
    rec = make_recording_adapter(mb, clock=FakeClock(start=T0))
    checked = 0
    for proto in (CourseAdapter, ReservationCacheRefreshable, BlindPostCapable):
        for name, member in vars(proto).items():
            # Dunders (a Protocol's generated __init__ etc.) are not contract members.
            if name.startswith("__") or not inspect.isfunction(member):
                continue
            ours = inspect.getattr_static(type(rec), name)
            assert inspect.isfunction(ours), name
            assert inspect.signature(ours) == inspect.signature(member), name
            assert inspect.iscoroutinefunction(ours) == inspect.iscoroutinefunction(member), name
            checked += 1
    # The MU-3 allowlist hook is a Mangrove Bay member, not a Protocol one: same rule.
    assert inspect.signature(
        inspect.getattr_static(type(rec), "set_blind_allowlist")
    ) == inspect.signature(MangroveBayAdapter.set_blind_allowlist)
    checked += 1
    for prop in ("is_authenticated", "snapshot_trusted", "blind_allowlist"):
        assert isinstance(inspect.getattr_static(type(rec), prop), property), prop
    assert (
        checked >= 10
    )  # authenticate/search/prepare_book/book/list/cancel/aclose/refresh/2 blind/hook


def test_recording_adapter_refuses_blind_inner_without_allowlist_hook() -> None:
    """``capabilities.blind_post=True`` promises the two BlindPostCapable members; the tenant
    runner ALSO needs the MU-3 allowlist hook on every blind-capable adapter. A blind inner
    that lacks it is refused at wrap time (fail loud at 05:51, not at T0)."""
    inner = FakeAdapter(course_id=CID, supports_blind_post=True)  # no set_blind_allowlist
    with pytest.raises(TypeError, match="set_blind_allowlist"):
        make_recording_adapter(inner, clock=FakeClock(start=T0))


# --- pass-through + recording of the core members ------------------------


async def test_recorder_records_booked_raw_id_stripped_and_returns_result_unchanged() -> None:
    fa = FakeAdapter(course_id=CID)
    clock = FakeClock(start=T0)
    rec = make_recording_adapter(fa, clock=clock)
    req = _request()
    slot = _slot(8, 15)

    result = await rec.book(slot, req)

    assert result.outcome == BookingOutcome.BOOKED
    assert result.confirmation_code == "TTB:FAKE-s-0815"
    log = rec.log()
    assert len(log.books) == 1
    assert log.books[0].raw_id == "FAKE-s-0815"  # TTB: stripped
    assert log.books[0].slot == slot
    assert log.books[0].at == T0
    assert log.book_failures == ()
    assert log.owned_raw_ids() == frozenset({"FAKE-s-0815"})
    assert log.is_owned("TTB:FAKE-s-0815") and log.is_owned("FAKE-s-0815")
    assert not log.is_owned(None)
    assert log.needs_reconcile() is False


async def test_recorder_slot_gone_is_not_recorded_and_reraised() -> None:
    fa = FakeAdapter(course_id=CID)
    rec = make_recording_adapter(fa, clock=FakeClock(start=T0))
    gone = SlotGoneError("claimed", reason="unavailable")
    fa.set_book_to_raise(gone)

    with pytest.raises(SlotGoneError) as info:
        await rec.book(_slot(8, 15), _request())

    assert info.value is gone  # the very same exception object — engine control flow untouched
    log = rec.log()
    assert log.books == () and log.book_failures == ()
    assert log.needs_reconcile() is False


async def test_recorder_records_uncertain_and_reraises() -> None:
    """Named in §12 MU-9a0: a non-SlotGone raise is UNCERTAIN (the POST may have landed) —
    recorded by exception CLASS NAME only (messages can carry PII) and re-raised unchanged."""
    fa = FakeAdapter(course_id=CID)
    rec = make_recording_adapter(fa, clock=FakeClock(start=T0))
    boom = RuntimeError("timeout for user a@x.test")
    fa.set_book_to_raise(boom)  # type: ignore[arg-type]  # any non-AdapterError also counts
    slot = _slot(8, 15)

    with pytest.raises(RuntimeError) as info:
        await rec.book(slot, _request())

    assert info.value is boom
    log = rec.log()
    assert log.books == ()
    assert len(log.book_failures) == 1
    failure = log.book_failures[0]
    assert failure.slot == slot
    assert failure.error == "RuntimeError"
    assert "a@x.test" not in repr(failure)
    assert failure.captcha is False
    assert failure.at == T0
    assert log.needs_reconcile() is True
    assert log.captcha_failures() == ()


@pytest.mark.parametrize("exc", [CaptchaError("challenge"), OtpChallengeError("code needed")])
async def test_recorder_records_captcha_family_as_captcha_failure(exc: CaptchaError) -> None:
    fa = FakeAdapter(course_id=CID)
    rec = make_recording_adapter(fa, clock=FakeClock(start=T0))
    fa.set_book_to_raise(exc)

    with pytest.raises(CaptchaError):
        await rec.book(_slot(8, 15), _request())

    log = rec.log()
    assert len(log.book_failures) == 1
    assert log.book_failures[0].error == type(exc).__name__
    assert log.book_failures[0].captcha is True
    assert log.captcha_failures() == log.book_failures


async def test_recorder_records_cancel_outcomes() -> None:
    """Named in §12 MU-9a0: ok and failed cancels are both recorded (raw id, TTB: stripped;
    failure by class name) and a failure is re-raised unchanged."""
    fa = FakeAdapter(course_id=CID)
    clock = FakeClock(start=T0)
    rec = make_recording_adapter(fa, clock=clock)
    await rec.book(_slot(8, 15), _request())
    await rec.book(_slot(8, 30), _request())

    await rec.cancel_reservation("TTB:FAKE-s-0830")
    refused = CancelError("refused")
    fa.set_cancel_to_raise(refused)
    with pytest.raises(CancelError) as info:
        await rec.cancel_reservation("FAKE-s-0815")
    assert info.value is refused

    log = rec.log()
    assert [(c.raw_id, c.ok, c.error) for c in log.cancels] == [
        ("FAKE-s-0830", True, None),
        ("FAKE-s-0815", False, "CancelError"),
    ]
    assert all(c.at == T0 for c in log.cancels)
    assert log.cancelled_extras() == ("FAKE-s-0830",)
    assert log.held_extras() == ("FAKE-s-0815",)
    # A cancelled-OK id is no longer owned; a failed cancel keeps the id OWNED (held_extra).
    assert log.owned_raw_ids() == frozenset({"FAKE-s-0815"})


async def test_recorder_cancel_retry_after_failure_clears_held_extra() -> None:
    fa = FakeAdapter(course_id=CID)
    rec = make_recording_adapter(fa, clock=FakeClock(start=T0))
    await rec.book(_slot(8, 30), _request())
    fa.set_cancel_to_raise(RateLimitError("429", retry_after_s=1))
    with pytest.raises(RateLimitError):
        await rec.cancel_reservation("TTB:FAKE-s-0830")
    assert rec.log().held_extras() == ("FAKE-s-0830",)

    fa.set_cancel_to_raise(None)  # type: ignore[arg-type]  # clear the scripted failure
    await rec.cancel_reservation("TTB:FAKE-s-0830")
    log = rec.log()
    assert log.held_extras() == ()
    assert log.cancelled_extras() == ("FAKE-s-0830",)
    assert log.owned_raw_ids() == frozenset()


async def test_recorder_counts_authenticate_and_refresh_and_passes_state_through() -> None:
    inner = _RefreshableFake()
    rec = make_recording_adapter(inner, clock=FakeClock(start=T0))
    assert isinstance(rec, ReservationCacheRefreshable)
    assert isinstance(rec, AuthStateReportable)
    assert isinstance(rec, ReservationSnapshotHealth)

    assert rec.is_authenticated is False  # type: ignore[attr-defined]
    assert rec.snapshot_trusted is False  # type: ignore[attr-defined]
    await rec.authenticate(CREDS)
    assert rec.is_authenticated is True  # type: ignore[attr-defined]
    await rec.refresh_reservations(CREDS)  # type: ignore[attr-defined]
    assert rec.snapshot_trusted is True  # type: ignore[attr-defined]

    log = rec.log()
    assert inner.refresh_calls == 1
    assert log.refreshes == 1
    # The recorder counts calls made THROUGH it: the inner's own internal re-login inside
    # refresh_reservations is the inner's business (visible on the inner, not the log).
    assert log.authenticates == 1
    assert inner.authenticate_call_count == 2


async def test_recorder_records_other_op_errors_by_op_and_class_and_reraises() -> None:
    fa = FakeAdapter(course_id=CID)
    rec = make_recording_adapter(fa, clock=FakeClock(start=T0))
    throttled = RateLimitError("slow down", retry_after_s=2)
    fa.set_search_to_raise(throttled)
    with pytest.raises(RateLimitError) as info:
        await rec.search(_request(), skip_initial_spacing=True)
    assert info.value is throttled
    assert fa.last_search_skip_initial_spacing is True  # kwarg passed through

    fa.set_prepare_book_to_raise(CaptchaError("solver down"))
    with pytest.raises(CaptchaError):
        await rec.prepare_book(None, _request(), count=3)
    assert fa.last_prepare_count == 3

    log = rec.log()
    assert [(e.op, e.error) for e in log.errors] == [
        ("search", "RateLimitError"),
        ("prepare_book", "CaptchaError"),
    ]
    assert all(e.at == T0 for e in log.errors)
    assert log.book_failures == ()  # only book() raises are UNCERTAIN


async def test_recorder_passes_search_list_and_aclose_through() -> None:
    fa = FakeAdapter(course_id=CID)
    rec = make_recording_adapter(fa, clock=FakeClock(start=T0))
    slots = [_slot(8, 0)]
    fa.set_search_response(slots)
    assert await rec.search(_request()) == slots
    assert fa.last_search_skip_initial_spacing is False
    existing = [
        ExistingReservation(
            course_id=CID,
            confirmation_code="MANUAL-1",
            tee_time=datetime(2026, 5, 13, 8, 0, tzinfo=UTC),
            party_size=1,
        )
    ]
    fa.set_existing_reservations(existing)
    assert await rec.list_reservations() == existing
    await rec.aclose()
    log = rec.log()
    assert log.errors == () and log.books == ()


def test_recorder_blind_members_pass_through() -> None:
    inner = BlindFakeAdapter(course_id=CID)
    inner.set_blind_slots([_slot(8, 0), _slot(8, 15), _slot(8, 30)])
    inner.set_captcha_pool_size(7)
    rec = make_recording_adapter(inner, clock=FakeClock(start=T0))
    assert isinstance(rec, BlindCapableRecordingAdapter)

    assert rec.captcha_pool_size() == 7
    assert rec.blind_allowlist is None
    rec.set_blind_allowlist(frozenset({SlotId("s-0830")}))
    assert inner.blind_allowlist == frozenset({SlotId("s-0830")})
    assert rec.blind_allowlist == frozenset({SlotId("s-0830")})
    got = rec.synthesize_blind_slots(_request(), TARGET, max_count=3)
    assert [s.slot_id for s in got] == ["s-0830"]
    assert inner.synthesize_blind_slots_call_count == 1
    assert rec.log().errors == ()  # pure pass-through: nothing recorded


# --- through the UNMODIFIED Orchestrator on the race path -----------------


async def test_recording_adapter_blind_capable_end_to_end() -> None:
    """Round-2 SF1's named test: a blind-capable adapter wrapped by the recorder runs a FULL
    race-path ``Orchestrator.run`` (``prefetch_book=True``, VirtualClock) — the orchestrator
    casts the RECORDER to BlindPostCapable and calls synthesize/captcha_pool_size on it — and
    the staggered burst fires with every BOOKED id + cancel-extras outcome recorded. The
    allowlist set on the recorder (MU-3 hook pass-through) is honoured by the burst."""
    inner = BlindFakeAdapter(course_id=CID)
    inner.set_blind_slots([_slot(8, 0), _slot(8, 15), _slot(8, 30)])
    clock = _race_clock()
    rec = make_recording_adapter(inner, clock=clock)
    assert isinstance(rec, BlindCapableRecordingAdapter)
    rec.set_blind_allowlist(frozenset({SlotId("s-0815"), SlotId("s-0830")}))
    orch = _race_orchestrator(rec, clock)
    result = await orch.run(_request())

    assert result.outcome == BookingOutcome.BOOKED
    assert result.slot is not None and result.slot.slot_id == "s-0815"
    # Allowlist honoured through the recorder: s-0800 was never POSTed; rank order kept.
    assert inner.book_slot_ids == ["s-0815", "s-0830"]
    assert inner.search_call_count == 0  # happy path: zero search GETs

    log = rec.log()
    assert [(b.raw_id, b.slot.slot_id) for b in log.books] == [
        ("FAKE-s-0815", "s-0815"),
        ("FAKE-s-0830", "s-0830"),
    ]
    # VirtualClock proof: the two POSTs were SENT at exactly their stagger offsets.
    assert [round((b.at - T0).total_seconds() * 1000) for b in log.books] == [-500, -250]
    assert [(c.raw_id, c.ok) for c in log.cancels] == [("FAKE-s-0830", True)]
    assert log.owned_raw_ids() == frozenset({"FAKE-s-0815"})
    assert log.is_owned(result.confirmation_code)
    assert log.cancelled_extras() == ("FAKE-s-0830",)
    assert log.held_extras() == ()
    assert log.needs_reconcile() is False
    assert log.authenticates == 1  # the pre-T0 pre-warm login only
    # Virtual time stopped at the LAST send instant: nothing in the happy path waits past it.
    assert clock.now_utc() == T0 - timedelta(milliseconds=250)


async def test_recording_adapter_records_swallowed_captcha_error_from_blind_burst() -> None:
    """A Captcha/OTP error on a BLIND POST is dropped by the orchestrator like any non-SlotGone
    error (the documented residual) — a sibling still books and the run returns BOOKED. The
    recorder is the ONLY place it survives, so the tenant runner can exit non-zero (§4.5)."""
    inner = BlindFakeAdapter(course_id=CID)
    inner.set_blind_slots([_slot(8, 0), _slot(8, 15), _slot(8, 30)])
    # Burst fires in rank order: 08:15 (rank 0), 08:00, 08:30.
    inner.set_book_side_effects(
        [BookingOutcome.BOOKED, OtpChallengeError("code required"), BookingOutcome.BOOKED]
    )
    clock = _race_clock()
    rec = make_recording_adapter(inner, clock=clock)
    orch = _race_orchestrator(rec, clock)

    result = await orch.run(_request())

    assert result.outcome == BookingOutcome.BOOKED  # the orchestrator swallowed it
    log = rec.log()
    assert [b.raw_id for b in log.books] == ["FAKE-s-0815", "FAKE-s-0830"]
    assert [(f.slot.slot_id, f.error, f.captcha) for f in log.book_failures] == [
        ("s-0800", "OtpChallengeError", True)
    ]
    assert log.captcha_failures() == log.book_failures
    assert log.needs_reconcile() is True  # an UNCERTAIN POST is on record
    assert [(c.raw_id, c.ok) for c in log.cancels] == [("FAKE-s-0830", True)]


async def test_recording_adapter_records_cancel_failure_as_held_extra() -> None:
    """A surplus whose in-run ``_cancel_extras`` FAILED stays live on the server. The
    orchestrator logs CRITICAL and keeps the best; the recorder turns that into
    ``held_extras()`` — OWNED, so the watcher's owned-only reconcile collapses it (§4.6)."""
    inner = BlindFakeAdapter(course_id=CID)
    inner.set_blind_slots([_slot(8, 15), _slot(8, 30)])
    inner.set_cancel_to_raise(CancelError("server refused"))
    clock = _race_clock()
    rec = make_recording_adapter(inner, clock=clock)
    orch = _race_orchestrator(rec, clock)

    result = await orch.run(_request())

    assert result.outcome == BookingOutcome.BOOKED
    assert result.confirmation_code == "TTB:FAKE-s-0815"
    log = rec.log()
    assert [(c.raw_id, c.ok, c.error) for c in log.cancels] == [
        ("FAKE-s-0830", False, "CancelError")
    ]
    assert log.held_extras() == ("FAKE-s-0830",)
    assert log.owned_raw_ids() == frozenset({"FAKE-s-0815", "FAKE-s-0830"})
    assert log.cancelled_extras() == ()


async def test_recording_adapter_pre_t0_already_booked_is_unowned() -> None:
    """The pre-T0 layer-2 guard finds an existing reservation and short-circuits to
    ALREADY_BOOKED before the burst. No book was recorded, so the id is NOT owned (§4.6: a
    manual booking is never upgradable or cancellable by the bot) and nothing needs reconcile."""
    inner = BlindFakeAdapter(course_id=CID)
    inner.set_existing_reservations(
        [
            ExistingReservation(
                course_id=CID,
                confirmation_code="MANUAL-77",
                tee_time=datetime(2026, 5, 13, 8, 0, tzinfo=UTC),
                party_size=1,
            )
        ]
    )
    clock = _race_clock()
    rec = make_recording_adapter(inner, clock=clock)
    orch = _race_orchestrator(rec, clock)

    result = await orch.run(_request())

    assert result.outcome == BookingOutcome.ALREADY_BOOKED
    assert result.confirmation_code == "MANUAL-77"
    assert inner.book_call_count == 0
    log = rec.log()
    assert log.books == () and log.book_failures == () and log.cancels == ()
    assert log.owned_raw_ids() == frozenset()
    assert log.is_owned(result.confirmation_code) is False
    assert log.needs_reconcile() is False
    assert clock.now_utc() < T0  # short-circuited BEFORE T0


class _LandedUncertainFake(BlindFakeAdapter):
    """Every book() POST lands server-side but raises (timeout after the write) — the §9
    UNCERTAIN case the re-guard exists for."""

    async def book(self, slot: TeeTimeSlot, request: BookingRequest) -> BookingResult:
        self.book_call_count += 1
        self.book_slot_ids.append(slot.slot_id)
        self._existing.append(
            ExistingReservation(
                course_id=self.course_id,
                confirmation_code=f"LANDED-{slot.slot_id}",
                tee_time=slot.tee_time,
                party_size=len(request.players),
            )
        )
        raise TimeoutError("read timed out after the POST")


async def test_recording_adapter_reguard_already_booked_after_uncertain_needs_reconcile() -> None:
    """0 blind booked but the POSTs landed: the re-guard finds them and returns ALREADY_BOOKED.
    The recorder holds the UNCERTAIN slots, so the row is ``needs_reconcile`` and the watcher
    can adopt the reservation OWNED by exact tee-time match (§4.6 ownership table)."""
    inner = _LandedUncertainFake(course_id=CID)
    inner.set_blind_slots([_slot(8, 15)])
    clock = _race_clock()
    rec = make_recording_adapter(inner, clock=clock)
    orch = _race_orchestrator(rec, clock)

    result = await orch.run(_request())

    assert result.outcome == BookingOutcome.ALREADY_BOOKED
    assert result.confirmation_code == "LANDED-s-0815"
    log = rec.log()
    assert log.books == ()
    assert [(f.slot.slot_id, f.error) for f in log.book_failures] == [("s-0815", "TimeoutError")]
    assert log.needs_reconcile() is True
    assert log.owned_raw_ids() == frozenset()  # adoption is the watcher's call, by tee time
    assert log.book_failures[0].slot.tee_time == datetime(2026, 5, 13, 8, 15, tzinfo=UTC)
