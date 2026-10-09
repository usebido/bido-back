"""Threading headers for an email in an invite's thread (RFC 5322).

`In-Reply-To` is the latest message with this supplier, in either direction; `References`
runs from the RFQ email's Message-ID (the thread's root) to that parent. Quarantined
messages (auto-replies, bounces, a sender outside the supplier's domain) and Message-IDs
Bido made up for messages that had none are never a parent. The mailbox senders find the
provider thread from these references (the root is always first), so the email stays in
the buyer's thread.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bido_back.db.models import Message

# References kept: the root and the latest ones (RFC 5322 allows trimming the middle).
MAX_REFERENCES: Final = 10
# Intake stores a synthetic id for an inbound message without one; it is nobody's parent.
_SYNTHETIC_SUFFIX: Final = "@inbound.invalid>"


@dataclass(frozen=True, slots=True)
class ThreadMessage:
    message_id: str
    at: datetime


def _usable(message_id: str) -> bool:
    return (
        message_id.startswith("<")
        and message_id.endswith(">")
        and "@" in message_id
        and not message_id.endswith(_SYNTHETIC_SUFFIX)
    )


def thread_headers(root: str, messages: Sequence[ThreadMessage]) -> tuple[str, tuple[str, ...]]:
    """(In-Reply-To, References) for a reply in the thread whose first message is `root`."""
    ordered = sorted((m for m in messages if _usable(m.message_id)), key=lambda m: m.at)
    chain: list[str] = [root]
    for message in ordered:
        if message.message_id not in chain:
            chain.append(message.message_id)
    parent = chain[-1]
    if len(chain) > MAX_REFERENCES:
        chain = [chain[0], *chain[-(MAX_REFERENCES - 1) :]]
    return parent, tuple(chain)


async def reply_headers(
    session: AsyncSession, invite_id: UUID, root: str
) -> tuple[str, tuple[str, ...]]:
    """The threading headers of the next email to an invite's supplier (reads only)."""
    rows = await session.execute(
        select(Message.message_id_header, Message.occurred_at)
        .where(
            Message.invite_id == invite_id,
            Message.message_id_header.is_not(None),
            Message.status != "quarantined",
        )
        .order_by(Message.occurred_at, Message.created_at, Message.id)
    )
    messages = [ThreadMessage(message_id, at) for message_id, at in rows if message_id]
    return thread_headers(root, messages)
