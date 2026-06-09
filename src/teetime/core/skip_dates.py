"""No-redeploy 'skip this day' parser (LEADTIME_SKIP_PLAN F2).

Parses the ``TEETIME_SKIP_DATES`` value (comma/space-separated ISO dates) into a
``frozenset[date]``. FAIL-OPEN: empty / unset / partially-malformed input yields the dates
it CAN parse (or an empty set) and NEVER raises — a fat-fingered Portal edit of the
Key Vault secret must not crash the 06:00 booker or the watcher (Edge E6). In prod the
value is injected into the ACA Jobs from a Key Vault secret editable in the Portal with no
redeploy; see LEADTIME_SKIP_PLAN §7 for the ACA secret-refresh behaviour.
"""

from __future__ import annotations

import logging
from datetime import date

_log = logging.getLogger(__name__)


def parse_skip_dates(raw: str | None) -> frozenset[date]:
    """Parse ``raw`` into a frozenset of ISO dates. Fail-open.

    - ``None`` / ``""`` / whitespace-only -> ``frozenset()``.
    - Tokens are split on commas AND whitespace; each is parsed via ``date.fromisoformat``.
    - An UNPARSEABLE token is logged (warning) and SKIPPED; other valid tokens still apply
      (partial-parse, NOT fail-closed — see Edge E6). The result is de-duplicated.
    """
    if not raw:
        return frozenset()
    out: set[date] = set()
    # Split on commas AND any whitespace; empty tokens drop out.
    for token in raw.replace(",", " ").split():
        try:
            out.add(date.fromisoformat(token))
        except ValueError:
            _log.warning("TEETIME_SKIP_DATES: ignoring unparseable date token %r", token)
    return frozenset(out)
