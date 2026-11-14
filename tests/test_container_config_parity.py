"""Guards on ``config/container.toml`` (the single-user TOML config baked into the image) and
on what ``infra/bicep/modules/compute.bicep`` injects into the ACA jobs.

Until MU-19 (2026-11-14) the ACA jobs RAN container.toml, so every ``*_env`` name it references
had to be wired in compute.bicep (the loader raises on a missing one). The jobs now run only the
tenant path, so that parity is gone and is pinned the other way round: compute.bicep must NOT
supply the env vars only this file reads, because each one is a Key Vault secret ref that ACA
validates at job-CREATE time and the operator is free to delete those secrets.

What still holds and is still pinned here:

  * container.toml stays parseable and keeps its shape (a full foursome, the skip-dates env
    ref, the green-fee cap): `teetime run|watch --config` still exists and the image's default
    CMD points at this file.
  * Its ``[scheduler]`` race knobs match ``config/example.toml``. The tenant runner's defaults
    mirror the same values (``test_tenant_scheduler_matches_the_shipped_toml_scheduler``), so
    this file is still the written-down reference for what prod's race path does.
  * ``TWOCAPTCHA_API_KEY`` is wired into the jobs: the tenant commands read it too.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_CONTAINER_TOML = _REPO / "config" / "container.toml"
_EXAMPLE_TOML = _REPO / "config" / "example.toml"
_COMPUTE_BICEP = _REPO / "infra" / "bicep" / "modules" / "compute.bicep"

# Runtime env vars the ACA jobs need that both paths read straight from the environment.
#   TWOCAPTCHA_API_KEY: read via os.environ in __main__ (`tenant-run` / `tenant-watch`, and the
#     TOML CLI). A dropped wiring would leave the live jobs with no CAPTCHA solver.
_REQUIRED_RUNTIME_ENV_VARS = {
    "TWOCAPTCHA_API_KEY",
}


def _load(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def _referenced_env_vars(cfg: dict) -> set[str]:
    """Every env-var NAME the config loader will require at runtime.

    Mirrors core/config.py: any key ending in ``_env`` (course creds, player
    contact fields, card fields) resolves an env var that must exist. The
    ``[notifier]`` block is console-only (no env refs).
    """
    names: set[str] = set()

    for course in cfg.get("courses", []):
        for key, val in course.items():
            if key.endswith("_env") and isinstance(val, str):
                names.add(val)
        for key, val in course.get("extra", {}).items():
            if key.endswith("_env") and isinstance(val, str):
                names.add(val)

    request = cfg.get("request", {})
    for player in request.get("players", []):
        for key, val in player.items():
            if key.endswith("_env") and isinstance(val, str):
                names.add(val)

    # Top-level request *_env refs (e.g. skip_dates_env, LEADTIME_SKIP_PLAN F2). Generalised so
    # any future top-level *_env is auto-discovered by the deploy-parity guard, not just players.
    for key, val in request.items():
        if key.endswith("_env") and isinstance(val, str):
            names.add(val)

    return names


def _bicep_env_var_names(bicep_text: str) -> set[str]:
    """UPPER_SNAKE env var names the ACA container exposes (secretRef or value)."""
    # Matches: { name: 'MB_USERNAME', secretRef: '...' } and value: entries.
    return set(re.findall(r"\{\s*name:\s*'([A-Z][A-Z0-9_]*)'", bicep_text))


def test_container_config_is_parseable() -> None:
    assert _CONTAINER_TOML.exists(), "config/container.toml is missing"
    _load(_CONTAINER_TOML)  # raises on malformed TOML


def test_no_container_env_ref_is_wired_in_compute_bicep() -> None:
    """MU-19: no ACA job runs container.toml, so compute.bicep supplies NONE of the env vars
    this file references (ForeUP credentials, Player 1 contact, skip dates).

    If this fails, a single-user secret ref came back into the job wiring: the deploy would
    then need a Key Vault secret the operator may have deleted (ACA validates secret refs at
    job-CREATE time), for a config no job reads.
    """
    cfg = _load(_CONTAINER_TOML)
    required = _referenced_env_vars(cfg)
    assert required, "container.toml references no env var: the guard below would be vacuous"
    provided = _bicep_env_var_names(_COMPUTE_BICEP.read_text())

    stale = required & provided
    assert not stale, (
        f"compute.bicep wires env var(s) only the TOML config reads: {sorted(stale)}. "
        "The ACA jobs run the tenant path only (MULTIUSER_PLAN §11 step 9, MU-19)."
    )


def test_referenced_env_vars_scans_request_top_level() -> None:
    """The parser discovers a top-level ``request.*_env`` ref (not just course/player ones), so
    a new top-level secret ref can't slip past the deploy-parity guard (LEADTIME_SKIP_PLAN F2)."""
    cfg = {"request": {"skip_dates_env": "TEETIME_SKIP_DATES", "players": []}}
    assert "TEETIME_SKIP_DATES" in _referenced_env_vars(cfg)


