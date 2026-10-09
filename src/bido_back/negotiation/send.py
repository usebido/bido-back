"""The `send_negotiation_email` act tool: send one approved counter-offer or BAFO.

Never offered to a model. The negotiation sweep (`bido_back.negotiation.service`)
records a proposal for it; a person approves it (the buyer by default, a
second person in four-eyes tenants: the approvals guard decides); the
worker's `ApprovalExecutor` runs this handler once, with the APPROVED arguments.

It goes out from the connected mailbox of the buyer who sent the RFQ email
(`sender_user_id`, also the approval's requester), as a reply in the invite's
thread, exactly like a follow-up: In-Reply-To the latest message with the
supplier (`supplier_email.threading`), text and HTML parts as approved.

Stale proposals are not sent: if the negotiation was closed, the supplier
sent a newer offer than the one this email answers, or the email's reply-by
date (a best-and-final request: its cut-off) passed before it was approved, the
row ends `canceled` and the handler returns `skipped`; the next sweep decides
again. A row whose Message-ID was claimed by an interrupted attempt may already
be with the supplier: while the negotiation is open the retry completes it (the
sender checks for that Message-ID first) whatever else changed since.

Idempotency follows the follow-ups: the Message-ID is claimed and committed on
the `negotiation_messages` row before sending and reused on a retry; the
outbound `messages` row is unique per Message-ID; a row already `sent` is a
no-op.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Final, Literal, cast
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from bido_back.agents.context import ToolContext
from bido_back.agents.events import args_hash
from bido_back.agents.tools import Tool
from bido_back.approvals.executor import ActToolRegistry
from bido_back.approvals.guard import RejectHook
from bido_back.comparison.policy import ComparisonPolicy
from bido_back.db import tenant_session
from bido_back.db.models import (
    Approval,
    AuditEvent,
    Contact,
    FieldCorrection,
    Invite,
    Message,
    NegotiationMessage,
    Proposal,
    Rfq,
    Tenant,
    User,
)
from bido_back.email.addresses import make_message_id
from bido_back.email.types import OutboundEmail
from bido_back.negotiation.decision import decision_of
from bido_back.rfqs.errors import InvalidState, NotFound
from bido_back.rfqs.schemas import EmailAddress
from bido_back.rfqs.send import RfqActDeps
from bido_back.supplier_email.threading import reply_headers

logger = logging.getLogger(__name__)

SEND_NEGOTIATION_EMAIL: Final = "send_negotiation_email"
NegotiationActionName = Literal["counter", "bafo"]

_MESSAGES_UNIQUE = "messages_tenant_id_message_id_header_key"
_NAMESPACE: Final = UUID("5b0d8e3c-2a7f-4c61-9e84-7d1f3a6c2b95")


def negotiation_message_id(invite_id: UUID, sequence: int) -> UUID:
    """The row id of the `sequence`-th negotiation email decided for an invite (stable).

    Rows written before migration 0036 used their round number, equal to their sequence.
    """
    return uuid5(_NAMESPACE, f"{invite_id}:{sequence}")


def negotiation_proposal_ids(row_id: UUID) -> tuple[UUID, str]:
    """The (run_id, tool_call_id) under which a negotiation email is proposed."""
    call_id = f"{SEND_NEGOTIATION_EMAIL}:{row_id}"
    return uuid5(_NAMESPACE, call_id), call_id


class SendNegotiationEmailArgs(BaseModel):
    """Exactly what the approver sees and approves."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    negotiation_message_id: UUID
    invite_id: UUID
    action: NegotiationActionName
    # The negotiation round: emails already sent to the supplier, plus one.
    round: int = Field(ge=1)
    sender_user_id: UUID
    to: list[EmailAddress] = Field(min_length=1, max_length=10)
    cc: list[EmailAddress] = Field(default_factory=list[str], max_length=10)
    subject: Annotated[str, StringConstraints(min_length=1, max_length=300, pattern=r"^[^\r\n]*$")]
    body: str = Field(min_length=1, max_length=20_000)
    # The same email as HTML. Left out of the arguments when unset: proposals made before
    # it existed keep their hash, and go out as text only.
    html_body: str | None = Field(default=None, max_length=200_000, exclude_if=lambda v: v is None)


class SendNegotiationEmailResult(BaseModel):
    negotiation_message_id: UUID
    outcome: Literal["sent", "already_sent", "skipped"]
    message_id: str | None = None


