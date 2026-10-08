"""The `send_followup_email` act tool: send one approved chase or clarification.

Like `send_rfq_email` (`bido_back.rfqs.send`), it is never offered to a model.
The sweeper (`bido_back.followups.service`) records a proposal for it; an
approver approves; the worker's `ApprovalExecutor` runs this handler once,
with the APPROVED arguments.

It goes out from the connected mailbox of the buyer who sent the RFQ email
(`sender_user_id`, also the approval's requester), as a reply in the invite's
thread: In-Reply-To / References point at the RFQ email's Message-ID, and the
mailbox sender puts it in the same provider thread.

Stale proposals are not sent. A chase whose invite got an answer in the
meantime (the invite is no longer `sent`), or any follow-up whose row was
canceled, ends as `canceled` and the handler returns `skipped`: approving an
old reminder never sends it after the supplier replied.

Idempotency follows `send_rfq_email`: the Message-ID is claimed and committed
on the `followup_messages` row before sending, and reused on a retry; the
outbound `messages` row is unique per Message-ID; a row already `sent` is a
no-op.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Final, Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from bido_back.agents.context import ToolContext
from bido_back.agents.tools import Tool
from bido_back.approvals.executor import ActToolRegistry
from bido_back.approvals.guard import RejectHook
from bido_back.db import tenant_session
from bido_back.db.models import (
    Approval,
    AuditEvent,
    Contact,
    FollowupMessage,
    Invite,
    Message,
    Tenant,
    User,
)
from bido_back.email.addresses import make_message_id
from bido_back.email.types import OutboundEmail
from bido_back.rfqs.errors import InvalidState, NotFound
from bido_back.rfqs.schemas import EmailAddress
from bido_back.rfqs.send import RfqActDeps

logger = logging.getLogger(__name__)

SEND_FOLLOWUP_EMAIL: Final = "send_followup_email"
FollowupKindName = Literal["chase", "clarification"]

_MESSAGES_UNIQUE = "messages_tenant_id_message_id_header_key"
_FOLLOWUP_NAMESPACE: Final = UUID("0c4f7a52-9d1e-4b8e-a2f3-6e5d8c7b1a90")


def followup_id(invite_id: UUID, kind: FollowupKindName, sequence: int) -> UUID:
    """The row id of the `sequence`-th follow-up of `kind` for an invite (stable)."""
    return uuid5(_FOLLOWUP_NAMESPACE, f"{invite_id}:{kind}:{sequence}")


def followup_proposal_ids(row_id: UUID) -> tuple[UUID, str]:
    """The (run_id, tool_call_id) under which a follow-up is proposed."""
    call_id = f"{SEND_FOLLOWUP_EMAIL}:{row_id}"
    return uuid5(_FOLLOWUP_NAMESPACE, call_id), call_id


class SendFollowupEmailArgs(BaseModel):
    """Exactly what the approver sees and approves."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    followup_id: UUID
    invite_id: UUID
    kind: FollowupKindName
    sender_user_id: UUID
    to: list[EmailAddress] = Field(min_length=1, max_length=10)
    cc: list[EmailAddress] = Field(default_factory=list[str], max_length=10)
    subject: Annotated[str, StringConstraints(min_length=1, max_length=300, pattern=r"^[^\r\n]*$")]
    body: str = Field(min_length=1, max_length=20_000)


class SendFollowupEmailResult(BaseModel):
    followup_id: UUID
    outcome: Literal["sent", "already_sent", "skipped"]
    message_id: str | None = None


@dataclass(frozen=True, slots=True)
class _Claim:
    message_id: str
    thread_message_id: str
    # The Message-ID was claimed by an earlier attempt, which may have sent it.
    possibly_sent: bool = False


