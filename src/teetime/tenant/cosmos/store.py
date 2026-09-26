"""CosmosTenantStore: the durable ``TenantStore`` on Azure Cosmos DB for NoSQL (MULTIUSER_PLAN
§3.1/§3.2/§3.5/§10.2, MU-8b).

It reproduces ``InMemoryTenantStore`` exactly (both run ``tests/tenant/conformance.py``; the
pure rules they share are ``tenant.semantics``) and differs only in HOW a write is committed:

- **One transactional batch per state change.** ``_commit`` turns a set of row writes into ONE
  ``execute_item_batch`` in the account's partition (``/accountId``): the row creates / IfMatch
  replaces, the ``slot|<date>`` pointer create / replace / delete derived from the status change
  (so the pointer and the rows can never disagree), the ``ruleday|<weekday>`` assertion of the
  "may become active" guard, and any ledger upserts. A batch is all-or-nothing, and ``_Batch``
  refuses an op for another partition before anything is sent.
- **Error mapping.** A 409 on a row create is "already exists" (``insert_rule_row_if_absent``
  returns None); a 409 on a slot create is "the date already has an active row"; a 412 / 404 on
  any IfMatch op is "changed since it was read" (``TransitionRefusedError``). Lease writes map a
  412 to "not acquired"; rule replaces re-read on a 412 and raise ``VersionConflictError`` only
  when the stored version really moved; claim creates map a 409 to ``UniquenessConflictError``
  once the re-read shows another owner.
- **The rule IfMatch** (round-5 MF2): a row write that makes a rule row bookable asserts the
  ``ruleday|<weekday>`` pointer in the same batch by an IfMatch self-replace. The pointer names
  the rule iff that rule is ACTIVE on that weekday in that account (``upsert_rule`` keeps it so,
  in its own batch), so a concurrent deactivation or weekday move aborts the row write. It
  bumps the pointer's ETag, which only ever costs a concurrent ``upsert_rule`` a transparent
  re-read (412 -> retry).
- **Cross-partition uniqueness** (§3.2, round-2 SF5) uses ``claim`` docs in ``global``: create
  PENDING (409 = taken) -> write the owner doc -> IfMatch bind to BOUND; a 412 on the bind means
  the claim was reclaimed, so the claimant deletes (or restores) the doc it wrote and reports the
  conflict. A PENDING claim is reclaimable only when older than ``CLAIM_RECLAIM_AFTER`` AND its
  owner does not hold that key; a BOUND claim only when its owner exists and holds a DIFFERENT key
  (a rename whose old-claim release crashed). ``max_accounts_per_course`` is an IfMatch counter.

Known residual (documented, single-user scale): the user-terminal history check of the "may
become active" guard is a partition QUERY, not a document the batch can assert, so a concurrent
user cancel landing between that read and the batch is not seen. Its window is milliseconds and a
user-terminal row can only follow an active row that held the slot, so the date is rarely open.

Auth is an Entra token only (``make_credential``: ``DefaultAzureCredential``, which is the
user-assigned managed identity in ACA and ``az login`` for a developer); the account has local
(key) auth disabled (§10.2), and ``cosmos_tenant_store`` refuses a key-shaped credential.
Container names are ``tenant`` / ``global`` unless ``CosmosSettings.container_suffix`` is
``-ci`` (the integration suite's ``tenant-ci`` / ``global-ci``), which no job ever sets.

Not wired into any command yet (MU-16).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID, uuid4

from azure.core import MatchConditions
from azure.core.credentials_async import AsyncTokenCredential
from azure.cosmos.aio import ContainerProxy, CosmosClient
from azure.cosmos.exceptions import CosmosBatchOperationError, CosmosHttpResponseError
from azure.identity.aio import DefaultAzureCredential

from ...core.clock import Clock, RealClock
from ...core.config import BookingCutoffConfig
from ...core.models import CourseId
from ...core.redaction import redact_payload
from ..materialize import RuleConflictError
from ..models import (
    ACTIVE_ROW_STATUSES,
    AccountStatus,
    Actor,
    BookingState,
    CourseAccount,
    CourseAccountId,
    EventRow,
    OwnedBooking,
    RankedWindow,
    RequestRow,
    ReservationSnapshot,
    RowFingerprint,
    RowId,
    RowSource,
    RowStatus,
    RuleId,
    RuleNoLongerCoversError,
    StandingRule,
    TransitionRefusedError,
    User,
    UserId,
    UserStatus,
    check_create,
    check_transition,
    derive_account_id,
    is_user_terminal,
    lease_held,
    row_is_frozen,
    rule_row_id,
)
from ..semantics import (
    LEASABLE_STATUSES,
    NOT_FOUND,
    SOFT_AUTH_FAILURE_LIMIT,
    RowIntent,
    becomes_bookable,
    fingerprint_matches,
    ledger_entries,
    new_row,
    outcome_row,
    restorable_rule_row,
    rule_covers_row,
    rule_intent,
    uncovered_reason,
    unleased_write,
    upserted_rule,
    validate_ledger,
)
from ..store import (
    RowLeaseError,
    RowOutcome,
    TenantNotFoundError,
    UniquenessConflictError,
    VersionConflictError,
)
from .documents import (
    GLOBAL_CONTAINER,
    TENANT_CONTAINER,
    AuditRecord,
    ClaimKind,
    ClaimState,
    LoginProbe,
    RuleDayPointer,
    SlotPointer,
    Stored,
    UniquenessClaim,
    booking_doc_id,
    claim_key_hash,
    course_count_claim_key,
    from_account_doc,
    from_booking_doc,
    from_claim_doc,
    from_probe_doc,
    from_row_doc,
    from_rule_doc,
    from_ruleday_doc,
    from_slot_doc,
    from_snapshot_doc,
    from_user_doc,
    identity_claim_key,
    row_doc_id,
    rule_doc_id,
    to_account_doc,
    to_audit_doc,
    to_booking_doc,
    to_claim_doc,
    to_probe_doc,
    to_row_doc,
    to_rule_doc,
    to_ruleday_doc,
    to_slot_doc,
    to_snapshot_doc,
    to_user_doc,
    username_claim_key,
)

log = logging.getLogger(__name__)

CI_CONTAINER_SUFFIX = "-ci"
_ALLOWED_SUFFIXES = frozenset({"", CI_CONTAINER_SUFFIX})
# A connect completes in seconds; a PENDING claim older than this is abandoned (§3.2).
CLAIM_RECLAIM_AFTER = timedelta(minutes=10)
# Optimistic-concurrency retries for read-modify-write paths whose in-memory twin is
# unconditional (watermarks, counters, lease release). Contention here is two writers at most.
_MAX_ATTEMPTS = 5

# Every document path the store's queries filter on. The §3.2 index policy (MU-15b, Bicep) must
# include ALL of them: Cosmos rejects a query that filters on a path excluded from indexing.
# Pinned against the query strings in this module by test_queried_paths_are_declared.
QUERIED_PATHS = frozenset(
    {
        "/type",
        "/accountId",
        "/courseId",
        "/targetDate",
        "/status",
        "/source",
        "/rowId",
        "/ruleId",
        "/active",
        "/userId",
        "/rawReservationId",
        "/oauthProvider",
        "/oauthSubject",
        "/usernameHash",
    }
)

_HTTP_NOT_FOUND = 404
_HTTP_CONFLICT = 409
_HTTP_PRECONDITION_FAILED = 412
_ACCOUNT_DOC_ID = "account"
_SNAPSHOT_DOC_ID = "snapshot"


# --- settings + auth -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CosmosSettings:
    """Where the tenant store lives. There is deliberately no key field: local auth is disabled
    on the account and the store authenticates with an Entra token only (§10.2)."""

    endpoint: str
    database: str
    # "" (every job and the web) or "-ci" (the integration suite's Bicep-owned CI containers).
    container_suffix: str = ""
    # The user-assigned managed identity's client id (ACA sets AZURE_CLIENT_ID); None for a
    # developer's ``az login``.
    managed_identity_client_id: str | None = None

    def __post_init__(self) -> None:
        if self.container_suffix not in _ALLOWED_SUFFIXES:
            raise ValueError(
                f"container suffix must be one of {sorted(_ALLOWED_SUFFIXES)}, "
                f"got {self.container_suffix!r}"
            )
        if self.container_suffix == "-ci" and self.database != "dev":
            # The CI containers exist only in `dev`, and the integration suite EMPTIES them.
            raise ValueError("the -ci containers may only be used with the dev database")

    @property
    def container_names(self) -> tuple[str, str]:
        return (
            f"{TENANT_CONTAINER}{self.container_suffix}",
            f"{GLOBAL_CONTAINER}{self.container_suffix}",
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> CosmosSettings:
        """``TENANT_COSMOS_ENDPOINT`` / ``TENANT_COSMOS_DATABASE`` (default ``dev``) /
        ``TENANT_COSMOS_CONTAINER_SUFFIX`` (default "") / ``AZURE_CLIENT_ID``."""
        endpoint = env.get("TENANT_COSMOS_ENDPOINT")
        if not endpoint:
            raise KeyError("TENANT_COSMOS_ENDPOINT is not set")
        return cls(
            endpoint=endpoint,
            database=env.get("TENANT_COSMOS_DATABASE") or "dev",
            container_suffix=env.get("TENANT_COSMOS_CONTAINER_SUFFIX", ""),
            managed_identity_client_id=env.get("AZURE_CLIENT_ID") or None,
        )


def make_credential(settings: CosmosSettings) -> AsyncTokenCredential:
    """The Entra credential: the managed identity in ACA (``managed_identity_client_id``), a
    developer's ``az login`` locally. Never an account key."""
    return DefaultAzureCredential(managed_identity_client_id=settings.managed_identity_client_id)


