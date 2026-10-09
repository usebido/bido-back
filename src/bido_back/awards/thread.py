"""What an email in an invite's thread needs: who sends it, to whom, under which subject.

Purchase orders and "not selected" notices are replies in the thread of the
RFQ email. They go out from the mailbox of the buyer who sent that RFQ email
(the requester of its executed `send_rfq_email` approval), so the supplier
sees one conversation, and to the invite's contact.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from bido_back.agents.context import ToolContext
from bido_back.db.models import (
    Approval,
    Contact,
    Invite,
    Message,
    Rfq,
    Supplier,
    Tenant,
    User,
)
from bido_back.email.types import OutboundAttachment, OutboundEmail
from bido_back.followups.service import reply_subject
from bido_back.rfqs.errors import NotFound
from bido_back.rfqs.language import thread_language
from bido_back.rfqs.send import SEND_RFQ_EMAIL, RfqActDeps, send_proposal_ids

_MESSAGES_UNIQUE = "messages_tenant_id_message_id_header_key"


@dataclass(frozen=True, slots=True)
class Thread:
    invite: Invite
    rfq: Rfq
    supplier: Supplier
    contact_email: str | None
    # None when the RFQ email was never sent through Bido.
    sender_user_id: UUID | None
    subject: str
    # The language the RFQ email went out in: every email of the thread keeps it.
    language: str


async def thread_of(session: AsyncSession, invite_id: UUID) -> Thread:
    row = (
        await session.execute(
            select(Invite, Rfq, Supplier, Contact.email)
            .join(Rfq, Rfq.id == Invite.rfq_id)
            .join(Supplier, Supplier.id == Invite.supplier_id)
            .join(Contact, Contact.id == Invite.contact_id)
            .where(Invite.id == invite_id)
        )
    ).one_or_none()
    if row is None:
        raise NotFound(f"invite {invite_id} not found")
    invite, rfq, supplier, contact_email = row
    sent = await session.scalar(
        select(Approval).where(
            Approval.tool == SEND_RFQ_EMAIL,
            Approval.tool_call_id == send_proposal_ids(invite.id)[1],
            Approval.status == "executed",
        )
    )
    sender: UUID | None = None
    if sent is not None:
        try:
            sender = UUID(str(sent.args.get("sender_user_id")))
        except ValueError:
            sender = None
    first_subject = await session.scalar(
        select(Message.subject).where(
            Message.invite_id == invite.id,
            Message.direction == "out",
            Message.message_id_header == invite.thread_message_id,
        )
    )
    return Thread(
        invite=invite,
        rfq=rfq,
        supplier=supplier,
        contact_email=contact_email,
        sender_user_id=sender,
        subject=reply_subject(first_subject, rfq.ref, rfq.title),
        language=thread_language(sent.args if sent is not None else None, supplier),
    )


@dataclass(frozen=True, slots=True)
class Outgoing:
    invite_id: UUID
    sender_user_id: UUID
    to: tuple[str, ...]
    cc: tuple[str, ...]
    subject: str
    body: str
    message_id: str
    thread_message_id: str
    attachments: tuple[OutboundAttachment, ...] = ()
    # An earlier attempt claimed `message_id` and may have sent it.
    possibly_sent: bool = False


async def deliver(
    session: AsyncSession, deps: RfqActDeps, ctx: ToolContext, email: Outgoing
) -> tuple[str, UUID | None]:
    """Send from the sender's mailbox as a reply in the thread; store the outbound message.

    Returns the Message-ID used and the `messages` row id (None if a retry
    already stored it).
    """
    buyer = await session.get(User, email.sender_user_id)
    tenant = await session.get(Tenant, ctx.tenant_id)
    if buyer is None or tenant is None:
        raise NotFound("sender or tenant not found")
    buyer_contact = await session.scalar(
        select(Contact)
        .where(
            Contact.kind == "buyer",
            Contact.email == buyer.email,
            Contact.pseudonymized_at.is_(None),
        )
        .order_by(Contact.created_at)
        .limit(1)
    )
    sender = await deps.mailbox_senders(session, ctx.tenant_id, buyer.id)
    mailbox_address: object = getattr(sender, "address", None)
    sent = await sender.send(
        OutboundEmail(
            from_name=(buyer_contact.name if buyer_contact else None) or tenant.name,
            from_address=mailbox_address
            if isinstance(mailbox_address, str) and mailbox_address
            else buyer.email,
            to=email.to,
            cc=email.cc,
            subject=email.subject,
            text_body=email.body,
            message_id=email.message_id,
            possibly_sent=email.possibly_sent,
            in_reply_to=email.thread_message_id,
            references=(email.thread_message_id,),
            attachments=email.attachments,
        )
    )
    message_id = sent.message_id or email.message_id
    row = await session.scalar(
        insert(Message)
        .values(
            tenant_id=ctx.tenant_id,
            invite_id=email.invite_id,
            direction="out",
            message_id_header=message_id,
            in_reply_to=email.thread_message_id,
            from_contact_id=buyer_contact.id if buyer_contact else None,
            subject=email.subject,
            body_text=email.body,
            status="stored",
            occurred_at=datetime.now(UTC),
        )
        .on_conflict_do_nothing(constraint=_MESSAGES_UNIQUE)
        .returning(Message.id)
    )
    return message_id, row
