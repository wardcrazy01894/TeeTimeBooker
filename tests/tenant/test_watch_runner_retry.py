"""The tenant watcher's transient-failure retries (retry audit, 2026-09-27).

Before: one Cosmos blip on any read ended the run as a systemic failure (the next chance ten
minutes later), a blip on the outcome write REFUSED a booking the watcher had just made (Cosmos
reports it as an ``ExceptionGroup``) so it was never ledgered as owned, and one 5xx on a group's
shared search silently dropped every row of that group for the run.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx
from azure.core.exceptions import ServiceResponseError
from azure.cosmos.exceptions import CosmosHttpResponseError

from teetime.core.adapter import AdapterError
from teetime.core.clock import FakeClock
from teetime.core.models import BookingRequest, TeeTimeSlot
from teetime.tenant.in_memory_store import InMemoryTenantStore
from teetime.tenant.models import RowStatus, TransitionRefusedError
from teetime.tenant.runner import ExitStatus, WatchReport
from teetime.tenant.watch_runner import run_tenant_watch, watch_exit_status

from .watch_runner_builders import (
    CUTOFF,
    KEYRING,
    POLICIES,
    POLICY_ON,
    WATCH_NOW,
    FakeFactory,
    RecordingNotifier,
    WatchFake,
    new_store,
    seed,
    stored_row,
    watch_scheduler,
)


class FlakyStore:
    """Delegates to ``inner``; ``method`` raises ``error`` on its first ``fails`` calls."""

    def __init__(self, inner: InMemoryTenantStore, method: str, *, fails: int, error: Any) -> None:
        self._inner = inner
        self._method = method
        self._left = fails
        self._error = error
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        target = getattr(self._inner, name)

        async def call(*args: Any, **kwargs: Any) -> Any:
            self.calls.append(name)
            if name == self._method and self._left > 0:
                self._left -= 1
                raise self._error()
            return await target(*args, **kwargs)

        return call


class FlakySearchFake(WatchFake):
    """The first ``fails`` searches raise ``error`` (what the ForeUP adapter lets out after its
    own transport retries: a 5xx ``HTTPStatusError`` or a persistent ``TransportError``)."""

    def __init__(self, *, fails: int, error: Any) -> None:
        super().__init__()
        self._left = fails
        self._error = error

    async def search(
        self, request: BookingRequest, *, skip_initial_spacing: bool = False
    ) -> list[TeeTimeSlot]:
        if self._left > 0:
            self._left -= 1
            self.search_call_count += 1
            raise self._error()
        return await super().search(request, skip_initial_spacing=skip_initial_spacing)


def _http_503() -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://foreupsoftware.com/api/booking/times")
    return httpx.HTTPStatusError(
        "503", request=request, response=httpx.Response(503, request=request)
    )


def _blip() -> CosmosHttpResponseError:
    return CosmosHttpResponseError(status_code=503, message="service unavailable")


async def _watch(store: Any, factory: FakeFactory, *, now: datetime = WATCH_NOW) -> WatchReport:
    return await run_tenant_watch(
        policies=POLICIES,
        store=store,
        clock=FakeClock(start=now),
        scheduler=watch_scheduler(),
        booking_policy=POLICY_ON,
        cutoff=CUTOFF,
        keyring=KEYRING,
        adapter_factory=factory,
        notifier=RecordingNotifier(),
        dry_run=False,
    )


async def test_watch_rows_read_survives_a_transient_store_error() -> None:
    inner = new_store()
    s = await seed(inner, n=1)
    store = FlakyStore(inner, "load_watch_rows", fails=1, error=_blip)

    report = await _watch(store, FakeFactory(adapters={s.account.id: WatchFake()}))

    assert report.systemic_error is None
    assert report.booked == (s.row.id,)
    assert store.calls.count("load_watch_rows") == 2


async def test_persistent_read_blips_are_still_systemic() -> None:
    inner = new_store()
    await seed(inner, n=1)
    store = FlakyStore(inner, "load_watch_rows", fails=99, error=_blip)

    report = await _watch(store, FakeFactory())

    assert report.systemic_error == "load_watch_rows: CosmosHttpResponseError"
    assert watch_exit_status(report) is ExitStatus.SYSTEMIC_FAILURE
    assert store.calls.count("load_watch_rows") == 3


async def test_outcome_write_retries_a_transient_exception_group() -> None:
    """The booking the watcher just made is written (row BOOKED, ledgered owned), not REFUSED."""
    inner = new_store()
    s = await seed(inner, n=1)
    store = FlakyStore(
        inner,
        "record_outcomes",
        fails=1,
        error=lambda: ExceptionGroup("record_outcomes: 1 row(s) not applied", [_blip()]),
    )

    report = await _watch(store, FakeFactory(adapters={s.account.id: WatchFake()}))

    assert report.outcome_write_failures == ()
    assert report.booked == (s.row.id,)
    assert (await stored_row(inner, s)).status is RowStatus.BOOKED
    assert watch_exit_status(report) is ExitStatus.OK


async def test_outcome_write_retries_a_transient_plain_error() -> None:
    inner = new_store()
    s = await seed(inner, n=1)
    store = FlakyStore(
        inner, "record_outcomes", fails=1, error=lambda: ServiceResponseError("lost")
    )

    report = await _watch(store, FakeFactory(adapters={s.account.id: WatchFake()}))

    assert report.systemic_error is None
    assert (await stored_row(inner, s)).status is RowStatus.BOOKED


async def test_outcome_write_refusal_is_not_retried() -> None:
    inner = new_store()
    s = await seed(inner, n=1)
    store = FlakyStore(
        inner,
        "record_outcomes",
        fails=1,
        error=lambda: ExceptionGroup("g", [TransitionRefusedError("moved")]),
    )

    report = await _watch(store, FakeFactory(adapters={s.account.id: WatchFake()}))

    assert report.outcome_write_failures == (s.row.id,)
    assert store.calls.count("record_outcomes") == 1


async def test_group_search_retries_once_on_a_transient_failure() -> None:
    inner = new_store()
    s = await seed(inner, n=1)
    fake = FlakySearchFake(fails=1, error=_http_503)

    report = await _watch(inner, FakeFactory(adapters={s.account.id: fake}))

    assert fake.search_call_count == 2
    assert report.booked == (s.row.id,)


async def test_group_search_retries_once_on_a_transport_error() -> None:
    inner = new_store()
    s = await seed(inner, n=1)
    fake = FlakySearchFake(fails=1, error=lambda: httpx.ReadTimeout("slow"))

    report = await _watch(inner, FakeFactory(adapters={s.account.id: fake}))

    assert fake.search_call_count == 2
    assert report.booked == (s.row.id,)


async def test_group_search_retry_is_bounded_to_one() -> None:
    inner = new_store()
    s = await seed(inner, n=1)
    fake = FlakySearchFake(fails=99, error=_http_503)

    report = await _watch(inner, FakeFactory(adapters={s.account.id: fake}))

    assert fake.search_call_count == 2
    assert report.booked == ()
    assert watch_exit_status(report) is ExitStatus.OK


async def test_group_search_non_transient_failure_is_not_retried() -> None:
    inner = new_store()
    s = await seed(inner, n=1)
    fake = FlakySearchFake(fails=1, error=lambda: AdapterError("schema changed"))

    await _watch(inner, FakeFactory(adapters={s.account.id: fake}))

    assert fake.search_call_count == 1
