"""Email types shared by sending, receiving and the act tools that send.

Only the contract lives here; transports (SMTP for Mailpit, SES) and inbound
parsing are implemented next to it.
"""

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True, slots=True)
class OutboundAttachment:
    filename: str
    content_type: str
    data: bytes


@dataclass(frozen=True, slots=True)
class OutboundEmail:
    from_name: str
    from_address: str
    to: tuple[str, ...]
    subject: str
    text_body: str
    cc: tuple[str, ...] = ()
    reply_to: str | None = None
    # Our own Message-ID (angle brackets included), set by the caller so it can be
    # stored before sending and matched on replies.
    message_id: str | None = None
    in_reply_to: str | None = None
    references: tuple[str, ...] = ()
    attachments: tuple[OutboundAttachment, ...] = field(default=())


@dataclass(frozen=True, slots=True)
class SentEmail:
    message_id: str
    raw: bytes


class EmailSender(Protocol):
    async def send(self, email: OutboundEmail) -> SentEmail: ...
