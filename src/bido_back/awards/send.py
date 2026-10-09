"""The award's act tools: `award_rfq`, `send_purchase_order`, `send_award_notice`.

None of them is offered to a model. The award service records a proposal; a
person approves it (the buyer by default; in four-eyes tenants an approver or
owner who is not the requester: the guard checks it); the worker's
`ApprovalExecutor` runs the handler once, with the APPROVED arguments.

`award_rfq` writes the award and one purchase order per awarded supplier
(`PO-<n>`, numbered per tenant), and closes the RFQ. It refuses to award an
offer that changed after the request: if a supplier sent a newer proposal, or
a price was corrected, the outcome is `stale`, nothing is written, and the
buyer asks again. An RFQ is awarded once.

`send_purchase_order` and `send_award_notice` are emails in the invite's
thread, from the mailbox of the buyer who sent the RFQ email. The PO email
carries the PO as a PDF in the supplier's language and as an .xlsx, both
rendered from the stored rows at send time (`awards.document`); the
approval holds the hash of those rows, and a PO whose rows changed is not
sent (it goes back to `draft`). Idempotency follows `send_followup_email`:
the Message-ID is claimed and committed before sending and reused on a retry;
a row already `sent` is a no-op.

Rejections (`AWARD_REJECT_HOOKS`): a rejected PO send puts the PO back to
`draft`; a rejected notice is not sent. A rejected award holds nothing.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final
from uuid import UUID, uuid5

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bido_back.agents.context import ToolContext
from bido_back.agents.tools import Tool
from bido_back.approvals.executor import ActToolRegistry
from bido_back.approvals.guard import RejectHook
from bido_back.awards.cancellation import register_cancellation_act_tool
from bido_back.awards.document import PDF_MEDIA_TYPE, po_pdf, po_pdf_filename
from bido_back.awards.orders import next_po_number, po_document, po_ref
from bido_back.awards.render import (
    XLSX_MEDIA_TYPE,
    PoDocument,
    content_hash,
    line_total,
    to_xlsx,
)
from bido_back.awards.schemas import (
    AwardLine,
    AwardRfqArgs,
    AwardRfqResult,
    SendAwardNoticeArgs,
    SendPurchaseOrderArgs,
    ThreadEmailResult,
)
from bido_back.awards.thread import Outgoing, deliver
from bido_back.db import tenant_session
from bido_back.db.models import (
    Approval,
    AuditEvent,
    Award,
    AwardNotice,
    Invite,
    IssuedPurchaseOrder,
    IssuedPurchaseOrderLine,
    Proposal,
    ProposalLine,
    Rfq,
)
from bido_back.email.addresses import make_message_id
from bido_back.email.types import OutboundAttachment
from bido_back.rfqs.errors import InvalidState, NotFound
from bido_back.rfqs.send import RfqActDeps

logger = logging.getLogger(__name__)

AWARD_RFQ: Final = "award_rfq"
SEND_PURCHASE_ORDER: Final = "send_purchase_order"
SEND_AWARD_NOTICE: Final = "send_award_notice"

_NAMESPACE: Final = UUID("4b0f3c1e-8a2d-4e6f-9b7a-1d2c3e4f5a6b")


def award_proposal_ids(rfq_id: UUID, attempt: int = 0) -> tuple[UUID, str]:
    """(run_id, tool_call_id) of an RFQ's award; `attempt` = earlier decided award proposals."""
    call_id = f"{AWARD_RFQ}:{rfq_id}"
    return uuid5(_NAMESPACE, f"{call_id}:{attempt}"), call_id


def po_send_proposal_ids(po_id: UUID, attempt: int = 0) -> tuple[UUID, str]:
    call_id = f"{SEND_PURCHASE_ORDER}:{po_id}"
    return uuid5(_NAMESPACE, f"{call_id}:{attempt}"), call_id


def notice_id(invite_id: UUID) -> UUID:
    return uuid5(_NAMESPACE, f"notice:{invite_id}")


