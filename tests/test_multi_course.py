"""Tests for multi-course booking extensibility (PLAN §3 course fallback).

Covers two gaps found during the multi-course design review:

1. _build_request() fingerprint must use course_preferences, not cfg.courses.
   If a standby course is in [[courses]] but not in course_preferences, it
   must not change the RequestId (and thus not invalidate idempotency records).

2. _build_adapters() must resolve adapters through _ADAPTER_REGISTRY so that
   adding a new ForeUP course is a one-liner (new file + one registry entry),
   not an if/elif chain in __main__.py.

The orchestrator's multi-course fallback logic itself (tries courses in
course_preferences order, stops at first success) is already covered by
test_orchestrator.py::test_run_falls_back_to_next_course_when_first_empty.
"""

from __future__ import annotations

from datetime import time

import click
import pytest

from teetime.__main__ import _build_adapters, _build_request
from teetime.core.config import AppConfig
from teetime.core.models import (
    CourseId,
    Player,
    TimeWindow,
    build_request_fingerprint,
    derive_request_id,
)
from teetime.courses.foreup.mangrove_bay import MangroveBayAdapter

# ---------------------------------------------------------------------------
# Gap 1: fingerprint must use course_preferences, not cfg.courses
# ---------------------------------------------------------------------------


def _make_cfg(*, extra_course: bool) -> AppConfig:
    """Build an AppConfig via model_validate (bypasses env-var resolution).

    If extra_course=True the [[courses]] block has TWO entries but
    course_preferences lists only ONE - the gap under test.
    """
    courses = [
        {
            "id": "foreup:mangrove_bay",
            "adapter": "foreup.mangrove_bay",
            "username_env": "MB_USERNAME",
            "password_env": "MB_PASSWORD",
        }
    ]
    if extra_course:
        courses.append(
            {
                "id": "foreup:twin_brooks",
                "adapter": "foreup.twin_brooks",
                "username_env": "TB_USERNAME",
                "password_env": "TB_PASSWORD",
            }
        )

    return AppConfig.model_validate(
        {
            "courses": courses,
            "request": {
                "target_offsets": [7],
                "time_windows": [{"earliest": "09:00:00", "latest": "10:30:00"}],
                "players": [
                    {
                        "first_name": "Alex",
                        "last_name": "Lancaster",
                        "email": "alex@example.test",  # set directly, bypassing env lookup
                    }
                ],
                "course_preferences": ["foreup:mangrove_bay"],  # only ONE preference
            },
        }
    )


def _expected_rid() -> object:
    """The RequestId that should result from a single-course config."""
    fp = build_request_fingerprint(
        course_ids=[CourseId("foreup:mangrove_bay")],
        target_offsets=[7],
        time_windows=[TimeWindow(earliest=time(9, 0), latest=time(10, 30))],
        players=[Player(first_name="Alex", last_name="Lancaster", email="alex@example.test")],
    )
    return derive_request_id(fp)


def test_request_id_stable_when_standby_course_added_to_courses_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adding a course to [[courses]] that is NOT in course_preferences must NOT
    change the RequestId.

    Without the fix: _build_request() uses cfg.courses for the fingerprint,
    so adding foreup:twin_brooks to [[courses]] changes the fingerprint from
    "foreup:mangrove_bay|..." to "foreup:mangrove_bay,foreup:twin_brooks|..."
    and invalidates all existing idempotency records.

    With the fix: fingerprint uses course_preferences only (just foreup:mangrove_bay),
    so the RequestId is identical whether or not twin_brooks is in [[courses]].
    """
    monkeypatch.setenv("MB_USERNAME", "u")
    monkeypatch.setenv("MB_PASSWORD", "p")
    monkeypatch.setenv("TB_USERNAME", "u")
    monkeypatch.setenv("TB_PASSWORD", "p")

    rid_single = _build_request(_make_cfg(extra_course=False), dry_run=True).request_id
    rid_extra = _build_request(_make_cfg(extra_course=True), dry_run=True).request_id

    assert rid_single == rid_extra, (
        "RequestId changed when a standby course was added to [[courses]] "
        "but NOT to course_preferences. _build_request() must use "
        "course_preferences for the fingerprint, not cfg.courses."
    )
    assert rid_single == _expected_rid(), (
        "RequestId does not match the expected fingerprint built from "
        "course_preferences=[foreup:mangrove_bay] only."
    )


def test_course_preferences_is_the_booking_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """BookingRequest.course_preferences must equal course_preferences from config,
    not the full cfg.courses list.

    This documents that even though twin_brooks is in [[courses]], the orchestrator
    will NOT try to book there unless it appears in course_preferences.
    """
    monkeypatch.setenv("MB_USERNAME", "u")
    monkeypatch.setenv("MB_PASSWORD", "p")
    monkeypatch.setenv("TB_USERNAME", "u")
    monkeypatch.setenv("TB_PASSWORD", "p")

    request = _build_request(_make_cfg(extra_course=True), dry_run=True)

    assert request.course_preferences == (CourseId("foreup:mangrove_bay"),), (
        "course_preferences must only include what's listed in [request].course_preferences, "
        "not all [[courses]] entries."
    )


# ---------------------------------------------------------------------------
# Gap 2: _build_adapters must use _ADAPTER_REGISTRY for known adapter names
# ---------------------------------------------------------------------------


def test_build_adapters_raises_clear_error_for_unknown_adapter() -> None:
    """_build_adapters() must raise a ClickException naming the unknown adapter,
    not a bare KeyError or AttributeError."""
    cfg = AppConfig.model_validate(
        {
            "courses": [
                {
                    "id": "foreup:imaginary_course",
                    "adapter": "foreup.does_not_exist",
                    "username_env": "U",
                    "password_env": "P",
                }
            ],
            "request": {
                "target_offsets": [7],
                "time_windows": [{"earliest": "09:00:00", "latest": "10:30:00"}],
                "players": [{"first_name": "A", "last_name": "L", "email": "a@x.test"}],
                "course_preferences": ["foreup:imaginary_course"],
            },
        }
    )

    with pytest.raises(click.ClickException) as exc_info:
        _build_adapters(cfg, dry_run=True)

    assert "foreup.does_not_exist" in str(exc_info.value.format_message()), (
        "Error message must name the unknown adapter so the operator knows what to fix."
    )


def test_build_adapters_mangrove_bay_resolves_via_registry() -> None:
    """foreup.mangrove_bay must resolve through _ADAPTER_REGISTRY (not a hardcoded
    if-branch), returning a MangroveBayAdapter instance."""
    cfg = AppConfig.model_validate(
        {
            "courses": [
                {
                    "id": "foreup:mangrove_bay",
                    "adapter": "foreup.mangrove_bay",
                    "username_env": "MB_USERNAME",
                    "password_env": "MB_PASSWORD",
                }
            ],
            "request": {
                "target_offsets": [7],
                "time_windows": [{"earliest": "09:00:00", "latest": "10:30:00"}],
                "players": [{"first_name": "A", "last_name": "L", "email": "a@x.test"}],
                "course_preferences": ["foreup:mangrove_bay"],
            },
        }
    )

    adapters = _build_adapters(cfg, dry_run=True)

    assert CourseId("foreup:mangrove_bay") in adapters
    assert isinstance(adapters[CourseId("foreup:mangrove_bay")], MangroveBayAdapter)
