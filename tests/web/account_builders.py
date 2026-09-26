"""Shared fakes for the MU-14 connect / refresh / cancel tests (MULTIUSER_PLAN §8.4-§8.6).

The ForeUP adapter is faked at the ``AdapterFactory`` boundary (never the service under test):
``ProbeFactory`` hands out ``ProbeAdapter``s (a ``FakeAdapter`` that is ``AuthStateReportable``
AND ``ReservationSnapshotHealth``, like ``ForeUpAdapter``) and records every build.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from teetime.core.models import CourseId, ExistingReservation
from teetime.courses.foreup.token_pool import LeaseKey, SharedCaptchaPool
from teetime.dev.fake_adapter import FakeAdapter
from teetime.tenant.crypto import Keyring
from teetime.tenant.models import CourseAccount

from ..tenant.conformance import MB

KEYRING = Keyring(active_kid="k1", keys={"k1": os.urandom(32)})
PASSWORD = "Sup3r-Secret-Pw!42"  # >= the E7 floor, so it is registered and masked


class ProbeAdapter(FakeAdapter):
    """A ForeUP-shaped fake: soft-fail login, snapshot trust, and a live reservation list."""

    def __init__(self, *, trusted: bool = True) -> None:
        super().__init__(course_id=MB)
        self.trusted = trusted
        self.closed = 0
        self.credentials: list[tuple[str, str]] = []

    async def authenticate(self, creds: Any) -> None:
        self.credentials.append((creds.username, creds.password))
        await super().authenticate(creds)

    @property
    def snapshot_trusted(self) -> bool:
        return self.trusted

    async def aclose(self) -> None:
        self.closed += 1


@dataclass
class ProbeFactory:
    """``AdapterFactory`` handing out ``adapter`` (a fresh ``ProbeAdapter`` by default) and
    recording each build's (course, account, pool, lease_key, dry_run)."""

    adapter: ProbeAdapter = field(default_factory=ProbeAdapter)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(
        self,
        *,
        course_id: CourseId,
        account: CourseAccount,
        pool: SharedCaptchaPool | None,
        lease_key: LeaseKey | None,
        dry_run: bool,
    ) -> ProbeAdapter:
        self.calls.append(
            {
                "course_id": course_id,
                "account": account,
                "pool": pool,
                "lease_key": lease_key,
                "dry_run": dry_run,
            }
        )
        return self.adapter


def reservation(raw_id: str, tee_time: datetime, *, party: int = 2) -> ExistingReservation:
    return ExistingReservation(
        course_id=MB, confirmation_code=raw_id, tee_time=tee_time, party_size=party
    )