def notice_proposal_ids(invite_id: UUID) -> tuple[UUID, str]:
    call_id = f"{SEND_AWARD_NOTICE}:{invite_id}"
    return uuid5(_NAMESPACE, call_id), call_id


# --- award_rfq -----------------------------------------------------------------------


async def _still_current(session: AsyncSession, lines: list[AwardLine]) -> bool:
    """Every awarded offer is still the supplier's latest proposal, at the approved price."""
    for invite_id in {line.invite_id for line in lines}:
        latest = await session.scalar(
            select(func.max(Proposal.version)).where(Proposal.invite_id == invite_id)
        )
        versions = {line.proposal_version for line in lines if line.invite_id == invite_id}
        if versions != {latest}:
            return False
    for line in lines:
        stored = await session.get(ProposalLine, line.proposal_line_id)
        if (
            stored is None
            or stored.proposal_id != line.proposal_id
            or stored.unit_price is None
            or stored.unit_price != Decimal(line.unit_price)
        ):
            return False
    return True


class _AwardRfq:
    def __init__(self, deps: RfqActDeps) -> None:
        self._deps = deps

    async def __call__(self, ctx: ToolContext, args: AwardRfqArgs) -> AwardRfqResult:
        async with tenant_session(self._deps.db, ctx.tenant_id) as session:
            rfq = await session.scalar(select(Rfq).where(Rfq.id == args.rfq_id).with_for_update())
            if rfq is None:
                raise NotFound(f"rfq {args.rfq_id} not found")
            existing = await session.scalar(select(Award).where(Award.rfq_id == rfq.id))
            if existing is not None:
                return AwardRfqResult(
                    rfq_id=rfq.id, outcome="already_awarded", award_id=existing.id
                )
            if not await _still_current(session, list(args.lines)):
                logger.info("award of rfq %s is stale: an offer changed", rfq.id)
                return AwardRfqResult(rfq_id=rfq.id, outcome="stale")
            approval_id = await session.scalar(
                select(Approval.id).where(
                    Approval.run_id == str(ctx.run_id), Approval.tool == AWARD_RFQ
                )
            )
            if ctx.user_id is None:
                raise InvalidState("an award is executed for the approver")
            award = Award(
                tenant_id=ctx.tenant_id,
                rfq_id=rfq.id,
                approval_id=approval_id,
                requested_by=args.requested_by,
                approved_by=ctx.user_id,
                differs_from_recommendation=args.differs_from_recommendation,
                reason=args.reason,
                lines=[line.model_dump(mode="json") for line in args.lines],
            )
            session.add(award)
            await session.flush()

            by_invite: dict[UUID, list[AwardLine]] = defaultdict(list)
            for line in args.lines:
                by_invite[line.invite_id].append(line)
            po_ids: list[UUID] = []
            number = await next_po_number(session, ctx.tenant_id)
            for invite_id, lines in by_invite.items():
                proposal = await session.get(Proposal, lines[0].proposal_id)
                po_lines = [
                    IssuedPurchaseOrderLine(
                        tenant_id=ctx.tenant_id,
                        position=line.position,
                        rfq_line_id=line.rfq_line_id,
                        proposal_line_id=line.proposal_line_id,
                        description=line.description,
                        mpn=line.mpn,
                        quantity=Decimal(line.quantity),
                        unit=line.unit,
                        unit_price=Decimal(line.unit_price),
                        line_total=line_total(Decimal(line.quantity), Decimal(line.unit_price)),
                        need_by=line.need_by,
                        lead_time_days=line.lead_time_days,
                    )
                    for line in lines
                ]
                po = IssuedPurchaseOrder(
                    tenant_id=ctx.tenant_id,
                    award_id=award.id,
                    rfq_id=rfq.id,
                    invite_id=invite_id,
                    supplier_id=lines[0].supplier_id,
                    number=number,
                    ref=po_ref(number),
                    status="draft",
                    currency=lines[0].currency,
                    incoterm=proposal.incoterm if proposal else None,
                    payment_terms=proposal.payment_terms if proposal else None,
                    total=sum((line.line_total for line in po_lines), Decimal("0.00")),
                )
                session.add(po)
                await session.flush()
                for po_line in po_lines:
                    po_line.purchase_order_id = po.id
                    session.add(po_line)
                po_ids.append(po.id)
                number += 1
            rfq.status = "closed"
            session.add(
                AuditEvent(
                    tenant_id=ctx.tenant_id,
                    actor_user_id=ctx.user_id,
                    action="rfq.awarded",
                    payload={
                        "rfq_id": str(rfq.id),
                        "award_id": str(award.id),
                        "purchase_order_ids": [str(i) for i in po_ids],
                        "differs_from_recommendation": args.differs_from_recommendation,
                        "requested_by": str(args.requested_by),
                        "run_id": str(ctx.run_id),
                    },
                )
            )
            await session.flush()
        logger.info(
            "awarded rfq %s tenant=%s purchase_orders=%d",
            args.rfq_id,
            ctx.tenant_id,
            len(po_ids),
        )
        return AwardRfqResult(
            rfq_id=args.rfq_id, outcome="awarded", award_id=award.id, purchase_order_ids=po_ids
        )


