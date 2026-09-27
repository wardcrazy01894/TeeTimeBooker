"""The collaborators every tenant command shares (MULTIUSER_PLAN §10.1/§10.2, MU-16a).

``open_tenant_store`` is the ONE tenant-store builder behind ``tenant-run``, ``tenant-plan``,
``tenant-watch``, ``tenant-migrate`` and ``web``:

- ``TENANT_COSMOS_ENDPOINT`` set -> a ``CosmosTenantStore`` (Entra / managed-identity auth via
  ``AZURE_CLIENT_ID``; the client is closed when the context exits). ``TENANT_COSMOS_DATABASE``
  is then REQUIRED — ``CosmosSettings.from_env`` would default it to ``dev``, and a prod job that
  lost the variable must not silently read and write the dev data.
- nothing set -> today's in-memory store with a loud WARNING (local runs keep working).
- anything in between (a database or container suffix with no endpoint, an invalid suffix) -> a
  ``TenantStoreConfigError`` before anything is opened. compute.bicep always sets
  ``TENANT_COSMOS_DATABASE`` in tenant mode, so a tenant job whose endpoint param was left empty
  fails closed instead of exiting 0 over an empty store.

``user_notifier_from_env`` is the users' mail path for the watcher and the web: ACS when
``ACS_EMAIL_CONNECTION`` + ``ACS_EMAIL_SENDER`` are set, else a logging stand-in (kind + row id
only). The booker keeps its own SF6 rule (``booking_job.operator_sink_from_env``: an unconfigured
mail path fails every send so misses can never hide).
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager

from ..core.clock import Clock
from ..core.config import BookingCutoffConfig
from ..core.models import CourseId
from ..core.release_policy import ReleasePolicy
from .acs_email import AcsConfigError, AcsEmailClient, load_acs_settings
from .booking_job import HOSTED_COURSES, StoreUserNotifier
from .cosmos.store import CosmosSettings, cosmos_tenant_store
from .in_memory_store import InMemoryTenantStore
from .notify import UserEvent, UserNotifier
from .store import TenantStore

log = logging.getLogger(__name__)

__all__ = [
    "COSMOS_CONTAINER_SUFFIX_ENV",
    "COSMOS_DATABASE_ENV",
    "COSMOS_ENDPOINT_ENV",
    "BookingCutoffConfig",
    "LoggingUserNotifier",
    "TenantStoreConfigError",
    "hosted_course_timezones",
    "hosted_policies",
    "open_tenant_store",
    "tenant_store_settings_from_env",
    "user_notifier_from_env",
]

COSMOS_ENDPOINT_ENV = "TENANT_COSMOS_ENDPOINT"
COSMOS_DATABASE_ENV = "TENANT_COSMOS_DATABASE"
COSMOS_CONTAINER_SUFFIX_ENV = "TENANT_COSMOS_CONTAINER_SUFFIX"
_CLIENT_ID_ENV = "AZURE_CLIENT_ID"


class TenantStoreConfigError(ValueError):
    """The tenant-store environment is half-configured (or durable storage is required and
    absent). The message names env vars, never a value."""


def hosted_policies() -> dict[CourseId, ReleasePolicy]:
    """Every hosted course's ``ReleasePolicy`` (the same ``HOSTED_COURSES`` the booker uses)."""
    return {cid: cls.release_policy for cid, cls in HOSTED_COURSES.items()}


def hosted_course_timezones() -> dict[CourseId, str]:
    return {cid: policy.timezone for cid, policy in hosted_policies().items()}


