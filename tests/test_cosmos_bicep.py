"""MU-15b (MULTIUSER_PLAN §10.2/§10.5): the shared Cosmos DB account in `rg-teetime-shared`.

Static assertions over `infra/bicep/modules/cosmos.bicep` (Bicep is not importable; CI's
`az bicep build` is the compile gate). They pin the cost and security invariants the plan relies
on: the free tier, no account keys, a hard account-wide throughput cap, two 400 RU/s databases,
the container partition keys and TTLs, an index policy that covers EVERY path the store filters
on, and no data-plane role assignment in Bicep (the operator creates those by hand).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from teetime.tenant.cosmos.documents import (
    CI_CONTAINER_DEFAULT_TTL_S,
    GLOBAL_CONTAINER,
    GLOBAL_PARTITION_KEY_PATH,
    TENANT_CONTAINER,
)
from teetime.tenant.cosmos.store import QUERIED_PATHS

BICEP_DIR = Path(__file__).resolve().parent.parent / "infra" / "bicep"
COSMOS_BICEP = BICEP_DIR / "modules" / "cosmos.bicep"
FREE_TIER_RU = 1000


@pytest.fixture(scope="module")
def bicep() -> str:
    return COSMOS_BICEP.read_text()


def _block(bicep: str, start: str) -> str:
    """The text from ``start`` to the matching closing brace (resources are one block each)."""
    i = bicep.index(start)
    depth = 0
    for j in range(i, len(bicep)):
        if bicep[j] == "{":
            depth += 1
        elif bicep[j] == "}":
            depth -= 1
            if depth == 0:
                return bicep[i : j + 1]
    raise AssertionError(f"unterminated block {start!r}")


def test_cosmos_free_tier_and_local_auth_disabled(bicep: str) -> None:
    """§10.2: free tier (only settable at creation), NO account keys, Strong consistency, and an
    account-wide cap so a mis-edit above the free tier cannot even be provisioned (r2 SF6)."""
    assert "enableFreeTier: true" in bicep
    assert "disableLocalAuth: true" in bicep
    assert f"totalThroughputLimit: {FREE_TIER_RU}" in bicep
    assert "defaultConsistencyLevel: 'Strong'" in bicep
    assert "name: 'cosmos-teetime-shared'" in bicep or "'cosmos-teetime-shared'" in bicep


def test_cosmos_two_databases_400ru_each_le_1000(bicep: str) -> None:
    """Two shared-throughput databases, 400 RU/s each (the shared-database minimum), inside the
    1000 RU/s free tier."""
    throughputs = [int(t) for t in re.findall(r"throughput:\s*(\d+)", bicep)]
    assert throughputs == [400, 400]
    assert sum(throughputs) <= FREE_TIER_RU
    for db in ("prod", "dev"):
        assert f"name: '{db}'" in bicep


def test_cosmos_containers_partition_keys(bicep: str) -> None:
    """§3.1: ``tenant`` by ``/accountId``; ``global`` by the prefixed ``/pk`` — the same paths
    the store's documents are written with."""
    assert f"'{TENANT_CONTAINER}'" in bicep
    assert f"'{GLOBAL_CONTAINER}'" in bicep
    assert "'/accountId'" in bicep
    assert f"'{GLOBAL_PARTITION_KEY_PATH}'" in bicep


def test_global_container_has_ttl_on_without_a_default(bicep: str) -> None:
    """Probe (2 h) and audit (400 d) docs carry a per-item ``ttl``; that only works when the
    container's default TTL is -1 (on, no default). The tenant container never expires docs."""
    assert "defaultTtl: -1" in bicep


def test_cosmos_ci_containers_dev_only(bicep: str) -> None:
    """``tenant-ci`` / ``global-ci`` exist ONLY in the dev database, with a 7-day container TTL
    that sweeps anything a crashed integration run left behind (§10.2 Testing)."""
    ci = _block(bicep, "resource devCiContainers")
    assert "devDb" in ci
    assert "prodDb" not in ci
    assert f"defaultTtl: {CI_CONTAINER_DEFAULT_TTL_S}" in ci
    assert "'-ci'" in bicep


def test_index_policy_covers_every_queried_path(bicep: str) -> None:
    """MU-8b: Cosmos rejects a filter on an unindexed path, so the index policy must include
    EXACTLY the paths the store's queries filter on (``QUERIED_PATHS``), everything else
    excluded (cheaper writes). A new query path fails here until the Bicep is updated."""
    start = bicep.index("var indexedPaths = [")
    listed = re.findall(r"'([^']+)'", bicep[start : bicep.index("]", start)])
    assert set(listed) == {f"{p}/?" for p in QUERIED_PATHS}
    assert "path: '/*'" in bicep  # the exclusion
    assert "excludedPaths" in bicep


def test_no_bicep_creates_cosmos_sql_role_assignments() -> None:
    """§10.5 (r2 SF3): data-plane role assignments are created by the operator by hand, never by
    a deploy, so no module may declare one."""
    for path in BICEP_DIR.rglob("*.bicep"):
        assert "databaseAccounts/sqlRoleAssignments" not in path.read_text(), path.name


def test_cosmos_is_deployed_standalone_not_from_main() -> None:
    """Like the shared ACR, the account lives in rg-teetime-shared and is deployed on its own;
    the per-env main.bicep (auto-deployed by CI) never creates it."""
    assert "cosmos.bicep" not in (BICEP_DIR / "main.bicep").read_text()