# --- send_purchase_order ---------------------------------------------------------------


def _po_attachments(doc: PoDocument) -> tuple[OutboundAttachment, ...]:
    """The PO as a PDF (what the supplier reads) and as .xlsx (for their ERP import).

    A PDF that cannot be rendered is left out, logged, rather than holding the order:
    the .xlsx carries the same approved content.
    """
    sheet = OutboundAttachment(
        filename=f"{doc.ref}.xlsx", content_type=XLSX_MEDIA_TYPE, data=to_xlsx(doc)
    )
    try:
        pdf = po_pdf(doc)
    except Exception:
        logger.exception("purchase order %s: the PDF could not be rendered; sent as .xlsx", doc.ref)
        return (sheet,)
    return (
        OutboundAttachment(filename=po_pdf_filename(doc), content_type=PDF_MEDIA_TYPE, data=pdf),
        sheet,
    )


@dataclass(frozen=True, slots=True)
class _Claim:
    message_id: str
    thread_message_id: str
    # The Message-ID was claimed by an earlier attempt, which may have sent it.
    possibly_sent: bool = False


class _SendPurchaseOrder:
    def __init__(self, deps: RfqActDeps) -> None:
        self._deps = deps

    async def __call__(self, ctx: ToolContext, args: SendPurchaseOrderArgs) -> ThreadEmailResult:
        claim = await self._claim(ctx, args)
        if isinstance(claim, ThreadEmailResult):
            return claim
        async with tenant_session(self._deps.db, ctx.tenant_id) as session:
            po = await session.scalar(
                select(IssuedPurchaseOrder)
                .where(IssuedPurchaseOrder.id == args.purchase_order_id)
                .with_for_update()
            )
            if po is None:
                raise NotFound(f"purchase order {args.purchase_order_id} not found")
            if po.status == "sent":
                return ThreadEmailResult(outcome="already_sent", message_id=po.message_id_header)
            doc = await po_document(session, po)
            message_id, message_row = await deliver(
                session,
                self._deps,
                ctx,
                Outgoing(
                    invite_id=args.invite_id,
                    sender_user_id=args.sender_user_id,
                    to=tuple(args.to),
                    cc=tuple(args.cc),
                    subject=args.subject,
                    body=args.body,
                    message_id=claim.message_id,
                    thread_message_id=claim.thread_message_id,
                    possibly_sent=claim.possibly_sent,
                    attachments=_po_attachments(doc),
                ),
            )
            po.status = "sent"
            po.sent_at = datetime.now(UTC)
            po.fulfilment_state = "sent"
            po.message_id_header = message_id
            session.add(
                AuditEvent(
                    tenant_id=ctx.tenant_id,
                    actor_user_id=ctx.user_id,
                    action="purchase_order.sent",
                    payload={
                        "purchase_order_id": str(po.id),
                        "invite_id": str(args.invite_id),
                        "message_row_id": str(message_row) if message_row else None,
                        "sender_user_id": str(args.sender_user_id),
                        "approved_by": str(ctx.user_id) if ctx.user_id else None,
                        "run_id": str(ctx.run_id),
                    },
                )
            )
            await session.flush()
        logger.info("sent purchase order %s tenant=%s", args.purchase_order_id, ctx.tenant_id)
        return ThreadEmailResult(outcome="sent", message_id=message_id)

    async def _claim(
        self, ctx: ToolContext, args: SendPurchaseOrderArgs
    ) -> _Claim | ThreadEmailResult:
        async with tenant_session(self._deps.db, ctx.tenant_id) as session:
            po = await session.scalar(
                select(IssuedPurchaseOrder)
                .where(IssuedPurchaseOrder.id == args.purchase_order_id)
                .with_for_update()
            )
            if po is None or po.invite_id != args.invite_id:
                raise NotFound(f"purchase order {args.purchase_order_id} not found")
            if po.status == "sent":
                return ThreadEmailResult(outcome="already_sent", message_id=po.message_id_header)
            if po.status != "pending_approval":
                return ThreadEmailResult(outcome="skipped")
            invite = await session.get(Invite, po.invite_id)
            if invite is None or invite.thread_message_id is None:
                po.status = "draft"
                await session.flush()
                return ThreadEmailResult(outcome="skipped")
            if content_hash(await po_document(session, po)) != args.content_hash:
                # The PO changed after the send was requested: not what was approved.
                po.status = "draft"
                await session.flush()
                logger.warning("purchase order %s changed since approval; not sent", po.id)
                return ThreadEmailResult(outcome="skipped")
            reused = po.message_id_header is not None
            if po.message_id_header is None:
                po.message_id_header = make_message_id(self._deps.email_domain)
                await session.flush()
            return _Claim(
                message_id=po.message_id_header,
                thread_message_id=invite.thread_message_id,
                possibly_sent=reused,
            )


