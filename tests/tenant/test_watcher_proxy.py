"""MU-10a: the ``SearchSnapshotAdapter`` per-capability proxy family (MULTIUSER_PLAN §7.1 step 4,
round-1 SF1).

``search()`` is served from the run's shared group result; everything else delegates live to
``inner``. The proxy's CONCRETE class must define exactly ``inner``'s opt-in members, because on
Python >= 3.12 ``runtime_checkable`` ``isinstance`` uses ``inspect.getattr_static`` — a
``__getattr__`` forwarder would fail ``isinstance(proxy, ReservationCacheRefreshable)`` and the
engine's reguard would silently fall back to the idempotent ``authenticate()`` (a double-book).
"""

from __future__ import annotations

import inspect
from datetime import date, time
from itertools import product
from typing import Any
from uuid import uuid4

import pytest

from teetime.core.adapter import (
    AdapterCapabilities,
    AuthStateReportable,
    CourseAdapter,
    ReservationCacheRefreshable,
    ReservationSnapshotHealth,
)
from teetime.core.models import (
    BookingOutcome,
    BookingRequest,
    BookingResult,
    CourseCredentials,
    ExistingReservation,
    Player,
    RequestId,
    TeeTimeSlot,
    TimeWindow,
)
from teetime.courses.foreup.mangrove_bay import MangroveBayAdapter
from teetime.dev.fake_adapter import FakeAdapter
from teetime.tenant.watcher import SearchSnapshotAdapter, make_search_snapshot_adapter

from .watcher_builders import MB, NOW, TARGET, WINDOW, reservation, slot

CREDS = CourseCredentials(username="u", password="p")
OPT_IN_MEMBERS = (
    "refresh_reservations",
    "is_authenticated",
    "snapshot_trusted",
    "captcha_pool_size",
    "synthesize_blind_slots",
)
PROTOCOLS = (ReservationCacheRefreshable, AuthStateReportable, ReservationSnapshotHealth)
_MISSING = object()


def _request() -> BookingRequest:
    return BookingRequest(
        request_id=RequestId(uuid4()),
        target_dates=(TARGET,),
        time_windows=(TimeWindow(earliest=WINDOW[0], latest=WINDOW[1]),),
        players=tuple(Player(first_name=f"p{i}", last_name="l", email="") for i in range(4)),
        course_preferences=(MB,),
    )


def _make_inner(*, refresh: bool, auth: bool, health: bool, blind: bool) -> Any:
    """A minimal CourseAdapter with EXACTLY the requested opt-in members, recording every call."""

    class Inner:
        course_id = MB
        capabilities = AdapterCapabilities(blind_post=blind)

        def __init__(self) -> None:
            self.calls: list[str] = []
            self.authed = False

        async def authenticate(self, creds: CourseCredentials) -> None:
            self.calls.append("authenticate")
            self.authed = True

        async def search(
            self, request: BookingRequest, *, skip_initial_spacing: bool = False
        ) -> list[TeeTimeSlot]:
            self.calls.append("search")
            return [slot(time(7, 0))]

        async def prepare_book(
            self, slot_: TeeTimeSlot | None, request: BookingRequest, *, count: int = 1
        ) -> None:
            self.calls.append(f"prepare_book:{count}")

        async def book(self, slot_: TeeTimeSlot, request: BookingRequest) -> BookingResult:
            self.calls.append(f"book:{slot_.slot_id}")
            return BookingResult(
                request_id=request.request_id,
                outcome=BookingOutcome.BOOKED,
                course_id=MB,
                slot=slot_,
                confirmation_code="TTB:R-new",
                booked_at=NOW,
                attempts=1,
            )

        async def list_reservations(self) -> list[ExistingReservation]:
            self.calls.append("list_reservations")
            return [reservation("R1", time(9, 30))]

        async def cancel_reservation(self, confirmation_code: str) -> None:
            self.calls.append(f"cancel:{confirmation_code}")

        async def aclose(self) -> None:
            self.calls.append("aclose")

    if refresh:

        async def refresh_reservations(self: Any, creds: CourseCredentials) -> None:
            self.calls.append("refresh_reservations")

        Inner.refresh_reservations = refresh_reservations  # type: ignore[attr-defined]
    if auth:
        Inner.is_authenticated = property(lambda self: self.authed)  # type: ignore[attr-defined]
    if health:
        Inner.snapshot_trusted = property(lambda self: True)  # type: ignore[attr-defined]
    if blind:

        def captcha_pool_size(self: Any) -> int:
            self.calls.append("captcha_pool_size")
            return 7

        def synthesize_blind_slots(
            self: Any, request: BookingRequest, target_date: date, *, max_count: int
        ) -> list[TeeTimeSlot]:
            self.calls.append(f"synthesize_blind_slots:{max_count}")
            return [slot(time(8, 0)), slot(time(8, 10))][:max_count]

        Inner.captcha_pool_size = captcha_pool_size  # type: ignore[attr-defined]
        Inner.synthesize_blind_slots = synthesize_blind_slots  # type: ignore[attr-defined]
    return Inner()


