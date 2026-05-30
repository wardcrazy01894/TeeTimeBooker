"""Azure Blob Storage lifecycle for the SQLite state file (AZURE_PLAN.md §6.2, §6.4).

The v0 `SqliteStore` knows nothing about Azure. On ACA Jobs the SQLite file lives
in a single blob (`teetime-state/teetime.db`) that each job execution downloads at
start, mutates locally during the run, and uploads on exit — the direct functional
equivalent of v0's `actions/cache/restore` + `actions/cache/save`.

`BlobStateManager` is a context manager that wraps the orchestrator's run/watch
execution:

    with BlobStateManager(account, "teetime-state", "teetime.db", sqlite_path) as db_path:
        # db_path is the local SQLite path; run the orchestrator against it
        ...

Concurrency safety (AZURE_PLAN.md §6.2): a 60-second exclusive blob lease is
acquired on enter and held for the whole run, renewed every 30 seconds by a
daemon thread. `parallelism = 1` is the primary defense; the lease is the
belt-and-suspenders guard against a manually triggered `az containerapp job start`
overlapping a scheduled run. A finite (not infinite) lease is used so a container
crash auto-expires the lease within 60 s rather than stranding it forever.

Auth uses `DefaultAzureCredential` (the user-assigned MI via `AZURE_CLIENT_ID`).
No connection string, no account key — only `AZURE_STORAGE_ACCOUNT_NAME` is config.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from types import TracebackType

from azure.core.exceptions import ResourceNotFoundError
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobLeaseClient, BlobServiceClient

log = logging.getLogger(__name__)

# Lease duration and renewal cadence (AZURE_PLAN.md §6.2). Renew at half the
# lease duration so a single missed renewal still leaves headroom.
_LEASE_DURATION_SECONDS = 60
_RENEW_INTERVAL_SECONDS = 30


class BlobStateManager:
    """Download → lease → (run) → upload → release lifecycle for the SQLite blob."""

    def __init__(
        self,
        account_name: str,
        container: str,
        blob_name: str,
        local_path: str | Path = "/tmp/teetime-state/teetime.db",
    ) -> None:
        self._account_name = account_name
        self._container = container
        self._blob_name = blob_name
        self._local_path = Path(local_path)

        self._blob_service: BlobServiceClient | None = None
        self._lease: BlobLeaseClient | None = None
        self._renew_stop = threading.Event()
        self._renew_thread: threading.Thread | None = None

    def __enter__(self) -> Path:
        account_url = f"https://{self._account_name}.blob.core.windows.net"
        self._blob_service = BlobServiceClient(
            account_url=account_url,
            credential=DefaultAzureCredential(),
        )
        blob_client = self._blob_service.get_blob_client(
            container=self._container,
            blob=self._blob_name,
        )

        # Ensure the local parent dir exists before any download/first-run write.
        self._local_path.parent.mkdir(parents=True, exist_ok=True)

        # Download the blob into the local path. A missing blob means first run:
        # no download, the orchestrator initializes a fresh DB locally, and the
        # upload-on-exit creates the blob. (AZURE_PLAN.md §6.3, §6.4)
        try:
            with self._local_path.open("wb") as fh:
                blob_client.download_blob().readinto(fh)
            log.info("Downloaded state blob %s -> %s", self._blob_name, self._local_path)
        except ResourceNotFoundError:
            log.info(
                "State blob %s not found; first run, initializing fresh DB at %s",
                self._blob_name,
                self._local_path,
            )

        # Acquire an exclusive 60 s lease and start the renewal thread.
        self._lease = BlobLeaseClient(blob_client)
        self._lease.acquire(lease_duration=_LEASE_DURATION_SECONDS)
        log.info("Acquired blob lease %s on %s", self._lease.id, self._blob_name)

        self._renew_stop.clear()
        self._renew_thread = threading.Thread(
            target=self._renew_loop,
            name="blob-lease-renew",
            daemon=True,
        )
        self._renew_thread.start()

        return self._local_path

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # Always stop the renewal thread first so it cannot renew a lease we are
        # about to release. Then upload (with the lease id) and release the lease.
        # Each step is best-effort and logged — we never swallow silently.
        self._renew_stop.set()
        if self._renew_thread is not None:
            self._renew_thread.join(timeout=_RENEW_INTERVAL_SECONDS)

        lease_id = self._lease.id if self._lease is not None else None

        try:
            if self._blob_service is not None:
                blob_client = self._blob_service.get_blob_client(
                    container=self._container,
                    blob=self._blob_name,
                )
                with self._local_path.open("rb") as fh:
                    # upload_blob accepts either a BlobLeaseClient or a lease-id str
                    # at runtime; the type stub only declares the former, so pass the
                    # id (per AZURE_PLAN.md §6.2 "Upload with lease ID") with a narrow ignore.
                    blob_client.upload_blob(fh, overwrite=True, lease=lease_id)  # type: ignore[arg-type]
                log.info("Uploaded state blob %s from %s", self._blob_name, self._local_path)
        except Exception:
            log.exception("Failed to upload state blob %s on exit", self._blob_name)
        finally:
            if self._lease is not None:
                try:
                    self._lease.release()
                    log.info("Released blob lease on %s", self._blob_name)
                except Exception:
                    log.exception("Failed to release blob lease on %s", self._blob_name)

    def _renew_loop(self) -> None:
        """Renew the lease every 30 s until signalled to stop (AZURE_PLAN.md §6.2)."""
        while not self._renew_stop.wait(_RENEW_INTERVAL_SECONDS):
            if self._lease is None:
                return
            try:
                self._lease.renew()
                log.debug("Renewed blob lease on %s", self._blob_name)
            except Exception:
                # A failed renewal is logged but not fatal here: the next scheduled
                # run reacquires cleanly once the lease auto-expires.
                log.exception("Failed to renew blob lease on %s", self._blob_name)
