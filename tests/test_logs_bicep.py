"""logs.bicep: the Log Analytics workspace has a daily ingestion cap (full-repo scan 2026-09-30).

Normal ingestion is under 10 MB/day per env (Usage table, 2026-09-16..30), so the cap never binds
in practice; it bounds a log flood (a crash loop, or httpx INFO from the always-warm web app)
that the $50 killswitch would otherwise be the only backstop for.
"""

from __future__ import annotations

import re
from pathlib import Path

LOGS = Path(__file__).resolve().parent.parent / "infra" / "bicep" / "modules" / "logs.bicep"


def test_workspace_has_a_daily_cap_well_above_normal_ingestion() -> None:
    text = LOGS.read_text()
    m = re.search(r"param dailyQuotaGb string = '([0-9.]+)'", text)
    assert m, "logs.bicep must declare a dailyQuotaGb param"
    assert 0.1 <= float(m.group(1)) <= 1.0
    assert "workspaceCapping: {" in text
    assert "dailyQuotaGb: json(dailyQuotaGb)" in text
