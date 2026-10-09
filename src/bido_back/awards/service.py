"""Award an RFQ, propose "not selected" notices, and send purchase orders. Nothing here sends.

Every step that commits or reaches a supplier is an `act` recorded as a
pending approval (`bido_back.awards.send`); a person decides (the buyer by
default; a second person in four-eyes tenants), and the worker executes it once.

- `request_award`: builds the awarded lines from the comparison (the
  recommendation unless the buyer picks another supplier, in which case a
  reason is required; a reason is also required for a line whose offer breaks
  the buyer's limits, e.g. above the approved maximum) and proposes
  `award_rfq`. Only an `open` RFQ with answers can be awarded, once.
- `request_notices`: after the award, one polite "not selected" email per
  supplier that took part and did not win, rendered by code in the standard
  layout and the thread's language (`bido_back.supplier_email`; the notice
  writer may only add an optional note), each proposed as `send_award_notice`.
- `request_po_send`: proposes `send_purchase_order` with the PO email (code
  writes it, in the supplier's language) and the hash of the PO it carries.
"""

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final, cast
from uuid import UUID, uuid4

from pydantic import JsonValue
from sqlalchemy import ColumnElement, and_, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.agents.approvals import ApprovalProposal
from bido_back.agents.catalog.contracts import AwardNoticeInput
from bido_back.agents.context import ToolContext
from bido_back.agents.events import args_hash
from bido_back.approvals.errors import ApprovalConflict
from bido_back.approvals.sink import PostgresApprovalSink
from bido_back.auth import Principal
from bido_back.awards.agents import NoticeWriter
from bido_back.awards.cancellation import (
    SEND_PO_CANCELLATION,
    SendPoCancellationArgs,
    cancellation_proposal_ids,
    cancellation_sent,
)
from bido_back.awards.document import PDF_MEDIA_TYPE, po_pdf, po_pdf_filename
from bido_back.awards.lifecycle import (
    awaiting_review,
    fulfilment_view,
    quantities,
    supplier_responses,
)
from bido_back.awards.orders import po_document, po_lines
from bido_back.awards.render import (
    HTML_MEDIA_TYPE,
    XLSX_MEDIA_TYPE,
    content_hash,
    document_gaps,
    po_email_body,
    to_html,
    to_xlsx,
)
from bido_back.awards.schemas import (
    OPEN_FULFILMENT_STATES,
    ApprovalRequested,
    AwardApprovalView,
    AwardLine,
    AwardRequest,
    AwardRequested,
    AwardRfqArgs,
    AwardView,
    FulfilmentState,
    IssuedPurchaseOrderLineView,
    NoticeProposed,
    NoticesRequested,
    PurchaseOrderStatus,
    PurchaseOrderSummary,
    PurchaseOrderView,
    SendAwardNoticeArgs,
    SendPurchaseOrderArgs,
    ThreadEmailApprovalView,
)
from bido_back.awards.selection import SelectionError, select_lines
from bido_back.awards.send import (
    AWARD_RFQ,
    SEND_AWARD_NOTICE,
    SEND_PURCHASE_ORDER,
    award_proposal_ids,
    notice_id,
    notice_proposal_ids,
    po_send_proposal_ids,
)
from bido_back.awards.thread import thread_of
from bido_back.comparison.loader import load_comparison
from bido_back.db import tenant_session
from bido_back.db.models import (
    Approval,
    Award,
    AwardNotice,
    Invite,
    IssuedPurchaseOrder,
    Message,
    Proposal,
    Rfq,
    Supplier,
    Tenant,
    User,
)
from bido_back.rfqs.errors import AgentFailed, InvalidState, NotFound, RfqError
from bido_back.rfqs.language import country_code
from bido_back.rfqs.mailbox import MailboxSenderFactory
from bido_back.rfqs.service import buyer_display_name
from bido_back.supplier_email.builders import Header, not_selected_email
from bido_back.supplier_email.checks import GateContext, problems_text
from bido_back.supplier_email.compose import Refused, compose
from bido_back.supplier_email.locale import Locale
from bido_back.supplier_email.model import Answered, Signature, SupplierEmail

