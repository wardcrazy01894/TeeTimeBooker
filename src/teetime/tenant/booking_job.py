"""The wiring behind ``teetime tenant-run`` / ``teetime tenant-plan`` (MULTIUSER_PLAN §4.2,
§4.5, §8.7; MU-9b — UNWIRED to infra: no ACA job runs it until MU-15a, and the CLI's tenant
store is in-memory until MU-16).

``run_booking_job`` is the job, in §4.2 order: NTP offset (wait path only) -> keyring + config
from the environment (a failure is systemic: exit non-zero, operator told) ->
``runner.run_release_event`` (DST gate, READ #1, claim, decrypt, then the ASYNC pool factory:
the site-key pre-flight GET once per course and a coordinated ``SharedCaptchaPool`` bound to
the real 2captcha provider, then the race and WRITE #2) -> the operator summary and the users'
emails -> ``runner.exit_code_for``. ``plan_booking_event`` is ``tenant-plan``: the rows and the
allocation with no ForeUP call.

Environment (names only; values come from Key Vault secret refs in the job, MU-15a):
``TENANT_CREDS_KEYRING`` (required), ``TWOCAPTCHA_API_KEY`` (required unless dry-run),
``ACS_EMAIL_CONNECTION`` + ``ACS_EMAIL_SENDER`` + ``OPERATOR_NOTIFY_EMAIL`` (the operator
summary; when any is missing every send fails, so a run with anything to report exits non-zero
— SF6: an unconfigured mail path must never hide misses).
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from datetime import timedelta
from typing import Protocol

from ..core.adapter import CourseAdapter
from ..core.clock import Clock, RealClock, measure_ntp_offset
from ..core.models import CourseId
from ..core.redaction import register_secret_literals
from ..core.release_policy import ReleasePolicy
from ..courses.foreup.captcha import make_2captcha_provider, resolve_invisible_site_key
from ..courses.foreup.mangrove_bay import MANGROVE_BAY_COURSE_ID, MangroveBayAdapter
from ..courses.foreup.token_pool import LeaseKey, SharedCaptchaPool
from .acs_email import AcsConfigError, AcsEmailClient, load_acs_settings
from .crypto import KEYRING_ENV_VAR, Keyring, KeyringError, load_keyring_from_env
from .models import CourseAccount, User, UserId, UserStatus
from .notify import EmailMessage, EmailSender, EmailSendResult, EmailUserNotifier, UserEvent
from .runner import (
    EventPlan,
    OperatorSink,
    ReleaseEvent,
    RunReport,
    exit_code_for,
    finish_run,
    plan_release_event,
    run_release_event,
    tenant_scheduler,
)
from .store import TenantStore

log = logging.getLogger(__name__)

TWOCAPTCHA_API_KEY_ENV = "TWOCAPTCHA_API_KEY"
OPERATOR_NOTIFY_EMAIL_ENV = "OPERATOR_NOTIFY_EMAIL"
# The operator summary must land before the replica timeout (the runner leaves ~60 s).
_ACS_POLL_TIMEOUT_S = 30.0

# Hosted courses (§6.2): the adapter class per course id. Its ``release_policy`` ClassVar is
# the single source of the release instant.
HOSTED_COURSES: Mapping[CourseId, type[MangroveBayAdapter]] = {
    MANGROVE_BAY_COURSE_ID: MangroveBayAdapter,
}


def _event(key: str, *courses: CourseId) -> ReleaseEvent:
    policy = HOSTED_COURSES[courses[0]].release_policy
    return ReleaseEvent(
        key=key,
        timezone=policy.timezone,
        release_time=policy.release_time,
        course_ids=courses,
    )


# One entry per distinct (timezone, release_time). MU-15a mirrors this in
# ``infra/bicep/release_events.json`` and parity-tests it.
RELEASE_EVENTS: Mapping[str, ReleaseEvent] = {
    "mb0600et": _event("mb0600et", MANGROVE_BAY_COURSE_ID),
}


def event_for(key: str) -> ReleaseEvent:
    try:
        return RELEASE_EVENTS[key]
    except KeyError:
        raise ValueError(
            f"unknown release event {key!r}; known: {', '.join(sorted(RELEASE_EVENTS))}"
        ) from None


def policies_for(event: ReleaseEvent) -> dict[CourseId, ReleasePolicy]:
    return {cid: HOSTED_COURSES[cid].release_policy for cid in event.course_ids}


class HostedPoolFactory:
    """The runner's (async) pool factory: the site-key pre-flight GET ONCE per course (the
    existing best-effort ``resolve_invisible_site_key``, which falls back to the hardcoded key),
    then one coordinated ``SharedCaptchaPool`` whose provider is the real 2captcha solver bound
    to that course's booking page + live site key (§4.2)."""

    def __init__(
        self,
        *,
        clock: Clock,
        api_key: str,
        resolve: Callable[[str], Awaitable[str]] = resolve_invisible_site_key,
    ) -> None:
        self._clock = clock
        self._api_key = api_key
        self._resolve = resolve
        self.site_keys: dict[CourseId, str] = {}

    async def __call__(self, course_id: CourseId) -> SharedCaptchaPool:
        page_url = HOSTED_COURSES[course_id].booking_page_url
        site_key = await self._resolve(page_url)
        self.site_keys[course_id] = site_key
        return SharedCaptchaPool(
            provider=make_2captcha_provider(self._api_key, page_url, site_key),
            clock=self._clock,
            course_id=course_id,
        )


