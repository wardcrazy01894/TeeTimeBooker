"""File a site report as a GitHub issue (operator decision 2026-09-29: the PUBLIC repo, anonymized).

``GitHubIssues.create`` POSTs ``/repos/<owner>/<repo>/issues`` with a fine-grained token scoped to
that one repo (Issues: read and write), from the ``GITHUB-ISSUES-TOKEN`` Key Vault secret. It never
raises: a failure is logged and returns None, so a report is never lost to GitHub. The token is
registered as an E7 secret literal on construction, so no log line can carry it.
"""

from __future__ import annotations

import logging

import httpx

from ..core.redaction import redact_text, register_secret_literals

log = logging.getLogger(__name__)

API = "https://api.github.com"
TIMEOUT_S = 10.0
REASON_MAX_LEN = 300


def _github_reason(r: httpx.Response) -> str:
    """GitHub's ``message`` (+ ``errors``): what tells an expired token (401) from a missing
    Issues permission (403), issues disabled (410) or a rejected title (422). Redacted, capped;
    it never carries the report (that is in the request, not the response)."""
    try:
        data = r.json()
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""
    parts = [str(data.get("message") or "")]
    if data.get("errors"):
        parts.append(f"errors={data['errors']}")
    return redact_text(" ".join(p for p in parts if p))[:REASON_MAX_LEN]


class GitHubIssues:
    def __init__(self, repo: str, token: str) -> None:
        self.repo = repo
        self._token = token
        register_secret_literals([token])

    async def create(self, *, title: str, body: str) -> str | None:
        """The new issue's URL, or None on any failure (logged, never raised)."""
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
                r = await client.post(
                    f"{API}/repos/{self.repo}/issues",
                    json={"title": title, "body": body},
                    headers={
                        "Authorization": f"Bearer {self._token}",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28",
                        "User-Agent": "spicyteetimebooker",
                    },
                )
            if r.status_code != httpx.codes.CREATED:
                log.warning(
                    "GitHub issue not created: HTTP %s %s", r.status_code, _github_reason(r)
                )
                return None
            url = r.json().get("html_url")
            return url if isinstance(url, str) else None
        except Exception:
            log.warning("GitHub issue not created", exc_info=True)
            return None
