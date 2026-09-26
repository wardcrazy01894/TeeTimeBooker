"""An in-process fake of the async ``azure-cosmos`` ``ContainerProxy`` surface MU-8b uses.

It fakes the SDK BOUNDARY, never the store: ``CosmosTenantStore`` runs unmodified on top of it.
Faithful where the store's correctness depends on it:

- ``id`` is unique per logical partition; every write mints a new ``_etag``;
- ``replace_item`` / ``delete_item`` honour ``etag`` + ``MatchConditions.IfNotModified`` (412);
- a create of an existing id is 409, a read/replace/delete of a missing id is 404, raised as the
  REAL SDK exception types;
- ``execute_item_batch`` is all-or-nothing within ONE partition, honours per-op
  ``if_match_etag``, and raises the real ``CosmosBatchOperationError`` (``error_index`` +
  ``status_code``) on the first failing op; an op for another partition is refused (400);
- documents round-trip through JSON, so a non-JSON value is caught here, not in production.

Queries: a tiny subset of the Cosmos SQL grammar, exactly the shapes the store issues:
``SELECT * FROM c WHERE <term> AND <term> ...`` with terms ``c.<key> = @p``, ``c.<key> >= @p``,
``c.<key> <= @p`` and ``ARRAY_CONTAINS(@p, c.<key>)``. Anything else raises, so a new query shape
fails loudly in unit tests instead of silently matching everything.

Race injection: ``before_write`` (a callable run once before the next write or batch) lets a
test change a document between the store's read and its conditional write.
"""

from __future__ import annotations

import copy
import itertools
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from azure.core import MatchConditions
from azure.cosmos.exceptions import (
    CosmosAccessConditionFailedError,
    CosmosBatchOperationError,
    CosmosHttpResponseError,
    CosmosResourceExistsError,
    CosmosResourceNotFoundError,
)

_TERM_CMP = re.compile(r"^c\.(\w+)\s*(=|>=|<=)\s*@(\w+)$")
_TERM_IN = re.compile(r"^ARRAY_CONTAINS\(@(\w+),\s*c\.(\w+)\)$")
_SELECT = re.compile(r"^SELECT \* FROM c WHERE (.+)$", re.DOTALL)
_etags = itertools.count(1)


@dataclass
class BatchCall:
    partition_key: str
    operations: list[tuple[Any, ...]]


