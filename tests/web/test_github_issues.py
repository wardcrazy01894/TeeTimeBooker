"""``GitHubIssues.create`` failure modes (scan 2026-09-30): it never raises, and every failure is
diagnosable from the log line alone (status AND GitHub's reason), with the token masked."""

from __future__ import annotations

import logging

import httpx
import pytest
import respx

from teetime.core.redaction import install_log_redaction
from teetime.web.feedback import TITLE_MAX_LEN, _fenced, issue_title
from teetime.web.github_issues import GitHubIssues

REPO = "owner/repo"
ISSUES_URL = f"https://api.github.com/repos/{REPO}/issues"
TOKEN = "github_pat_test_fedcba9876543210"


@pytest.fixture
def issues() -> GitHubIssues:
    install_log_redaction()
    return GitHubIssues(REPO, TOKEN)


@respx.mock
async def test_a_rejection_logs_githubs_reason(
    issues: GitHubIssues, caplog: pytest.LogCaptureFixture
) -> None:
    respx.post(ISSUES_URL).mock(
        return_value=httpx.Response(
            403,
            json={
                "message": "Resource not accessible by personal access token",
                "errors": [
                    {"code": "custom", "field": "title", "message": f"echo {TOKEN}"},
                    {"resource": "Issue", "code": "invalid", "value": "the report text"},
                ],
            },
        )
    )
    caplog.set_level(logging.WARNING, logger="teetime.web.github_issues")
    assert await issues.create(title="t", body="b") is None
    (line,) = [r.getMessage() for r in caplog.records]
    assert "HTTP 403" in line
    assert "Resource not accessible by personal access token" in line
    assert "custom:title" in line and "invalid" in line
    # An error entry can echo a submitted value (the title comes from the report): never logged.
    assert "the report text" not in caplog.text and "echo" not in caplog.text
    assert TOKEN not in caplog.text


@respx.mock
async def test_a_non_json_rejection_still_logs_the_status(
    issues: GitHubIssues, caplog: pytest.LogCaptureFixture
) -> None:
    respx.post(ISSUES_URL).mock(return_value=httpx.Response(502, text="<html>bad gateway"))
    caplog.set_level(logging.WARNING, logger="teetime.web.github_issues")
    assert await issues.create(title="t", body="b") is None
    assert "HTTP 502" in caplog.text


@respx.mock
async def test_a_transport_error_is_logged_not_raised(
    issues: GitHubIssues, caplog: pytest.LogCaptureFixture
) -> None:
    respx.post(ISSUES_URL).mock(side_effect=httpx.ConnectError("refused"))
    caplog.set_level(logging.WARNING, logger="teetime.web.github_issues")
    assert await issues.create(title="t", body="b") is None
    assert "ConnectError" in caplog.text


@respx.mock
async def test_a_created_issue_without_a_url_is_none(issues: GitHubIssues) -> None:
    respx.post(ISSUES_URL).mock(return_value=httpx.Response(201, json={"html_url": 7}))
    assert await issues.create(title="t", body="b") is None


def test_a_tilde_fence_in_the_message_cannot_close_ours() -> None:
    message = "~~~~\n@everyone <img src=x>\n~~~~~~"
    fenced = _fenced(message)
    assert fenced.startswith("~~~~~~~text\n") and fenced.endswith("\n~~~~~~~")


def test_the_title_is_capped_and_mention_free() -> None:
    title = issue_title("bug", "@" + "x" * 500 + "\nsecond line")
    assert title == "[Bug report] " + "x" * TITLE_MAX_LEN
