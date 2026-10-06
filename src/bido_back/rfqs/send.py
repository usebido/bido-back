"""The `send_rfq_email` act tool: send one approved RFQ email from the buyer's mailbox.

It is never offered to a model and never run by the agent runner. The RFQ
service records a proposal for it (`prepare_send`); an approver approves; the
worker's `ApprovalExecutor` runs this handler once, with the APPROVED
arguments (re-validated and re-hashed by the executor first).

Whose mailbox: the email goes out from the connected mailbox (Gmail or
Microsoft 365) of the buyer who REQUESTED the send, `sender_user_id` in the
approved arguments, never the approver's. The handler checks that this is
the requester recorded on the approval for this invite. The approver is the
`ToolContext.user_id` the executor passes; both are recorded in the
`rfq.email_sent` audit event.

What is sent is exactly `to`, `cc`, `subject` (carrying the `[RFQ-<n>]`
marker) and `body` from the approved arguments. The handler adds only the
From name/address (the buyer) and our own Message-ID.

Idempotency (the executor may retry after a crash):

1. In one transaction, the invite is locked; an invite already `sent` is a
   no-op. Otherwise a Message-ID is generated (or the one stored by an earlier
   attempt is reused), stored as `invites.thread_message_id`, and that
   transaction commits BEFORE anything is sent.
2. In a second transaction: the buyer's mailbox sender is obtained (it may
   use the session, e.g. to store the provider thread id), the email is sent,
   the outbound `messages` row is stored (unique per tenant and Message-ID, so
   a retry adds nothing), the invite becomes `sent`, a draft RFQ becomes
   `open`, and the audit event is written.

A crash after the send and before that commit resends on retry with the SAME
Message-ID, so mail clients and reply matching see one thread. Removing that
window needs the transactional outbox, which does not exist yet.
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Final
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.agents.context import ToolContext
from bido_back.agents.tools import Tool
from bido_back.approvals.executor import ActToolRegistry
from bido_back.db import tenant_session
from bido_back.db.models import Approval, AuditEvent, Contact, Invite, Message, Rfq, Tenant, User
from bido_back.email.addresses import make_message_id
from bido_back.email.types import OutboundEmail
from bido_back.rfqs.errors import InvalidState, NotFound
from bido_back.rfqs.mailbox import MailboxSenderFactory, default_mailbox_sender
from bido_back.rfqs.schemas import EmailAddress

logger = logging.getLogger(__name__)

SEND_RFQ_EMAIL: Final = "send_rfq_email"

_ALREADY_SENT: Final = frozenset({"sent", "responded", "declined"})
_MESSAGES_UNIQUE = "messages_tenant_id_message_id_header_key"
# Namespace for the deterministic run id of a send proposal (one per invite).
_SEND_NAMESPACE: Final = UUID("6f1d2c0e-5b8a-4f53-9a43-2f6b1f0c7e11")


def send_proposal_ids(invite_id: UUID) -> tuple[UUID, str]:
    """The (run_id, tool_call_id) under which an invite's send is proposed."""
    return uuid5(_SEND_NAMESPACE, f"{SEND_RFQ_EMAIL}:{invite_id}"), f"{SEND_RFQ_EMAIL}:{invite_id}"


def subject_marker(rfq_ref: str) -> str:
    """The marker every RFQ subject carries, e.g. `[RFQ-12]`."""
    return f"[{rfq_ref}]"


