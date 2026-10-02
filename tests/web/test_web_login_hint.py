"""The sign-in page tells a person without a Google/GitHub account (a comcast.net address, say)
how to get in: make a Google account on the email that was invited, then "Continue with Google"
(operator question 2026-10-02: Comcast has no sign-in service of its own; the fix is education)."""

from __future__ import annotations

import re

import httpx

_TAG = re.compile(r"<[^>]+>")


async def test_sign_in_page_explains_the_google_account_on_any_email_route(
    client: httpx.AsyncClient,
) -> None:
    html = (await client.get("/login")).text
    text = " ".join(_TAG.sub(" ", html).split())
    assert "don't have a google account" in text.lower()
    assert "Use my current email address instead" in text
    assert "same email address that was invited" in text
    assert 'href="https://accounts.google.com/signup"' in html
