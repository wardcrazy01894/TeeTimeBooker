"""`compute.bicep`'s booking/watch job wiring. MU-15a derived the booking-job loop from
`release_events.json` and added `bookingMode` / `watchMode` params so one set of job resources
could run either the TOML or the tenant path; MU-19 (2026-11-14, MULTIUSER_PLAN §11 step 9)
removed the params and the TOML wiring, so the jobs can only run the tenant path. Static text
assertions — bicep is compile-validated by CI's `az bicep build`, not pytest-importable (see the
other `test_compute_bicep_*` files).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

COMPUTE_BICEP = (
    Path(__file__).resolve().parent.parent / "infra" / "bicep" / "modules" / "compute.bicep"
)


@pytest.fixture(scope="module")
def bicep() -> str:
    return COMPUTE_BICEP.read_text()


@pytest.fixture(scope="module")
def parts(bicep: str) -> tuple[str, str]:
    assert "resource watchJob" in bicep, "watch job resource not found"
    booking_part, watch_part = bicep.split("resource watchJob", 1)
    return booking_part, watch_part


def test_booking_loop_reads_release_events_json(bicep: str) -> None:
    assert "loadJsonContent('../release_events.json')" in bicep


def _args(text: str) -> list[str]:
    """The entries of the first container `args: [...]` array in ``text``, inline comments
    stripped."""
    start = text.index("          args: [")
    block = text[start : text.index("\n          ]", start)]
    return [line.split(" //")[0].strip() for line in block.splitlines()[1:]]


def test_booking_jobs_run_tenant_run_with_wait(parts: tuple[str, str]) -> None:
    """Every booking job: `teetime tenant-run --event <key> --wait --dry-run <x>`. `--wait` is
    the race path (core/CLAUDE.md), so losing it would silently book off the release instant."""
    booking_part, _ = parts
    assert _args(booking_part) == [
        "'tenant-run'",
        "'--event'",
        "job.eventKey",
        "'--wait'",
        "'--dry-run'",
        "dryRun ? 'true' : 'false'",
    ]
    assert "env: jobEnv" in booking_part
    assert "secrets: jobSecrets" in booking_part


def test_watch_job_runs_tenant_watch(parts: tuple[str, str]) -> None:
    _, watch_part = parts
    watch_job = watch_part.split("resource migrateJob", 1)[0]
    assert _args(watch_job) == ["'tenant-watch'", "'--dry-run'", "dryRun ? 'true' : 'false'"]
    assert "env: jobEnv" in watch_job
    assert "secrets: jobSecrets" in watch_job


def test_job_secrets_are_exactly_what_the_tenant_commands_read(bicep: str) -> None:
    """ACA validates every KV secret ref at job-CREATE time, so the list is a deploy
    prerequisite (MULTIUSER_PLAN §10.1): the shared 2captcha key plus the three tenant secrets.
    A new ref needs its secret created in BOTH vaults before the deploy that adds it."""
    start = bicep.index("var jobSecrets = [")
    block = bicep[start : bicep.index("\n]", start)]
    assert sorted(re.findall(r"secrets/([A-Z0-9-]+)'", block)) == [
        "ACS-EMAIL-CONNECTION",
        "OPERATOR-NOTIFY-EMAIL",
        "TENANT-CREDS-KEYRING",
        "TWOCAPTCHA-API-KEY",
    ]
    # Every secretRef an env entry names resolves to a declared job secret (a dangling ref
    # fails the deploy with InvalidParameterValueInContainerTemplate).
    declared = set(re.findall(r"\{ name: '([a-z0-9-]+)',\s+keyVaultUrl", block))
    assert set(re.findall(r"secretRef: '([a-z0-9-]+)'", bicep)) == declared


def test_tenant_env_refs_wired_in_compute_bicep(bicep: str) -> None:
    # Every env var name the tenant settings loaders read (booking_job.py, cosmos/store.py,
    # acs_email.py) must be wired somewhere in compute.bicep, as a secretRef or a plain value.
    required_env_vars = [
        "TWOCAPTCHA_API_KEY",
        "TEETIME_ENV",
        "TENANT_COSMOS_ENDPOINT",
        "TENANT_COSMOS_DATABASE",
        "AZURE_CLIENT_ID",
        "TENANT_CREDS_KEYRING",
        "ACS_EMAIL_CONNECTION",
        "ACS_EMAIL_SENDER",
        "OPERATOR_NOTIFY_EMAIL",
    ]
    start = bicep.index("var jobEnv = [")
    job_env = bicep[start : bicep.index("\n]", start)]
    for name in required_env_vars:
        assert f"'{name}'" in job_env, f"{name} is not wired in compute.bicep's jobEnv"


def test_job_names_le_32_chars() -> None:
    # ACA job names are capped at 32 chars. Mirrors the naming rule compute.bicep applies to
    # release_events.json: legacy jobNamePrefix -> "<prefix>-<env>-edt/-est"; otherwise the
    # generic "teetime-rel-<key>-<env>-<half>" (MULTIUSER_PLAN §6.2), which bounds key<=10 for
    # a realistic (<=4-char) env name.

    events = json.loads(
        (
            Path(__file__).resolve().parent.parent / "infra" / "bicep" / "release_events.json"
        ).read_text()
    )
    for event in events:
        for env in ("dev", "prod"):
            for half, suffix in (("edt", "edt"), ("est", "est")):
                prefix = event["jobNamePrefix"]
                name = (
                    f"{prefix}-{env}-{suffix}"
                    if prefix
                    else f"teetime-rel-{event['key']}-{env}-{half}"
                )
                assert len(name) <= 32, name

    # Boundary check on the generic formula itself (no event ships this yet). "teetime-rel-"
    # (12) + key + "-" + env (<=4 for dev/prod) + "-" + half (3) <= 32 => key <= 11.
    generic_name = f"teetime-rel-{'x' * 11}-prod-dst"
    assert len(generic_name) <= 32
    too_long_name = f"teetime-rel-{'x' * 12}-prod-dst"
    assert len(too_long_name) > 32


# --- MU-19: the TOML job wiring is retired -----------------------------------------------------

_BICEP_DIR = COMPUTE_BICEP.parent.parent
_WORKFLOW = _BICEP_DIR.parent.parent / ".github" / "workflows" / "azure-iac.yml"
_MODE_FILES = (
    COMPUTE_BICEP,
    _BICEP_DIR / "main.bicep",
    _BICEP_DIR / "main.bicepparam.dev",
    _BICEP_DIR / "main.bicepparam.prod",
    _WORKFLOW,
)
# Key Vault secrets (and the env vars they fed) that only the TOML path read. The tenant path
# takes ForeUP credentials from the encrypted per-user store and skips from the tenant store.
_TOML_ONLY_SECRETS = (
    "MB-USERNAME",
    "MB-PASSWORD",
    "PLAYER1-EMAIL",
    "PLAYER1-PHONE",
    "PLAYER1-MB-MEMBER",
    "TEETIME-SKIP-DATES",
)


def _code_lines(text: str) -> str:
    """The text without full-line comments (`//` in Bicep, `#` in YAML): comments may still
    tell the history, code may not carry the wiring."""
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith(("//", "#"))
    )


def test_no_toml_mode_remaining() -> None:
    """MULTIUSER_PLAN §12 MU-19's named test: no mode param, no mode ternary and no TOML job
    argument is left in the Bicep or the deploy workflow, so an ACA job can only run the tenant
    path. (The TOML CLI itself stays for local course testing; MU-20 was dropped.)"""
    for path in _MODE_FILES:
        code = _code_lines(path.read_text())
        for token in ("bookingMode", "watchMode", "BOOKING_MODE", "WATCH_MODE"):
            assert token not in code, f"{path.name} still carries {token}"
        assert "== 'tenant'" not in code, f"{path.name} still has a mode ternary"
        assert "'toml'" not in code, f"{path.name} still names the toml mode"
    compute = _code_lines(COMPUTE_BICEP.read_text())
    assert "container.toml" not in compute, "a job still passes --config container.toml"
    assert "'--config'" not in compute
    for secret in _TOML_ONLY_SECRETS:
        assert f"secrets/{secret}" not in compute, f"{secret} is still a job secret ref"
        assert f"'{secret.replace('-', '_')}'" not in compute, f"{secret} env var still wired"
