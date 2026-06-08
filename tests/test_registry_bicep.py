"""Static assertions over registry.bicep — the scheduled ACR image-purge task that
caps unbounded SHA-tag growth (full-repo-scan cost finding: every merge pushes a new
teetime:<sha> image and nothing ever deletes old tags).

Bicep is not pytest-importable; CI's `az bicep build` is the compile gate. These pin
the purge task's INTENT so a future edit can't silently drop or weaken it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REGISTRY_BICEP = (
    Path(__file__).resolve().parent.parent / "infra" / "bicep" / "modules" / "registry.bicep"
)


@pytest.fixture(scope="module")
def bicep() -> str:
    return REGISTRY_BICEP.read_text()


def test_purge_task_defined(bicep: str) -> None:
    assert "Microsoft.ContainerRegistry/registries/tasks" in bicep
    assert "name: 'purge-old-images'" in bicep
    assert "acr purge" in bicep


def test_purge_keeps_recent_tags_and_untagged(bicep: str) -> None:
    assert "--keep ${purgeKeepCount}" in bicep
    assert "param purgeKeepCount int = 10" in bicep
    assert "--untagged" in bicep  # also reap dangling manifests


def test_purge_runs_on_a_weekly_timer(bicep: str) -> None:
    assert "timerTriggers" in bicep
    assert "param purgeSchedule string = '0 4 * * Sun'" in bicep


def test_purge_task_is_gated_default_on(bicep: str) -> None:
    assert "param enablePurgeTask bool = true" in bicep
    assert "if (enablePurgeTask)" in bicep