logger = logging.getLogger(__name__)

Db = AsyncEngine | async_sessionmaker[AsyncSession]

_OPEN_APPROVAL: Final = ("pending", "approved")
_SUBJECT_MAX: Final = 300


class ReasonRequired(RfqError):
    """The award differs from the recommendation and no reason was given."""


class AwardSelectionInvalid(RfqError):
    """The requested lines cannot be ordered (see the message)."""


# --- Award -----------------------------------------------------------------------------


async def _decided_attempts(session: AsyncSession, tool: str, call_id: str) -> int:
    count = await session.scalar(
        select(func.count()).where(
            Approval.tool == tool,
            Approval.tool_call_id == call_id,
            Approval.status.not_in(_OPEN_APPROVAL),
        )
    )
    return count or 0


async def _open_approval(session: AsyncSession, tool: str, call_id: str) -> Approval | None:
    return await session.scalar(
        select(Approval)
        .where(
            Approval.tool == tool,
            Approval.tool_call_id == call_id,
            Approval.status.in_(_OPEN_APPROVAL),
        )
        .order_by(Approval.created_at.desc())
        .limit(1)
    )


async def request_award(
    db: Db, principal: Principal, rfq_id: UUID, body: AwardRequest, today: date
) -> AwardRequested:
    """Propose the award of an open RFQ; a person decides (the buyer by default)."""
    call_id = award_proposal_ids(rfq_id)[1]
    async with tenant_session(db, principal.tenant_id) as session:
        rfq = await session.get(Rfq, rfq_id)
        if rfq is None:
            raise NotFound(f"rfq {rfq_id} not found")
        if await session.scalar(select(Award.id).where(Award.rfq_id == rfq_id)) is not None:
            raise InvalidState(f"rfq {rfq_id} is already awarded")
        if rfq.status != "open":
            raise InvalidState(f"rfq {rfq_id} is {rfq.status}; only an open RFQ can be awarded")
        if await _open_approval(session, AWARD_RFQ, call_id) is not None:
            raise InvalidState(f"rfq {rfq_id} has an award waiting for approval")
        attempt = await _decided_attempts(session, AWARD_RFQ, call_id)
        rfq_ref = rfq.ref

    comparison = await load_comparison(db, principal, rfq_id, today)
    try:
        selected = select_lines(comparison, body.lines)
    except SelectionError as exc:
        raise AwardSelectionInvalid(str(exc)) from None
    reason = body.reason or None
    if selected.above_limits and not reason:
        # The approved limits (the negotiation mandate's maximums) bind the order too.
        positions = ", ".join(str(line.position) for line in selected.above_limits)
        raise ReasonRequired(
            f"say why: line(s) {positions} break the approved limits (maximum unit price "
            "or lead time)"
        )
    if selected.differs_from_recommendation and not reason:
        raise ReasonRequired("say why: the award differs from the recommendation")

    args = AwardRfqArgs(
        rfq_id=rfq_id,
        rfq_ref=rfq_ref,
        requested_by=principal.user_id,
        lines=selected.lines,
        differs_from_recommendation=selected.differs_from_recommendation,
        reason=reason,
    )
    approval_id = await _propose(
        db,
        principal,
        tool=AWARD_RFQ,
        ids=award_proposal_ids(rfq_id, attempt),
        session_id=f"rfq:{rfq_id}",
        payload=cast(dict[str, JsonValue], args.model_dump(mode="json")),
    )
    logger.info(
        "award requested rfq=%s tenant=%s approval=%s", rfq_id, principal.tenant_id, approval_id
    )
    return AwardRequested(
        rfq_id=rfq_id,
        approval_id=approval_id,
        differs_from_recommendation=selected.differs_from_recommendation,
        lines=selected.lines,
    )