class HostedAdapterFactory:
    """The runner's ``AdapterFactory``: one real adapter per account (own httpx client, cookies,
    login cache). Live: a 2captcha provider (with a pool injected it is only the on/off switch —
    every solve uses the pool's provider) + the course's shared pool and this row's lease key.
    Dry-run (or no key): no provider, so nothing is ever solved."""

    def __init__(self, *, api_key: str | None) -> None:
        self._api_key = api_key

    def __call__(
        self,
        *,
        course_id: CourseId,
        account: CourseAccount,
        pool: SharedCaptchaPool | None,
        lease_key: LeaseKey | None,
        dry_run: bool,
    ) -> CourseAdapter:
        cls = HOSTED_COURSES[course_id]
        provider = (
            None
            if dry_run or self._api_key is None
            else make_2captcha_provider(self._api_key, cls.booking_page_url)
        )
        return cls(captcha_provider=provider, captcha_pool=pool, captcha_lease_key=lease_key)


class UnconfiguredEmailSender:
    """``EmailSender`` for a missing mail configuration: every send FAILS (never raises), so
    ``deliver_operator_summary`` turns a run with anything to report non-zero (SF6)."""

    def __init__(self, reason: str) -> None:
        self.reason = reason

    async def send(self, message: EmailMessage) -> EmailSendResult:
        return EmailSendResult(ok=False, status="unconfigured", error=self.reason)


def operator_sink_from_env(env: Mapping[str, str] | None = None) -> OperatorSink:
    """ACS (``load_acs_settings``, which registers the access key as an E7 literal) + the
    ``OPERATOR_NOTIFY_EMAIL`` recipient; anything missing -> an ``UnconfiguredEmailSender`` and
    a loud WARNING naming the env var (never a value)."""
    source: Mapping[str, str] = os.environ if env is None else env
    to = source.get(OPERATOR_NOTIFY_EMAIL_ENV, "").strip()
    try:
        settings = load_acs_settings(source)
    except AcsConfigError as exc:
        reason = str(exc)
    else:
        if to:
            client = AcsEmailClient(
                settings.connection,
                sender_address=settings.sender_address,
                poll_timeout_s=_ACS_POLL_TIMEOUT_S,
            )
            return OperatorSink(sender=client, to=to)
        reason = f"{OPERATOR_NOTIFY_EMAIL_ENV} is not set"
    log.warning(
        "tenant-run: operator email is NOT configured (%s): a run with anything to report will "
        "exit non-zero",
        reason,
    )
    return OperatorSink(sender=UnconfiguredEmailSender(reason), to=to or "(unset)")


class _UserDirectory(Protocol):
    async def get_user_unscoped(self, user_id: UserId) -> User | None: ...