# --- send_award_notice ------------------------------------------------------------------


class _SendAwardNotice:
    def __init__(self, deps: RfqActDeps) -> None:
        self._deps = deps

    async def __call__(self, ctx: ToolContext, args: SendAwardNoticeArgs) -> ThreadEmailResult:
        claim = await self._claim(ctx, args)
        if isinstance(claim, ThreadEmailResult):
            return claim
        async with tenant_session(self._deps.db, ctx.tenant_id) as session:
            row = await session.scalar(
                select(AwardNotice).where(AwardNotice.id == args.notice_id).with_for_update()
            )
            if row is None:
                raise NotFound(f"notice {args.notice_id} not found")
            if row.status == "sent":
                return ThreadEmailResult(outcome="already_sent", message_id=row.message_id_header)
            message_id, message_row = await deliver(
                session,
                self._deps,
                ctx,
                Outgoing(
                    invite_id=args.invite_id,
                    sender_user_id=args.sender_user_id,
                    to=tuple(args.to),
                    cc=tuple(args.cc),
                    subject=args.subject,
                    body=args.body,
                    message_id=claim.message_id,
                    thread_message_id=claim.thread_message_id,
                    possibly_sent=claim.possibly_sent,
                ),
            )
            row.status = "sent"
            row.sent_at = datetime.now(UTC)
            row.message_id_header = message_id
            session.add(
                AuditEvent(
                    tenant_id=ctx.tenant_id,
                    actor_user_id=ctx.user_id,
                    action="award.notice_sent",
                    payload={
                        "notice_id": str(row.id),
                        "invite_id": str(args.invite_id),
                        "message_row_id": str(message_row) if message_row else None,
                        "approved_by": str(ctx.user_id) if ctx.user_id else None,
                        "run_id": str(ctx.run_id),
                    },
                )
            )
            await session.flush()
        return ThreadEmailResult(outcome="sent", message_id=message_id)

    async def _claim(
        self, ctx: ToolContext, args: SendAwardNoticeArgs
    ) -> _Claim | ThreadEmailResult:
        async with tenant_session(self._deps.db, ctx.tenant_id) as session:
            row = await session.scalar(
                select(AwardNotice).where(AwardNotice.id == args.notice_id).with_for_update()
            )
            if row is None or row.invite_id != args.invite_id:
                raise NotFound(f"notice {args.notice_id} not found")
            if row.status == "sent":
                return ThreadEmailResult(outcome="already_sent", message_id=row.message_id_header)
            if row.status != "proposed":
                return ThreadEmailResult(outcome="skipped")
            invite = await session.get(Invite, row.invite_id)
            if invite is None or invite.thread_message_id is None:
                row.status = "canceled"
                await session.flush()
                return ThreadEmailResult(outcome="skipped")
            reused = row.message_id_header is not None
            if row.message_id_header is None:
                row.message_id_header = make_message_id(self._deps.email_domain)
                await session.flush()
            return _Claim(
                message_id=row.message_id_header,
                thread_message_id=invite.thread_message_id,
                possibly_sent=reused,
            )


