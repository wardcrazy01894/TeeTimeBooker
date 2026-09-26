"""MU-15a: `compute.bicep` gains a `bookingMode` / `watchMode` param (default `toml`, so the
first deploy changes nothing) and derives its booking-job loop from `release_events.json`
instead of two hand-written cron vars. Static text assertions — bicep is compile-validated by
CI's `az bicep build`, not pytest-importable (see the other `test_compute_bicep_*` files).
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


def test_booking_mode_param_defaults_to_toml(bicep: str) -> None:
    m = re.search(r"param bookingMode string = '(\w+)'", bicep)
    assert m is not None, "bookingMode param not found or has no default"
    assert m.group(1) == "toml"


def test_watch_mode_param_defaults_to_toml(bicep: str) -> None:
    m = re.search(r"param watchMode string = '(\w+)'", bicep)
    assert m is not None, "watchMode param not found or has no default"
    assert m.group(1) == "toml"


def test_booking_loop_reads_release_events_json(bicep: str) -> None:
    assert "loadJsonContent('../release_events.json')" in bicep


def test_compute_default_mode_is_toml() -> None:
    """MULTIUSER_PLAN §12 MU-15a's named test: both compute.bicep AND both param files must
    default/set bookingMode + watchMode to 'toml', so a plain merge of this PR changes nothing
    about what either env's ACA jobs run."""
    dev_params = (
        Path(__file__).resolve().parent.parent / "infra" / "bicep" / "main.bicepparam.dev"
    ).read_text()
    prod_params = (
        Path(__file__).resolve().parent.parent / "infra" / "bicep" / "main.bicepparam.prod"
    ).read_text()
    for params in (dev_params, prod_params):
        assert "param bookingMode = 'toml'" in params
        assert "param watchMode = 'toml'" in params


def test_toml_mode_booking_args_are_unchanged(parts: tuple[str, str]) -> None:
    booking_part, _ = parts
    assert "'run'" in booking_part
    assert "'--config'" in booking_part
    assert "'/app/config/container.toml'" in booking_part
    assert "'--wait'" in booking_part
    assert "dryRun ? 'true' : 'false'" in booking_part


def test_tenant_mode_booking_args_present(parts: tuple[str, str]) -> None:
    booking_part, _ = parts
    assert "'tenant-run'" in booking_part
    assert "'--event'" in booking_part
    assert "bookingMode == 'tenant'" in booking_part


def test_toml_mode_watch_args_are_unchanged(parts: tuple[str, str]) -> None:
    _, watch_part = parts
    assert "'watch'" in watch_part
    assert "'--config'" in watch_part


def test_tenant_mode_watch_args_present(parts: tuple[str, str]) -> None:
    _, watch_part = parts
    assert "'tenant-watch'" in watch_part
    assert "watchMode == 'tenant'" in watch_part


def test_toml_mode_never_references_tenant_secrets_unconditionally(bicep: str) -> None:
    # A KV secret ref that ACA validates at job-CREATE time must never be added
    # unconditionally, or the toml-mode (default) dev auto-deploy would fail on a Key Vault
    # secret the operator has not created yet (TENANT-CREDS-KEYRING, ACS-EMAIL-CONNECTION,
    # OPERATOR-NOTIFY-EMAIL do not exist until the operator pre-creates them, MULTIUSER_PLAN
    # §10.1). The unconditional `jobSecrets` array (today's TOML secrets) must NOT contain any
    # tenant-only secret name; the tenant secrets live in a separate array that is only ever
    # spliced in behind a `== 'tenant'` ternary at its use sites.
    unconditional_block_start = bicep.index("var jobSecrets = [")
    unconditional_block_end = bicep.index("]", unconditional_block_start)
    unconditional_block = bicep[unconditional_block_start:unconditional_block_end]
    tenant_secret_names = ["TENANT-CREDS-KEYRING", "ACS-EMAIL-CONNECTION", "OPERATOR-NOTIFY-EMAIL"]
    for name in tenant_secret_names:
        assert name not in unconditional_block, f"{name} leaked into the unconditional jobSecrets"
        assert name in bicep, f"{name} must still be wired somewhere (the tenant-mode array)"

    # Every reference to the tenant-only secrets array must sit next to a `== 'tenant'` gate.
    for marker in ("jobSecretsTenant", "tenantEnv"):
        start = 0
        found_use_site = False
        while True:
            idx = bicep.find(marker, start)
            if idx == -1:
                break
            start = idx + 1
            line_start = bicep.rfind("\n", 0, idx) + 1
            line_end = bicep.find("\n", idx)
            line = bicep[line_start:line_end]
            stripped = line.strip()
            if stripped.startswith(f"var {marker}") or stripped.startswith("//"):
                continue  # the declaration itself, or a comment mentioning it — not a use site
            assert "== 'tenant'" in line, f"un-gated use of {marker}: {line!r}"
            found_use_site = True
        assert found_use_site, f"{marker} is never used"


def test_tenant_env_refs_wired_in_compute_bicep(bicep: str) -> None:
    # Every env var name the tenant settings loaders read (booking_job.py, cosmos/store.py,
    # acs_email.py) must be wired somewhere in compute.bicep, as a secretRef or a plain value.
    required_env_vars = [
        "TENANT_COSMOS_ENDPOINT",
        "TENANT_COSMOS_DATABASE",
        "AZURE_CLIENT_ID",
        "TENANT_CREDS_KEYRING",
        "ACS_EMAIL_CONNECTION",
        "ACS_EMAIL_SENDER",
        "OPERATOR_NOTIFY_EMAIL",
    ]
    for name in required_env_vars:
        assert f"'{name}'" in bicep, f"{name} is not wired in compute.bicep"


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
