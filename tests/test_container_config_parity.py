"""Guard against drift between the container runtime config and the Azure infra.

These tests run in the normal (non-integration) CI job. They exist because the
v1 Azure deploy is split across two files that MUST agree:

  * ``config/container.toml`` — baked into the image; the bot's config loader
    (``core/config.py``) RAISES on any ``*_env`` reference whose env var is
    missing at container start. ACA also validates Key Vault secret refs at
    job-CREATE time, so a missing secret fails the deployment, not just the run.
  * ``infra/bicep/modules/compute.bicep`` — declares the Key Vault secret refs
    and the container ``env`` block that supply those env vars.

If someone adds a ``*_env`` to container.toml (e.g. a new player email) without
wiring the matching secret + env var in compute.bicep, the deploy breaks with a
cryptic ``InvalidParameterValueInContainerTemplate``. These tests turn that into
a fast, local red test instead.

They also pin the booking invariants the operator cares about (full foursome),
so an accidental edit to the party size is caught in review.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_CONTAINER_TOML = _REPO / "config" / "container.toml"
_EXAMPLE_TOML = _REPO / "config" / "example.toml"
_COMPUTE_BICEP = _REPO / "infra" / "bicep" / "modules" / "compute.bicep"

# Runtime env vars the container needs that are NOT config ``*_env`` references,
# so ``_referenced_env_vars`` can't discover them — they must be asserted by name.
#   TWOCAPTCHA_API_KEY: read via os.environ directly in __main__ (CAPTCHA solve).
#     Critically, its absence does NOT crash at config load, so a dropped wiring
#     would fail SILENTLY at the 6 AM booking — the worst failure mode.
# The bot makes no authenticated Azure SDK calls at runtime (state is in-process;
# no Blob Storage), so no AZURE_CLIENT_ID / AZURE_STORAGE_ACCOUNT_NAME is needed.
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

    for player in cfg.get("request", {}).get("players", []):
        for key, val in player.items():
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


def test_every_container_env_ref_is_wired_in_compute_bicep() -> None:
    """The crash-the-deploy guard: no orphan *_env in container.toml.

    If this fails, container.toml references an env var that compute.bicep does
    not supply — the ACA job would fail to create (missing Key Vault secret) or
    the container would crash at config load. Wire the secret + env var in
    compute.bicep (jobSecrets + commonEnv) and add the Key Vault secret.
    """
    cfg = _load(_CONTAINER_TOML)
    required = _referenced_env_vars(cfg)
    provided = _bicep_env_var_names(_COMPUTE_BICEP.read_text())

    missing = required - provided
    assert not missing, (
        "container.toml references env var(s) not wired in compute.bicep: "
        f"{sorted(missing)}. Add matching jobSecrets + commonEnv entries in "
        "infra/bicep/modules/compute.bicep AND create the Key Vault secret(s)."
    )


def test_booking_is_a_full_foursome() -> None:
    """Operator invariant: every tee time is booked for 4 players."""
    cfg = _load(_CONTAINER_TOML)
    players = cfg.get("request", {}).get("players", [])
    assert len(players) == 4, (
        f"container.toml has {len(players)} players; a full foursome (4) is "
        "required. Update config/container.toml [[request.players]] blocks."
    )


def test_critical_runtime_env_vars_are_wired_in_compute_bicep() -> None:
    """Non-``*_env`` runtime vars must still be wired in compute.bicep.

    ``_referenced_env_vars`` only finds config ``*_env`` references. Some env vars
    the container needs are read directly (not via a config ref) — most
    importantly ``TWOCAPTCHA_API_KEY``, whose absence fails SILENTLY at booking
    time rather than crashing at load, so the generic guard above can't catch it.
    Assert them by name. See ``_REQUIRED_RUNTIME_ENV_VARS``.
    """
    provided = _bicep_env_var_names(_COMPUTE_BICEP.read_text())
    missing = _REQUIRED_RUNTIME_ENV_VARS - provided
    assert not missing, (
        f"compute.bicep is missing critical runtime env var(s): {sorted(missing)}. "
        "These are not config *_env refs, so dropping them fails silently at run "
        "time. Re-add them to commonEnv (and the matching jobSecrets) in "
        "infra/bicep/modules/compute.bicep."
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