def test_skip_dates_env_declared_in_container_toml() -> None:
    """LEADTIME_SKIP_PLAN F2: container.toml names the skip-dates env var (never a literal date
    list), so a single-user run can skip a day with no config edit. compute.bicep no longer
    injects it (MU-19; pinned above): the tenant path keeps skips in its own store."""
    cfg = _load(_CONTAINER_TOML)
    assert cfg["request"]["skip_dates_env"] == "TEETIME_SKIP_DATES"


def test_booking_is_a_full_foursome() -> None:
    """Operator invariant: every tee time is booked for 4 players."""
    cfg = _load(_CONTAINER_TOML)
    players = cfg.get("request", {}).get("players", [])
    assert len(players) == 4, (
        f"container.toml has {len(players)} players; a full foursome (4) is "
        "required. Update config/container.toml [[request.players]] blocks."
    )


def test_critical_runtime_env_vars_are_wired_in_compute_bicep() -> None:
    """Env vars the jobs read straight from the environment must be wired in compute.bicep.

    Most importantly ``TWOCAPTCHA_API_KEY``: ``tenant-run`` and ``tenant-watch`` read it from
    the environment, so nothing else catches a missing bicep wiring. Without it a live job has
    no CAPTCHA solver. Assert them by name. See ``_REQUIRED_RUNTIME_ENV_VARS``.
    """
    provided = _bicep_env_var_names(_COMPUTE_BICEP.read_text())
    missing = _REQUIRED_RUNTIME_ENV_VARS - provided
    assert not missing, (
        f"compute.bicep is missing critical runtime env var(s): {sorted(missing)}. "
        "The tenant commands read them straight from the environment, and the deployed live "
        "job would have no CAPTCHA solver. Re-add them to jobEnv (and the matching "
        "jobSecrets) in infra/bicep/modules/compute.bicep."
    )


def test_container_and_example_captcha_prefetch_match() -> None:
    """The race-path CAPTCHA prefetch knobs must agree across the committed configs.

    ``captcha_prefetch_count`` (RACE_PREWARM_PLAN §4.4) and ``captcha_prefetch_lead_s``
    are race-critical timing/depth params; a silent drift between the container image's
    config and the committed reference would mean prod pre-solves a different number of
    tokens (or with a different lead) than reviewed. ``config/local.toml`` is gitignored
    (absent in CI), so — like the party-size guard — we anchor to ``example.toml``.
    """
    example = _load(_EXAMPLE_TOML).get("scheduler", {})
    container = _load(_CONTAINER_TOML).get("scheduler", {})
    assert example.get("captcha_prefetch_count") == container.get("captcha_prefetch_count"), (
        "captcha_prefetch_count drift between example.toml and container.toml — keep the "
        "race-path token-pool depth in sync."
    )
    assert example.get("captcha_prefetch_lead_s") == container.get("captcha_prefetch_lead_s"), (
        "captcha_prefetch_lead_s drift between example.toml and container.toml — keep the "
        "race-path prefetch lead in sync."
    )
    assert example.get("blind_post_max_count") == container.get("blind_post_max_count"), (
        "blind_post_max_count drift between example.toml and container.toml — keep the "
        "blind-POST fan-out cap in sync (docs/plans/BLIND_POST_PLAN.md §5)."
    )


def test_container_and_example_blind_fallback_reserve_match() -> None:
    """The blind-POST 0-booked fallback token reserve must agree across the committed configs.

    ``blind_post_fallback_token_reserve`` (RESEARCH_FALLBACK_PLAN §2 Q3) pre-solves spare
    CAPTCHA tokens beyond the blind burst so the post-reguard fresh search books with a
    pooled token, not a ~75 s inline solve. It is a race-critical pool-depth knob; a silent
    drift would mean prod reserves a different number of spare tokens than reviewed.
    ``config/local.toml`` is gitignored (absent in CI), so we anchor to ``example.toml``.
    """
    example = _load(_EXAMPLE_TOML).get("scheduler", {})
    container = _load(_CONTAINER_TOML).get("scheduler", {})
    assert example.get("blind_post_fallback_token_reserve") is not None, (
        "example.toml [scheduler] is missing blind_post_fallback_token_reserve — pin the "
        "blind-POST fallback reserve depth explicitly (RESEARCH_FALLBACK_PLAN §2 Q3)."
    )
    assert example.get("blind_post_fallback_token_reserve") == container.get(
        "blind_post_fallback_token_reserve"
    ), (
        "blind_post_fallback_token_reserve drift between example.toml and container.toml — "
        "keep the blind-POST fallback reserve depth in sync (RESEARCH_FALLBACK_PLAN §2 Q3)."
    )


