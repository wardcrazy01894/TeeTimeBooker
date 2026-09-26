"""MU-8b: ``CosmosTenantStore`` (MULTIUSER_PLAN §3.1/§3.2/§3.5/§10.2).

Three layers:

1. ``TestCosmosTenantStoreOnFakeContainer`` runs the WHOLE conformance suite (the contract) on
   the real store over ``FakeContainer``, an in-process fake of the async ``azure-cosmos``
   container API (the SDK boundary is faked, never the store). It runs in CI.
2. ``TestCosmosTenantStoreIntegration`` runs the same suite against the real free-tier ``dev``
   database's ``tenant-ci`` / ``global-ci`` containers. ``integration``-marked AND skipped unless
   ``TENANT_COSMOS_ENDPOINT`` is set with ``TENANT_COSMOS_CONTAINER_SUFFIX=-ci`` (README).
3. Unit tests of the batch / ETag / error-mapping / claim-protocol logic, driving races through
   ``FakeContainer.before_write``.
"""

from __future__ import annotations

import os
import re
from collections.abc import AsyncIterator
from dataclasses import fields, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from azure.identity.aio import DefaultAzureCredential

from teetime.core.clock import FakeClock
from teetime.core.models import CourseId
from teetime.tenant.cosmos import store as store_module
from teetime.tenant.cosmos.documents import (
    ClaimKind,
    ClaimState,
    SlotPointer,
    UniquenessClaim,
    claim_key_hash,
    to_claim_doc,
    to_row_doc,
    to_slot_doc,
    username_claim_key,
)
from teetime.tenant.cosmos.store import (
    QUERIED_PATHS,
    CosmosSettings,
    CosmosTenantStore,
    _Batch,
    cosmos_tenant_store,
    make_credential,
)
from teetime.tenant.models import (
    AccountProvenance,
    AccountStatus,
    Actor,
    CourseAccount,
    CourseAccountId,
    RowId,
    RowStatus,
    RuleId,
    TransitionRefusedError,
    UserId,
    derive_account_id,
)
from teetime.tenant.store import (
    UniquenessConflictError,
    VersionConflictError,
)

from ..conformance import (
    COURSE_TIMEZONES,
    CUTOFF,
    MAX_ACCOUNTS_PER_COURSE,
    MB,
    NOW,
    TARGET,
    WATCHER,
    StoreHarness,
    TenantStoreConformance,
    _book,
    _explicit,
    _outcome,
    _rule,
    _rule_row,
    _tenant,
)
from .fake_container import FakeContainer

CLAIM_T0 = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


def _fake_store(
    clock: FakeClock | None = None,
) -> tuple[CosmosTenantStore, FakeContainer, FakeContainer]:
    tenant = FakeContainer(pk_field="accountId")
    global_ = FakeContainer(pk_field="pk")
    store = CosmosTenantStore(
        tenant=tenant,
        global_=global_,
        course_timezones=COURSE_TIMEZONES,
        cutoff=CUTOFF,
        max_accounts_per_course=MAX_ACCOUNTS_PER_COURSE,
        clock=clock or FakeClock(start=CLAIM_T0),
    )
    return store, tenant, global_


def _harness(store: CosmosTenantStore) -> StoreHarness:
    async def slot_pointer(account: CourseAccountId, day: date) -> RowId | None:
        return await store.slot_pointer(account, day)

    async def ruleday_pointer(account: CourseAccountId, weekday: int) -> RuleId | None:
        return await store.ruleday_pointer(account, weekday)

    return StoreHarness(store=store, slot_pointer=slot_pointer, ruleday_pointer=ruleday_pointer)


# --- 1. the contract, on the fake SDK boundary (CI) ------------------------------------------


class TestCosmosTenantStoreOnFakeContainer(TenantStoreConformance):
    @pytest.fixture
    def harness(self) -> StoreHarness:
        store, _, _ = _fake_store()
        return _harness(store)


# --- 2. the contract, against the real dev database (integration) --------------------------

_IT_SUFFIX = "-ci"


def _integration_settings() -> CosmosSettings | None:
    endpoint = os.environ.get("TENANT_COSMOS_ENDPOINT")
    if not endpoint or os.environ.get("TENANT_COSMOS_CONTAINER_SUFFIX") != _IT_SUFFIX:
        return None
    return CosmosSettings.from_env(os.environ)


