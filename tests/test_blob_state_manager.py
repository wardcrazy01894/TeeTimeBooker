"""Tests for BlobStateManager (AZURE_PLAN.md §6.2, §6.4).

All Azure SDK objects are mocked — no live Azure required. The manager is the
unit under test; BlobServiceClient / BlobLeaseClient / DefaultAzureCredential
are collaborators and are patched at the import site in the module.
"""

from __future__ import annotations

import threading
from pathlib import Path
from unittest import mock

import pytest
from azure.core.exceptions import ResourceNotFoundError

from teetime.persistence import blob_state_manager as bsm
from teetime.persistence.blob_state_manager import BlobStateManager


@pytest.fixture
def patched_azure(monkeypatch):
    """Patch the three Azure SDK names the manager imports.

    Returns a namespace with handles to the mock blob service, blob client,
    lease client, and credential so tests can assert on them.
    """
    blob_client = mock.MagicMock(name="blob_client")
    # download_blob().readinto(fh) by default succeeds (blob exists).
    download = mock.MagicMock(name="download")
    blob_client.download_blob.return_value = download

    blob_service = mock.MagicMock(name="blob_service")
    blob_service.get_blob_client.return_value = blob_client

    service_cls = mock.MagicMock(name="BlobServiceClient", return_value=blob_service)

    lease = mock.MagicMock(name="lease")
    lease.id = "lease-abc-123"
    lease_cls = mock.MagicMock(name="BlobLeaseClient", return_value=lease)

    credential_cls = mock.MagicMock(name="DefaultAzureCredential")

    monkeypatch.setattr(bsm, "BlobServiceClient", service_cls)
    monkeypatch.setattr(bsm, "BlobLeaseClient", lease_cls)
    monkeypatch.setattr(bsm, "DefaultAzureCredential", credential_cls)

    return mock.Mock(
        service_cls=service_cls,
        blob_service=blob_service,
        blob_client=blob_client,
        download=download,
        lease=lease,
        lease_cls=lease_cls,
        credential_cls=credential_cls,
    )


def _manager(tmp_path: Path) -> BlobStateManager:
    return BlobStateManager(
        account_name="teetimedevsa",
        container="teetime-state",
        blob_name="teetime.db",
        local_path=tmp_path / "teetime.db",
    )


def test_enter_acquires_lease(patched_azure, tmp_path):
    mgr = _manager(tmp_path)
    with mgr as db_path:
        assert isinstance(db_path, Path)
        patched_azure.lease.acquire.assert_called_once()
        # 60-second finite lease (AZURE_PLAN.md §6.2), never infinite (-1).
        _, kwargs = patched_azure.lease.acquire.call_args
        assert kwargs.get("lease_duration") == 60


def test_enter_downloads_blob_to_local_path(patched_azure, tmp_path):
    mgr = _manager(tmp_path)
    with mgr:
        patched_azure.blob_client.download_blob.assert_called_once()
        patched_azure.download.readinto.assert_called_once()


def test_enter_uses_account_url_and_credential(patched_azure, tmp_path):
    mgr = _manager(tmp_path)
    with mgr:
        _, kwargs = patched_azure.service_cls.call_args
        assert kwargs["account_url"] == "https://teetimedevsa.blob.core.windows.net"
        # DefaultAzureCredential is instantiated and passed as the credential.
        patched_azure.credential_cls.assert_called_once()


def test_renewal_thread_calls_renew(patched_azure, tmp_path, monkeypatch):
    """The renewal thread fires renew() on its cadence while inside the block.

    Shrink the renew interval to a few ms (instead of 30 s) so the daemon thread
    runs at least one real iteration inside the block. This patches only the
    module constant — no global threading internals are touched, so it cannot
    interfere with other tests.
    """
    renew_fired = threading.Event()

    def fake_renew() -> None:
        renew_fired.set()

    patched_azure.lease.renew.side_effect = fake_renew

    monkeypatch.setattr(bsm, "_RENEW_INTERVAL_SECONDS", 0.01)

    mgr = _manager(tmp_path)
    with mgr:
        # Wait for the daemon thread to complete at least one renew iteration.
        assert renew_fired.wait(timeout=2.0), "renewal thread never called renew()"

    patched_azure.lease.renew.assert_called()


def test_exit_uploads_with_lease_id(patched_azure, tmp_path):
    mgr = _manager(tmp_path)
    with mgr:
        pass
    patched_azure.blob_client.upload_blob.assert_called_once()
    _, kwargs = patched_azure.blob_client.upload_blob.call_args
    assert kwargs["lease"] == "lease-abc-123"
    assert kwargs["overwrite"] is True


def test_exit_releases_lease(patched_azure, tmp_path):
    mgr = _manager(tmp_path)
    with mgr:
        pass
    patched_azure.lease.release.assert_called_once()


def test_exit_stops_renewal_thread(patched_azure, tmp_path):
    mgr = _manager(tmp_path)
    with mgr:
        thread = mgr._renew_thread
        assert thread is not None
        assert thread.is_alive()
    assert mgr._renew_stop.is_set()
    # Thread is a daemon and joined on exit; should be stopped.
    assert not mgr._renew_thread.is_alive()


def test_first_run_missing_blob_no_download_but_uploads(patched_azure, tmp_path):
    """Blob 404 on enter -> treated as first run: no readinto, fresh DB, upload still happens."""
    patched_azure.blob_client.download_blob.side_effect = ResourceNotFoundError("404")

    mgr = _manager(tmp_path)
    with mgr as db_path:
        # Parent dir created so a fresh DB can be written by the orchestrator.
        assert db_path.parent.exists()
        # Lease still acquired on first run.
        patched_azure.lease.acquire.assert_called_once()

    # Upload still happens on exit (creates the blob).
    patched_azure.blob_client.upload_blob.assert_called_once()
    patched_azure.lease.release.assert_called_once()


def test_crash_inside_block_still_releases_and_stops(patched_azure, tmp_path):
    """An exception inside the with-block still releases the lease and stops the thread."""
    mgr = _manager(tmp_path)

    class BoomError(Exception):
        pass

    with pytest.raises(BoomError), mgr:
        thread = mgr._renew_thread
        assert thread is not None and thread.is_alive()
        raise BoomError("simulated crash")

    patched_azure.lease.release.assert_called_once()
    assert mgr._renew_stop.is_set()
    assert not mgr._renew_thread.is_alive()


def test_upload_failure_still_releases_lease(patched_azure, tmp_path):
    """If upload raises on exit, the lease is still released (not stranded)."""
    patched_azure.blob_client.upload_blob.side_effect = RuntimeError("upload boom")

    mgr = _manager(tmp_path)
    # __exit__ swallows the upload error (logged), so the with-block exits cleanly.
    with mgr:
        pass

    patched_azure.lease.release.assert_called_once()