async def _propose(
    db: Db,
    principal: Principal,
    *,
    tool: str,
    ids: tuple[UUID, str],
    session_id: str,
    payload: dict[str, JsonValue],
) -> UUID:
    run_id, call_id = ids
    try:
        await PostgresApprovalSink(db).request_approval(
            ApprovalProposal(
                tenant_id=principal.tenant_id,
                run_id=run_id,
                session_id=session_id,
                tool_call_id=call_id,
                tool=tool,
                args=payload,
                args_hash=args_hash(payload),
                requested_by=principal.user_id,
            )
        )
    except ApprovalConflict:
        raise InvalidState(f"{call_id}: another request is already waiting") from None
    async with tenant_session(db, principal.tenant_id) as session:
        approval_id = await session.scalar(
            select(Approval.id).where(
                Approval.run_id == str(run_id), Approval.tool_call_id == call_id
            )
        )
    if approval_id is None:
        raise InvalidState(f"{call_id}: the approval was not recorded")
    return approval_id


async def _summaries(
    session: AsyncSession, where: ColumnElement[bool]
) -> list[PurchaseOrderSummary]:
    rows = list(
        await session.execute(
            select(IssuedPurchaseOrder, Rfq.ref, Supplier.name)
            .join(Rfq, Rfq.id == IssuedPurchaseOrder.rfq_id)
            .join(Supplier, Supplier.id == IssuedPurchaseOrder.supplier_id)
            .where(where)
            .order_by(IssuedPurchaseOrder.number)
        )
    )
    totals = await quantities(session, [po.id for po, _, _ in rows])
    review = await awaiting_review(session, [po.id for po, _, _ in rows])
    return [
        PurchaseOrderSummary(
            id=po.id,
            ref=po.ref,
            status=cast(PurchaseOrderStatus, po.status),
            rfq_id=po.rfq_id,
            rfq_ref=rfq_ref,
            supplier_name=supplier_name,
            currency=po.currency,
            total=po.total,
            created_at=po.created_at,
            sent_at=po.sent_at,
            fulfilment=fulfilment_view(po, totals[po.id]),
            needs_review=po.id in review,
        )
        for po, rfq_ref, supplier_name in rows
    ]


async def get_award(db: Db, principal: Principal, rfq_id: UUID) -> AwardView:
    async with tenant_session(db, principal.tenant_id) as session:
        rfq = await session.get(Rfq, rfq_id)
        if rfq is None:
            raise NotFound(f"rfq {rfq_id} not found")
        award = await session.scalar(select(Award).where(Award.rfq_id == rfq_id))
        if award is not None:
            return AwardView(
                rfq_id=rfq.id,
                rfq_ref=rfq.ref,
                state="awarded",
                approval_id=award.approval_id,
                award_id=award.id,
                differs_from_recommendation=award.differs_from_recommendation,
                reason=award.reason,
                lines=[AwardLine.model_validate(line) for line in award.lines],
                purchase_orders=await _summaries(session, IssuedPurchaseOrder.award_id == award.id),
            )
        pending = await _open_approval(session, AWARD_RFQ, award_proposal_ids(rfq_id)[1])
        if pending is None:
            return AwardView(rfq_id=rfq.id, rfq_ref=rfq.ref, state="none")
        args = AwardRfqArgs.model_validate(pending.args)
        return AwardView(
            rfq_id=rfq.id,
            rfq_ref=rfq.ref,
            state="pending_approval",
            approval_id=pending.id,
            differs_from_recommendation=args.differs_from_recommendation,
            reason=args.reason,
            lines=list(args.lines),
        )


# --- "Not selected" notices ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _NoticeDraft:
    invite_id: UUID
    supplier_name: str
    sender_user_id: UUID
    to: str
    email: SupplierEmail
    gate: GateContext
    # What the notice writer sees when it may add its optional note.
    task: AwardNoticeInput