@pytest.mark.integration
@pytest.mark.skipif(
    _integration_settings() is None,
    reason="set TENANT_COSMOS_ENDPOINT and TENANT_COSMOS_CONTAINER_SUFFIX=-ci (README)",
)
class TestCosmosTenantStoreIntegration(TenantStoreConformance):
    """The suite scans whole containers (READ #1, the finalizer, probe counts), so every test
    starts from EMPTY CI containers: runs must not overlap (one developer at a time)."""

    @pytest.fixture
    async def harness(self) -> AsyncIterator[StoreHarness]:
        settings = _integration_settings()
        assert settings is not None
        async with cosmos_tenant_store(
            settings,
            course_timezones=COURSE_TIMEZONES,
            cutoff=CUTOFF,
            max_accounts_per_course=MAX_ACCOUNTS_PER_COURSE,
        ) as store:
            await store.purge_ci_containers()
            try:
                yield _harness(store)
            finally:
                await store.purge_ci_containers()


# --- 3. unit tests of the Cosmos-specific logic ----------------------------------------------


async def test_batch_is_single_partition() -> None:
    """Every multi-doc write is ONE transactional batch whose ops all live in the batch's
    partition (§3.2); an outcome whose ledger belongs to another account is refused before any
    batch is sent."""
    store, tenant, _ = _fake_store()
    t = await _tenant(store)
    _, rule_row = await _rule_row(store, t)
    await _explicit(store, t)  # supersede: row replace + explicit create + slot replace
    booked = await _book(store, await _explicit(store, t, target=TARGET + timedelta(days=7)))
    assert booked.status is RowStatus.BOOKED
    assert tenant.batches, "no transactional batch was executed"
    assert any(len(b.operations) > 1 for b in tenant.batches)
    for batch in tenant.batches:
        for op in batch.operations:
            if op[0] in ("create", "upsert"):
                assert op[1][0]["accountId"] == batch.partition_key
            elif op[0] == "replace":
                assert op[1][1]["accountId"] == batch.partition_key

    other = await _tenant(store, n=1)
    with pytest.raises(ValueError, match="cross-partition"):
        _Batch(other.account.id).create(to_row_doc(rule_row), role="row")
    sent = len(tenant.batches)
    foreign = _outcome(rule_row, course_account_id=other.account.id)
    with pytest.raises(ExceptionGroup) as info:
        await store.record_outcomes([foreign])
    assert info.group_contains(ValueError)
    assert len(tenant.batches) == sent


async def test_batch_create_slot_conflict_aborts_row_create() -> None:
    """A slot created by a concurrent writer between the read and the batch is a 409 on the slot
    create: the WHOLE batch aborts, so the row doc is not written either."""
    store, tenant, _ = _fake_store()
    t = await _tenant(store)
    squatter = RowId(uuid4())

    async def race() -> None:
        doc = to_slot_doc(SlotPointer(t.account.id, TARGET, squatter))
        await tenant.create_item(doc)

    tenant.before_write = race
    with pytest.raises(TransitionRefusedError):
        await _explicit(store, t)
    assert await store.rows_for_account_date(t.account.id, TARGET) == []
    assert await store.slot_pointer(t.account.id, TARGET) == squatter


async def test_rule_row_create_asserts_ruleday_pointer_in_batch() -> None:
    """The rule IfMatch (round-5 MF2): the rule is deactivated between the materializer's read
    and its batch, so the batch's IfMatch on the ``ruleday`` pointer fails and NO row is created
    under the no-longer-active rule."""
    store, tenant, _ = _fake_store()
    t = await _tenant(store)
    rule = await store.upsert_rule(_rule(t), user_id=t.user.id)

    async def deactivate() -> None:
        await store.upsert_rule(replace(rule, active=False), user_id=t.user.id)

    tenant.before_write = deactivate
    with pytest.raises(TransitionRefusedError):
        await store.insert_rule_row_if_absent(rule, TARGET, now=NOW)
    assert await store.rows_for_account_date(t.account.id, TARGET) == []
    assert await store.slot_pointer(t.account.id, TARGET) is None


