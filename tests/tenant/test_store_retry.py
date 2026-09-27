"""The tenant store's transient-error retry helper (``tenant/retry.py``).

The azure-cosmos SDK already retries 429 (up to 9x / 30 s), 503, connection-establishment
failures, and — for READS only — 408/5xx and read timeouts. What reaches our code is what the SDK
gave up on (or chose not to retry: every write on a 408/5xx/lost response). ``retry_transient``
is the bounded, clock-driven layer the runner/watcher put on top of IDEMPOTENT calls only.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from azure.core.exceptions import ServiceRequestError, ServiceResponseError
from azure.cosmos.exceptions import (
    CosmosAccessConditionFailedError,
    CosmosClientTimeoutError,
    CosmosHttpResponseError,
    CosmosResourceExistsError,
    CosmosResourceNotFoundError,
)

from teetime.core.clock import FakeClock
from teetime.tenant.models import TransitionRefusedError
from teetime.tenant.retry import RetryPolicy, is_transient_store_error, retry_transient

START = datetime(2026, 10, 3, 9, 51, tzinfo=UTC)
FAST = RetryPolicy(attempts=3, backoff_s=1.0)


@pytest.mark.parametrize("status", [408, 429, 449, 500, 502, 503, 504])
def test_transient_http_statuses(status: int) -> None:
    assert is_transient_store_error(CosmosHttpResponseError(status_code=status, message="x"))


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 412, 413])
def test_non_transient_http_statuses(status: int) -> None:
    assert not is_transient_store_error(CosmosHttpResponseError(status_code=status, message="x"))


def test_sdk_typed_errors() -> None:
    assert not is_transient_store_error(CosmosResourceNotFoundError(status_code=404, message="x"))
    assert not is_transient_store_error(CosmosResourceExistsError(status_code=409, message="x"))
    assert not is_transient_store_error(
        CosmosAccessConditionFailedError(status_code=412, message="x")
    )
    assert is_transient_store_error(ServiceRequestError("connect failed"))
    assert is_transient_store_error(ServiceResponseError("response lost"))
    assert is_transient_store_error(CosmosClientTimeoutError())


def test_domain_and_budget_errors_are_not_transient() -> None:
    """A refusal is a decision, not a blip; our own ``_store_call`` budget ``TimeoutError`` is
    NOT retried either (it already spent the call's whole budget — the bound stays the bound)."""
    assert not is_transient_store_error(TransitionRefusedError("moved"))
    assert not is_transient_store_error(ValueError("bad"))
    assert not is_transient_store_error(TimeoutError("store call exceeded 20.0s"))


def test_exception_group_is_transient_only_when_every_leaf_is() -> None:
    blip = CosmosHttpResponseError(status_code=503, message="x")
    assert is_transient_store_error(ExceptionGroup("g", [blip]))
    assert not is_transient_store_error(ExceptionGroup("g", [blip, TransitionRefusedError("m")]))


async def test_retries_a_transient_failure_then_returns() -> None:
    clock = FakeClock(start=START)
    calls: list[int] = []

    async def call() -> str:
        calls.append(1)
        if len(calls) == 1:
            raise CosmosHttpResponseError(status_code=503, message="busy")
        return "ok"

    assert await retry_transient(call, label="read", clock=clock, policy=FAST) == "ok"
    assert len(calls) == 2
    assert clock.now_utc() == START + timedelta(seconds=1)  # linear backoff, on the clock


async def test_does_not_retry_a_non_transient_failure() -> None:
    clock = FakeClock(start=START)
    calls: list[int] = []

    async def call() -> str:
        calls.append(1)
        raise TransitionRefusedError("moved")

    with pytest.raises(TransitionRefusedError):
        await retry_transient(call, label="read", clock=clock, policy=FAST)
    assert len(calls) == 1


async def test_gives_up_after_the_attempt_budget() -> None:
    clock = FakeClock(start=START)
    calls: list[int] = []

    async def call() -> str:
        calls.append(1)
        raise ServiceResponseError("lost")

    with pytest.raises(ServiceResponseError):
        await retry_transient(call, label="read", clock=clock, policy=FAST)
    assert len(calls) == 3
    assert clock.now_utc() == START + timedelta(seconds=1 + 2)


async def test_never_sleeps_past_not_after() -> None:
    """The race-window guard: a retry whose backoff would end at or past ``not_after`` is not
    attempted — the last error is raised instead."""
    clock = FakeClock(start=START)
    calls: list[int] = []

    async def call() -> str:
        calls.append(1)
        raise ServiceResponseError("lost")

    with pytest.raises(ServiceResponseError):
        await retry_transient(
            call,
            label="read",
            clock=clock,
            policy=FAST,
            not_after=START + timedelta(seconds=2),
        )
    assert len(calls) == 2  # attempt 1, sleep 1 s, attempt 2; a 2 s sleep would reach not_after
    assert clock.now_utc() < START + timedelta(seconds=2)


async def test_honours_the_server_retry_after_hint_capped() -> None:
    clock = FakeClock(start=START)
    calls: list[int] = []

    async def call() -> str:
        calls.append(1)
        if len(calls) == 1:
            throttled = CosmosHttpResponseError(status_code=429, message="throttled")
            throttled.headers = {"x-ms-retry-after-ms": "4000"}  # the SDK reads it off the response
            raise throttled
        return "ok"

    policy = RetryPolicy(attempts=2, backoff_s=1.0, max_delay_s=3.0)
    assert await retry_transient(call, label="read", clock=clock, policy=policy) == "ok"
    assert clock.now_utc() == START + timedelta(seconds=3)  # hint 4 s, capped at 3 s


def test_policy_rejects_nonsense() -> None:
    with pytest.raises(ValueError):
        RetryPolicy(attempts=0)
    with pytest.raises(ValueError):
        RetryPolicy(backoff_s=-1.0)