class StoreUserNotifier:
    """The runner's ``UserNotifier``: looks each event's user up (a post-race system read) and
    mails them through an ``EmailUserNotifier`` bound to that ONE user. An unknown or disabled
    user is skipped with a log line (ids only)."""

    def __init__(
        self,
        directory: _UserDirectory,
        sender: EmailSender,
        *,
        course_labels: Mapping[CourseId, str] | None = None,
    ) -> None:
        self._directory = directory
        self._sender = sender
        self._labels = dict(course_labels or {})

    async def send(self, event: UserEvent) -> None:
        user = await self._directory.get_user_unscoped(event.user_id) if event.user_id else None
        if user is None or user.status is UserStatus.DISABLED:
            log.warning(
                "tenant-run: row %s: no active user %s to notify (%s)",
                event.row_id,
                event.user_id,
                event.kind.value,
            )
            return
        notifier = EmailUserNotifier(self._sender, user=user, course_labels=self._labels)
        await notifier.send(event)


async def run_booking_job(
    *,
    event_key: str,
    dry_run: bool,
    wait: bool,
    store: TenantStore,
    env: Mapping[str, str] | None = None,
    operator: OperatorSink | None = None,
    measure_offset: Callable[[], timedelta] = measure_ntp_offset,
    resolve_site_key: Callable[[str], Awaitable[str]] = resolve_invisible_site_key,
    clock: Clock | None = None,
) -> int:
    """One ``tenant-run`` execution; returns the process exit code (§4.5)."""
    source: Mapping[str, str] = os.environ if env is None else env
    event = event_for(event_key)
    # §4.2: the NTP offset first, and only on the race path (meaningless off it).
    offset = measure_offset() if wait else timedelta(0)
    run_clock = clock or RealClock(offset=offset)
    if wait:
        log.info(
            "tenant-run: real-timing path (--wait); release %s %s, NTP offset_ms=%.1f",
            event.release_time,
            event.timezone,
            offset.total_seconds() * 1000.0,
        )
    sink = operator or operator_sink_from_env(source)
    notifier = StoreUserNotifier(store, sink.sender)
    failure, keyring, api_key = _load_config(source, dry_run=dry_run)
    if failure is not None or keyring is None:
        report = RunReport(
            event_key=event.key,
            rows_loaded=0,
            rows_claimed=0,
            outcomes=(),
            systemic_error=failure,
        )
        log.critical("tenant-run %s: systemic failure at startup (%s)", event.key, failure)
        report = await finish_run(report, (), notifier=notifier, operator=sink, clock=run_clock)
        return int(exit_code_for(report))
    report = await run_release_event(
        event=event,
        policies=policies_for(event),
        store=store,
        clock=run_clock,
        scheduler=tenant_scheduler(),
        keyring=keyring,
        adapter_factory=HostedAdapterFactory(api_key=None if dry_run else api_key),
        notifier=notifier,
        dry_run=dry_run,
        wait=wait,
        pool_factory=(
            None
            if dry_run or api_key is None
            else HostedPoolFactory(clock=run_clock, api_key=api_key, resolve=resolve_site_key)
        ),
        operator=sink,
    )
    return int(exit_code_for(report))


def _load_config(
    source: Mapping[str, str], *, dry_run: bool
) -> tuple[str | None, Keyring | None, str | None]:
    """(systemic failure or None, keyring, 2captcha key). Registers the keyring material and the
    2captcha key as E7 secret literals before anything can log them."""
    try:
        keyring = load_keyring_from_env(source)
    except KeyringError:
        return "keyring: KeyringError", None, None
    register_secret_literals(json.loads(source[KEYRING_ENV_VAR])["keys"].values())
    api_key = source.get(TWOCAPTCHA_API_KEY_ENV, "").strip() or None
    if api_key is not None:
        register_secret_literals([api_key])
    elif not dry_run:
        return f"config: {TWOCAPTCHA_API_KEY_ENV} is not set", None, None
    return None, keyring, api_key


async def plan_booking_event(*, event_key: str, store: TenantStore, clock: Clock) -> EventPlan:
    """``tenant-plan``: the event's rows + allocation over dry-run adapters, no ForeUP call."""
    event = event_for(event_key)
    return await plan_release_event(
        event=event,
        policies=policies_for(event),
        store=store,
        clock=clock,
        scheduler=tenant_scheduler(),
        adapter_factory=HostedAdapterFactory(api_key=None),
    )
