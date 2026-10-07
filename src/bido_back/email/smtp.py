"""Sending over plain SMTP (Mailpit locally: localhost:1025, UI on :8025)."""

import asyncio
import smtplib

from bido_back.email.mime import build_mime, recipients
from bido_back.email.types import OutboundEmail, SentEmail


class SmtpEmailSender:
    """`EmailSender` over smtplib, run in a thread (smtplib blocks).

    Meant for local development against Mailpit: no TLS, no auth. AWS uses
    `SesEmailSender`.
    """

    def __init__(self, host: str, port: int, *, timeout_s: float = 10.0) -> None:
        self._host = host
        self._port = port
        self._timeout_s = timeout_s

    async def send(self, email: OutboundEmail) -> SentEmail:
        msg = build_mime(email)
        raw = msg.as_bytes()
        await asyncio.to_thread(self._send_sync, email.from_address, recipients(email), raw)
        return SentEmail(message_id=str(msg["Message-ID"]), raw=raw)

    def _send_sync(self, envelope_from: str, to: list[str], raw: bytes) -> None:
        with smtplib.SMTP(self._host, self._port, timeout=self._timeout_s) as smtp:
            smtp.sendmail(envelope_from, to, raw)
