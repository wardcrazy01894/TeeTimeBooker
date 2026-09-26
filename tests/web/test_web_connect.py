"""MU-14: connect / re-verify a course account — the live ForeUP login probe (MULTIUSER_PLAN §8.4).

Service-level, against a real ``InMemoryTenantStore`` + ``FakeClock``; the ForeUP adapter is
faked at the ``AdapterFactory`` boundary (``account_builders.ProbeFactory``).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest

from teetime.core.adapter import AuthError
from teetime.core.clock import FakeClock
from teetime.core.redaction import redact_text
from teetime.tenant.crypto import CredentialDecryptError, credential_aad, decrypt_password
from teetime.tenant.in_memory_store import InMemoryTenantStore
from teetime.tenant.models import (
    AccountProvenance,
    AccountStatus,
    User,
    UserId,
    UserRole,
    UserStatus,
    derive_account_id,
)
from teetime.web import services
from teetime.web.services import ActionRefusedError, ProbeLimits, RateLimitedError

from ..tenant.conformance import MB
from .account_builders import KEYRING, PASSWORD, ProbeAdapter, ProbeFactory
from .conftest import T0, new_store

USERNAME = "turk@golf.example"


def _user(n: int = 0) -> User:
    return User(
        id=UserId(uuid4()),
        oauth_provider="github",
        oauth_subject=f"sub-{n}",
        email=f"u{n}@example.test",
        display_name=f"U{n}",
        role=UserRole.MEMBER,
        status=UserStatus.ACTIVE,
    )


@pytest.fixture
def store() -> InMemoryTenantStore:
    return new_store()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=T0)


async def _connect(
    store: InMemoryTenantStore,
    clock: FakeClock,
    factory: ProbeFactory,
    *,
    user: User,
    username: str = USERNAME,
    password: str = PASSWORD,
    limits: ProbeLimits | None = None,
) -> services.CourseAccount:
    return await services.connect_account(
        store,
        user_id=user.id,
        course_id=MB,
        username=username,
        password=password,
        keyring=KEYRING,
        adapter_factory=factory,
        clock=clock,
        limits=limits or ProbeLimits(),
    )


async def test_connect_encrypts_with_aad(store: InMemoryTenantStore, clock: FakeClock) -> None:
    user = _user()
    factory = ProbeFactory()
    account = await _connect(store, clock, factory, user=user)

    stored = await store.get_account(derive_account_id(user.id, MB), user_id=user.id)
    assert stored == account
    assert stored.provenance is AccountProvenance.USER_SUPPLIED
    assert stored.status is AccountStatus.ACTIVE
    assert stored.verified_at == T0
    assert stored.key_id == KEYRING.active_kid
    assert PASSWORD not in stored.password_ciphertext
    # decrypts ONLY under this account's AAD (course_account_id|course_id|username, §9.2)
    assert decrypt_password(KEYRING, stored.password_ciphertext, aad=credential_aad(stored)) == (
        PASSWORD
    )
    other = replace(stored, id=derive_account_id(_user(1).id, MB))
    with pytest.raises(CredentialDecryptError):
        decrypt_password(KEYRING, stored.password_ciphertext, aad=credential_aad(other))
    # one live login, on a throwaway adapter: no shared CAPTCHA pool, never able to book
    (call,) = factory.calls
    assert call["pool"] is None and call["lease_key"] is None and call["dry_run"] is True
    assert factory.adapter.credentials == [(USERNAME, PASSWORD)]
    assert factory.adapter.closed == 1
    # the plaintext is registered with the log filter (E7) for the rest of the process
    assert PASSWORD not in redact_text(f"login with {PASSWORD}")


async def test_connect_soft_login_failure_stores_nothing(
    store: InMemoryTenantStore, clock: FakeClock
) -> None:
    user = _user()
    factory = ProbeFactory()
    factory.adapter.set_auth_soft_fail()
    with pytest.raises(ActionRefusedError, match=r"(?i)login failed"):
        await _connect(store, clock, factory, user=user)
    assert await store.list_accounts_for_user(user.id) == []
    since = T0 - timedelta(hours=1)
    assert await store.count_login_probes(user_id=user.id, username_hash=None, since=since) == 1


async def test_probe_never_auto_retries(store: InMemoryTenantStore, clock: FakeClock) -> None:
    """A failed login (soft fail, a hard AuthError, or a transport blip) is ONE authenticate
    call and ONE recorded probe: never retried automatically (PLAN §8.1/§12)."""
    user = _user()
    for n, script in enumerate(("soft", "auth", "transport")):
        adapter = ProbeAdapter()
        if script == "soft":
            adapter.set_auth_soft_fail()
        elif script == "auth":
            adapter.set_authenticate_side_effects([AuthError("bad password"), None, None])
        else:
            adapter.set_authenticate_side_effects([ConnectionError("reset"), None, None])
        factory = ProbeFactory(adapter=adapter)
        with pytest.raises(ActionRefusedError):
            await _connect(store, clock, factory, user=user, username=f"u{n}@golf.example")
        assert adapter.authenticate_call_count == 1, script
        assert len(factory.calls) == 1, script
        assert adapter.closed == 1, script
    since = T0 - timedelta(hours=1)
    assert await store.count_login_probes(user_id=user.id, username_hash=None, since=since) == 3
    assert await store.list_accounts_for_user(user.id) == []


async def _seed_probes(
    store: InMemoryTenantStore, *, user: User, n: int, username_hash: str
) -> None:
    for _ in range(n):
        await store.record_login_probe(
            user_id=user.id, course_id=MB, username_hash=username_hash, ok=False, at=T0
        )


async def test_probe_rate_limits(store: InMemoryTenantStore, clock: FakeClock) -> None:
    """Rate-check FIRST (§8.4): over any limit, no adapter is built and nothing is recorded."""
    limits = ProbeLimits()
    me, other = _user(0), _user(1)
    h = services.probe_username_hash(MB, USERNAME)

    # per user: 5 / hour
    await _seed_probes(store, user=me, n=limits.per_user_per_hour, username_hash="x")
    factory = ProbeFactory()
    with pytest.raises(RateLimitedError):
        await _connect(store, clock, factory, user=me, username="fresh@golf.example")
    assert factory.calls == []

    # per username: 3 / hour, across users (another user probing the same login)
    store = new_store()
    await _seed_probes(store, user=other, n=limits.per_username_per_hour, username_hash=h)
    await clock.sleep(timedelta(minutes=16).total_seconds())  # past the 15 min lockout window
    with pytest.raises(RateLimitedError):
        await _connect(store, clock, factory, user=me)
    assert factory.calls == []

    # site-wide: 30 / hour
    store = new_store()
    await _seed_probes(store, user=other, n=limits.site_per_hour, username_hash="y")
    with pytest.raises(RateLimitedError):
        await _connect(store, clock, factory, user=me, username="fresh@golf.example")
    assert factory.calls == []

    # lockout: 2 probes of a username in the last 15 min (conservative form of "2 consecutive
    # failures -> 15 min lockout"; the store counts probes, it does not read their outcome)
    store = new_store()
    await store.record_login_probe(
        user_id=other.id, course_id=MB, username_hash=h, ok=False, at=clock.now_utc()
    )
    await store.record_login_probe(
        user_id=other.id, course_id=MB, username_hash=h, ok=False, at=clock.now_utc()
    )
    with pytest.raises(RateLimitedError, match=r"(?i)try again"):
        await _connect(store, clock, factory, user=me)
    assert factory.calls == []
    await clock.sleep(timedelta(minutes=15, seconds=1).total_seconds())
    await _connect(store, clock, factory, user=me)  # the lockout has passed
    assert len(factory.calls) == 1


async def test_reverify_replaces_ciphertext_and_reactivates(
    store: InMemoryTenantStore, clock: FakeClock
) -> None:
    user = _user()
    account = await _connect(store, clock, ProbeFactory(), user=user)
    await store.upsert_account(
        replace(account, status=AccountStatus.AUTH_FAILED, consecutive_soft_auth_failures=3)
    )
    await clock.sleep(timedelta(hours=2).total_seconds())
    new_password = "An0ther-Secret-Pw"
    factory = ProbeFactory()
    got = await services.reverify_account(
        store,
        user_id=user.id,
        account_id=account.id,
        password=new_password,
        keyring=KEYRING,
        adapter_factory=factory,
        clock=clock,
        limits=ProbeLimits(),
    )
    assert got.status is AccountStatus.ACTIVE
    assert got.consecutive_soft_auth_failures == 0
    assert got.verified_at == clock.now_utc()
    assert factory.adapter.credentials == [(USERNAME, new_password)]
    assert decrypt_password(KEYRING, got.password_ciphertext, aad=credential_aad(got)) == (
        new_password
    )


async def test_reverify_other_users_account_is_not_found(
    store: InMemoryTenantStore, clock: FakeClock
) -> None:
    victim = _user(1)
    account = await _connect(store, clock, ProbeFactory(), user=victim)
    factory = ProbeFactory()
    with pytest.raises(services.WebNotFoundError):
        await services.reverify_account(
            store,
            user_id=_user(2).id,
            account_id=account.id,
            password=PASSWORD,
            keyring=KEYRING,
            adapter_factory=factory,
            clock=clock,
            limits=ProbeLimits(),
        )
    assert factory.calls == []