async def test_unskip_asserts_ruleday_pointer_in_batch() -> None:
    """The shared "may become active" guard asserts the pointer too: an unskip racing a rule
    deactivation is refused and the row stays SKIPPED."""
    store, tenant, _ = _fake_store()
    t = await _tenant(store)
    rule, row = await _rule_row(store, t)
    await store.transition_row(
        row.id, user_id=t.user.id, to=RowStatus.SKIPPED, actor=Actor.WEB, reason=None, now=NOW
    )

    async def deactivate() -> None:
        await store.upsert_rule(replace(rule, active=False), user_id=t.user.id)

    tenant.before_write = deactivate
    with pytest.raises(TransitionRefusedError):
        await store.transition_row(
            row.id, user_id=t.user.id, to=RowStatus.PENDING, actor=Actor.WEB, reason=None, now=NOW
        )
    (after,) = await store.rows_for_account_date(t.account.id, TARGET)
    assert after.status is RowStatus.SKIPPED


async def test_ifmatch_412_maps_to_version_conflict() -> None:
    """A rule edited between the store's read and its IfMatch replace is a 412, surfaced as
    ``VersionConflictError`` once the re-read shows the caller's version is stale."""
    store, tenant, _ = _fake_store()
    t = await _tenant(store)
    stored = await store.upsert_rule(_rule(t), user_id=t.user.id)

    async def race() -> None:
        tenant.before_write = None
        await store.upsert_rule(replace(stored, party_size=3), user_id=t.user.id)

    tenant.before_write = race
    with pytest.raises(VersionConflictError):
        await store.upsert_rule(replace(stored, party_size=4), user_id=t.user.id)
    (current,) = await store.list_rules_for_user(t.user.id)
    assert (current.party_size, current.version) == (3, stored.version + 1)


async def test_ifmatch_lease_412_is_not_acquired() -> None:
    """Two writers race for one row lease: the loser's IfMatch replace is a 412 and it is NOT
    acquired (§3.5); the winner's lease stands."""
    store, tenant, _ = _fake_store()
    t = await _tenant(store)
    _, row = await _rule_row(store, t)
    until = NOW + timedelta(seconds=300)

    async def race() -> None:
        assert await store.acquire_row_lease(
            row.id, owner="web:other", until=until, now=NOW, expected=None
        )

    tenant.before_write = race
    assert not await store.acquire_row_lease(
        row.id, owner=WATCHER, until=until, now=NOW, expected=None
    )
    rows = await store.rows_for_account_date(t.account.id, TARGET)
    assert rows[0].lease_owner == "web:other"


def _account(user_id: UserId, *, username: str, course: CourseId = MB) -> CourseAccount:
    return CourseAccount(
        id=derive_account_id(user_id, course),
        user_id=user_id,
        course_id=course,
        provenance=AccountProvenance.USER_SUPPLIED,
        username=username,
        password_ciphertext="v1:k1:nonce:ct",
        key_id="k1",
        status=AccountStatus.ACTIVE,
    )


def _username_claim(
    *, username: str, owner: CourseAccountId, state: ClaimState, created_at: datetime
) -> dict[str, Any]:
    return to_claim_doc(
        UniquenessClaim(
            kind=ClaimKind.USERNAME,
            key_hash=claim_key_hash(ClaimKind.USERNAME, username_claim_key(MB, username)),
            state=state,
            created_at=created_at,
            owner_id=owner,
        )
    )


async def test_create_409_maps_to_uniqueness_conflict() -> None:
    """Another connect creates (and binds) the same username claim between our read and our
    create: the create is a 409 and surfaces as ``UniquenessConflictError``; nothing of ours is
    left behind."""
    store, tenant, global_ = _fake_store()
    rival = CourseAccountId(uuid4())

    async def race() -> None:
        await global_.create_item(
            _username_claim(
                username="golfer", owner=rival, state=ClaimState.BOUND, created_at=CLAIM_T0
            )
        )

    global_.before_write = race
    user_id = UserId(uuid4())
    with pytest.raises(UniquenessConflictError):
        await store.upsert_account(_account(user_id, username="golfer"))
    assert await store.get_account_unscoped(derive_account_id(user_id, MB)) is None
    assert not [k for k in tenant.docs if k[1] == "account"]


