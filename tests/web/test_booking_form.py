"""MU-R3 (MULTIUSER_PLAN §16.1): parsing the ranked booking form. Pure, no store.

The form carries up to ``MAX_OPTIONS`` option rows (course account, earliest, latest, rank), a
party size, and one optional price per connected course. Blank option rows are ignored. Ranks are
the user's order: they must be distinct, and are renumbered 1..N in that order.
"""

from __future__ import annotations

from datetime import time
from decimal import Decimal
from uuid import uuid4

import pytest
from teetime.web.booking_form import MAX_OPTIONS, RankedChoice, parse_ranked_form

from teetime.tenant.models import CourseAccountId, RankedWindow
from teetime.web.services import InvalidInputError, WebNotFoundError

A = CourseAccountId(uuid4())
B = CourseAccountId(uuid4())
OWN = frozenset({A, B})


def _row(i: int, account: CourseAccountId, earliest: str, latest: str, rank: int) -> dict[str, str]:
    return {
        f"opt{i}_account": str(account),
        f"opt{i}_earliest": earliest,
        f"opt{i}_latest": latest,
        f"opt{i}_rank": str(rank),
    }


def test_the_operators_example_groups_options_per_course_in_rank_order() -> None:
    """A 9-10 (1) > B 9-10 (2) > A 8-9 (3): two courses, A keeps ranks 1 and 3."""
    form = {
        "party_size": "4",
        **_row(1, A, "09:00", "10:00", 1),
        **_row(2, B, "09:00", "10:00", 2),
        **_row(3, A, "08:00", "09:00", 3),
    }
    got = parse_ranked_form(form, own_accounts=OWN)
    assert got.party_size == 4
    assert got.per_account == {
        A: (RankedWindow(1, time(9), time(10)), RankedWindow(3, time(8), time(9))),
        B: (RankedWindow(2, time(9), time(10)),),
    }
    assert got.prices == {}


def test_ranks_are_the_users_order_renumbered_contiguously() -> None:
    form = {
        "party_size": "2",
        **_row(1, A, "08:00", "09:00", 30),
        **_row(2, B, "09:00", "10:00", 10),
    }
    got = parse_ranked_form(form, own_accounts=OWN)
    assert got.per_account[B] == (RankedWindow(1, time(9), time(10)),)
    assert got.per_account[A] == (RankedWindow(2, time(8), time(9)),)


def test_blank_option_rows_are_ignored() -> None:
    form = {"party_size": "2", **_row(2, A, "08:00", "09:00", 1), "opt1_account": ""}
    got = parse_ranked_form(form, own_accounts=OWN)
    assert list(got.per_account) == [A]


def test_prices_per_course_blank_means_account_default() -> None:
    form = {
        "party_size": "2",
        **_row(1, A, "08:00", "09:00", 1),
        **_row(2, B, "09:00", "10:00", 2),
        f"price_{A}": "85.50",
        f"price_{B}": "",
    }
    got = parse_ranked_form(form, own_accounts=OWN)
    assert got.prices == {A: Decimal("85.50")}


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({}, "at least one"),
        ({**_row(1, A, "10:00", "09:00", 1)}, "before"),
        ({**_row(1, A, "08:00", "09:00", 1), **_row(2, B, "09:00", "10:00", 1)}, "rank"),
        ({**_row(1, A, "08:00", "09:00", 1), f"price_{A}": "-1"}, "price"),
        ({**_row(1, A, "08:00", "09:00", 1), f"price_{A}": "abc"}, "price"),
        ({**_row(1, A, "08:00", "09:00", 1), f"price_{A}": "5000"}, "price"),
    ],
)
def test_invalid_forms_are_refused(extra: dict[str, str], message: str) -> None:
    with pytest.raises(InvalidInputError, match=message):
        parse_ranked_form({"party_size": "2", **extra}, own_accounts=OWN)


def test_an_account_that_is_not_yours_is_not_found() -> None:
    """IDOR (§9.1): a foreign account id is the same 404 as a malformed one."""
    stranger = CourseAccountId(uuid4())
    with pytest.raises(WebNotFoundError):
        parse_ranked_form(
            {"party_size": "2", **_row(1, stranger, "08:00", "09:00", 1)}, own_accounts=OWN
        )


def test_option_rows_beyond_the_limit_are_not_read() -> None:
    form = {"party_size": "2", **_row(1, A, "08:00", "09:00", 1)}
    form |= _row(MAX_OPTIONS + 1, B, "09:00", "10:00", 2)
    got = parse_ranked_form(form, own_accounts=OWN)
    assert list(got.per_account) == [A]
    assert isinstance(got, RankedChoice)