class ContainerApi(Protocol):
    """The subset of ``azure.cosmos.aio.ContainerProxy`` the store uses (the unit tests' fake
    implements exactly this)."""

    async def read_item(self, item: str, partition_key: str) -> dict[str, Any]: ...

    async def create_item(self, body: dict[str, Any]) -> dict[str, Any]: ...

    async def upsert_item(self, body: dict[str, Any]) -> dict[str, Any]: ...

    async def replace_item(
        self,
        item: str,
        body: dict[str, Any],
        *,
        etag: str | None = ...,
        match_condition: MatchConditions | None = ...,
    ) -> dict[str, Any]: ...

    async def delete_item(
        self,
        item: str,
        partition_key: str,
        *,
        etag: str | None = ...,
        match_condition: MatchConditions | None = ...,
    ) -> None: ...

    def query_items(
        self,
        query: str,
        *,
        parameters: list[dict[str, object]] | None = ...,
        partition_key: str | None = ...,
    ) -> AsyncIterator[dict[str, Any]]: ...

    async def execute_item_batch(
        self, batch_operations: Sequence[tuple[Any, ...]], partition_key: str
    ) -> list[dict[str, Any]]: ...


class _SdkContainer:
    """``ContainerApi`` over a real ``azure.cosmos.aio.ContainerProxy``: forwards each call with
    the SDK's keyword names and narrows its dict/list subclasses to plain ``dict`` / ``list``."""

    def __init__(self, proxy: ContainerProxy) -> None:
        self._proxy = proxy

    async def read_item(self, item: str, partition_key: str) -> dict[str, Any]:
        return dict(await self._proxy.read_item(item, partition_key))

    async def create_item(self, body: dict[str, Any]) -> dict[str, Any]:
        return dict(await self._proxy.create_item(body))

    async def upsert_item(self, body: dict[str, Any]) -> dict[str, Any]:
        return dict(await self._proxy.upsert_item(body))

    async def replace_item(
        self,
        item: str,
        body: dict[str, Any],
        *,
        etag: str | None = None,
        match_condition: MatchConditions | None = None,
    ) -> dict[str, Any]:
        return dict(
            await self._proxy.replace_item(item, body, etag=etag, match_condition=match_condition)
        )

    async def delete_item(
        self,
        item: str,
        partition_key: str,
        *,
        etag: str | None = None,
        match_condition: MatchConditions | None = None,
    ) -> None:
        await self._proxy.delete_item(
            item, partition_key, etag=etag, match_condition=match_condition
        )

    def query_items(
        self,
        query: str,
        *,
        parameters: list[dict[str, object]] | None = None,
        partition_key: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        if partition_key is None:  # cross-partition (the async SDK's default without a key)
            return self._proxy.query_items(query, parameters=parameters)
        return self._proxy.query_items(query, parameters=parameters, partition_key=partition_key)

    async def execute_item_batch(
        self, batch_operations: Sequence[tuple[Any, ...]], partition_key: str
    ) -> list[dict[str, Any]]:
        return list(await self._proxy.execute_item_batch(batch_operations, partition_key))


@asynccontextmanager
async def cosmos_tenant_store(
    settings: CosmosSettings,
    *,
    course_timezones: Mapping[CourseId, str],
    cutoff: BookingCutoffConfig,
    max_accounts_per_course: int = 8,
    clock: Clock | None = None,
    credential: AsyncTokenCredential | None = None,
) -> AsyncIterator[CosmosTenantStore]:
    """Open a ``CosmosTenantStore`` on ``settings`` (token auth only) and close the client on
    exit. A credential this function created is closed too; a passed one is the caller's."""
    if credential is not None and (
        isinstance(credential, str | Mapping) or not hasattr(credential, "get_token")
    ):
        raise TypeError("the tenant store authenticates with an Entra token credential only")
    owned = credential is None
    cred = credential if credential is not None else make_credential(settings)
    client = CosmosClient(settings.endpoint, credential=cred)
    try:
        database = client.get_database_client(settings.database)
        tenant_name, global_name = settings.container_names
        yield CosmosTenantStore(
            tenant=_SdkContainer(database.get_container_client(tenant_name)),
            global_=_SdkContainer(database.get_container_client(global_name)),
            course_timezones=course_timezones,
            cutoff=cutoff,
            max_accounts_per_course=max_accounts_per_course,
            clock=clock or RealClock(),
            container_names=settings.container_names,
        )
    finally:
        await client.close()
        if owned:
            await cred.close()


# --- batch plumbing --------------------------------------------------------------------------


class _StaleWriteError(TransitionRefusedError):
    """An IfMatch op found a newer document (412) or none (404): re-read and retry."""


class _RowExistsError(TransitionRefusedError):
    """A row create found the deterministic id taken (409)."""


class _RuleDayTakenError(TransitionRefusedError):
    """A ``ruleday|<weekday>`` pointer create found the weekday taken (409)."""


@dataclass(frozen=True, slots=True)
class _Op:
    kind: str  # create | replace | upsert | delete
    role: str  # row | slot | ruleday | ledger
    doc_id: str
    body: dict[str, Any] | None = None
    etag: str | None = None

    def sdk(self) -> tuple[Any, ...]:
        if self.kind == "create":
            return ("create", (self.body,))
        if self.kind == "upsert":
            return ("upsert", (self.body,))
        if self.kind == "replace":
            return ("replace", (self.doc_id, self.body), {"if_match_etag": self.etag})
        return ("delete", (self.doc_id,), {"if_match_etag": self.etag})


@dataclass
class _Batch:
    """One transactional batch in ONE logical partition (the account)."""

    partition: CourseAccountId
    ops: list[_Op] = field(default_factory=list)

    def _check(self, body: Mapping[str, Any]) -> None:
        if body.get("accountId") != str(self.partition):
            raise ValueError(
                f"cross-partition batch op: {body.get('id')!r} is not in {self.partition}"
            )

    def _has(self, doc_id: str) -> bool:
        return any(op.doc_id == doc_id for op in self.ops)

    def create(self, body: dict[str, Any], *, role: str) -> None:
        self._check(body)
        self.ops.append(_Op("create", role, str(body["id"]), body))

    def replace(self, body: dict[str, Any], *, etag: str | None, role: str) -> None:
        self._check(body)
        self.ops.append(_Op("replace", role, str(body["id"]), body, etag))

    def upsert(self, body: dict[str, Any], *, role: str) -> None:
        self._check(body)
        self.ops.append(_Op("upsert", role, str(body["id"]), body))

    def delete(self, doc_id: str, *, etag: str | None, role: str) -> None:
        self.ops.append(_Op("delete", role, doc_id, None, etag))

    def assert_unchanged(self, body: dict[str, Any], *, etag: str | None, role: str) -> None:
        """Assert a document's ETag inside the batch: an IfMatch replace with its own body."""
        if not self._has(str(body["id"])):
            self.replace(body, etag=etag, role=role)


def _status(exc: CosmosHttpResponseError | CosmosBatchOperationError) -> int:
    status = getattr(exc, "status_code", None)
    return int(status) if status is not None else 0


def _params(**values: object) -> list[dict[str, object]]:
    return [{"name": f"@{name}", "value": value} for name, value in values.items()]


def _body(doc: Mapping[str, Any]) -> dict[str, Any]:
    """A read document minus the Cosmos system properties, ready to write back."""
    return {k: v for k, v in doc.items() if not k.startswith("_")}


@dataclass(frozen=True, slots=True)
class _ClaimTicket:
    pk: str
    etag: str | None
    bound: bool
    fresh: bool  # we created / reclaimed it in this call (so a failure must release it)


# --- the store -------------------------------------------------------------------------------


class CosmosTenantStore:
    """``TenantStore`` over two Cosmos containers (see module docstring)."""

    def __init__(
        self,
        *,
        tenant: ContainerApi,
        global_: ContainerApi,
        course_timezones: Mapping[CourseId, str],
        cutoff: BookingCutoffConfig,
        max_accounts_per_course: int = 8,
        clock: Clock | None = None,
        container_names: tuple[str, str] = (TENANT_CONTAINER, GLOBAL_CONTAINER),
    ) -> None:
        self._tenant = tenant
        self._global = global_
        self._course_timezones = dict(course_timezones)
        self._cutoff = cutoff
        self._max_accounts_per_course = max_accounts_per_course
        self._clock: Clock = clock or RealClock()
        self.container_names = container_names
        # Row and rule documents never move partition or id, so where one was seen is cached to
        # turn a cross-partition lookup by id into a point read.
        self._row_home: dict[RowId, tuple[CourseAccountId, str]] = {}
        self._rule_home: dict[RuleId, CourseAccountId] = {}

    # --- introspection (conformance hooks, CI housekeeping) -------------------------------

    async def slot_pointer(self, account_id: CourseAccountId, day: date) -> RowId | None:
        """The ``slot|<date>`` doc's ``activeRowId`` (conformance ``StoreHarness`` hook)."""
        stored = await self._slot(account_id, day)
        return stored.item.active_row_id if stored is not None else None

    async def ruleday_pointer(self, account_id: CourseAccountId, weekday: int) -> RuleId | None:
        """The ``ruleday|<weekday>`` doc's ``activeRuleId`` (conformance ``StoreHarness`` hook)."""
        stored = await self._ruleday(account_id, weekday)
        return stored.item.active_rule_id if stored is not None else None

    async def purge_ci_containers(self) -> None:
        """Delete every document in the ``-ci`` containers (integration-suite isolation). Refused
        on any other container, so it can never empty ``tenant`` / ``global``."""
        if not all(name.endswith(CI_CONTAINER_SUFFIX) for name in self.container_names):
            raise RuntimeError(f"refusing to purge non-CI containers {self.container_names}")
        for container, pk_key in ((self._tenant, "accountId"), (self._global, "pk")):
            async for doc in container.query_items("SELECT * FROM c"):
                await container.delete_item(str(doc["id"]), str(doc[pk_key]))
        self._row_home.clear()
        self._rule_home.clear()

    def course_timezone(self, course_id: CourseId) -> str:
        """The course's timezone; an unconfigured course is a loud ``KeyError``."""
        return self._course_timezones[course_id]

    # --- low-level reads ------------------------------------------------------------------

    @staticmethod
    async def _read(container: ContainerApi, doc_id: str, pk: str) -> dict[str, Any] | None:
        try:
            return await container.read_item(doc_id, pk)
        except CosmosHttpResponseError as exc:
            if _status(exc) == _HTTP_NOT_FOUND:
                return None
            raise

    @staticmethod
    async def _query(
        container: ContainerApi,
        where: str,
        *,
        partition_key: str | None = None,
        **params: object,
    ) -> list[dict[str, Any]]:
        query = f"SELECT * FROM c WHERE {where}"
        items = container.query_items(
            query, parameters=_params(**params), partition_key=partition_key
        )
        return [doc async for doc in items]

    async def _account(self, account_id: CourseAccountId) -> Stored[CourseAccount] | None:
        doc = await self._read(self._tenant, _ACCOUNT_DOC_ID, str(account_id))
        return from_account_doc(doc) if doc is not None else None

    async def _slot(self, account_id: CourseAccountId, day: date) -> Stored[SlotPointer] | None:
        doc = await self._read(self._tenant, f"slot|{day.isoformat()}", str(account_id))
        return from_slot_doc(doc) if doc is not None else None

    async def _ruleday(
        self, account_id: CourseAccountId, weekday: int
    ) -> Stored[RuleDayPointer] | None:
        doc = await self._read(self._tenant, f"ruleday|{weekday}", str(account_id))
        return from_ruleday_doc(doc) if doc is not None else None

    async def _rule_in(
        self, account_id: CourseAccountId, rule_id: RuleId
    ) -> Stored[StandingRule] | None:
        doc = await self._read(self._tenant, f"rule|{rule_id}", str(account_id))
        if doc is None:
            return None
        self._rule_home[rule_id] = account_id
        return from_rule_doc(doc)

    async def _rule_anywhere(self, rule_id: RuleId) -> Stored[StandingRule] | None:
        home = self._rule_home.get(rule_id)
        if home is not None:
            stored = await self._rule_in(home, rule_id)
            if stored is not None:
                return stored
        docs = await self._query(
            self._tenant, "c.type = @type AND c.ruleId = @rule", type="rule", rule=str(rule_id)
        )
        if not docs:
            return None
        stored = from_rule_doc(docs[0])
        self._rule_home[rule_id] = stored.item.course_account_id
        return stored

    async def _stored_rule_of(self, row: RequestRow) -> StandingRule | None:
        """The STORED rule of a rule row, read in the ROW's partition (a rule in another account
        is therefore "missing", which ``rule_covers_row`` / ``uncovered_reason`` treat exactly
        like the in-memory account leg)."""
        if row.rule_id is None:
            return None
        stored = await self._rule_in(row.course_account_id, row.rule_id)
        return stored.item if stored is not None else None

    def _remember_row(self, stored: Stored[RequestRow]) -> Stored[RequestRow]:
        row = stored.item
        self._row_home[row.id] = (row.course_account_id, row_doc_id(row))
        return stored

    async def _row_stored(self, row_id: RowId) -> Stored[RequestRow] | None:
        home = self._row_home.get(row_id)
        if home is not None:
            doc = await self._read(self._tenant, home[1], str(home[0]))
            if doc is not None:
                return self._remember_row(from_row_doc(doc))
        docs = await self._query(
            self._tenant, "c.type = @type AND c.rowId = @row", type="row", row=str(row_id)
        )
        return self._remember_row(from_row_doc(docs[0])) if docs else None

    async def _row_in(
        self, account_id: CourseAccountId, row_id: RowId
    ) -> Stored[RequestRow] | None:
        """A row by id within ONE partition (a slot pointer's target): a single-partition query."""
        found = await self._rows_where(
            "c.rowId = @row", partition_key=str(account_id), row=str(row_id)
        )
        return found[0] if found else None

    async def _row(self, row_id: RowId) -> Stored[RequestRow]:
        stored = await self._row_stored(row_id)
        if stored is None:
            raise TenantNotFoundError(NOT_FOUND)
        return stored

    async def _rows_where(
        self, where: str, *, partition_key: str | None = None, **params: object
    ) -> list[Stored[RequestRow]]:
        docs = await self._query(
            self._tenant,
            f"c.type = @type AND {where}",
            partition_key=partition_key,
            type="row",
            **params,
        )
        return sorted((self._remember_row(from_row_doc(d)) for d in docs), key=lambda s: s.item.id)

    async def _history(self, account_id: CourseAccountId, day: date) -> list[Stored[RequestRow]]:
        return await self._rows_where(
            "c.targetDate = @day", partition_key=str(account_id), day=day.isoformat()
        )

    async def _account_for_user(
        self, account_id: CourseAccountId, user_id: UserId | None
    ) -> CourseAccount:
        stored = await self._account(account_id)
        if stored is None or (user_id is not None and stored.item.user_id != user_id):
            raise TenantNotFoundError(NOT_FOUND)
        return stored.item

    async def _user_accounts(self, user_id: UserId) -> list[CourseAccount]:
        docs = await self._query(
            self._tenant, "c.type = @type AND c.userId = @user", type="account", user=str(user_id)
        )
        return [from_account_doc(d).item for d in docs]

    # --- the batch primitive --------------------------------------------------------------

    async def _execute(self, batch: _Batch) -> None:
        if not batch.ops:
            return
        try:
            await self._tenant.execute_item_batch(
                [op.sdk() for op in batch.ops], partition_key=str(batch.partition)
            )
        except CosmosBatchOperationError as exc:
            index = exc.error_index if isinstance(exc.error_index, int) else -1
            op = batch.ops[index] if 0 <= index < len(batch.ops) else None
            status = _status(exc)
            if op is not None and op.kind == "create" and status == _HTTP_CONFLICT:
                if op.role == "slot":
                    raise TransitionRefusedError(
                        "that date already has an active row for this account"
                    ) from exc
                if op.role == "row":
                    raise _RowExistsError(f"row {op.doc_id} already exists") from exc
                if op.role == "ruleday":
                    raise _RuleDayTakenError(f"{op.doc_id} is taken") from exc
                if op.role == "rule":
                    raise _StaleWriteError(f"{op.doc_id} was created concurrently") from exc
            if status in (_HTTP_PRECONDITION_FAILED, _HTTP_NOT_FOUND):
                what = op.doc_id if op is not None else "a document"
                raise _StaleWriteError(f"{what} changed since it was read") from exc
            raise

    async def _commit(
        self,
        writes: Sequence[tuple[Stored[RequestRow] | None, RequestRow]],
        *,
        extra: Callable[[_Batch], None] | None = None,
    ) -> None:
        """One transactional batch: row creates / IfMatch replaces, the slot ops derived from
        the status changes, the "may become active" guard's pointer assertion and ``extra``."""
        partition = writes[0][1].course_account_id
        batch = _Batch(partition)
        for old, new in writes:
            if new.course_account_id != partition:
                raise ValueError("a batch spans ONE account partition")
            old_row = old.item if old is not None else None
            if new.source is RowSource.RULE and becomes_bookable(old_row, new):
                await self._guard_rule_row_may_become_active(new, batch)
        await self._slot_ops(writes, batch)
        for old, new in writes:
            body = to_row_doc(new)
            if old is None:
                batch.create(body, role="row")
            else:
                batch.replace(body, etag=old.etag, role="row")
        if extra is not None:
            extra(batch)
        await self._execute(batch)
        for _, new in writes:
            self._row_home[new.id] = (new.course_account_id, row_doc_id(new))

    async def _slot_ops(
        self, writes: Sequence[tuple[Stored[RequestRow] | None, RequestRow]], batch: _Batch
    ) -> None:
        """Mirror of the in-memory ``_commit`` slot logic, as create / replace / delete ops."""

        def active(row: RequestRow | None) -> bool:
            return row is not None and row.status in ACTIVE_ROW_STATUSES

        moving = [
            (o.item if o else None, n)
            for o, n in writes
            if active(o.item if o else None) != active(n)
        ]
        if not moving:
            return
        days = sorted({n.target_date for _, n in moving})
        original = {d: await self._slot(batch.partition, d) for d in days}
        slots = {d: (s.item.active_row_id if s else None) for d, s in original.items()}
        for old, new in moving:  # releases first, so a supersede frees the slot it re-points
            if active(old) and not active(new) and slots[new.target_date] == new.id:
                slots[new.target_date] = None
        for old, new in moving:
            if not active(old) and active(new):
                held = slots[new.target_date]
                if held is not None and held != new.id:
                    raise TransitionRefusedError(
                        f"{new.target_date} already has an active row for this account"
                    )
                slots[new.target_date] = new.id
        for day in days:
            before, after = original[day], slots[day]
            if before is None and after is not None:
                batch.create(to_slot_doc(SlotPointer(batch.partition, day, after)), role="slot")
            elif before is not None and after is None:
                batch.delete(f"slot|{day.isoformat()}", etag=before.etag, role="slot")
            elif before is not None and after is not None and before.item.active_row_id != after:
                batch.replace(
                    to_slot_doc(SlotPointer(batch.partition, day, after)),
                    etag=before.etag,
                    role="slot",
                )

    # --- shared guards --------------------------------------------------------------------

    async def _refuse_if_user_terminal(self, account_id: CourseAccountId, day: date) -> None:
        if any(is_user_terminal(s.item) for s in await self._history(account_id, day)):
            raise TransitionRefusedError(
                f"{day} has a user-terminal row; only an explicit re-request reopens it"
            )

    async def _guard_rule_row_may_become_active(self, row: RequestRow, batch: _Batch) -> None:
        """The shared "may become active" guard (round-5 MF1): the stored rule must cover the row
        and the date must have no user-terminal row; the ``ruleday`` pointer naming the rule is
        asserted in the same batch (the rule IfMatch, see module docstring)."""
        rule = await self._stored_rule_of(row)
        if not rule_covers_row(row, rule):
            raise RuleNoLongerCoversError(f"the rule no longer covers {row.target_date}")
        await self._refuse_if_user_terminal(row.course_account_id, row.target_date)
        pointer = await self._ruleday(row.course_account_id, row.target_date.weekday())
        if pointer is None or pointer.item.active_rule_id != row.rule_id:
            raise RuleNoLongerCoversError(f"the rule no longer covers {row.target_date}")
        batch.assert_unchanged(to_ruleday_doc(pointer.item), etag=pointer.etag, role="ruleday")

    async def _stored_rule_matching(self, rule: StandingRule) -> StandingRule:
        """IfMatch on the rule (round-5 MF2 b/c): the caller's copy must be the stored version."""
        stored = await self._rule_in(rule.course_account_id, rule.id)
        if stored is None or stored.item.version != rule.version:
            raise TransitionRefusedError(f"stale rule {rule.id}: re-read and retry")
        return stored.item

    def _new_row(
        self,
        *,
        row_id: RowId,
        account: CourseAccount,
        target_date: date,
        intent: RowIntent,
        status: RowStatus,
        source: RowSource,
        rule_id: RuleId | None,
    ) -> RequestRow:
        return new_row(
            row_id=row_id,
            account=account,
            timezone=self.course_timezone(account.course_id),
            cutoff=self._cutoff,
            target_date=target_date,
            intent=intent,
            status=status,
            source=source,
            rule_id=rule_id,
        )

    # --- TenantStore: lifecycle -----------------------------------------------------------

    async def initialize(self) -> None:
        """Connectivity + data-plane RBAC check: a point read in each container (a missing
        document is a 404 = reachable and authorised; a missing role assignment is a 403)."""
        await self._read(self._tenant, _ACCOUNT_DOC_ID, "initialize-probe")
        await self._read(self._global, "initialize-probe", "initialize-probe")

    # --- booking runner -------------------------------------------------------------------

    async def _event_rows(
        self, stored: Sequence[Stored[RequestRow]], keep: Callable[[RequestRow, bool], bool]
    ) -> list[EventRow]:
        accounts: dict[CourseAccountId, CourseAccount | None] = {}
        out: list[EventRow] = []
        for s in sorted(stored, key=lambda x: x.item.id):
            row = s.item
            if row.course_account_id not in accounts:
                acc = await self._account(row.course_account_id)
                accounts[row.course_account_id] = acc.item if acc is not None else None
            account = accounts[row.course_account_id]
            if account is None or account.status is not AccountStatus.ACTIVE:
                continue
            covered = rule_covers_row(row, await self._stored_rule_of(row))
            if keep(row, covered):
                out.append(EventRow(row=row, account=account))
        return out

    async def load_event_rows(
        self,
        *,
        targets: Mapping[CourseId, date],
        now: datetime,
    ) -> list[EventRow]:
        found: list[Stored[RequestRow]] = []
        for course_id, day in targets.items():
            found += await self._rows_where(
                "c.status = @status AND c.courseId = @course AND c.targetDate = @day",
                status=RowStatus.PENDING.value,
                course=str(course_id),
                day=day.isoformat(),
            )
        # Belt and braces for a non-atomic deactivation (§7.7): never offer the booker a row of
        # an inactive rule, even before the materializer withdrew it.
        return await self._event_rows(found, lambda row, covered: row.cutoff_at > now and covered)

    async def claim_rows(
        self,
        row_ids: Sequence[RowId],
        *,
        owner: str,
        until: datetime,
        now: datetime,
    ) -> frozenset[RowId]:
        claimed: set[RowId] = set()
        for row_id in row_ids:
            stored = await self._row_stored(row_id)
            if stored is None or stored.item.status is not RowStatus.PENDING:
                continue
            row = stored.item
            if lease_held(row, now=now) and row.lease_owner != owner:
                continue
            try:
                await self._commit(
                    [(stored, replace(row, lease_owner=owner, lease_expires_at=until))]
                )
            except _StaleWriteError:
                continue  # another writer got there first: not claimed
            claimed.add(row_id)
        return frozenset(claimed)

    async def record_outcomes(self, outcomes: Sequence[RowOutcome]) -> None:
        errors: list[Exception] = []
        for outcome in outcomes:
            try:
                await self._apply_outcome(outcome)
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise ExceptionGroup(f"record_outcomes: {len(errors)} row(s) not applied", errors)

    async def _apply_outcome(self, o: RowOutcome) -> None:
        stored = await self._row_stored(o.row_id)
        row = stored.item if stored is not None else None
        course_id = row.course_id if row is not None else None
        # Before ANY write: the row and its ledger are one unit (SF3), in ONE partition.
        validate_ledger(o, course_id=course_id)
        if row is not None and row.course_account_id != o.course_account_id:
            raise ValueError(f"outcome for row {row.id} names another account")
        ledger = await self._ledger_writes(o, course_id=course_id)
        try:
            if stored is None or row is None:
                raise TenantNotFoundError(NOT_FOUND)
            new = outcome_row(row, o)

            def add_ledger(batch: _Batch) -> None:
                for entry in ledger:
                    batch.upsert(to_booking_doc(entry), role="ledger")

            await self._commit([(stored, new)], extra=add_ledger)
        except (TransitionRefusedError, RowLeaseError, TenantNotFoundError):
            # The row moved: keep what the bot did (ledger by account + date) and make the
            # date's active row reconcile it on the next watcher run (M4).
            await self._write_ledger_and_flag(o, ledger)
            raise

    async def _ledger_writes(
        self, o: RowOutcome, *, course_id: CourseId | None
    ) -> list[OwnedBooking]:
        """The ledger docs one outcome upserts: its entries, then ``cancelled_upgrade`` applied
        to every entry (stored or just written) for that raw id (the in-memory
        ``_write_ledger``)."""
        final: dict[str, OwnedBooking] = {}
        raw = o.cancelled_upgrade_raw_id
        if raw is not None:
            docs = await self._query(
                self._tenant,
                "c.type = @type AND c.rawReservationId = @raw",
                partition_key=str(o.course_account_id),
                type="booking",
                raw=raw,
            )
            for doc in docs:
                entry = from_booking_doc(doc).item
                final[booking_doc_id(entry)] = entry
        for entry in ledger_entries(o):
            final[booking_doc_id(entry)] = entry
        if raw is not None:
            for doc_id, entry in list(final.items()):
                if entry.raw_reservation_id == raw and (
                    course_id is None or entry.course_id == course_id
                ):
                    final[doc_id] = replace(entry, state=BookingState.CANCELLED_UPGRADE)
        return list(final.values())

    async def _write_ledger_and_flag(self, o: RowOutcome, ledger: Sequence[OwnedBooking]) -> None:
        """The refused-outcome path, one batch: the ledger entries + ``needs_reconcile`` on the
        date's active row (if any). Re-read on a concurrent write."""
        for _ in range(_MAX_ATTEMPTS):
            batch = _Batch(o.course_account_id)
            for entry in ledger:
                batch.upsert(to_booking_doc(entry), role="ledger")
            slot = await self._slot(o.course_account_id, o.target_date)
            if slot is not None:
                active = await self._row_in(o.course_account_id, slot.item.active_row_id)
                if active is not None and not active.item.needs_reconcile:
                    row = active.item
                    flagged = replace(row, needs_reconcile=True, version=row.version + 1)
                    batch.replace(to_row_doc(flagged), etag=active.etag, role="row")
            try:
                await self._execute(batch)
                return
            except _StaleWriteError:
                continue
        raise TransitionRefusedError(f"could not flag {o.target_date} for reconcile: contention")

    # --- leases ---------------------------------------------------------------------------

    async def acquire_row_lease(
        self,
        row_id: RowId,
        *,
        owner: str,
        until: datetime,
        now: datetime,
        expected: RowFingerprint | None,
    ) -> bool:
        stored = await self._row(row_id)
        row = stored.item
        if row.status not in LEASABLE_STATUSES:
            return False  # nothing is ever booked, upgraded or cancelled from any other status
        if lease_held(row, now=now) and row.lease_owner != owner:
            return False
        if not fingerprint_matches(row, expected):
            return False
        try:
            await self._commit([(stored, replace(row, lease_owner=owner, lease_expires_at=until))])
        except _StaleWriteError:
            return False  # a 412: another writer got there first (§3.5)
        return True

    async def release_row_lease(self, row_id: RowId, *, owner: str) -> None:
        for _ in range(_MAX_ATTEMPTS):
            stored = await self._row(row_id)
            row = stored.item
            if row.lease_owner != owner:
                return
            try:
                await self._commit(
                    [(stored, replace(row, lease_owner=None, lease_expires_at=None))]
                )
                return
            except _StaleWriteError:
                continue
        log.warning("cosmos store: release of row %s lease lost to contention", row_id)

    # --- watcher --------------------------------------------------------------------------

    async def load_watch_rows(
        self,
        *,
        horizons: Mapping[CourseId, tuple[date, date]],
        now: datetime,
    ) -> list[EventRow]:
        found: list[Stored[RequestRow]] = []
        for course_id, (first, last) in horizons.items():
            found += await self._rows_where(
                "c.courseId = @course AND c.targetDate >= @first AND c.targetDate <= @last"
                " AND ARRAY_CONTAINS(@statuses, c.status)",
                course=str(course_id),
                first=first.isoformat(),
                last=last.isoformat(),
                statuses=[RowStatus.PENDING.value, RowStatus.BOOKED.value],
            )

        def keep(row: RequestRow, covered: bool) -> bool:
            # A pending row of an INACTIVE rule is never offered (round-3 MF1); held bookings are
            # watched regardless; needs_reconcile rows stay watched (round-6 SF2, §7.6).
            live_pending = (
                row.status is RowStatus.PENDING
                and not row_is_frozen(row, now=now)
                and (covered or row.needs_reconcile)
            )
            return live_pending or row.status is RowStatus.BOOKED

        return await self._event_rows(found, keep)

    async def finalize_lost(self, *, now: datetime) -> list[RequestRow]:
        lost: list[RequestRow] = []
        pending = await self._rows_where("c.status = @status", status=RowStatus.PENDING.value)
        for stored in pending:
            row = stored.item
            if not row_is_frozen(row, now=now) or lease_held(row, now=now):
                continue  # a leased row: an actor is mid-act; the next finalizer pass gets it
            rule = await self._stored_rule_of(row)
            try:
                if not rule_covers_row(row, rule):
                    # The user switched the rule off, deleted it or moved its weekday: a system
                    # withdraw, never a "lost" email.
                    reason = uncovered_reason(row, rule)
                    check_transition(
                        row, RowStatus.WITHDRAWN, actor=Actor.MATERIALIZER, now=now, reason=reason
                    )
                    new = replace(
                        unleased_write(row, now),
                        status=RowStatus.WITHDRAWN,
                        status_reason=reason,
                        version=row.version + 1,
                    )
                    await self._commit([(stored, new)])
                    continue
                check_transition(row, RowStatus.LOST, actor=Actor.WATCHER, now=now)
                new = replace(
                    unleased_write(row, now),
                    status=RowStatus.LOST,
                    status_reason="cutoff" if now >= row.cutoff_at else "date_passed",
                    version=row.version + 1,
                )
                await self._commit([(stored, new)])
            except _StaleWriteError:
                log.info("cosmos store: row %s moved during finalize; next pass", row.id)
                continue
            lost.append(new)
        return lost

    async def rows_no_longer_covered(self, *, now: datetime) -> list[RequestRow]:
        candidates = await self._rows_where(
            "c.source = @source AND ARRAY_CONTAINS(@statuses, c.status)",
            source=RowSource.RULE.value,
            statuses=[RowStatus.PENDING.value, RowStatus.SUPERSEDED.value],
        )
        out: list[RequestRow] = []
        for stored in candidates:
            row = stored.item
            if row.needs_reconcile or lease_held(row, now=now):
                continue  # the watcher reconciles it first (round-6 SF2) / leased: next tick
            if not rule_covers_row(row, await self._stored_rule_of(row)):
                out.append(row)
        return out

    async def get_snapshot(self, account_id: CourseAccountId) -> ReservationSnapshot | None:
        doc = await self._read(self._tenant, _SNAPSHOT_DOC_ID, str(account_id))
        return from_snapshot_doc(doc).item if doc is not None else None

    async def save_snapshot(self, snapshot: ReservationSnapshot) -> None:
        await self._tenant.upsert_item(to_snapshot_doc(snapshot))

    async def list_owned_bookings(
        self, account_id: CourseAccountId, *, target_date: date
    ) -> list[OwnedBooking]:
        docs = await self._query(
            self._tenant,
            "c.type = @type AND c.targetDate = @day",
            partition_key=str(account_id),
            type="booking",
            day=target_date.isoformat(),
        )
        entries = [from_booking_doc(d).item for d in docs]
        return sorted(entries, key=lambda e: (e.tee_time, e.raw_reservation_id))

    async def set_upgrade_marker(
        self, row_id: RowId, *, owner: str, at: datetime, expected: RowFingerprint
    ) -> bool:
        stored = await self._row(row_id)
        row = stored.item
        if row.lease_owner != owner or not lease_held(row, now=at):
            return False
        if not fingerprint_matches(row, expected):
            return False
        try:
            await self._commit([(stored, replace(row, upgrade_started_at=at))])
        except _StaleWriteError:
            return False
        return True

    async def record_soft_auth_failure(self, account_id: CourseAccountId) -> int:
        for _ in range(_MAX_ATTEMPTS):
            stored = await self._account(account_id)
            if stored is None:
                raise TenantNotFoundError(NOT_FOUND)
            account = stored.item
            count = account.consecutive_soft_auth_failures + 1
            status = (
                AccountStatus.AUTH_FAILED if count >= SOFT_AUTH_FAILURE_LIMIT else account.status
            )
            updated = replace(account, consecutive_soft_auth_failures=count, status=status)
            if await self._replace_if_match(
                self._tenant, _ACCOUNT_DOC_ID, to_account_doc(updated), stored.etag
            ):
                return count
        raise VersionConflictError(f"account {account_id}: soft-auth counter contention")

    @staticmethod
    async def _replace_if_match(
        container: ContainerApi, doc_id: str, body: dict[str, Any], etag: str | None
    ) -> bool:
        """IfMatch replace; False on a 412 (the caller re-reads)."""
        try:
            await container.replace_item(
                doc_id, body, etag=etag, match_condition=MatchConditions.IfNotModified
            )
        except CosmosHttpResponseError as exc:
            if _status(exc) == _HTTP_PRECONDITION_FAILED:
                return False
            raise
        return True

    # --- materializer ---------------------------------------------------------------------

    async def rules_needing_materialization(self, *, through: date) -> list[StandingRule]:
        docs = await self._query(
            self._tenant, "c.type = @type AND c.active = @active", type="rule", active=True
        )
        rules = [from_rule_doc(d).item for d in docs]
        due = (
            r for r in rules if r.materialized_through is None or r.materialized_through < through
        )
        return sorted(due, key=lambda r: r.id)

    async def get_rule_unscoped(self, rule_id: RuleId) -> StandingRule | None:
        stored = await self._rule_anywhere(rule_id)
        return stored.item if stored is not None else None

    async def get_account_unscoped(self, account_id: CourseAccountId) -> CourseAccount | None:
        stored = await self._account(account_id)
        return stored.item if stored is not None else None

    async def get_user_unscoped(self, user_id: UserId) -> User | None:
        """SYSTEM read (the booking runner mails a row's user by id); the web never calls it."""
        stored = await self._user(user_id)
        return stored.item if stored is not None else None

    async def rows_for_account_date(
        self, account_id: CourseAccountId, target_date: date
    ) -> list[RequestRow]:
        return [s.item for s in await self._history(account_id, target_date)]

    async def insert_rule_row_if_absent(
        self, rule: StandingRule, target_date: date, *, now: datetime
    ) -> RequestRow | None:
        account = await self._account_for_user(rule.course_account_id, None)
        rule = await self._stored_rule_matching(rule)
        if target_date.weekday() != rule.weekday:
            raise ValueError(f"{target_date} is not on the rule's weekday ({rule.weekday})")
        if not rule.active:
            raise TransitionRefusedError(f"rule {rule.id} is inactive")
        row_id = rule_row_id(rule.id, target_date)
        history = await self._history(account.id, target_date)
        if any(s.item.id == row_id for s in history):
            return None  # a row EXISTS; the caller consults the history (round-2 M1)
        if any(is_user_terminal(s.item) for s in history):
            raise TransitionRefusedError(
                f"{target_date} has a user-terminal row; only an explicit re-request reopens it"
            )
        slot_held = await self._slot(account.id, target_date) is not None
        row = self._new_row(
            row_id=row_id,
            account=account,
            target_date=target_date,
            intent=rule_intent(rule),
            status=RowStatus.SUPERSEDED if slot_held else RowStatus.PENDING,
            source=RowSource.RULE,
            rule_id=rule.id,
        )
        check_create(row, actor=Actor.MATERIALIZER, now=now)

        # A SUPERSEDED create skips the bookable guard; the row is still created under this rule,
        # so the rule's weekday claim (its ``ruleday`` pointer) is asserted in the batch anyway.
        pointer = await self._ruleday(account.id, rule.weekday)
        if pointer is None or pointer.item.active_rule_id != rule.id:
            raise TransitionRefusedError(f"stale rule {rule.id}: re-read and retry")
        pointer_doc = to_ruleday_doc(pointer.item)

        def assert_pointer(batch: _Batch) -> None:
            batch.assert_unchanged(pointer_doc, etag=pointer.etag, role="ruleday")

        try:
            await self._commit([(None, row)], extra=assert_pointer)
        except _RowExistsError:
            return None
        return row

    async def reactivate_rule_row(
        self, row: RequestRow, rule: StandingRule, *, now: datetime
    ) -> RequestRow:
        stored = await self._row(row.id)
        # Round-5: back to the pre-supersede status if it was superseded before the withdraw.
        target = stored.item.superseded_from or RowStatus.PENDING
        check_transition(row, target, actor=Actor.MATERIALIZER, now=now)
        if row.rule_id != rule.id:
            raise ValueError(f"row {row.id} does not belong to rule {rule.id}")
        rule = await self._stored_rule_matching(rule)
        if not rule.active:
            raise TransitionRefusedError(f"rule {rule.id} is inactive")
        await self._refuse_if_user_terminal(row.course_account_id, row.target_date)
        if lease_held(stored.item, now=now):
            raise RowLeaseError(f"row {row.id} is leased by {stored.item.lease_owner!r}")
        if stored.item != row:
            raise TransitionRefusedError(f"row {row.id} changed since it was read")
        new = replace(
            unleased_write(row, now),
            status=target,
            status_reason=None,
            superseded_from=None,
            options=rule.options,
            party_size=rule.party_size,
            max_price=rule.max_price,
            group_id=rule.group_id,
            group_rank=rule.group_rank,
            version=row.version + 1,
        )
        await self._commit([(stored, new)])
        return new

    async def rewrite_pending_rule_row(
        self,
        row_id: RowId,
        *,
        rule: StandingRule,
        expected_version: int,
        now: datetime,
    ) -> RequestRow:
        stored = await self._row(row_id)
        current = stored.item
        if current.version != expected_version:
            raise TransitionRefusedError(f"row {row_id} changed since it was read")
        if current.source is not RowSource.RULE or current.rule_id != rule.id:
            raise TransitionRefusedError("only this rule's own rule row can be rewritten")
        if current.status is not RowStatus.PENDING:
            raise TransitionRefusedError(
                f"only a pending row is rewritten (row is {current.status})"
            )
        if row_is_frozen(current, now=now):
            raise TransitionRefusedError(f"{current.target_date} is frozen (cutoff or date passed)")
        if lease_held(current, now=now):
            raise RowLeaseError(f"booking in progress for {current.target_date}")
        rule = await self._stored_rule_matching(rule)
        new = replace(
            unleased_write(current, now),
            options=rule.options,
            party_size=rule.party_size,
            max_price=rule.max_price,
            group_id=rule.group_id,
            group_rank=rule.group_rank,
            version=current.version + 1,
        )
        # pending -> pending does not pass becomes_bookable, so the guard is applied here.
        guard = _Batch(current.course_account_id)
        await self._guard_rule_row_may_become_active(current, guard)

        def assert_guard(batch: _Batch) -> None:
            for op in guard.ops:
                assert op.body is not None
                batch.assert_unchanged(op.body, etag=op.etag, role=op.role)

        await self._commit([(stored, new)], extra=assert_guard)
        return new

    async def _update_rule(
        self, rule_id: RuleId, change: Callable[[StandingRule], StandingRule | None]
    ) -> None:
        """Read-modify-IfMatch-replace a rule (``change`` returns None for "no write")."""
        for _ in range(_MAX_ATTEMPTS):
            stored = await self._rule_anywhere(rule_id)
            if stored is None:
                raise TenantNotFoundError(NOT_FOUND)
            updated = change(stored.item)
            if updated is None:
                return
            if await self._replace_if_match(
                self._tenant, rule_doc_id(updated), to_rule_doc(updated), stored.etag
            ):
                return
        raise VersionConflictError(f"rule {rule_id}: watermark contention")

    async def set_materialized_through(self, rule_id: RuleId, through: date) -> None:
        def advance(rule: StandingRule) -> StandingRule | None:
            if rule.materialized_through is not None and rule.materialized_through >= through:
                return None  # never moves backwards (§3.2)
            return replace(rule, materialized_through=through)

        await self._update_rule(rule_id, advance)

    async def reset_materialized_through(self, rule_id: RuleId) -> None:
        await self._update_rule(rule_id, lambda r: replace(r, materialized_through=None))

    # --- web ------------------------------------------------------------------------------

    async def get_user_by_subject(self, provider: str, subject: str) -> User | None:
        docs = await self._query(
            self._global,
            "c.type = @type AND c.oauthProvider = @provider AND c.oauthSubject = @subject",
            type="user",
            provider=provider,
            subject=subject,
        )
        users = sorted((from_user_doc(d).item for d in docs), key=lambda u: u.id)
        return users[0] if users else None

    async def _user(self, user_id: UserId) -> Stored[User] | None:
        pk = f"user:{user_id}"
        doc = await self._read(self._global, pk, pk)
        return from_user_doc(doc) if doc is not None else None

    async def _user_holds(self, owner_id: UUID, key_hash: str) -> bool:
        stored = await self._user(UserId(owner_id))
        if stored is None or stored.item.oauth_subject is None:
            return False
        user = stored.item
        key = identity_claim_key(user.oauth_provider, user.oauth_subject or "")
        return claim_key_hash(ClaimKind.IDENTITY, key) == key_hash

    async def _account_holds(self, owner_id: UUID, key_hash: str) -> bool:
        stored = await self._account(CourseAccountId(owner_id))
        if stored is None:
            return False
        key = username_claim_key(stored.item.course_id, stored.item.username)
        return claim_key_hash(ClaimKind.USERNAME, key) == key_hash

    async def upsert_user(self, user: User) -> None:
        existing = await self._user(user.id)
        old_key = (
            identity_claim_key(existing.item.oauth_provider, existing.item.oauth_subject)
            if existing is not None and existing.item.oauth_subject is not None
            else None
        )
        if user.oauth_subject is None:
            await self._global.upsert_item(to_user_doc(user))
        else:
            key = identity_claim_key(user.oauth_provider, user.oauth_subject)
            ticket = await self._acquire_claim(
                ClaimKind.IDENTITY,
                key,
                owner=user.id,
                holds=self._user_holds,
                taken="that sign-in identity is bound to another user",
            )
            await self._global.upsert_item(to_user_doc(user))
            if not await self._bind(ClaimKind.IDENTITY, key, ticket, owner=user.id):
                if existing is not None:
                    await self._global.upsert_item(to_user_doc(existing.item))
                else:
                    await self._delete_quietly(self._global, f"user:{user.id}", f"user:{user.id}")
                raise UniquenessConflictError("that sign-in identity is bound to another user")
            if old_key == key:
                return
        if old_key is not None:
            await self._release_claim(ClaimKind.IDENTITY, old_key, owner=user.id)

    async def bind_invited_user(self, *, email: str, provider: str, subject: str) -> User | None:
        existing = await self.get_user_by_subject(provider, subject)
        if existing is not None:
            return existing
        wanted = email.casefold()
        docs = await self._query(
            self._global,
            "c.type = @type AND c.status = @status",
            type="user",
            status=UserStatus.INVITED.value,
        )
        invited = sorted((from_user_doc(d).item for d in docs), key=lambda u: u.id)
        for user in invited:
            if user.email.casefold() == wanted:
                bound = replace(
                    user, oauth_provider=provider, oauth_subject=subject, status=UserStatus.ACTIVE
                )
                await self.upsert_user(bound)
                return bound
        return None

    async def list_rows_for_user(
        self, user_id: UserId, *, from_date: date, to_date: date
    ) -> list[RequestRow]:
        rows: list[RequestRow] = []
        for account in await self._user_accounts(user_id):
            found = await self._rows_where(
                "c.targetDate >= @first AND c.targetDate <= @last",
                partition_key=str(account.id),
                first=from_date.isoformat(),
                last=to_date.isoformat(),
            )
            rows += [s.item for s in found]
        return sorted(rows, key=lambda r: (r.target_date, r.id))

    async def get_account(
        self, account_id: CourseAccountId, *, user_id: UserId
    ) -> CourseAccount | None:
        stored = await self._account(account_id)
        if stored is None or stored.item.user_id != user_id:
            return None
        return stored.item

    async def get_row(self, row_id: RowId, *, user_id: UserId) -> RequestRow | None:
        stored = await self._row_stored(row_id)
        if stored is None:
            return None
        account = await self._account(stored.item.course_account_id)
        if account is None or account.item.user_id != user_id:
            return None
        return stored.item

    async def list_accounts_for_user(self, user_id: UserId) -> list[CourseAccount]:
        return sorted(await self._user_accounts(user_id), key=lambda a: str(a.course_id))

    async def list_rules_for_user(self, user_id: UserId) -> list[StandingRule]:
        rules: list[StandingRule] = []
        for account in await self._user_accounts(user_id):
            docs = await self._query(
                self._tenant, "c.type = @type", partition_key=str(account.id), type="rule"
            )
            rules += [from_rule_doc(d).item for d in docs]
        return sorted(rules, key=lambda r: (r.weekday, str(r.id)))

    async def upsert_account(self, account: CourseAccount) -> None:
        if account.id != derive_account_id(account.user_id, account.course_id):
            raise UniquenessConflictError("account id is not derive_account_id(user, course)")
        existing = await self._account(account.id)
        key = username_claim_key(account.course_id, account.username)
        old_key = (
            username_claim_key(existing.item.course_id, existing.item.username)
            if existing is not None
            else None
        )
        ticket = await self._acquire_claim(
            ClaimKind.USERNAME,
            key,
            owner=account.id,
            holds=self._account_holds,
            taken="that course login is already connected",
        )
        counted = False
        try:
            if existing is None:
                await self._adjust_course_count(account.course_id, +1)
                counted = True
        except UniquenessConflictError:
            if ticket.fresh:
                await self._release_claim(ClaimKind.USERNAME, key, owner=account.id)
            raise
        created = await self._write_account(account, existing)
        if counted and not created:
            await self._adjust_course_count(account.course_id, -1)  # a racing create won
        if not await self._bind(ClaimKind.USERNAME, key, ticket, owner=account.id):
            # §3.2 step 3: the claim was reclaimed under us; roll back what we wrote.
            if existing is not None:
                await self._tenant.upsert_item(to_account_doc(existing.item))
            else:
                await self._delete_quietly(self._tenant, _ACCOUNT_DOC_ID, str(account.id))
                if counted and created:
                    await self._adjust_course_count(account.course_id, -1)
            raise UniquenessConflictError("that course login is already connected")
        if old_key is not None and old_key != key:
            await self._release_claim(ClaimKind.USERNAME, old_key, owner=account.id)

    async def _write_account(
        self, account: CourseAccount, existing: Stored[CourseAccount] | None
    ) -> bool:
        """Write the account doc; True iff this call CREATED it."""
        body = to_account_doc(account)
        if existing is None:
            try:
                await self._tenant.create_item(body)
                return True
            except CosmosHttpResponseError as exc:
                if _status(exc) != _HTTP_CONFLICT:
                    raise
        await self._tenant.upsert_item(body)
        return False

    # --- claims (§3.2) --------------------------------------------------------------------

    async def _acquire_claim(
        self,
        kind: ClaimKind,
        key: str,
        *,
        owner: UUID,
        holds: Callable[[UUID, str], Awaitable[bool]],
        taken: str,
    ) -> _ClaimTicket:
        """Step 1: create the claim PENDING for ``owner`` (or find it already ours, or reclaim
        an abandoned one). ``UniquenessConflictError`` when another owner holds it."""
        key_hash = claim_key_hash(kind, key)
        pk = f"claim:{key_hash}"
        for _ in range(_MAX_ATTEMPTS):
            doc = await self._read(self._global, pk, pk)
            now = self._clock.now_utc()
            if doc is None:
                claim = UniquenessClaim(kind, key_hash, ClaimState.PENDING, now, owner_id=owner)
                try:
                    created = await self._global.create_item(to_claim_doc(claim))
                except CosmosHttpResponseError as exc:
                    if _status(exc) == _HTTP_CONFLICT:
                        continue  # created under us: re-read who holds it
                    raise
                return _ClaimTicket(pk, created.get("_etag"), bound=False, fresh=True)
            stored = from_claim_doc(doc)
            claim = stored.item
            if claim.owner_id == owner:
                bound = claim.state is ClaimState.BOUND
                return _ClaimTicket(pk, stored.etag, bound=bound, fresh=False)
            if not await self._reclaimable(claim, holds, now=now):
                raise UniquenessConflictError(taken)
            reclaimed = replace(claim, state=ClaimState.PENDING, created_at=now, owner_id=owner)
            try:
                written = await self._global.replace_item(
                    pk,
                    to_claim_doc(reclaimed),
                    etag=stored.etag,
                    match_condition=MatchConditions.IfNotModified,
                )
            except CosmosHttpResponseError as exc:
                if _status(exc) in (_HTTP_PRECONDITION_FAILED, _HTTP_NOT_FOUND):
                    continue
                raise
            log.info("cosmos store: reclaimed an abandoned %s claim", kind.value)
            return _ClaimTicket(pk, written.get("_etag"), bound=False, fresh=True)
        raise UniquenessConflictError(taken)

    async def _reclaimable(
        self,
        claim: UniquenessClaim,
        holds: Callable[[UUID, str], Awaitable[bool]],
        *,
        now: datetime,
    ) -> bool:
        """PENDING: older than ``CLAIM_RECLAIM_AFTER`` AND its owner does not hold the key.
        BOUND: only when the owner EXISTS and holds a different key (a rename orphan); a bound
        claim whose owner is missing stays taken (accounts are never deleted today)."""
        if claim.owner_id is None:
            return False
        if claim.state is ClaimState.PENDING:
            old = now - claim.created_at > CLAIM_RECLAIM_AFTER
            return old and not await holds(claim.owner_id, claim.key_hash)
        owner_exists = (
            await self._user(UserId(claim.owner_id)) is not None
            if claim.kind is ClaimKind.IDENTITY
            else await self._account(CourseAccountId(claim.owner_id)) is not None
        )
        return owner_exists and not await holds(claim.owner_id, claim.key_hash)

    async def _bind(self, kind: ClaimKind, key: str, ticket: _ClaimTicket, *, owner: UUID) -> bool:
        """Step 3: IfMatch the claim to BOUND. False on a 412 / 404 (it was reclaimed)."""
        if ticket.bound:
            return True
        claim = UniquenessClaim(
            kind,
            claim_key_hash(kind, key),
            ClaimState.BOUND,
            self._clock.now_utc(),
            owner_id=owner,
        )
        try:
            return await self._replace_if_match(
                self._global, ticket.pk, to_claim_doc(claim), ticket.etag
            )
        except CosmosHttpResponseError as exc:
            if _status(exc) == _HTTP_NOT_FOUND:
                return False
            raise

    async def _release_claim(self, kind: ClaimKind, key: str, *, owner: UUID) -> None:
        """Delete ``owner``'s claim on ``key`` (a rename's old key, or a failed connect). Best
        effort: a leftover is an orphan the reclaim rule recovers."""
        pk = f"claim:{claim_key_hash(kind, key)}"
        doc = await self._read(self._global, pk, pk)
        if doc is None or from_claim_doc(doc).item.owner_id != owner:
            return
        try:
            await self._global.delete_item(
                pk, pk, etag=doc.get("_etag"), match_condition=MatchConditions.IfNotModified
            )
        except CosmosHttpResponseError:
            log.warning("cosmos store: could not release a %s claim; left as orphan", kind.value)

    async def _adjust_course_count(self, course_id: CourseId, delta: int) -> None:
        """The ``max_accounts_per_course`` IfMatch counter (a soft cap; a lost race retries)."""
        key_hash = claim_key_hash(ClaimKind.COURSE_COUNT, course_count_claim_key(course_id))
        pk = f"claim:{key_hash}"
        for _ in range(_MAX_ATTEMPTS):
            doc = await self._read(self._global, pk, pk)
            now = self._clock.now_utc()
            if doc is None:
                if delta < 0:
                    return
                counter = UniquenessClaim(
                    ClaimKind.COURSE_COUNT, key_hash, ClaimState.BOUND, now, count=delta
                )
                try:
                    await self._global.create_item(to_claim_doc(counter))
                    return
                except CosmosHttpResponseError as exc:
                    if _status(exc) == _HTTP_CONFLICT:
                        continue
                    raise
            stored = from_claim_doc(doc)
            count = stored.item.count
            if delta > 0 and count >= self._max_accounts_per_course:
                raise UniquenessConflictError(
                    f"max_accounts_per_course ({self._max_accounts_per_course}) reached"
                )
            updated = replace(stored.item, count=max(0, count + delta))
            if await self._replace_if_match(self._global, pk, to_claim_doc(updated), stored.etag):
                return
        raise UniquenessConflictError("max_accounts_per_course counter contention; retry")

    @staticmethod
    async def _delete_quietly(container: ContainerApi, doc_id: str, pk: str) -> None:
        try:
            await container.delete_item(doc_id, pk)
        except CosmosHttpResponseError as exc:
            if _status(exc) != _HTTP_NOT_FOUND:
                raise

    # --- web: rows ------------------------------------------------------------------------

    async def create_explicit_row(
        self,
        *,
        user_id: UserId,
        account_id: CourseAccountId,
        target_date: date,
        options: tuple[RankedWindow, ...],
        party_size: int,
        now: datetime,
        max_price: Decimal | None = None,
        group_id: UUID | None = None,
        group_rank: int | None = None,
    ) -> RequestRow:
        account = await self._account_for_user(account_id, user_id)
        row = self._new_row(
            row_id=RowId(uuid4()),
            account=account,
            target_date=target_date,
            intent=RowIntent(
                options=options,
                party_size=party_size,
                max_price=max_price,
                group_id=group_id,
                group_rank=group_rank,
            ),
            status=RowStatus.PENDING,
            source=RowSource.EXPLICIT,
            rule_id=None,
        )
        check_create(row, actor=Actor.WEB, now=now)
        writes: list[tuple[Stored[RequestRow] | None, RequestRow]] = [(None, row)]
        slot = await self._slot(account_id, target_date)
        if slot is not None:
            found = await self._row_in(account_id, slot.item.active_row_id)
            if found is None:
                raise TransitionRefusedError(f"{target_date}: slot names a missing row")
            holder_stored = found
            holder = holder_stored.item
            if holder.source is not RowSource.RULE or holder.status is RowStatus.BOOKED:
                raise TransitionRefusedError(
                    f"{target_date} already has a {holder.status} {holder.source} row"
                )
            check_transition(holder, RowStatus.SUPERSEDED, actor=Actor.WEB, now=now)
            if lease_held(holder, now=now):
                raise RowLeaseError(f"booking in progress for {target_date}")
            superseded = replace(
                unleased_write(holder, now),
                status=RowStatus.SUPERSEDED,
                superseded_from=holder.status,
                version=holder.version + 1,
            )
            writes.insert(0, (holder_stored, superseded))
        await self._commit(writes)
        return row

    async def transition_row(
        self,
        row_id: RowId,
        *,
        user_id: UserId | None,
        to: RowStatus,
        actor: Actor,
        reason: str | None,
        now: datetime,
    ) -> RequestRow:
        stored = await self._row(row_id)
        row = stored.item
        if actor is Actor.WEB and user_id is None:
            raise TenantNotFoundError(NOT_FOUND)
        await self._account_for_user(row.course_account_id, user_id)
        if actor not in (Actor.WEB, Actor.MATERIALIZER):
            raise TransitionRefusedError(f"{actor} writes through record_outcomes (leased path)")
        if to is RowStatus.SUPERSEDED:
            raise TransitionRefusedError("supersede is written only by create_explicit_row")
        if to is RowStatus.CANCELLED:
            raise TransitionRefusedError("booked -> cancelled is written only by record_outcomes")
        if row.status is RowStatus.WITHDRAWN and to in (RowStatus.PENDING, RowStatus.SKIPPED):
            raise TransitionRefusedError("reactivation is written only by reactivate_rule_row")
        check_transition(row, to, actor=actor, now=now, reason=reason)
        if lease_held(row, now=now):
            raise RowLeaseError(f"booking in progress for {row.target_date}")
        new = replace(
            unleased_write(row, now),
            status=to,
            status_reason=reason,
            # Round-5: a system withdraw KEEPS the pre-supersede status for reactivation.
            superseded_from=row.superseded_from if to is RowStatus.WITHDRAWN else None,
            version=row.version + 1,
        )
        writes: list[tuple[Stored[RequestRow] | None, RequestRow]] = [(stored, new)]
        if row.source is RowSource.EXPLICIT and to is RowStatus.WITHDRAWN:
            restore = await self._restore_for(row, now)
            if restore is not None:
                writes.append(restore)
        await self._commit(writes)
        return new

    async def _restore_for(
        self, explicit: RequestRow, now: datetime
    ) -> tuple[Stored[RequestRow], RequestRow] | None:
        history = await self._history(explicit.course_account_id, explicit.target_date)
        rules: dict[RuleId, StandingRule] = {}
        for s in history:
            rule = await self._stored_rule_of(s.item)
            if rule is not None:
                rules[rule.id] = rule
        restore = restorable_rule_row(history=[s.item for s in history], rules=rules, now=now)
        if restore is None:
            return None
        old, new = restore
        assert old is not None  # a restore always rewrites an existing row
        (stored,) = [s for s in history if s.item.id == old.id]
        return (stored, new)

    async def upsert_rule(self, rule: StandingRule, *, user_id: UserId) -> StandingRule:
        await self._account_for_user(rule.course_account_id, user_id)
        for _ in range(_MAX_ATTEMPTS):
            stored = await self._rule_in(rule.course_account_id, rule.id)
            if stored is None and await self._rule_anywhere(rule.id) is not None:
                raise TenantNotFoundError(NOT_FOUND)  # the id is another account's rule
            existing = stored.item if stored is not None else None
            if existing is not None and rule.version != existing.version:
                raise VersionConflictError(
                    f"rule edited from version {rule.version}; stored is {existing.version}"
                )
            result = upserted_rule(rule, existing)
            batch = _Batch(rule.course_account_id)
            await self._ruleday_ops(existing, result, batch)
            if stored is None:
                batch.create(to_rule_doc(result), role="rule")
            else:
                batch.replace(to_rule_doc(result), etag=stored.etag, role="rule")
            try:
                await self._execute(batch)
            except _StaleWriteError:
                continue  # a concurrent write to the rule or its pointer: re-read (412)
            except _RuleDayTakenError as exc:
                raise RuleConflictError(
                    f"account already has an active rule for weekday {result.weekday}"
                ) from exc
            self._rule_home[result.id] = result.course_account_id
            return result
        raise VersionConflictError(f"rule {rule.id}: contention; re-read and retry")

    async def _ruleday_ops(
        self, existing: StandingRule | None, rule: StandingRule, batch: _Batch
    ) -> None:
        """The ``ruleday|<weekday>`` pointer ops of one rule upsert (round-3 SF1): release the
        old weekday's pointer if it named the rule, claim the new one if the rule is active."""
        account = rule.course_account_id
        keys = {rule.weekday} | ({existing.weekday} if existing is not None else set())
        before = {wd: await self._ruleday(account, wd) for wd in keys}
        after = {wd: (p.item.active_rule_id if p else None) for wd, p in before.items()}
        if existing is not None and existing.active and after[existing.weekday] == rule.id:
            after[existing.weekday] = None
        if rule.active:
            held = after[rule.weekday]
            if held is not None and held != rule.id:
                raise RuleConflictError(
                    f"account already has an active rule for weekday {rule.weekday}"
                )
            after[rule.weekday] = rule.id
        for wd in sorted(keys):
            old, new = before[wd], after[wd]
            if old is None and new is not None:
                batch.create(to_ruleday_doc(RuleDayPointer(account, wd, new)), role="ruleday")
            elif old is not None and new is None:
                batch.delete(f"ruleday|{wd}", etag=old.etag, role="ruleday")
            elif old is not None and new is not None and old.item.active_rule_id != new:
                batch.replace(
                    to_ruleday_doc(RuleDayPointer(account, wd, new)), etag=old.etag, role="ruleday"
                )

    # --- probes + audit -------------------------------------------------------------------

    async def count_login_probes(
        self, *, user_id: UserId | None, username_hash: str | None, since: datetime
    ) -> int:
        where = ["c.type = @type"]
        params: dict[str, Any] = {"type": "probe"}
        if user_id is not None:
            where.append("c.userId = @user")
            params["user"] = str(user_id)
        if username_hash is not None:
            where.append("c.usernameHash = @hash")
            params["hash"] = username_hash
        docs = await self._query(self._global, " AND ".join(where), **params)
        return sum(1 for d in docs if from_probe_doc(d).item.at >= since)

    async def record_login_probe(
        self, *, user_id: UserId, course_id: CourseId, username_hash: str, ok: bool, at: datetime
    ) -> None:
        probe = LoginProbe(
            id=uuid4(),
            user_id=user_id,
            course_id=course_id,
            username_hash=username_hash,
            ok=ok,
            at=at,
        )
        await self._global.create_item(to_probe_doc(probe))

    async def append_audit(
        self,
        *,
        user_id: UserId | None,
        action: str,
        row_id: RowId | None,
        detail: Mapping[str, object],
        at: datetime,
    ) -> None:
        record = AuditRecord(
            id=uuid4(),
            user_id=user_id,
            action=action,
            row_id=row_id,
            detail=redact_payload(detail),
            at=at,
        )
        await self._global.create_item(to_audit_doc(record))
