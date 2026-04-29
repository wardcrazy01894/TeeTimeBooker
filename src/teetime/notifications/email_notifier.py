"""SMTP-based Notifier. Reads SMTP creds from env per NotifierConfig. Stub."""

from __future__ import annotations

from ..core.models import BookingResult


class EmailNotifier:
    """SMTP notifier. Concrete impl in M4.T1."""

    def __init__(
        self,
        *,
        smtp_host: str,
        smtp_user: str,
        smtp_password: str,
        recipient: str,
        smtp_port: int = 587,
    ) -> None:
        self._smtp_host = smtp_host
        self._smtp_user = smtp_user
        self._smtp_password = smtp_password
        self._recipient = recipient
        self._smtp_port = smtp_port

    async def notify(self, result: BookingResult) -> None:
        raise NotImplementedError
