"""MULTIUSER_PLAN MU-9b: ``tenant.booking_job`` — the wiring ``teetime tenant-run`` /
``tenant-plan`` run: the hosted release events, the real adapter + pool factories (site-key
pre-flight once per course, the 2captcha provider bound to the coordinated pool), the operator
sink from the environment, per-user routing, and the job-level exit code (§4.5).
"""

from __future__ import annotations

import base64
import json
import os
from datetime import UTC, datetime, time, timedelta
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
import respx

from teetime.core.clock import FakeClock
from teetime.core.config import BookingCutoffConfig
from teetime.courses.foreup.mangrove_bay import MANGROVE_BAY_COURSE_ID, MangroveBayAdapter
from teetime.courses.foreup.token_pool import LeaseKey, SharedCaptchaPool
from teetime.tenant.acs_email import AcsEmailClient
from teetime.tenant.booking_job import (
    OPERATOR_NOTIFY_EMAIL_ENV,
    RELEASE_EVENTS,
    TWOCAPTCHA_API_KEY_ENV,
    HostedAdapterFactory,
    HostedPoolFactory,
    StoreUserNotifier,
    UnconfiguredEmailSender,
    event_for,
    operator_sink_from_env,
    plan_booking_event,
    policies_for,
    run_booking_job,
)
from teetime.tenant.crypto import KEYRING_ENV_VAR, Keyring, credential_aad, encrypt_password
from teetime.tenant.in_memory_store import InMemoryTenantStore
from teetime.tenant.models import (
    AccountProvenance,
    AccountStatus,
    CourseAccount,
    User,
    UserId,
    UserRole,
    UserStatus,
    derive_account_id,
)
from teetime.tenant.notify import EmailMessage, FakeEmailSender, UserEvent, UserEventKind
from teetime.tenant.runner import OperatorSink

MB = MANGROVE_BAY_COURSE_ID
TZ = "America/New_York"
KEY_RAW = os.urandom(32)
GOOD_KEYRING = json.dumps({"active": "k1", "keys": {"k1": base64.b64encode(KEY_RAW).decode()}})
KEYRING = Keyring(active_kid="k1", keys={"k1": KEY_RAW})
# Mon 2026-10-05 12:00 ET: rows for Mon 10/12 (advance 7) are well before their cutoff.
NOW = datetime(2026, 10, 5, 16, 0, tzinfo=UTC)


def _store() -> InMemoryTenantStore:
    return InMemoryTenantStore(course_timezones={MB: TZ}, cutoff=BookingCutoffConfig())


async def _seed(store: InMemoryTenantStore, *, n: int) -> tuple[User, CourseAccount]:
    user = User(
        id=UserId(uuid4()),
        oauth_provider="github",
        oauth_subject=f"gh-{n}",
        email=f"golfer{n}@example.test",
        display_name=f"Golfer {n}",
        role=UserRole.MEMBER,
        status=UserStatus.ACTIVE,
    )
    await store.upsert_user(user)
    draft = CourseAccount(
        id=derive_account_id(user.id, MB),
        user_id=user.id,
        course_id=MB,
        provenance=AccountProvenance.USER_SUPPLIED,
        username=f"mb-login-{n}",
        password_ciphertext="x",
        key_id="k1",
        status=AccountStatus.ACTIVE,
    )
    blob = encrypt_password(KEYRING, f"pw-{n}-{uuid4().hex}", aad=credential_aad(draft))
    account = CourseAccount(**{**_fields(draft), "password_ciphertext": blob})
    await store.upsert_account(account)
    target = NOW.astimezone(ZoneInfo(TZ)).date() + timedelta(days=7)
    await store.create_explicit_row(
        user_id=user.id,
        account_id=account.id,
        target_date=target,
        window_earliest=time(8, 45),
        window_latest=time(10, 0),
        party_size=2,
        now=NOW - timedelta(days=1),
    )
    return user, account


def _fields(account: CourseAccount) -> dict[str, Any]:
    return {name: getattr(account, name) for name in account.__dataclass_fields__}


# --- registry ------------------------------------------------------------------------------


def test_release_event_registry_derives_from_the_adapter_policy() -> None:
    event = RELEASE_EVENTS["mb0600et"]
    policy = MangroveBayAdapter.release_policy
    assert (event.timezone, event.release_time) == (policy.timezone, policy.release_time)
    assert event.course_ids == (MB,)
    assert len(event.key) <= 10  # derived ACA job names stay <= 32 chars (§6.2)
    assert event_for("mb0600et") is event
    assert policies_for(event) == {MB: policy}


def test_unknown_event_is_refused_with_the_known_keys() -> None:
    with pytest.raises(ValueError, match="mb0600et"):
        event_for("nope")


# --- factories -------------------------------------------------------------------------------


async def test_hosted_pool_factory_runs_the_site_key_preflight_once_per_course() -> None:
    urls: list[str] = []

    async def resolve(url: str) -> str:
        urls.append(url)
        return "live-site-key"

    factory = HostedPoolFactory(clock=FakeClock(start=NOW), api_key="2c-key", resolve=resolve)
    pool = await factory(MB)

    assert isinstance(pool, SharedCaptchaPool)
    assert pool.course_id == MB
    assert urls == [MangroveBayAdapter.booking_page_url]
    assert factory.site_keys == {MB: "live-site-key"}


