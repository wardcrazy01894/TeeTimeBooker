"""LEADTIME_SKIP_PLAN PR3 — TEETIME_SKIP_DATES parser.

Fail-open: empty/unset/partially-malformed input yields the dates it can parse (or empty),
never raises — a fat-fingered Portal edit must not crash the booker/watcher (Edge E6).
"""

from __future__ import annotations

import logging
from datetime import date

import pytest

from teetime.core.skip_dates import parse_skip_dates


@pytest.mark.parametrize("raw", [None, "", "   ", "\t\n ", ",", " , "])
def test_parse_empty_and_none_is_empty(raw: str | None) -> None:
    assert parse_skip_dates(raw) == frozenset()


def test_parse_comma_separated() -> None:
    assert parse_skip_dates("2026-06-14,2026-06-21") == frozenset(
        {date(2026, 6, 14), date(2026, 6, 21)}
    )


def test_parse_space_separated() -> None:
    assert parse_skip_dates("2026-06-14 2026-06-21") == frozenset(
        {date(2026, 6, 14), date(2026, 6, 21)}
    )


def test_parse_mixed_comma_and_space() -> None:
    assert parse_skip_dates("2026-06-14, 2026-06-21,  2026-07-05") == frozenset(
        {date(2026, 6, 14), date(2026, 6, 21), date(2026, 7, 5)}
    )


def test_parse_ignores_malformed_token_keeps_valid(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        result = parse_skip_dates("2026-06-14, garbage, 2026-06-21")
    assert result == frozenset({date(2026, 6, 14), date(2026, 6, 21)})
    assert "garbage" in caplog.text


def test_parse_all_malformed_is_empty_not_raise() -> None:
    # A fully fat-fingered value must NOT crash — returns empty, logs, never raises.
    assert parse_skip_dates("x, y, 2026-13-99") == frozenset()


def test_parse_dedupes() -> None:
    assert parse_skip_dates("2026-06-14, 2026-06-14") == frozenset({date(2026, 6, 14)})