async def test_claim_protocol_rolls_back_on_lost_bind() -> None:
    """§3.2 step 3: the claim is reclaimed after we created the account doc, so our IfMatch bind
    is a 412: the claimant DELETES the account doc it created and reports "already connected"."""
    store, tenant, global_ = _fake_store()
    user_id = UserId(uuid4())
    account = _account(user_id, username="golfer")
    key = claim_key_hash(ClaimKind.USERNAME, username_claim_key(MB, "golfer"))

    async def reclaim() -> None:  # runs just before OUR account-doc create
        pk = f"claim:{key}"
        doc = await global_.read_item(pk, pk)
        doc["ownerId"] = str(uuid4())
        await global_.upsert_item({k: v for k, v in doc.items() if k != "_etag"})

    tenant.before_write = reclaim
    with pytest.raises(UniquenessConflictError):
        await store.upsert_account(account)
    assert await store.get_account_unscoped(account.id) is None
    assert await store.list_accounts_for_user(user_id) == []


async def test_reclaim_requires_age_and_missing_account() -> None:
    """A PENDING claim is reclaimable only when it is older than 10 min AND its owner has no
    account doc (§3.2, round-2 SF5)."""
    clock = FakeClock(start=CLAIM_T0)
    store, _, global_ = _fake_store(clock)
    ghost = CourseAccountId(uuid4())
    await global_.create_item(
        _username_claim(
            username="golfer", owner=ghost, state=ClaimState.PENDING, created_at=CLAIM_T0
        )
    )
    newcomer = UserId(uuid4())

    await clock.sleep(5 * 60)  # young + owner missing -> still taken
    with pytest.raises(UniquenessConflictError):
        await store.upsert_account(_account(newcomer, username="golfer"))

    # Old, but its owner HAS an account doc holding that username -> still taken.
    holder = await _tenant(store)
    await store.upsert_account(replace(holder.account, username="other-login"))
    holder_claim = _username_claim(
        username="shared", owner=holder.account.id, state=ClaimState.PENDING, created_at=CLAIM_T0
    )
    await global_.create_item(holder_claim)
    await store.upsert_account(replace(holder.account, username="shared"))  # its own: fine
    await clock.sleep(6 * 60)  # now 11 min after the claims
    with pytest.raises(UniquenessConflictError):
        await store.upsert_account(_account(UserId(uuid4()), username="shared"))

    # Old AND owner missing -> reclaimed.
    await store.upsert_account(_account(newcomer, username="golfer"))
    (connected,) = await store.list_accounts_for_user(newcomer)
    assert connected.username == "golfer"


async def test_orphan_username_claim_reclaimed() -> None:
    """A BOUND claim whose owner no longer holds that username (a rename whose old-claim release
    crashed) is an orphan: another connect reclaims it instead of being blocked forever."""
    store, _, global_ = _fake_store()
    holder = await _tenant(store)
    await store.upsert_account(replace(holder.account, username="renamed"))
    await global_.upsert_item(
        _username_claim(
            username="old-name",
            owner=holder.account.id,
            state=ClaimState.BOUND,
            created_at=CLAIM_T0,
        )
    )
    newcomer = UserId(uuid4())
    await store.upsert_account(_account(newcomer, username="old-name"))
    (connected,) = await store.list_accounts_for_user(newcomer)
    assert connected.username == "old-name"