@dataclass(frozen=True, slots=True)
class _Claim:
    message_id: str
    thread_message_id: str
    # The Message-ID was claimed by an earlier attempt, which may have sent it.
    possibly_sent: bool = False


async def stale_reason(
    session: AsyncSession,
    row: NegotiationMessage,
    invite: Invite,
    *,
    now: datetime | None = None,
    claimed: bool = False,
) -> str | None:
    """Why a proposed negotiation email must not go out any more, or None.

    `claimed`: an earlier attempt claimed its Message-ID and may have sent it; only a
    closed negotiation stops it then."""
    rfq = await session.get(Rfq, invite.rfq_id)
    if rfq is None or rfq.negotiation_status == "closed" or rfq.status != "open":
        return "negotiation_closed"
    if claimed:
        return None
    decision = decision_of(cast(dict[str, object], row.terms))
    if decision is not None:
        at = now or datetime.now(UTC)
        if decision.kind == "bafo" and decision.cutoff_at is not None:
            if at >= decision.cutoff_at:
                return "cutoff_passed"
        elif decision.reply_by < at.date():
            return "reply_by_passed"
    stored_hash = row.terms.get("comparison_policy_hash")
    if stored_hash is not None:
        current_hash = args_hash(
            cast(
                dict[str, JsonValue],
                ComparisonPolicy.model_validate(rfq.comparison_policy or {}).model_dump(
                    mode="json"
                ),
            )
        )
        if stored_hash != current_hash:
            return "comparison_policy_changed"
    revision = await session.scalar(
        select(func.count(FieldCorrection.id)).where(
            FieldCorrection.proposal_id == row.proposal_id,
        )
    )
    if revision != row.terms.get("correction_revision", 0):
        return "offer_corrected"
    if invite.thread_message_id is None:
        return "no_thread"
    latest = await session.scalar(
        select(func.max(Proposal.version)).where(Proposal.invite_id == invite.id)
    )
    answered = await session.scalar(select(Proposal.version).where(Proposal.id == row.proposal_id))
    if latest is not None and answered is not None and latest > answered:
        return "newer_offer"
    return None


