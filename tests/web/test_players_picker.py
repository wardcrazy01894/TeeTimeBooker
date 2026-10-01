"""The shared ``players_picker`` macro (operator request 2026-09-29): four radio buttons 1-4,
styled as a button row, with the given party checked. Every party-size field uses it."""

from __future__ import annotations

import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

TEMPLATES = Path(__file__).resolve().parents[2] / "src" / "teetime" / "web" / "templates"


def _macro(name: str):  # type: ignore[no-untyped-def]
    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=True)
    # The app registers the real ones; unused by this macro, but Jinja resolves filters at
    # compile time for the whole file.
    env.filters["course_name"] = str
    env.filters["course_signup_url"] = lambda _c: None
    env.filters["release_cycle"] = lambda _c: None
    return getattr(env.get_template("_macros.html").module, name)


def test_players_picker_checks_the_given_party() -> None:
    html = str(_macro("players_picker")(4))
    radios = re.findall(r'<input type="radio" name="party_size" value="(\d)"([^>]*)>', html)
    assert [(v, "checked" in rest) for v, rest in radios] == [
        ("1", False),
        ("2", False),
        ("3", False),
        ("4", True),
    ]
    assert "required" in html
    assert "<legend" in html and "Players" in html


def test_no_template_types_a_party_size() -> None:
    offenders = [
        p.name
        for p in TEMPLATES.glob("*.html")
        if re.search(r'type="number"[^>]*name="party_size"', p.read_text())
    ]
    assert offenders == []