async def _answered(session: AsyncSession, invite: Invite) -> Answered | None:
    """The supplier's latest offer, by date; our RFQ email when it never sent one."""
    proposal = await session.scalar(
        select(Proposal)
        .where(Proposal.invite_id == invite.id)
        .order_by(Proposal.version.desc())
        .limit(1)
    )
    if proposal is not None:
        received = await session.scalar(
            select(Message.occurred_at).where(Message.id == proposal.message_id)
        )
        if received is not None:
            return Answered("offer", received.date())
    return Answered("request", invite.sent_at.date()) if invite.sent_at else None


async def request_notices(
    db: Db, principal: Principal, rfq_id: UUID, writer: NoticeWriter
) -> NoticesRequested:
    """Propose a "not selected" email to every invited supplier that did not win."""
    drafts: list[_NoticeDraft] = []
    skipped: list[UUID] = []
    async with tenant_session(db, principal.tenant_id) as session:
        award = await session.scalar(select(Award).where(Award.rfq_id == rfq_id))
        if award is None:
            raise InvalidState(f"rfq {rfq_id} is not awarded yet")
        winners = {UUID(str(line["invite_id"])) for line in award.lines}
        noticed = set(
            await session.scalars(
                select(AwardNotice.invite_id).where(AwardNotice.award_id == award.id)
            )
        )
        invites = list(
            await session.scalars(
                select(Invite)
                .where(Invite.rfq_id == rfq_id, Invite.status.in_(("sent", "responded")))
                .order_by(Invite.created_at, Invite.id)
            )
        )
        tenant = await session.get(Tenant, principal.tenant_id)
        if tenant is None:
            raise NotFound("tenant not found")
        rows = await session.execute(
            select(Invite.id, Supplier.name)
            .join(Supplier, Supplier.id == Invite.supplier_id)
            .where(Invite.rfq_id == rfq_id)
        )
        names = {invite_id: name for invite_id, name in rows}
        for invite in invites:
            if invite.id in winners:
                continue
            if invite.id in noticed:
                skipped.append(invite.id)
                continue
            thread = await thread_of(session, invite.id)
            sender = (
                await session.get(User, thread.sender_user_id) if thread.sender_user_id else None
            )
            if sender is None or not thread.contact_email or invite.thread_message_id is None:
                skipped.append(invite.id)
                continue
            buyer_name = await buyer_display_name(session, sender, tenant.name)
            header = Header(
                rfq_ref=thread.rfq.ref,
                subject=thread.subject,
                locale=Locale.of(thread.language, country_code(thread.supplier.country)),
                supplier_name=thread.supplier.name,
                buyer_company=tenant.name,
                signature=Signature(name=buyer_name, company=tenant.name),
            )
            drafts.append(
                _NoticeDraft(
                    invite_id=invite.id,
                    supplier_name=thread.supplier.name,
                    sender_user_id=sender.id,
                    to=thread.contact_email,
                    email=not_selected_email(header, answered=await _answered(session, invite)),
                    gate=GateContext(
                        thread_language=thread.language,
                        first_subject=thread.first_subject,
                        rfq_title=thread.rfq.title,
                        other_suppliers=tuple(n for i, n in names.items() if i != invite.id),
                    ),
                    task=AwardNoticeInput(
                        rfq_ref=thread.rfq.ref,
                        rfq_title=thread.rfq.title,
                        supplier_name=thread.supplier.name,
                        # The language the notice is rendered in (the thread's).
                        supplier_language=header.locale.language,
                    ),
                )
            )
        award_id = award.id

    proposed: list[NoticeProposed] = []
    for draft in drafts:
        context = ToolContext(
            tenant_id=principal.tenant_id, user_id=principal.user_id, run_id=uuid4()
        )

        async def note(draft: _NoticeDraft = draft, context: ToolContext = context) -> str:
            try:
                return (await writer(draft.task, context)).note
            except AgentFailed:
                logger.warning("notice writer failed for invite %s", draft.invite_id)
                return ""

        composed = await compose(draft.email, draft.gate, note)
        if isinstance(composed, Refused):
            logger.warning(
                "notice for invite %s refused: %s",
                draft.invite_id,
                problems_text(composed.problems),
            )
            skipped.append(draft.invite_id)
            continue
        if composed.note_dropped:
            logger.info(
                "notice note left out invite=%s problems=%s",
                draft.invite_id,
                problems_text(composed.note_dropped),
            )
        row_id = notice_id(draft.invite_id)
        args = SendAwardNoticeArgs(
            notice_id=row_id,
            invite_id=draft.invite_id,
            sender_user_id=draft.sender_user_id,
            to=[draft.to],
            subject=composed.rendered.subject,
            body=composed.rendered.text,
            html_body=composed.rendered.html,
        )
        approval_id = await _propose(
            db,
            principal,
            tool=SEND_AWARD_NOTICE,
            ids=notice_proposal_ids(draft.invite_id),
            session_id=f"rfq:{rfq_id}",
            payload=cast(dict[str, JsonValue], args.model_dump(mode="json")),
        )
        async with tenant_session(db, principal.tenant_id) as session:
            await session.execute(
                insert(AwardNotice)
                .values(
                    id=row_id,
                    tenant_id=principal.tenant_id,
                    award_id=award_id,
                    invite_id=draft.invite_id,
                    approval_id=approval_id,
                    status="proposed",
                )
                .on_conflict_do_nothing(index_elements=["invite_id"])
            )
        proposed.append(
            NoticeProposed(
                invite_id=draft.invite_id,
                supplier_name=draft.supplier_name,
                approval_id=approval_id,
            )
        )
    return NoticesRequested(rfq_id=rfq_id, proposed=proposed, skipped=skipped)


