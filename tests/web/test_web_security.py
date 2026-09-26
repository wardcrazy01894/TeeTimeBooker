"""Framework-free security primitives (web/security.py, MULTIUSER_PLAN §8.3)."""

from __future__ import annotations

from teetime.web.security import (
    CookiePolicy,
    issue_csrf_token,
    security_headers,
    verify_csrf_token,
)


def test_csrf_token_is_random_and_urlsafe() -> None:
    a, b = issue_csrf_token(), issue_csrf_token()
    assert a != b
    assert len(a) >= 32
    assert all(c.isalnum() or c in "-_" for c in a)


def test_verify_csrf_token_requires_both_sides_and_equality() -> None:
    tok = issue_csrf_token()
    assert verify_csrf_token(tok, tok)
    assert not verify_csrf_token(tok, tok + "x")
    assert not verify_csrf_token(None, tok)
    assert not verify_csrf_token(tok, None)
    assert not verify_csrf_token(None, None)
    assert not verify_csrf_token("", "")


def test_security_headers_values() -> None:
    h = security_headers()
    csp = h["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "unsafe-inline" not in csp
    assert "frame-ancestors 'none'" in csp
    assert h["X-Frame-Options"] == "DENY"
    assert h["Referrer-Policy"] == "same-origin"
    assert h["X-Content-Type-Options"] == "nosniff"
    assert h["Strict-Transport-Security"].startswith("max-age=")


def test_cookie_policy_defaults() -> None:
    p = CookiePolicy()
    assert (p.secure, p.http_only, p.same_site, p.path) == (True, True, "lax", "/")