class _SendFollowupEmail:
    def __init__(self, deps: RfqActDeps) -> None:
        self._deps = deps

    async def __call__(
        self, ctx: ToolContext, args: SendFollowupEmailArgs
    ) -> SendFollowupEmailResult:
        claim = await self._claim(ctx, args)
        if isinstance(claim, SendFollowupEmailResult):
            return claim
        message_id = await self._send_and_record(ctx, args, claim)
        logger.info(
            "sent followup tenant=%s invite=%s followup=%s kind=%s run=%s",
            ctx.tenant_id,
            args.invite_id,
            args.followup_id,
            args.kind,
            ctx.run_id,
        )
        return SendFollowupEmailResult(
            followup_id=args.followup_id, outcome="sent", message_id=message_id
        )

    async def _claim(
        self, ctx: ToolContext, args: SendFollowupEmailArgs
    ) -> _Claim | SendFollowupEmailResult:
        _, call_id = followup_proposal_ids(args.followup_id)
        async with tenant_session(self._deps.db, ctx.tenant_id) as session:
            proposal = (
                await session.execute(
                    select(Approval.requested_by, Approval.status).where(
                        Approval.run_id == str(ctx.run_id),
                        Approval.tool_call_id == call_id,
                        Approval.tool == SEND_FOLLOWUP_EMAIL,
                    )
                )
            ).one_or_none()
            if proposal is None:
                raise InvalidState(f"followup {args.followup_id}: approval was not proposed for it")
            requester, approval_status = proposal
            if approval_status == "rejected":
                raise InvalidState(f"followup {args.followup_id}: the send was rejected")
            if requester != args.sender_user_id:
                raise InvalidState(f"followup {args.followup_id}: sender is not the requester")

            row = await session.scalar(
                select(FollowupMessage)
                .where(FollowupMessage.id == args.followup_id)
                .with_for_update()
            )
            if row is None or row.invite_id != args.invite_id or row.kind != args.kind:
                raise NotFound(f"followup {args.followup_id} not found")
            if row.status == "sent":
                return SendFollowupEmailResult(
                    followup_id=row.id, outcome="already_sent", message_id=row.message_id_header
                )
            invite = await session.scalar(
                select(Invite).where(Invite.id == args.invite_id).with_for_update()
            )
            if invite is None:
                raise NotFound(f"invite {args.invite_id} not found")
            stale = row.status != "proposed" or (row.kind == "chase" and invite.status != "sent")
            if stale or invite.thread_message_id is None:
                if row.status == "proposed":
                    row.status = "canceled"
                    row.canceled_reason = (
                        "no_thread"
                        if invite.thread_message_id is None
                        else "invite_" + invite.status
                    )
                await session.flush()
                logger.info(
                    "followup %s skipped: row %s, invite %s", row.id, row.status, invite.status
                )
                return SendFollowupEmailResult(followup_id=row.id, outcome="skipped")
            reused = row.message_id_header is not None
            if row.message_id_header is None:
                row.message_id_header = make_message_id(self._deps.email_domain)
                await session.flush()
            return _Claim(
                message_id=row.message_id_header,
                thread_message_id=invite.thread_message_id,
                possibly_sent=reused,
            )

    async def _send_and_record(
        self, ctx: ToolContext, args: SendFollowupEmailArgs, claim: _Claim
    ) -> str:
        now = datetime.now(UTC)
        async with tenant_session(self._deps.db, ctx.tenant_id) as session:
            row = await session.scalar(
                select(FollowupMessage)
                .where(FollowupMessage.id == args.followup_id)
                .with_for_update()
            )
            if row is None:
                raise NotFound(f"followup {args.followup_id} not found")
            if row.status == "sent":
                return row.message_id_header or claim.message_id
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
            sender = await self._deps.mailbox_senders(session, ctx.tenant_id, buyer.id)
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
                    possibly_sent=claim.possibly_sent,
                    in_reply_to=claim.thread_message_id,
                    references=(claim.thread_message_id,),
                )
            )
            message_id = sent.message_id or claim.message_id
            message_row = await session.scalar(
                insert(Message)
                .values(
                    tenant_id=ctx.tenant_id,
                    invite_id=args.invite_id,
                    direction="out",
                    message_id_header=message_id,
                    in_reply_to=claim.thread_message_id,
                    from_contact_id=buyer_contact.id if buyer_contact else None,
                    subject=args.subject,
                    body_text=args.body,
                    status="stored",
                    occurred_at=now,
                )
                .on_conflict_do_nothing(constraint=_MESSAGES_UNIQUE)
                .returning(Message.id)
            )
            row.status = "sent"
            row.sent_at = now
            row.message_id_header = message_id
            session.add(
                AuditEvent(
                    tenant_id=ctx.tenant_id,
                    actor_user_id=ctx.user_id,
                    action="rfq.followup_sent",
                    payload={
                        "followup_id": str(row.id),
                        "invite_id": str(args.invite_id),
                        "kind": args.kind,
                        "message_row_id": str(message_row) if message_row else None,
                        "requested_by": str(args.sender_user_id),
                        "approved_by": str(ctx.user_id) if ctx.user_id else None,
                        "run_id": str(ctx.run_id),
                    },
                )
            )
            await session.flush()
            return message_id


def send_followup_email_tool(
    deps: RfqActDeps,
) -> Tool[SendFollowupEmailArgs, SendFollowupEmailResult]:
    return Tool(
        name=SEND_FOLLOWUP_EMAIL,
        description="Send one approved chase or clarification email in an RFQ thread.",
        input_model=SendFollowupEmailArgs,
        output_model=SendFollowupEmailResult,
        kind="act",
        handler=_SendFollowupEmail(deps),
    )


async def release_rejected_followup(session: AsyncSession, approval: Approval) -> None:
    """A rejected follow-up is not sent; a rejected chase still counts as one attempt."""
    args = SendFollowupEmailArgs.model_validate(approval.args)
    await session.execute(
        update(FollowupMessage)
        .where(FollowupMessage.id == args.followup_id, FollowupMessage.status == "proposed")
        .values(status="rejected")
    )


FOLLOWUP_REJECT_HOOKS: Final[dict[str, RejectHook]] = {
    SEND_FOLLOWUP_EMAIL: release_rejected_followup
}


def register_followup_act_tools(registry: ActToolRegistry, deps: RfqActDeps) -> None:
    registry.register(send_followup_email_tool(deps))
