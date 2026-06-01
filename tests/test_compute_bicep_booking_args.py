"""M6 PR3: the booking ACA job takes the real-timing path (--wait) with a timeout
that covers the in-replica busy-wait; the watch job is unchanged.

Static assertions over compute.bicep (bicep is compile-validated by CI's
`az bicep build`, not pytest-importable). The file is split at `resource watchJob`
so booking-job and watch-job assertions don't bleed into each other.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

COMPUTE_BICEP = (
    Path(__file__).resolve().parent.parent / "infra" / "bicep" / "modules" / "compute.bicep"
)


@pytest.fixture(scope="module")
def parts() -> tuple[str, str]:
    content = COMPUTE_BICEP.read_text()
    assert "resource watchJob" in content, "watch job resource not found"
    booking_part, watch_part = content.split("resource watchJob", 1)
    return booking_part, watch_part


def test_booking_job_passes_wait_flag(parts: tuple[str, str]) -> None:
    booking_part, watch_part = parts
    assert "'--wait'" in booking_part  # booking job opts into the real 06:00 ET busy-wait
    assert "'--wait'" not in watch_part  # watch job has no busy-wait — must NOT pass --wait


def test_booking_args_still_pass_dry_run_param(parts: tuple[str, str]) -> None:
    booking_part, _ = parts
    # dry-run wiring must survive next to --wait.
    assert "dryRun ? 'true' : 'false'" in booking_part


def test_booking_replica_timeout_covers_busy_wait() -> None:
    content = COMPUTE_BICEP.read_text()
    m = re.search(r"var bookingReplicaTimeout = (\d+)", content)
    assert m is not None, "bookingReplicaTimeout var not defined"
    assert int(m.group(1)) >= 1200  # must cover lead + ~12 min busy-wait + post-T0 work
    booking_part, _ = content.split("resource watchJob", 1)
    assert "replicaTimeout: bookingReplicaTimeout" in booking_part
    # The old generic 900s var must be gone (no remaining consumer).
    assert "var replicaTimeout" not in content


def test_watch_replica_timeout_covers_idempotent_retries(parts: tuple[str, str]) -> None:
    """The watch job's replicaTimeout must give headroom for the in-run retries on
    idempotent ForeUP calls (warm-up/login/search). With the 30s httpx timeout and
    up to 2 retries across ~3 calls, a pathological all-timeout run can approach
    ~270s, so 120s is no longer safe — it would convert a recovered run into a
    replica-timeout Failure. Bumped to 300s. See base.py _send_with_retry."""
    content = COMPUTE_BICEP.read_text()
    _, watch_part = parts
    assert "replicaTimeout: watchReplicaTimeout" in watch_part
    m = re.search(r"var watchReplicaTimeout = (\d+)", content)
    assert m is not None, "watchReplicaTimeout var not defined"
    assert int(m.group(1)) >= 300