class SendRfqEmailArgs(BaseModel):
    """Exactly what the approver sees and approves."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    invite_id: UUID
    # The requesting buyer: the email goes out from their connected mailbox.
    sender_user_id: UUID
    to: list[EmailAddress] = Field(min_length=1, max_length=10)
    cc: list[EmailAddress] = Field(default_factory=list[str], max_length=10)
    # One line: it becomes a header.
    subject: Annotated[str, StringConstraints(min_length=1, max_length=300, pattern=r"^[^\r\n]*$")]
    body: str = Field(min_length=1, max_length=100_000)


class SendRfqEmailResult(BaseModel):
    invite_id: UUID
    message_id: str
    already_sent: bool


@dataclass(frozen=True, slots=True)
class RfqActDeps:
    db: AsyncEngine | async_sessionmaker[AsyncSession]
    email_domain: str
    # Default: the buyer's connected mailbox (bido_back.mailbox, imported lazily).
    mailbox_senders: MailboxSenderFactory = field(default=default_mailbox_sender)


@dataclass(frozen=True, slots=True)
class _Claim:
    message_id: str
    rfq_id: UUID


class _SendRfqEmail:
    def __init__(self, deps: RfqActDeps) -> None:
        self._deps = deps

    async def __call__(self, ctx: ToolContext, args: SendRfqEmailArgs) -> SendRfqEmailResult:
        claim = await self._claim(ctx, args)
        if isinstance(claim, SendRfqEmailResult):
            return claim
        message_id = await self._send_and_record(ctx, args, claim)
        logger.info(
            "sent rfq email tenant=%s invite=%s run=%s", ctx.tenant_id, args.invite_id, ctx.run_id
        )
        return SendRfqEmailResult(
            invite_id=args.invite_id, message_id=message_id, already_sent=False
        )

    async def _claim(self, ctx: ToolContext, args: SendRfqEmailArgs) -> _Claim | SendRfqEmailResult:
        """Check who sends, then store the invite's Message-ID before sending (committed)."""
        run_id, call_id = send_proposal_ids(args.invite_id)
        if ctx.run_id != run_id:
            raise InvalidState(f"invite {args.invite_id}: approval was not proposed for it")
        async with tenant_session(self._deps.db, ctx.tenant_id) as session:
            requester = await session.scalar(
                select(Approval.requested_by).where(
                    Approval.run_id == str(run_id),
                    Approval.tool_call_id == call_id,
                    Approval.tool == SEND_RFQ_EMAIL,
                )
            )
            if requester is None or requester != args.sender_user_id:
                raise InvalidState(f"invite {args.invite_id}: sender is not the requester")

            invite = await session.scalar(
                select(Invite).where(Invite.id == args.invite_id).with_for_update()
            )
            if invite is None:
                raise NotFound(f"invite {args.invite_id} not found")
            if invite.status in _ALREADY_SENT:
                logger.info("invite %s already %s; send is a no-op", invite.id, invite.status)
                return SendRfqEmailResult(
                    invite_id=invite.id,
                    message_id=invite.thread_message_id or "",
                    already_sent=True,
                )
            if invite.status != "pending_approval":
                raise InvalidState(f"invite {invite.id} is {invite.status}, not pending_approval")
            if invite.thread_message_id is None:
                invite.thread_message_id = make_message_id(self._deps.email_domain)
                await session.flush()
            return _Claim(message_id=invite.thread_message_id, rfq_id=invite.rfq_id)

    async def _send_and_record(
        self, ctx: ToolContext, args: SendRfqEmailArgs, claim: _Claim
    ) -> str:
        now = datetime.now(UTC)
        async with tenant_session(self._deps.db, ctx.tenant_id) as session:
            invite = await session.scalar(
                select(Invite).where(Invite.id == args.invite_id).with_for_update()
            )
            if invite is None:
                raise NotFound(f"invite {args.invite_id} not found")
            if invite.status in _ALREADY_SENT:
                return invite.thread_message_id or claim.message_id
            buyer = await session.get(User, args.sender_user_id)
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

            # The session stays open through the send: the mailbox sender may use
            # it (token refresh, binding the provider thread to this invite).
            sender = await self._deps.mailbox_senders(session, ctx.tenant_id, buyer.id)
            # The connected mailbox's own address when the sender exposes it
            # (it forces From to that address anyway); else the user's email.
            mailbox_address: object = getattr(sender, "address", None)
            sent = await sender.send(
                OutboundEmail(
                    from_name=(buyer_contact.name if buyer_contact else None) or tenant.name,
                    from_address=mailbox_address
                    if isinstance(mailbox_address, str) and mailbox_address
                    else buyer.email,
                    to=tuple(args.to),
                    cc=tuple(args.cc),
                    subject=args.subject,
                    text_body=args.body,
                    message_id=claim.message_id,
                )
            )
            # A provider that rewrites the Message-ID wins: replies reference
            # the header the supplier actually received.
            message_id = sent.message_id or claim.message_id

            message_row = await session.scalar(
                insert(Message)
                .values(
                    tenant_id=ctx.tenant_id,
                    invite_id=invite.id,
                    direction="out",
                    message_id_header=message_id,
                    from_contact_id=buyer_contact.id if buyer_contact else None,
                    subject=args.subject,
                    body_text=args.body,
                    status="stored",
                    occurred_at=now,
                )
                .on_conflict_do_nothing(constraint=_MESSAGES_UNIQUE)
                .returning(Message.id)
            )
            invite.thread_message_id = message_id
            invite.status = "sent"
            invite.sent_at = now
            await session.execute(
                update(Rfq)
                .where(Rfq.id == claim.rfq_id, Rfq.status == "draft")
                .values(status="open")
            )
            # Ids only: who asked (and whose mailbox sent it) and who approved.
            session.add(
                AuditEvent(
                    tenant_id=ctx.tenant_id,
                    actor_user_id=ctx.user_id,
                    action="rfq.email_sent",
                    payload={
                        "invite_id": str(invite.id),
                        "rfq_id": str(claim.rfq_id),
                        "message_row_id": str(message_row) if message_row else None,
                        "requested_by": str(args.sender_user_id),
                        "approved_by": str(ctx.user_id) if ctx.user_id else None,
                        "run_id": str(ctx.run_id),
                    },
                )
            )
            await session.flush()
            return message_id


def send_rfq_email_tool(deps: RfqActDeps) -> Tool[SendRfqEmailArgs, SendRfqEmailResult]:
    return Tool(
        name=SEND_RFQ_EMAIL,
        description="Send one approved RFQ email to a supplier contact from the buyer's mailbox.",
        input_model=SendRfqEmailArgs,
        output_model=SendRfqEmailResult,
        kind="act",
        handler=_SendRfqEmail(deps),
    )


def register_act_tools(registry: ActToolRegistry, deps: RfqActDeps) -> None:
    """Register the RFQ flow's act tools with the worker's executor registry."""
    registry.register(send_rfq_email_tool(deps))