def test_container_and_example_blind_stagger_match() -> None:
    """The blind-POST T0 stagger must agree across the committed configs.

    ``blind_post_stagger_ms`` (docs/plans/STAGGER_PLAN.md) decides WHEN each POST in the T0 burst
    fires relative to the release boundary — the single most race-critical timing knob we
    have, and the one that makes a miss diagnosable at all. A silent drift would mean prod
    straddles the boundary differently than reviewed, and would invalidate the
    offset→outcome correlation the whole feature exists to produce.
    ``config/local.toml`` is gitignored (absent in CI), so we anchor to ``example.toml``.
    """
    example = _load(_EXAMPLE_TOML).get("scheduler", {})
    container = _load(_CONTAINER_TOML).get("scheduler", {})
    assert example.get("blind_post_stagger_ms") is not None, (
        "example.toml [scheduler] is missing blind_post_stagger_ms — pin the blind-POST T0 "
        "stagger explicitly (docs/plans/STAGGER_PLAN.md §3.1)."
    )
    assert example.get("blind_post_stagger_ms") == container.get("blind_post_stagger_ms"), (
        "blind_post_stagger_ms drift between example.toml and container.toml — keep the "
        "blind-POST T0 stagger in sync (docs/plans/STAGGER_PLAN.md §3.1)."
    )


def test_blind_stagger_keeps_the_best_slot_at_todays_fire_instant() -> None:
    """The shipped stagger's FIRST offset must equal ``-early_arrival_ms``.

    That is the non-regression guarantee (STAGGER_PLAN §2.1): the rank-0 (best,
    nearest-midpoint) slot fires at exactly the instant it does today, so every drop we
    currently win is unaffected and only the surplus POSTs move. Pinned mechanically
    because a well-meaning tweak to either key alone would silently break it.
    """
    container = _load(_CONTAINER_TOML).get("scheduler", {})
    stagger = container.get("blind_post_stagger_ms")
    assert stagger, "container.toml [scheduler] is missing blind_post_stagger_ms"
    assert stagger[0] == -container["early_arrival_ms"], (
        f"blind_post_stagger_ms[0] ({stagger[0]}) must equal -early_arrival_ms "
        f"({-container['early_arrival_ms']}) so the best-ranked slot keeps today's fire "
        "instant (STAGGER_PLAN §2.1)."
    )
    assert min(stagger) == -container["early_arrival_ms"], (
        f"blind_post_stagger_ms {stagger} schedules a POST EARLIER than the rank-0 offset "
        f"({-container['early_arrival_ms']} ms). Operator directive 2026-08-15: nothing "
        "moves earlier than today's fire instant."
    )
    assert max(stagger) >= 0, (
        "blind_post_stagger_ms must SEND at least one POST no earlier than T0 — that is "
        "the hedge that makes a 0/N wipeout impossible under the boundary hypothesis. "
        "(0 = sent at 06:00:00.000; network latency carries it past the open on arrival, "
        "so it is the tightest post-open probe available.)"
    )


def test_container_and_example_party_size_match() -> None:
    """The committed reference config and the Azure container must agree on size.

    The drift that bit us: the canonical config books 4 but the container booked
    2. ``config/local.toml`` is gitignored (absent in CI), so we anchor to the
    committed ``config/example.toml`` instead — this test is therefore live in
    CI, not silently skipped. Other request params (price, windows) may differ
    per environment; party size is the booking's defining shape and must match.
    """
    assert _EXAMPLE_TOML.exists(), "config/example.toml (committed reference) is missing"
    example = _load(_EXAMPLE_TOML)
    container = _load(_CONTAINER_TOML)
    n_example = len(example.get("request", {}).get("players", []))
    n_container = len(container.get("request", {}).get("players", []))
    assert n_example == n_container, (
        f"party-size drift: example.toml has {n_example} players, container.toml "
        f"has {n_container}. Keep them in sync so Azure books the intended party."
    )


def test_prod_green_fee_cap_is_100() -> None:
    """Operator directive 2026-09-26: the single-user prod bot's green-fee cap is $100 per
    player (was $60). The multi-user site gets its own per-account cap (MU-P1), default $100."""
    cfg = _load(_CONTAINER_TOML)
    assert cfg["request"]["max_price_per_player"] == "100.00"