# --- Purchase orders ------------------------------------------------------------------------


async def list_purchase_orders(
    db: Db,
    principal: Principal,
    status: PurchaseOrderStatus | None = None,
    *,
    open: bool | None = None,
    state: FulfilmentState | None = None,
    rfq_id: UUID | None = None,
) -> list[PurchaseOrderSummary]:
    """The tenant's POs by number; `open` = sent and still expecting goods (or not)."""
    conditions: list[ColumnElement[bool]] = []
    if status:
        conditions.append(IssuedPurchaseOrder.status == status)
    if open is not None:
        is_open = IssuedPurchaseOrder.fulfilment_state.in_(list(OPEN_FULFILMENT_STATES))
        conditions.append(is_open if open else ~is_open)
    if state:
        conditions.append(IssuedPurchaseOrder.fulfilment_state == state)
    if rfq_id:
        conditions.append(IssuedPurchaseOrder.rfq_id == rfq_id)
    async with tenant_session(db, principal.tenant_id) as session:
        return await _summaries(session, and_(IssuedPurchaseOrder.id.is_not(None), *conditions))


async def _po(session: AsyncSession, po_id: UUID, *, lock: bool = False) -> IssuedPurchaseOrder:
    query = select(IssuedPurchaseOrder).where(IssuedPurchaseOrder.id == po_id)
    po = await session.scalar(query.with_for_update() if lock else query)
    if po is None:
        raise NotFound(f"purchase order {po_id} not found")
    return po