class _SendNegotiationEmail:
    def __init__(self, deps: RfqActDeps) -> None:
        self._deps = deps

    async def __call__(
        self, ctx: ToolContext, args: SendNegotiationEmailArgs
    ) -> SendNegotiationEmailResult:
        claim = await self._claim(ctx, args)
        if isinstance(claim, SendNegotiationEmailResult):
            return claim
        message_id = await self._send_and_record(ctx, args, claim)
        if message_id is None:
            return SendNegotiationEmailResult(
                negotiation_message_id=args.negotiation_message_id,
                outcome="skipped",
            )
        logger.info(
            "sent negotiation email tenant=%s invite=%s row=%s action=%s round=%s run=%s",
            ctx.tenant_id,
            args.invite_id,
            args.negotiation_message_id,
            args.action,
            args.round,
            ctx.run_id,
        )
        return SendNegotiationEmailResult(
            negotiation_message_id=args.negotiation_message_id,
            outcome="sent",
            message_id=message_id,
        )

    async def _claim(
        self, ctx: ToolContext, args: SendNegotiationEmailArgs
    ) -> _Claim | SendNegotiationEmailResult:
        _, call_id = negotiation_proposal_ids(args.negotiation_message_id)
        async with tenant_session(self._deps.db, ctx.tenant_id) as session:
            proposal = (
                await session.execute(
                    select(Approval.requested_by, Approval.status).where(
                        Approval.run_id == str(ctx.run_id),
                        Approval.tool_call_id == call_id,
                        Approval.tool == SEND_NEGOTIATION_EMAIL,
                    )
                )
            ).one_or_none()
            if proposal is None:
                raise InvalidState(
                    f"negotiation email {args.negotiation_message_id}: approval was not proposed"
                )
            requester, approval_status = proposal
            if approval_status == "rejected":
                raise InvalidState(
                    f"negotiation email {args.negotiation_message_id}: the send was rejected"
                )
            if requester != args.sender_user_id:
                raise InvalidState(
                    f"negotiation email {args.negotiation_message_id}: sender is not the requester"
                )
            # The invite first, then the row: the order the negotiation sweep locks them in.
            invite = await session.scalar(
                select(Invite).where(Invite.id == args.invite_id).with_for_update()
            )
            row = await session.scalar(
                select(NegotiationMessage)
                .where(NegotiationMessage.id == args.negotiation_message_id)
                .with_for_update()
            )
            if row is None or row.invite_id != args.invite_id or row.action != args.action:
                raise NotFound(f"negotiation email {args.negotiation_message_id} not found")
            if row.status == "sent":
                return SendNegotiationEmailResult(
                    negotiation_message_id=row.id,
                    outcome="already_sent",
                    message_id=row.message_id_header,
                )
            if invite is None:
                raise NotFound(f"invite {args.invite_id} not found")
            reason = "not_proposed" if row.status != "proposed" else None
            # A claimed row may already be with the supplier: the retry completes it.
            reason = reason or await stale_reason(
                session, row, invite, claimed=row.message_id_header is not None
            )
            if reason is not None or invite.thread_message_id is None:
                if row.status == "proposed":
                    row.status = "canceled"
                    row.canceled_reason = reason or "no_thread"
                await session.flush()
                logger.info("negotiation email %s skipped: %s", row.id, reason)
                return SendNegotiationEmailResult(negotiation_message_id=row.id, outcome="skipped")
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
        self, ctx: ToolContext, args: SendNegotiationEmailArgs, claim: _Claim
    ) -> str | None:
        now = datetime.now(UTC)
        async with tenant_session(self._deps.db, ctx.tenant_id) as session:
            row = await session.scalar(
                select(NegotiationMessage)
                .where(NegotiationMessage.id == args.negotiation_message_id)
                .with_for_update()
            )
            if row is None:
                raise NotFound(f"negotiation email {args.negotiation_message_id} not found")
            if row.status == "sent":
                return row.message_id_header or claim.message_id
            invite = await session.get(Invite, args.invite_id)
            reason = "not_proposed" if row.status != "proposed" else None
            reason = reason or (
                await stale_reason(session, row, invite, claimed=claim.possibly_sent)
                if invite
                else "no_invite"
            )
            if reason is not None:
                if row.status == "proposed":
                    row.status = "canceled"
                    row.canceled_reason = reason
                await session.flush()
                return None
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
            in_reply_to, references = await reply_headers(
                session, args.invite_id, claim.thread_message_id
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
                    html_body=args.html_body,
                    message_id=claim.message_id,
                    possibly_sent=claim.possibly_sent,
                    in_reply_to=in_reply_to,
                    references=references,
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
                    in_reply_to=in_reply_to,
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
                    action="rfq.negotiation_sent",
                    payload={
                        "negotiation_message_id": str(row.id),
                        "invite_id": str(args.invite_id),
                        "action": args.action,
                        "round": args.round,
                        "message_row_id": str(message_row) if message_row else None,
                        "requested_by": str(args.sender_user_id),
                        "approved_by": str(ctx.user_id) if ctx.user_id else None,
                        "run_id": str(ctx.run_id),
                    },
                )
            )
            await session.flush()
            return message_id


def send_negotiation_email_tool(
    deps: RfqActDeps,
) -> Tool[SendNegotiationEmailArgs, SendNegotiationEmailResult]:
    return Tool(
        name=SEND_NEGOTIATION_EMAIL,
        description="Send one approved counter-offer or best-and-final request in an RFQ thread.",
        input_model=SendNegotiationEmailArgs,
        output_model=SendNegotiationEmailResult,
        kind="act",
        handler=_SendNegotiationEmail(deps),
    )


async def release_rejected_negotiation(session: AsyncSession, approval: Approval) -> None:
    """A rejected negotiation email is not sent and uses no round; the offer it answered
    gets no other email (the buyer decides, `bido_back.negotiation.planner`)."""
    args = SendNegotiationEmailArgs.model_validate(approval.args)
    await session.execute(
        update(NegotiationMessage)
        .where(
            NegotiationMessage.id == args.negotiation_message_id,
            NegotiationMessage.status == "proposed",
        )
        .values(status="rejected")
    )


NEGOTIATION_REJECT_HOOKS: Final[dict[str, RejectHook]] = {
    SEND_NEGOTIATION_EMAIL: release_rejected_negotiation
}


def register_negotiation_act_tools(registry: ActToolRegistry, deps: RfqActDeps) -> None:
    registry.register(send_negotiation_email_tool(deps))