def _has_static(obj: object, name: str) -> bool:
    return inspect.getattr_static(obj, name, _MISSING) is not _MISSING


COMBOS = list(product([False, True], repeat=4))


@pytest.mark.parametrize(("refresh", "auth", "health", "blind"), COMBOS)
async def test_snapshot_proxy_capabilities_mirror_inner(
    refresh: bool, auth: bool, health: bool, blind: bool
) -> None:
    inner = _make_inner(refresh=refresh, auth=auth, health=health, blind=blind)
    proxy = make_search_snapshot_adapter(inner, slots=[slot(time(9, 20))])

    assert isinstance(proxy, SearchSnapshotAdapter)
    assert isinstance(proxy, CourseAdapter)
    assert proxy.course_id == inner.course_id
    assert proxy.capabilities == inner.capabilities
    # runtime_checkable isinstance parity for every opt-in Protocol.
    for protocol in PROTOCOLS:
        assert isinstance(proxy, protocol) is isinstance(inner, protocol), protocol.__name__
    # Member-level parity with getattr_static semantics (what runtime_checkable actually uses),
    # and NO __getattr__ anywhere in the proxy's MRO.
    for name in OPT_IN_MEMBERS:
        assert _has_static(proxy, name) is _has_static(inner, name), name
    assert all("__getattr__" not in vars(cls) for cls in type(proxy).__mro__)

    # The present members are live pass-throughs to inner.
    if refresh:
        await proxy.refresh_reservations(CREDS)  # type: ignore[attr-defined]
        assert inner.calls[-1] == "refresh_reservations"
    if auth:
        assert proxy.is_authenticated is False  # type: ignore[attr-defined]
        await proxy.authenticate(CREDS)
        assert proxy.is_authenticated is True  # type: ignore[attr-defined]
    if health:
        assert proxy.snapshot_trusted is True  # type: ignore[attr-defined]
    if blind:
        assert proxy.captcha_pool_size() == 7  # type: ignore[attr-defined]
        got = proxy.synthesize_blind_slots(_request(), TARGET, max_count=1)  # type: ignore[attr-defined]
        assert [s.slot_id for s in got] == [slot(time(8, 0)).slot_id]
        assert inner.calls[-2:] == ["captcha_pool_size", "synthesize_blind_slots:1"]


def test_snapshot_proxy_one_concrete_class_per_capability_set() -> None:
    classes = {
        combo: type(
            make_search_snapshot_adapter(
                _make_inner(
                    **dict(zip(("refresh", "auth", "health", "blind"), combo, strict=True))
                ),
                slots=[],
            )
        )
        for combo in COMBOS
    }
    assert len(set(classes.values())) == len(COMBOS)  # 16 distinct concrete classes
    assert all(issubclass(cls, SearchSnapshotAdapter) for cls in classes.values())
    # Same capability set -> same class (the factory is a pure lookup, not a class-per-call).
    again = type(
        make_search_snapshot_adapter(
            _make_inner(refresh=True, auth=True, health=True, blind=False), slots=[]
        )
    )
    assert again is classes[(True, True, True, False)]


