"""An invite's email thread: what was exchanged with one supplier about one RFQ.

Unlike the activity feed (`bido_back.rfqs.activity`), this returns content: each
message's subject, text body, sender and attachment names, so the buyer reads the
thread in Bido instead of opening their inbox. It is returned only to an
authenticated member of the company that owns the RFQ (the `tenant_session`
below, forced RLS); for anyone else the RFQ and the invite do not exist (404).

What is stored is what is shown: the text body (outbound HTML and recipients are
not kept), and attachments of inbound emails only. Quarantined messages (an
auto-reply, a bounce, a sender outside the supplier's domain) are listed and
flagged. Nothing read here is logged.

Three queries whatever the thread's length: the invite, its messages with their
senders, their attachments.
"""

from collections import defaultdict
from typing import Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.auth import Principal
from bido_back.db import tenant_session
from bido_back.db.models import Attachment, Contact, Invite, Message, Supplier
from bido_back.rfqs.errors import NotFound
from bido_back.rfqs.schemas import InviteMessage, InviteMessages, MessageAttachment
from bido_back.uploads import files as stored

type Db = AsyncEngine | async_sessionmaker[AsyncSession]

QUARANTINED: Final = "quarantined"


async def invite_messages(
    db: Db, principal: Principal, rfq_id: UUID, invite_id: UUID
) -> InviteMessages:
    """The invite's messages, oldest first. `NotFound` when the invite is not this RFQ's."""
    async with tenant_session(db, principal.tenant_id) as session:
        supplier_name = await session.scalar(
            select(Supplier.name)
            .join(Invite, Invite.supplier_id == Supplier.id)
            .where(Invite.id == invite_id, Invite.rfq_id == rfq_id)
        )
        if supplier_name is None:
            raise NotFound(f"invite {invite_id} not found")
        rows = (
            await session.execute(
                select(Message, Contact.name, Contact.email)
                .outerjoin(Contact, Contact.id == Message.from_contact_id)
                .where(Message.invite_id == invite_id)
                .order_by(Message.occurred_at, Message.created_at, Message.id)
            )
        ).all()
        attachments: defaultdict[UUID, list[MessageAttachment]] = defaultdict(list)
        if rows:
            for attachment in await session.scalars(
                select(Attachment)
                .where(Attachment.message_id.in_([message.id for message, _, _ in rows]))
                .order_by(Attachment.created_at, Attachment.storage_key)
            ):
                attachments[attachment.message_id].append(
                    MessageAttachment(
                        id=attachment.id,
                        filename=attachment.filename,
                        content_type=stored.served_type(stored.file_ref(attachment)),
                        size_bytes=attachment.size_bytes,
                    )
                )
    return InviteMessages(
        invite_id=invite_id,
        supplier_name=supplier_name,
        messages=[
            InviteMessage(
                id=message.id,
                direction="in" if message.direction == "in" else "out",
                origin=message.origin,
                status=message.status,
                quarantined=message.status == QUARANTINED,
                subject=message.subject,
                body_text=message.body_text,
                from_name=name,
                from_email=email,
                occurred_at=message.occurred_at,
                attachments=attachments[message.id],
            )
            for message, name, email in rows
        ],
    )