async def test_hosted_adapter_factory_builds_mb_on_the_shared_pool() -> None:
    _, account = await _seed(_store(), n=1)
    pool = SharedCaptchaPool(provider=_never, clock=FakeClock(start=NOW), course_id=MB)
    factory = HostedAdapterFactory(api_key="2c-key")

    live = factory(
        course_id=MB, account=account, pool=pool, lease_key=LeaseKey("r1"), dry_run=False
    )
    dry = factory(course_id=MB, account=account, pool=None, lease_key=None, dry_run=True)

    assert isinstance(live, MangroveBayAdapter)
    assert live._captcha_pool is pool  # the coordinated pool, not a private one
    assert live._captcha_provider is not None  # the on/off switch for pooled solving
    assert isinstance(dry, MangroveBayAdapter)
    assert dry._captcha_provider is None  # dry-run never solves
    await live.aclose()
    await dry.aclose()


async def _never() -> str:
    raise AssertionError("no solve expected")


# --- notifications -------------------------------------------------------------------------


async def test_store_user_notifier_mails_only_the_rows_user() -> None:
    store = _store()
    user, _ = await _seed(store, n=1)
    sender = FakeEmailSender()
    notifier = StoreUserNotifier(store, sender)

    await notifier.send(_event(user.id))
    await notifier.send(_event(UserId(uuid4())))  # unknown user: skipped, never raises

    (message,) = sender.sent
    assert isinstance(message, EmailMessage)
    assert message.to == user.email
    assert "Golfer," in message.body


def _event(user_id: UserId) -> UserEvent:
    return UserEvent(
        kind=UserEventKind.MISSED_DROP,
        user_id=user_id,
        row_id=None,
        course_id=MB,
        target_date=NOW.date(),
        tee_time=None,
        confirmation=None,
        detail="no_inventory",
        at=NOW,
    )


async def test_operator_sink_unconfigured_fails_every_send() -> None:
    sink = operator_sink_from_env({})
    assert isinstance(sink.sender, UnconfiguredEmailSender)
    result = await sink.sender.send(EmailMessage(to="x", subject="s", body="b"))
    assert result.ok is False


def test_operator_sink_from_env_uses_acs() -> None:
    env = {
        "ACS_EMAIL_CONNECTION": "endpoint=https://acs.example.test/;accesskey="
        + base64.b64encode(b"k" * 32).decode(),
        "ACS_EMAIL_SENDER": "DoNotReply@x.azurecomm.net",
        OPERATOR_NOTIFY_EMAIL_ENV: "ops@example.test",
    }
    sink = operator_sink_from_env(env)
    assert isinstance(sink.sender, AcsEmailClient)
    assert sink.to == "ops@example.test"


# --- the job -------------------------------------------------------------------------------


async def test_run_booking_job_keyring_missing_is_systemic_and_tells_the_operator() -> None:
    sender = FakeEmailSender()
    code = await run_booking_job(
        event_key="mb0600et",
        dry_run=True,
        wait=False,
        store=_store(),
        env={},
        operator=OperatorSink(sender=sender, to="ops@example.test"),
    )
    assert code == 1
    (summary,) = sender.sent
    assert "keyring: KeyringError" in summary.body


async def test_run_booking_job_live_requires_the_2captcha_key() -> None:
    sender = FakeEmailSender()
    code = await run_booking_job(
        event_key="mb0600et",
        dry_run=False,
        wait=False,
        store=_store(),
        env={KEYRING_ENV_VAR: GOOD_KEYRING},
        operator=OperatorSink(sender=sender, to="ops@example.test"),
    )
    assert code == 1
    (summary,) = sender.sent
    assert f"config: {TWOCAPTCHA_API_KEY_ENV}" in summary.body


async def test_run_booking_job_with_no_rows_exits_zero_and_sends_nothing() -> None:
    sender = FakeEmailSender()
    code = await run_booking_job(
        event_key="mb0600et",
        dry_run=True,
        wait=False,
        store=_store(),
        env={KEYRING_ENV_VAR: GOOD_KEYRING},
        operator=OperatorSink(sender=sender, to="ops@example.test"),
    )
    assert code == 0
    assert sender.sent == []


async def test_run_booking_job_measures_ntp_only_on_the_wait_path() -> None:
    calls: list[int] = []

    def measure() -> timedelta:
        calls.append(1)
        return timedelta(0)

    for wait in (False, True):
        await run_booking_job(
            event_key="mb0600et",
            dry_run=True,
            wait=wait,
            store=_store(),
            env={KEYRING_ENV_VAR: GOOD_KEYRING},
            operator=OperatorSink(sender=FakeEmailSender(), to="ops@example.test"),
            measure_offset=measure,
        )
    assert calls == [1]


@respx.mock  # no routes: ANY HTTP request raises
async def test_tenant_plan_with_the_real_mb_adapter_makes_no_http_request() -> None:
    store = _store()
    _, a = await _seed(store, n=1)
    _, b = await _seed(store, n=2)

    plan = await plan_booking_event(event_key="mb0600et", store=store, clock=FakeClock(start=NOW))

    assert len(plan.rows) == 2
    assert not respx.calls
    first, second = plan.rows
    assert len(first.allowlist_times) == len(second.allowlist_times) == 3
    assert not set(first.allowlist_times) & set(second.allowlist_times)
    text = "\n".join(plan.render())
    assert a.username not in text and b.username not in text