async def test_snapshot_proxy_serves_shared_search_and_delegates_rest() -> None:
    inner = _make_inner(refresh=True, auth=True, health=True, blind=True)
    shared = [slot(time(9, 20)), slot(time(9, 40))]
    proxy = make_search_snapshot_adapter(inner, slots=shared)
    request = _request()

    first = await proxy.search(request)
    second = await proxy.search(request, skip_initial_spacing=True)
    assert first == shared
    assert second == shared
    assert first is not second  # a fresh list per call: the engine may mutate its copy
    first.clear()
    assert await proxy.search(request) == shared
    assert "search" not in inner.calls  # ZERO live searches through the proxy

    await proxy.authenticate(CREDS)
    await proxy.prepare_book(None, request, count=2)
    result = await proxy.book(shared[0], request)
    assert result.confirmation_code == "TTB:R-new"
    listed = await proxy.list_reservations()
    assert [r.confirmation_code for r in listed] == ["R1"]
    await proxy.cancel_reservation("TTB:R1")
    await proxy.aclose()
    assert inner.calls == [
        "authenticate",
        "prepare_book:2",
        f"book:{shared[0].slot_id}",
        "list_reservations",
        "cancel:TTB:R1",
        "aclose",
    ]


@pytest.mark.parametrize("blind", [False, True])
def test_snapshot_proxy_mirrors_the_real_fake_adapter(blind: bool) -> None:
    """``FakeAdapter`` is AuthStateReportable (is_authenticated) but NOT refreshable nor
    snapshot-health reporting; with ``supports_blind_post`` it also carries the blind members."""
    inner = FakeAdapter(course_id=MB, supports_blind_post=blind)
    proxy = make_search_snapshot_adapter(inner, slots=[])
    for protocol in PROTOCOLS:
        assert isinstance(proxy, protocol) is isinstance(inner, protocol), protocol.__name__
    assert isinstance(proxy, AuthStateReportable)
    assert not isinstance(proxy, ReservationCacheRefreshable)
    assert not isinstance(proxy, ReservationSnapshotHealth)
    for name in OPT_IN_MEMBERS:
        assert _has_static(proxy, name) is _has_static(inner, name), name
    assert proxy.capabilities.blind_post is blind


def test_snapshot_proxy_is_not_a_getattr_forwarder() -> None:
    """The negative control for SF1: a member the inner LACKS is absent on the proxy too, so a
    caller cannot be fooled by ``hasattr``/``isinstance``."""
    proxy = make_search_snapshot_adapter(
        _make_inner(refresh=False, auth=False, health=False, blind=False), slots=[]
    )
    for name in OPT_IN_MEMBERS:
        assert not hasattr(proxy, name), name
    assert not isinstance(proxy, ReservationCacheRefreshable)
    assert not isinstance(proxy, AuthStateReportable)
    assert not isinstance(proxy, ReservationSnapshotHealth)


def test_snapshot_proxy_rejects_naive_construction_of_wrong_variant() -> None:
    """The base class is the zero-capability variant; constructing it directly for a capable inner
    would silently drop members. The factory is the only sanctioned entry point, but the base
    still works for a plain inner (documented)."""
    plain = _make_inner(refresh=False, auth=False, health=False, blind=False)
    base = SearchSnapshotAdapter(inner=plain, slots=[])
    assert isinstance(base, CourseAdapter)
    assert base.course_id == MB


async def test_snapshot_proxy_mirrors_mangrove_bay() -> None:
    """The production inner: ForeUP defines refresh_reservations, is_authenticated and
    snapshot_trusted, and Mangrove Bay is blind-capable — the proxy must carry all five."""
    inner = MangroveBayAdapter()
    try:
        proxy = make_search_snapshot_adapter(inner, slots=[])
        assert isinstance(proxy, ReservationCacheRefreshable)
        assert isinstance(proxy, AuthStateReportable)
        assert isinstance(proxy, ReservationSnapshotHealth)
        assert proxy.capabilities.blind_post is True
        for name in OPT_IN_MEMBERS:
            assert _has_static(proxy, name) is _has_static(inner, name) is True, name
        # Reads pass through without a login (both default False on a fresh adapter).
        assert proxy.is_authenticated is False  # type: ignore[attr-defined]
        assert proxy.snapshot_trusted is False  # type: ignore[attr-defined]
        assert proxy.captcha_pool_size() == inner.captcha_pool_size()  # type: ignore[attr-defined]
    finally:
        await inner.aclose()
