"""M2.T2 tests: stable RequestId derivation from a config fingerprint.

PLAN §13.1: fingerprint = course_ids|target_offsets|time_windows|party_fingerprint
with sort order canonical and PII excluded. Resolved dates NOT in the fingerprint
(weekend cron must produce identical RequestId across the same-day runs).
"""

from __future__ import annotations

from datetime import time

from teetime.core.models import (
    CourseId,
    Player,
    TimeWindow,
    build_request_fingerprint,
    derive_request_id,
)


def _w(h1: int, m1: int, h2: int, m2: int, wd: int = 6) -> tuple[int, TimeWindow]:
    # Per-day windows: the fingerprint takes (weekday_index, window) pairs. Default Sunday (6).
    return (wd, TimeWindow(earliest=time(h1, m1), latest=time(h2, m2)))


def _alex() -> Player:
    return Player(first_name="Alex", last_name="Lancaster", email="a@x.test")


def _guest() -> Player:
    return Player(first_name="Guest", last_name="Player", email="g@x.test")


def test_fingerprint_canonical_form() -> None:
    """Canonical fingerprint matches the production config: Mangrove Bay,
    offset 7, morning window 09:00-10:30, one player."""
    fp = build_request_fingerprint(
        course_ids=[CourseId("foreup:mangrove_bay")],
        target_offsets=[7],
        time_windows=[_w(9, 0, 10, 30)],
        players=[_alex()],
    )
    assert fp == "foreup:mangrove_bay|7|6:09:00-10:30|Alex|Lancaster"


def test_fingerprint_sorts_courses_offsets_windows_players() -> None:
    """Order of inputs must not change the fingerprint."""
    fp1 = build_request_fingerprint(
        course_ids=[CourseId("foreup:b"), CourseId("foreup:a")],
        target_offsets=[14, 7],
        time_windows=[_w(16, 0, 18, 0), _w(7, 0, 9, 30)],
        players=[_guest(), _alex()],
    )
    fp2 = build_request_fingerprint(
        course_ids=[CourseId("foreup:a"), CourseId("foreup:b")],
        target_offsets=[7, 14],
        time_windows=[_w(7, 0, 9, 30), _w(16, 0, 18, 0)],
        players=[_alex(), _guest()],
    )
    assert fp1 == fp2


def test_fingerprint_excludes_email_and_phone() -> None:
    """Rotating contact info must NOT change the RequestId — it's still the same goal."""
    p1 = Player(first_name="Alex", last_name="L", email="old@x.test", phone="555-0000")
    p2 = Player(first_name="Alex", last_name="L", email="new@x.test", phone="555-9999")
    fp1 = build_request_fingerprint(
        course_ids=[CourseId("c1")],
        target_offsets=[7],
        time_windows=[_w(7, 0, 9, 0)],
        players=[p1],
    )
    fp2 = build_request_fingerprint(
        course_ids=[CourseId("c1")],
        target_offsets=[7],
        time_windows=[_w(7, 0, 9, 0)],
        players=[p2],
    )
    assert fp1 == fp2


def test_request_id_stable_across_calls() -> None:
    fp = build_request_fingerprint(
        course_ids=[CourseId("c1")],
        target_offsets=[7],
        time_windows=[_w(7, 0, 9, 0)],
        players=[_alex()],
    )
    assert derive_request_id(fp) == derive_request_id(fp)


def test_request_id_differs_for_different_party() -> None:
    fp1 = build_request_fingerprint(
        course_ids=[CourseId("c1")],
        target_offsets=[7],
        time_windows=[_w(7, 0, 9, 0)],
        players=[_alex()],
    )
    fp2 = build_request_fingerprint(
        course_ids=[CourseId("c1")],
        target_offsets=[7],
        time_windows=[_w(7, 0, 9, 0)],
        players=[_alex(), _guest()],
    )
    assert derive_request_id(fp1) != derive_request_id(fp2)


def test_fingerprint_includes_window_weekday() -> None:
    """Per-day windows: the SAME time-of-day window on Saturday vs Sunday is a DISTINCT
    request identity (it books a different day). PERDAY_WINDOWS_PLAN §6."""
    sat = build_request_fingerprint(
        course_ids=[CourseId("c1")],
        target_offsets=[7],
        time_windows=[_w(9, 0, 10, 0, wd=5)],
        players=[_alex()],
    )
    sun = build_request_fingerprint(
        course_ids=[CourseId("c1")],
        target_offsets=[7],
        time_windows=[_w(9, 0, 10, 0, wd=6)],
        players=[_alex()],
    )
    assert sat != sun
    assert derive_request_id(sat) != derive_request_id(sun)


def test_request_id_independent_of_resolved_dates() -> None:
    """The whole point of §13.1: target_offsets=[7] firing on day N and day N+1
    produces the SAME RequestId, because resolved dates are excluded."""
    fp = build_request_fingerprint(
        course_ids=[CourseId("c1")],
        target_offsets=[7],
        time_windows=[_w(7, 0, 9, 0)],
        players=[_alex()],
    )
    # No date input is even accepted; this test documents that absence.
    rid_today = derive_request_id(fp)
    rid_tomorrow = derive_request_id(fp)
    assert rid_today == rid_tomorrow