async def get_purchase_order(db: Db, principal: Principal, po_id: UUID) -> PurchaseOrderView:
    async with tenant_session(db, principal.tenant_id) as session:
        po = await _po(session, po_id)
        rfq = await session.get(Rfq, po.rfq_id)
        supplier = await session.get(Supplier, po.supplier_id)
        if rfq is None or supplier is None:
            raise NotFound(f"purchase order {po_id} not found")
        pending = await _open_approval(session, SEND_PURCHASE_ORDER, po_send_proposal_ids(po.id)[1])
        totals = (await quantities(session, [po.id]))[po.id]
        cancellation = await session.scalar(
            select(Approval)
            .where(
                Approval.tool == SEND_PO_CANCELLATION,
                Approval.tool_call_id == cancellation_proposal_ids(po.id)[1],
            )
            .order_by(Approval.created_at.desc())
            .limit(1)
        )
        doc = await po_document(session, po, branded=False)
        return PurchaseOrderView(
            id=po.id,
            ref=po.ref,
            status=cast(PurchaseOrderStatus, po.status),
            rfq_id=po.rfq_id,
            rfq_ref=rfq.ref,
            invite_id=po.invite_id,
            supplier_id=po.supplier_id,
            supplier_name=supplier.name,
            currency=po.currency,
            incoterm=po.incoterm,
            payment_terms=po.payment_terms,
            total=po.total,
            created_at=po.created_at,
            sent_at=po.sent_at,
            send_approval_id=pending.id if pending is not None else None,
            fulfilment=fulfilment_view(po, totals),
            lines=[
                IssuedPurchaseOrderLineView(
                    id=line.id,
                    position=line.position,
                    rfq_line_id=line.rfq_line_id,
                    proposal_line_id=line.proposal_line_id,
                    description=line.description,
                    mpn=line.mpn,
                    quantity=line.quantity,
                    unit=line.unit,
                    unit_price=line.unit_price,
                    line_total=line.line_total,
                    need_by=line.need_by,
                    lead_time_days=line.lead_time_days,
                    received_quantity=totals.by_line.get(line.id, Decimal(0)),
                    remaining_quantity=line.quantity - totals.by_line.get(line.id, Decimal(0)),
                )
                for line in await po_lines(session, po.id)
            ],
            needs_review=po.id in await awaiting_review(session, [po.id]),
            supplier_responses=await supplier_responses(session, po.id),
            cancellation_approval_id=cancellation.id if cancellation else None,
            cancellation_sent=await cancellation_sent(session, po.id),
            document_gaps=document_gaps(doc),
        )


@dataclass(frozen=True, slots=True)
class RenderedDocument:
    content: bytes
    media_type: str
    filename: str


async def render_purchase_order(
    db: Db, principal: Principal, po_id: UUID, fmt: str
) -> RenderedDocument:
    async with tenant_session(db, principal.tenant_id) as session:
        doc = await po_document(session, await _po(session, po_id))
    if fmt == "xlsx":
        return RenderedDocument(to_xlsx(doc), XLSX_MEDIA_TYPE, f"{doc.ref}.xlsx")
    if fmt == "pdf":
        return RenderedDocument(po_pdf(doc), PDF_MEDIA_TYPE, po_pdf_filename(doc))
    return RenderedDocument(to_html(doc).encode(), HTML_MEDIA_TYPE, f"{doc.ref}.html")


