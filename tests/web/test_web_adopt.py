"""MU-16b (MULTIUSER_PLAN §11 steps 6-7): the operator-only "Adopt existing bookings" action.

At the prod cutover the operator adopts the TOML bot's live reservations as OWNED, from the
account's latest TRUSTED snapshot (a fresh "Refresh from course"), after confirming on screen.
End-to-end over ASGI with a real store and clock; only the OAuth provider is mocked.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, time, timedelta
from uuid import uuid4

import httpx
import pytest
import respx
from fastapi import FastAPI

from teetime.core.clock import FakeClock
from teetime.core.release_policy import ReleasePolicy
from teetime.tenant.in_memory_store import InMemoryTenantStore
from teetime.tenant.models import (
    AccountProvenance,
    AccountStatus,
    BookingSource,
    CourseAccount,
    RankedWindow,
    ReservationSnapshot,
    RowStatus,
    SnapshotEntry,
    UserRole,
    derive_account_id,
)
from teetime.web.app import WebSettings, create_app

from ..tenant.conformance import CUTOFF, MB, TZ
from .conftest import OPERATOR_EMAIL, T0, GitHubIdentity, make_invited, mock_github, sign_in

POLICY = ReleasePolicy(advance_days=7, release_time=time(6, 0), timezone=TZ)
OCT3 = date(2026, 10, 3)
TEE = datetime(2026, 10, 3, 13, 30, tzinfo=UTC)  # 09:30 EDT


@pytest.fixture
def app(settings: WebSettings, store: InMemoryTenantStore, clock: FakeClock) -> FastAPI:
    return create_app(settings, store=store, clock=clock, policies={str(MB): POLICY}, cutoff=CUTOFF)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver"
    ) as c:
        yield c


async def _sign_in_as(
    client: httpx.AsyncClient,
    store: InMemoryTenantStore,
    router: respx.MockRouter,
    email: str,
    subject: str,
) -> CourseAccount:
    await store.upsert_user(make_invited(email, role=UserRole.MEMBER))
    mock_github(router, GitHubIdentity(subject=subject, emails=[(email, True)]))
    assert (await sign_in(client)).status_code == 303
    user = await store.get_user_by_subject("github", subject)
    assert user is not None
    account = CourseAccount(
        id=derive_account_id(user.id, MB),
        user_id=user.id,
        course_id=MB,
        provenance=AccountProvenance.USER_SUPPLIED,
        username=f"golfer-{uuid4().hex[:8]}",
        password_ciphertext="v1:k1:nonce:ct",
        key_id="k1",
        status=AccountStatus.ACTIVE,
    )
    await store.upsert_account(account)
    await store.create_explicit_row(
        user_id=user.id,
        account_id=account.id,
        target_date=OCT3,
        options=(RankedWindow(1, time(8, 0), time(10, 0)),),
        party_size=2,
        now=T0,
    )
    return account


async def _snapshot(store: InMemoryTenantStore, account: CourseAccount, *, at: datetime) -> None:
    await store.save_snapshot(
        ReservationSnapshot(
            course_account_id=account.id,
            observed_at=at,
            source="web",
            trusted=True,
            entries=(SnapshotEntry(raw_id="TOML-1", tee_time=TEE, party_size=2),),
        )
    )


async def _post(client: httpx.AsyncClient, path: str, data: dict[str, str]) -> httpx.Response:
    page = await client.get("/")
    marker = 'name="csrf_token" value="'
    start = page.text.index(marker) + len(marker)
    token = page.text[start : page.text.index('"', start)]
    return await client.post(path, data={"csrf_token": token, **data})


async def _row(store: InMemoryTenantStore, account: CourseAccount):  # type: ignore[no-untyped-def]
    (row,) = await store.rows_for_account_date(account.id, OCT3)
    return row


async def test_operator_sees_the_adoption_preview(
    client: httpx.AsyncClient, store: InMemoryTenantStore, provider_mock: respx.MockRouter
) -> None:
    account = await _sign_in_as(client, store, provider_mock, OPERATOR_EMAIL, "1")
    await _snapshot(store, account, at=T0)
    page = await client.get("/accounts")
    assert f'action="/accounts/{account.id}/adopt"' in page.text
    assert "TOML-1" in page.text and "09:30" in page.text


async def test_seed_adopt_requires_confirmation(
    client: httpx.AsyncClient, store: InMemoryTenantStore, provider_mock: respx.MockRouter
) -> None:
    """MULTIUSER_PLAN §12's named test: nothing is adopted without the on-screen confirm."""
    account = await _sign_in_as(client, store, provider_mock, OPERATOR_EMAIL, "1")
    await _snapshot(store, account, at=T0)
    resp = await _post(client, f"/accounts/{account.id}/adopt", {})
    assert resp.status_code == 400
    assert (await _row(store, account)).status is RowStatus.PENDING


async def test_confirmed_adoption_books_the_row_owned(
    client: httpx.AsyncClient, store: InMemoryTenantStore, provider_mock: respx.MockRouter
) -> None:
    account = await _sign_in_as(client, store, provider_mock, OPERATOR_EMAIL, "1")
    await _snapshot(store, account, at=T0)
    resp = await _post(client, f"/accounts/{account.id}/adopt", {"confirm": "on"})
    assert resp.status_code == 303
    row = await _row(store, account)
    assert (row.status, row.booked_raw_id) == (RowStatus.BOOKED, "TOML-1")
    (entry,) = await store.list_owned_bookings(account.id, target_date=OCT3)
    assert entry.source is BookingSource.ADOPTED_OWNED


async def test_a_stale_snapshot_must_be_refreshed_first(
    client: httpx.AsyncClient,
    store: InMemoryTenantStore,
    provider_mock: respx.MockRouter,
) -> None:
    account = await _sign_in_as(client, store, provider_mock, OPERATOR_EMAIL, "1")
    await _snapshot(store, account, at=T0 - timedelta(minutes=30))
    resp = await _post(client, f"/accounts/{account.id}/adopt", {"confirm": "on"})
    assert resp.status_code == 409
    assert "Refresh from course" in resp.text
    assert (await _row(store, account)).status is RowStatus.PENDING


async def test_a_member_cannot_adopt(
    client: httpx.AsyncClient, store: InMemoryTenantStore, provider_mock: respx.MockRouter
) -> None:
    account = await _sign_in_as(client, store, provider_mock, "turk@example.test", "2")
    await _snapshot(store, account, at=T0)
    page = await client.get("/accounts")
    assert "/adopt" not in page.text
    resp = await _post(client, f"/accounts/{account.id}/adopt", {"confirm": "on"})
    assert resp.status_code == 403
    assert (await _row(store, account)).status is RowStatus.PENDING