# --- Registration ---------------------------------------------------------------------------


def award_tools(
    deps: RfqActDeps,
) -> tuple[
    Tool[AwardRfqArgs, AwardRfqResult],
    Tool[SendPurchaseOrderArgs, ThreadEmailResult],
    Tool[SendAwardNoticeArgs, ThreadEmailResult],
]:
    return (
        Tool(
            name=AWARD_RFQ,
            description="Award an RFQ as approved and create one purchase order per supplier.",
            input_model=AwardRfqArgs,
            output_model=AwardRfqResult,
            kind="act",
            handler=_AwardRfq(deps),
        ),
        Tool(
            name=SEND_PURCHASE_ORDER,
            description="Send one approved purchase order to its supplier in the RFQ thread.",
            input_model=SendPurchaseOrderArgs,
            output_model=ThreadEmailResult,
            kind="act",
            handler=_SendPurchaseOrder(deps),
        ),
        Tool(
            name=SEND_AWARD_NOTICE,
            description="Send one approved 'not selected' email in the RFQ thread.",
            input_model=SendAwardNoticeArgs,
            output_model=ThreadEmailResult,
            kind="act",
            handler=_SendAwardNotice(deps),
        ),
    )


def register_award_act_tools(registry: ActToolRegistry, deps: RfqActDeps) -> None:
    for tool in award_tools(deps):
        registry.register(tool)
    register_cancellation_act_tool(registry, deps)


async def release_rejected_po_send(session: AsyncSession, approval: Approval) -> None:
    """A rejected PO send is not sent; the PO goes back to `draft`."""
    args = SendPurchaseOrderArgs.model_validate(approval.args)
    await session.execute(
        update(IssuedPurchaseOrder)
        .where(
            IssuedPurchaseOrder.id == args.purchase_order_id,
            IssuedPurchaseOrder.status == "pending_approval",
        )
        .values(status="draft")
    )


async def release_rejected_notice(session: AsyncSession, approval: Approval) -> None:
    args = SendAwardNoticeArgs.model_validate(approval.args)
    await session.execute(
        update(AwardNotice)
        .where(AwardNotice.id == args.notice_id, AwardNotice.status == "proposed")
        .values(status="rejected")
    )


AWARD_REJECT_HOOKS: Final[dict[str, RejectHook]] = {
    SEND_PURCHASE_ORDER: release_rejected_po_send,
    SEND_AWARD_NOTICE: release_rejected_notice,
}