async def test_ci_suffix_only_from_setting() -> None:
    """The CI containers are selected ONLY by the container-suffix setting (§10.2 round-2 SF2):
    the default, and an environment without the variable, use ``tenant`` / ``global``."""
    base = CosmosSettings(endpoint="https://x.documents.azure.com:443/", database="dev")
    assert base.container_suffix == ""
    assert base.container_names == ("tenant", "global")
    assert replace(base, container_suffix="-ci").container_names == ("tenant-ci", "global-ci")
    with pytest.raises(ValueError, match="suffix"):
        replace(base, container_suffix="-prod")

    env = {"TENANT_COSMOS_ENDPOINT": base.endpoint, "TENANT_COSMOS_DATABASE": "dev"}
    assert CosmosSettings.from_env(env).container_names == ("tenant", "global")
    ci_env = env | {"TENANT_COSMOS_CONTAINER_SUFFIX": "-ci"}
    assert CosmosSettings.from_env(ci_env).container_names == ("tenant-ci", "global-ci")

    async with cosmos_tenant_store(
        replace(base, container_suffix="-ci"),
        course_timezones=COURSE_TIMEZONES,
        cutoff=CUTOFF,
        credential=_NoNetworkCredential(),
    ) as store:
        assert store.container_names == ("tenant-ci", "global-ci")
    async with cosmos_tenant_store(
        base, course_timezones=COURSE_TIMEZONES, cutoff=CUTOFF, credential=_NoNetworkCredential()
    ) as store:
        assert store.container_names == ("tenant", "global")


async def test_no_account_key_auth_path() -> None:
    """Auth is an Entra token only (§10.2: local auth is disabled on the account): the settings
    carry no key, the opener refuses a key-shaped credential, and the default credential is
    ``DefaultAzureCredential`` (managed identity in ACA, ``az login`` for a developer)."""
    names = {f.name for f in fields(CosmosSettings)}
    assert not {n for n in names if any(w in n for w in ("key", "secret", "connection"))}
    settings = CosmosSettings(
        endpoint="https://x.documents.azure.com:443/",
        database="dev",
        managed_identity_client_id="00000000-0000-0000-0000-00000000c1d0",
    )
    for key_like in ("master-key==", {"masterKey": "k"}):
        with pytest.raises(TypeError, match="token credential"):
            async with cosmos_tenant_store(
                settings,
                course_timezones=COURSE_TIMEZONES,
                cutoff=CUTOFF,
                credential=key_like,  # type: ignore[arg-type]
            ):
                pass
    credential = make_credential(settings)
    try:
        assert isinstance(credential, DefaultAzureCredential)
    finally:
        await credential.close()


class _NoNetworkCredential:
    """An ``AsyncTokenCredential`` that fails loudly if anything asks it for a token."""

    async def get_token(self, *scopes: str, **kwargs: object) -> Any:
        raise AssertionError("no token may be requested in a unit test")

    async def close(self) -> None:
        return None

    async def __aenter__(self) -> _NoNetworkCredential:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


async def test_purge_only_empties_ci_containers() -> None:
    """The integration suite's reset refuses ``tenant`` / ``global`` outright and empties only
    the ``-ci`` containers."""
    store, tenant, global_ = _fake_store()
    await _tenant(store)
    with pytest.raises(RuntimeError, match="non-CI"):
        await store.purge_ci_containers()
    assert tenant.docs and global_.docs

    ci_store = CosmosTenantStore(
        tenant=tenant,
        global_=global_,
        course_timezones=COURSE_TIMEZONES,
        cutoff=CUTOFF,
        container_names=("tenant-ci", "global-ci"),
    )
    await ci_store.purge_ci_containers()
    assert not tenant.docs and not global_.docs


def test_queried_paths_are_declared() -> None:
    """Every ``c.<path>`` the store filters on is in ``QUERIED_PATHS`` (the list MU-15b's index
    policy must include): a filter on an unindexed path is rejected by Cosmos at runtime."""
    source = Path(store_module.__file__).read_text(encoding="utf-8")
    used = {f"/{m}" for m in re.findall(r"(?<![\w.])c\.(\w+)", source)}
    assert used, "no query paths found; the scan is broken"
    assert used <= QUERIED_PATHS, sorted(used - QUERIED_PATHS)


def test_ci_containers_refused_outside_the_dev_database() -> None:
    """MU-8b review SF2: the `-ci` containers exist only in the `dev` database, and the integration
    suite EMPTIES them — so a `-ci` suffix paired with any other database is refused outright."""
    with pytest.raises(ValueError, match="dev"):
        CosmosSettings(
            endpoint="https://x.documents.azure.com:443/", database="prod", container_suffix="-ci"
        )
    ok = CosmosSettings(
        endpoint="https://x.documents.azure.com:443/", database="dev", container_suffix="-ci"
    )
    assert ok.container_names == ("tenant-ci", "global-ci")