@dataclass
class FakeContainer:
    """One container, partitioned by ``pk_field`` (``accountId`` or ``pk``)."""

    pk_field: str
    docs: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    batches: list[BatchCall] = field(default_factory=list)
    queries: list[tuple[str, str | None]] = field(default_factory=list)
    before_write: Callable[[], Awaitable[None]] | None = None

    # --- helpers ------------------------------------------------------------------------------

    async def _hook(self) -> None:
        hook, self.before_write = self.before_write, None
        if hook is not None:
            await hook()

    def _pk_of(self, body: Mapping[str, Any]) -> str:
        value = body.get(self.pk_field)
        if not isinstance(value, str):
            raise CosmosHttpResponseError(status_code=400, message="missing partition key")
        return value

    @staticmethod
    def _stamp(body: Mapping[str, Any]) -> dict[str, Any]:
        doc: dict[str, Any] = json.loads(json.dumps(dict(body)))  # JSON-only, like the wire
        doc["_etag"] = f'"etag-{next(_etags)}"'
        return doc

    @staticmethod
    def _check_etag(
        stored: Mapping[str, Any], etag: str | None, match: MatchConditions | None
    ) -> None:
        if match is MatchConditions.IfNotModified and stored.get("_etag") != etag:
            raise CosmosAccessConditionFailedError(status_code=412, message="precondition failed")

    # --- point operations -----------------------------------------------------------------------

    async def read_item(self, item: str, partition_key: str) -> dict[str, Any]:
        doc = self.docs.get((partition_key, item))
        if doc is None:
            raise CosmosResourceNotFoundError(status_code=404, message="not found")
        return copy.deepcopy(doc)

    async def create_item(self, body: dict[str, Any]) -> dict[str, Any]:
        await self._hook()
        key = (self._pk_of(body), body["id"])
        if key in self.docs:
            raise CosmosResourceExistsError(status_code=409, message="conflict")
        self.docs[key] = self._stamp(body)
        return copy.deepcopy(self.docs[key])

    async def upsert_item(self, body: dict[str, Any]) -> dict[str, Any]:
        await self._hook()
        key = (self._pk_of(body), body["id"])
        self.docs[key] = self._stamp(body)
        return copy.deepcopy(self.docs[key])

    async def replace_item(
        self,
        item: str,
        body: dict[str, Any],
        *,
        etag: str | None = None,
        match_condition: MatchConditions | None = None,
    ) -> dict[str, Any]:
        await self._hook()
        key = (self._pk_of(body), item)
        stored = self.docs.get(key)
        if stored is None:
            raise CosmosResourceNotFoundError(status_code=404, message="not found")
        self._check_etag(stored, etag, match_condition)
        self.docs[key] = self._stamp(body)
        return copy.deepcopy(self.docs[key])

    async def delete_item(
        self,
        item: str,
        partition_key: str,
        *,
        etag: str | None = None,
        match_condition: MatchConditions | None = None,
    ) -> None:
        await self._hook()
        stored = self.docs.get((partition_key, item))
        if stored is None:
            raise CosmosResourceNotFoundError(status_code=404, message="not found")
        self._check_etag(stored, etag, match_condition)
        del self.docs[(partition_key, item)]

    # --- queries ------------------------------------------------------------------------------

    def query_items(
        self,
        query: str,
        *,
        parameters: list[dict[str, object]] | None = None,
        partition_key: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        self.queries.append((query, partition_key))
        params = {str(p["name"]).lstrip("@"): p["value"] for p in parameters or []}
        predicates = _parse(query, params)
        matches = [
            copy.deepcopy(doc)
            for (pk, _), doc in sorted(self.docs.items())
            if (partition_key is None or pk == partition_key) and all(p(doc) for p in predicates)
        ]

        async def gen() -> AsyncIterator[dict[str, Any]]:
            for doc in matches:
                yield doc

        return gen()

    # --- transactional batch ----------------------------------------------------------------

    async def execute_item_batch(
        self, batch_operations: Sequence[tuple[Any, ...]], partition_key: str
    ) -> list[dict[str, Any]]:
        await self._hook()
        self.batches.append(BatchCall(partition_key, [tuple(op) for op in batch_operations]))
        staged = dict(self.docs)
        results: list[dict[str, Any]] = []
        for index, op in enumerate(batch_operations):
            status = self._stage(staged, op, partition_key)
            if status >= 300:
                raise CosmosBatchOperationError(
                    error_index=index,
                    headers={},
                    status_code=status,
                    message=f"batch op {index} failed with {status}",
                    operation_responses=[{"statusCode": status}],
                )
            results.append({"statusCode": status})
        self.docs = staged
        return results

    def _stage(  # noqa: PLR0911 - one return per SDK status the fake models
        self, staged: dict[tuple[str, str], dict[str, Any]], op: tuple[Any, ...], pk: str
    ) -> int:
        kind, args = op[0], op[1]
        kwargs: dict[str, Any] = op[2] if len(op) > 2 else {}
        if_match = kwargs.get("if_match_etag")
        if kind in ("create", "upsert"):
            body = args[0]
            if self._pk_of(body) != pk:
                raise CosmosHttpResponseError(status_code=400, message="cross-partition batch op")
            key = (pk, body["id"])
            if kind == "create" and key in staged:
                return 409
            staged[key] = self._stamp(body)
            return 201
        if kind == "replace":
            item, body = args
            if self._pk_of(body) != pk:
                raise CosmosHttpResponseError(status_code=400, message="cross-partition batch op")
            key = (pk, item)
            if key not in staged:
                return 404
            if if_match is not None and staged[key].get("_etag") != if_match:
                return 412
            staged[key] = self._stamp(body)
            return 200
        if kind == "delete":
            key = (pk, args[0])
            if key not in staged:
                return 404
            if if_match is not None and staged[key].get("_etag") != if_match:
                return 412
            del staged[key]
            return 204
        raise AssertionError(f"unsupported batch op {kind!r}")


def _parse(query: str, params: Mapping[str, object]) -> list[Callable[[Mapping[str, Any]], bool]]:
    if query.strip() == "SELECT * FROM c":
        return []
    select = _SELECT.match(query.strip())
    if select is None:
        raise AssertionError(f"unsupported query shape: {query!r}")
    predicates: list[Callable[[Mapping[str, Any]], bool]] = []
    for raw in select.group(1).split(" AND "):
        term = raw.strip()
        if (cmp := _TERM_CMP.match(term)) is not None:
            key, op, name = cmp.groups()
            predicates.append(_compare(key, op, params[name]))
        elif (contains := _TERM_IN.match(term)) is not None:
            name, key = contains.groups()
            values = params[name]
            assert isinstance(values, list)
            predicates.append(lambda d, k=key, vs=values: d.get(k) in vs)
        else:
            raise AssertionError(f"unsupported query term: {term!r}")
    return predicates


def _compare(key: str, op: str, value: object) -> Callable[[Mapping[str, Any]], bool]:
    def pred(doc: Mapping[str, Any]) -> bool:
        if key not in doc:
            return False
        got = doc[key]
        if op == "=":
            return bool(got == value)
        if got is None or type(got) is not type(value):
            return False
        return bool(got >= value) if op == ">=" else bool(got <= value)

    return pred