async def request_po_send(
    db: Db, principal: Principal, po_id: UUID, mailboxes: MailboxSenderFactory
) -> ApprovalRequested:
    """Propose sending a draft PO to its supplier, in the RFQ thread."""
    async with tenant_session(db, principal.tenant_id) as session:
        po = await _po(session, po_id)
        if po.fulfilment_state == "cancelled":
            raise InvalidState(f"purchase order {po_id} is cancelled")
        if po.status != "draft":
            raise InvalidState(f"purchase order {po_id} is {po.status}")
        thread = await thread_of(session, po.invite_id)
        sender = await session.get(User, thread.sender_user_id) if thread.sender_user_id else None
        if sender is None or not thread.contact_email or thread.invite.thread_message_id is None:
            raise InvalidState(f"purchase order {po_id}: the RFQ email was never sent")
        # Fail early: nobody should approve an email that cannot be sent.
        await mailboxes(session, principal.tenant_id, sender.id)
        tenant = await session.get(Tenant, principal.tenant_id)
        if tenant is None:
            raise NotFound("tenant not found")
        doc = await po_document(session, po)
        body = po_email_body(
            doc,
            thread.language,
            await buyer_display_name(session, sender, tenant.name),
        )
        call_id = po_send_proposal_ids(po.id)[1]
        attempt = await _decided_attempts(session, SEND_PURCHASE_ORDER, call_id)
        args = SendPurchaseOrderArgs(
            purchase_order_id=po.id,
            purchase_order_ref=po.ref,
            content_hash=content_hash(doc),
            invite_id=po.invite_id,
            sender_user_id=sender.id,
            to=[thread.contact_email],
            subject=f"{thread.subject} – {po.ref}"[:_SUBJECT_MAX],
            body=body,
        )

    approval_id = await _propose(
        db,
        principal,
        tool=SEND_PURCHASE_ORDER,
        ids=po_send_proposal_ids(po_id, attempt),
        session_id=f"rfq:{args.invite_id}",
        payload=cast(dict[str, JsonValue], args.model_dump(mode="json")),
    )
    async with tenant_session(db, principal.tenant_id) as session:
        po = await _po(session, po_id, lock=True)
        if po.status == "draft":
            po.status = "pending_approval"
            await session.flush()
    return ApprovalRequested(approval_id=approval_id)


# --- The approval queue -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QueueDetails:
    awards: dict[UUID, AwardApprovalView]
    emails: dict[UUID, ThreadEmailApprovalView]


async def queue_details(session: AsyncSession, approvals: list[Approval]) -> QueueDetails:
    """What the approval queue shows for the award's tools, by approval id."""
    awards: dict[UUID, AwardApprovalView] = {}
    emails: dict[UUID, ThreadEmailApprovalView] = {}
    for approval in approvals:
        try:
            if approval.tool == AWARD_RFQ:
                args = AwardRfqArgs.model_validate(approval.args)
                awards[approval.id] = AwardApprovalView(
                    rfq_id=args.rfq_id,
                    rfq_ref=args.rfq_ref,
                    differs_from_recommendation=args.differs_from_recommendation,
                    reason=args.reason,
                    lines=list(args.lines),
                )
            elif approval.tool in (SEND_PURCHASE_ORDER, SEND_AWARD_NOTICE, SEND_PO_CANCELLATION):
                email: SendPurchaseOrderArgs | SendAwardNoticeArgs | SendPoCancellationArgs = (
                    SendPurchaseOrderArgs.model_validate(approval.args)
                    if approval.tool == SEND_PURCHASE_ORDER
                    else SendPoCancellationArgs.model_validate(approval.args)
                    if approval.tool == SEND_PO_CANCELLATION
                    else SendAwardNoticeArgs.model_validate(approval.args)
                )
                row = (
                    await session.execute(
                        select(Rfq.id, Rfq.ref, Supplier.name)
                        .select_from(Invite)
                        .join(Rfq, Rfq.id == Invite.rfq_id)
                        .join(Supplier, Supplier.id == Invite.supplier_id)
                        .where(Invite.id == email.invite_id)
                    )
                ).one_or_none()
                rfq_id, rfq_ref, supplier_name = row if row is not None else (None, None, None)
                po = email if not isinstance(email, SendAwardNoticeArgs) else None
                emails[approval.id] = ThreadEmailApprovalView(
                    kind="purchase_order" if po else "award_notice",
                    invite_id=email.invite_id,
                    rfq_id=rfq_id,
                    rfq_ref=rfq_ref,
                    supplier_name=supplier_name,
                    purchase_order_id=po.purchase_order_id if po else None,
                    purchase_order_ref=po.purchase_order_ref if po else None,
                    sender_user_id=email.sender_user_id,
                    to=list(email.to),
                    cc=list(email.cc),
                    subject=email.subject,
                    body=email.body,
                    html_body=email.html_body,
                )
        except ValueError:
            logger.error("approval %s has args that do not validate", approval.id)
    return QueueDetails(awards=awards, emails=emails)