def tenant_store_settings_from_env(env: Mapping[str, str]) -> CosmosSettings | None:
    """``None`` = no durable store configured (in-memory). Raises ``TenantStoreConfigError`` on
    a half-configured environment. An empty value counts as unset (ACA passes '' for an empty
    bicep param)."""

    def value(name: str) -> str:
        return env.get(name, "").strip()

    endpoint = value(COSMOS_ENDPOINT_ENV)
    database = value(COSMOS_DATABASE_ENV)
    suffix = value(COSMOS_CONTAINER_SUFFIX_ENV)
    if not endpoint:
        stray = [
            n
            for n, v in ((COSMOS_DATABASE_ENV, database), (COSMOS_CONTAINER_SUFFIX_ENV, suffix))
            if v
        ]
        if stray:
            raise TenantStoreConfigError(
                f"{COSMOS_ENDPOINT_ENV} is not set but {', '.join(stray)} is: the tenant store "
                "is half-configured (refusing to fall back to an empty in-memory store)"
            )
        return None
    if not database:
        raise TenantStoreConfigError(
            f"{COSMOS_ENDPOINT_ENV} is set but {COSMOS_DATABASE_ENV} is not: name the database "
            "explicitly (dev or prod)"
        )
    try:
        return CosmosSettings(
            endpoint=endpoint,
            database=database,
            container_suffix=suffix,
            managed_identity_client_id=value(_CLIENT_ID_ENV) or None,
        )
    except ValueError as exc:
        raise TenantStoreConfigError(f"invalid tenant store settings: {exc}") from exc


@asynccontextmanager
async def open_tenant_store(
    env: Mapping[str, str] | None = None,
    *,
    command: str,
    cutoff: BookingCutoffConfig | None = None,
    clock: Clock | None = None,
    require_durable: bool = False,
) -> AsyncIterator[TenantStore]:
    """The tenant store for one command run (see the module docstring). ``require_durable``
    refuses the in-memory fallback (``tenant-migrate``: migrating nothing is not a success)."""
    source: Mapping[str, str] = os.environ if env is None else env
    settings = tenant_store_settings_from_env(source)
    effective_cutoff = cutoff if cutoff is not None else BookingCutoffConfig()
    zones = hosted_course_timezones()
    if settings is None:
        if require_durable:
            raise TenantStoreConfigError(
                f"{COSMOS_ENDPOINT_ENV} is not set: {command} needs the durable tenant store"
            )
        log.warning(
            "teetime %s: tenant store is IN-MEMORY (%s unset) — no rows survive this process; "
            "set %s + %s to use Cosmos",
            command,
            COSMOS_ENDPOINT_ENV,
            COSMOS_ENDPOINT_ENV,
            COSMOS_DATABASE_ENV,
        )
        yield InMemoryTenantStore(course_timezones=zones, cutoff=effective_cutoff)
        return
    # The Azure SDK logs every HTTP request's headers at INFO: noise that buries the job's own
    # lines in Log Analytics. Warnings and errors still come through.
    logging.getLogger("azure").setLevel(logging.WARNING)
    log.info(
        "teetime %s: tenant store is Cosmos %s database=%s containers=%s",
        command,
        settings.endpoint,
        settings.database,
        ",".join(settings.container_names),
    )
    async with cosmos_tenant_store(
        settings, course_timezones=zones, cutoff=effective_cutoff, clock=clock
    ) as store:
        yield store


class LoggingUserNotifier:
    """``UserNotifier`` for an unconfigured mail path: one INFO line per event, kind + row id
    only (no PII)."""

    async def send(self, event: UserEvent) -> None:
        log.info("tenant: notify %s for row %s (email not configured)", event.kind, event.row_id)


def user_notifier_from_env(
    directory: TenantStore,
    env: Mapping[str, str] | None = None,
    *,
    command: str,
) -> UserNotifier:
    """ACS-backed ``StoreUserNotifier`` when ACS is configured (``load_acs_settings`` registers
    the access key as an E7 literal), else ``LoggingUserNotifier`` with a WARNING naming the
    missing env var."""
    source: Mapping[str, str] = os.environ if env is None else env
    try:
        settings = load_acs_settings(source)
    except AcsConfigError as exc:
        log.warning(
            "teetime %s: user email is NOT configured (%s); events are logged", command, exc
        )
        return LoggingUserNotifier()
    client = AcsEmailClient(settings.connection, sender_address=settings.sender_address)
    return StoreUserNotifier(directory, client)
