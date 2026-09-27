"""The connect / re-verify login probe is NEVER retried (MULTIUSER_PLAN §8.4; retry audit
2026-09-27), not even by the ForeUP adapter's own transport-error retry (``_send_with_retry``,
default 2 retries around the login POST). A timed-out login may have reached ForeUP, and every
extra attempt counts toward the course's login limits and our own probe rate limits, which only
count the ONE recorded probe.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import httpx
import respx

from teetime.core.models import CourseCredentials, CourseId
from teetime.courses.foreup.base import FOREUP_BASE_URL, LOGIN_PATH
from teetime.courses.foreup.mangrove_bay import MangroveBayAdapter
from teetime.courses.foreup.token_pool import LeaseKey, SharedCaptchaPool
from teetime.tenant.models import CourseAccount, UserId
from teetime.web.services import _probe_login

from .account_builders import PASSWORD, stored_account

_LOGIN_URL = f"{FOREUP_BASE_URL}{LOGIN_PATH}"


class _RealAdapterFactory:
    """Hands out a REAL ``MangroveBayAdapter`` (its HTTP is mocked by respx)."""

    def __init__(self) -> None:
        self.built: list[MangroveBayAdapter] = []

    def __call__(
        self,
        *,
        course_id: CourseId,
        account: CourseAccount,
        pool: SharedCaptchaPool | None,
        lease_key: LeaseKey | None,
        dry_run: bool,
    ) -> Any:
        adapter = MangroveBayAdapter()
        self.built.append(adapter)
        return adapter


@respx.mock
async def test_probe_login_post_is_never_retried_on_a_transport_error() -> None:
    respx.get(url__startswith=f"{FOREUP_BASE_URL}/index.php/booking/").mock(
        return_value=httpx.Response(200, text="<html/>")
    )
    login = respx.post(_LOGIN_URL).mock(
        side_effect=[
            httpx.ReadTimeout("slow"),
            httpx.Response(200, json={"success": True, "jwt": "t", "reservations": []}),
        ]
    )
    factory = _RealAdapterFactory()

    ok = await _probe_login(
        account=stored_account(UserId(uuid4())),
        password=PASSWORD,
        adapter_factory=factory,
    )

    assert ok is False
    assert login.call_count == 1


@respx.mock
async def test_booking_adapters_keep_their_transport_retry() -> None:
    """Only the probe opts out: a booker/watcher adapter still retries the login POST."""
    respx.get(url__startswith=f"{FOREUP_BASE_URL}/index.php/booking/").mock(
        return_value=httpx.Response(200, text="<html/>")
    )
    login = respx.post(_LOGIN_URL).mock(
        side_effect=[
            httpx.ReadTimeout("slow"),
            httpx.Response(200, json={"success": True, "jwt": "t", "reservations": []}),
        ]
    )
    adapter = MangroveBayAdapter()
    adapter.set_transport_retries(2, backoff_s=0)  # the defaults, minus the real sleep
    try:
        await adapter.authenticate(CourseCredentials(username="u@example.test", password="p"))
    finally:
        await adapter.aclose()

    assert adapter.is_authenticated
    assert login.call_count == 2
