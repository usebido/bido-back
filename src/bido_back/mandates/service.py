"""Negotiation mandates: the buyer approves once; in-bounds emails then go out on their own.

Flow:

1. `propose_mandate` (the buyer, or the Copilot on their behalf): renders the
   initial RFQ emails (pending approvals, as before), then records ONE pending
   approval of `approve_negotiation_mandate` whose payload lists the suppliers
   and their addresses, per-line target and maximum, the deadline, the round
   cap, the daily cap per supplier, and the initial emails by approval id and
   args hash.
2. A person approves it through the normal guard. The worker executes the act
   once (`approve_negotiation_mandate`, below): it saves the negotiation
   settings, records the mandate, and approves exactly the initial emails it
   names (hash-checked), which the executor then sends.
3. From then on `auto_approve_pending` (run after every follow-up and
   negotiation sweep) approves the RFQ's pending chase, clarification and
   counter/BAFO emails on the buyer's behalf, but only when code finds each one
   inside the mandate (`bido_back.mandates.policy`): a mandated invite and
   address, no copies, the buyer's mailbox, no commitment language, prices
   within the approved maximum, rounds and daily cap respected, limits
   unchanged. Anything else is HELD: left pending for a person, with the
   reason recorded. Approval still happens per email, by the mandate's
   approver of record, through the same executor (once, hash-checked).
4. Pausing stops step 3 at once; closing the negotiation ends the mandate.

Awards and purchase orders are never covered: they keep their own approval.
Logs carry ids, counts and reasons only.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final, cast
from uuid import UUID, uuid5

from pydantic import BaseModel, JsonValue, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.agents.approvals import ApprovalProposal
from bido_back.agents.context import ToolContext
from bido_back.agents.events import args_hash
from bido_back.agents.tools import Tool
from bido_back.approvals.audit import approval_audit
from bido_back.approvals.errors import ApprovalConflict
from bido_back.approvals.executor import ActToolRegistry, approval_execute_envelope
from bido_back.approvals.integrity import payload_matches
from bido_back.approvals.sink import PostgresApprovalSink
from bido_back.auth import Principal
from bido_back.db import tenant_session
from bido_back.db.models import (
    Approval,
    AuditEvent,
    Contact,
    Invite,
    MandateSend,
    NegotiationMandate,
    NegotiationMessage,
    Rfq,
    RfqLine,
    Supplier,
)
from bido_back.followups.send import SEND_FOLLOWUP_EMAIL, SendFollowupEmailArgs
from bido_back.mandates.policy import (
    commitment_language,
    negotiation_terms_problem,
    recipients_problem,
)
from bido_back.mandates.schemas import (
    ApproveMandateArgs,
    MandateActivity,
    MandateActivityItem,
    MandateInitialEmail,
    MandateLine,
    MandateSendOutcome,
    MandateStatus,
    MandateSupplier,
    MandateView,
    ProposedMandate,
    ProposeMandateRequest,
)
from bido_back.negotiation.schemas import LineLimitsUpdate, NegotiationSettingsUpdate
from bido_back.negotiation.send import SEND_NEGOTIATION_EMAIL, SendNegotiationEmailArgs
from bido_back.negotiation.service import apply_settings
from bido_back.rfqs.agents import EmailWriterAgent
from bido_back.rfqs.errors import InvalidState, NotFound
from bido_back.rfqs.mailbox import MailboxSenderFactory
from bido_back.rfqs.send import SEND_RFQ_EMAIL, SendRfqEmailArgs
from bido_back.rfqs.service import prepare_send
from bido_back.worker.jobs import JobQueue

logger = logging.getLogger(__name__)

Db = AsyncEngine | async_sessionmaker[AsyncSession]

APPROVE_NEGOTIATION_MANDATE: Final = "approve_negotiation_mandate"
# The emails a mandate may approve. Awards and purchase orders are never among them.
AUTO_TOOLS: Final = frozenset({SEND_RFQ_EMAIL, SEND_FOLLOWUP_EMAIL, SEND_NEGOTIATION_EMAIL})

_NAMESPACE: Final = UUID("7a1c3e5f-9b2d-4f60-8e17-3c5a9d2b4e81")
_DAY: Final = timedelta(hours=24)


def mandate_proposal_ids(rfq_id: UUID, attempt: int) -> tuple[UUID, str]:
    """(run_id, tool_call_id) of the `attempt`-th mandate proposal for an RFQ."""
    call_id = f"{APPROVE_NEGOTIATION_MANDATE}:{rfq_id}"
    return uuid5(_NAMESPACE, f"{call_id}:{attempt}"), call_id


# --- Proposing --------------------------------------------------------------------------


async def propose_mandate(
    db: Db,
    principal: Principal,
    rfq_id: UUID,
    request: ProposeMandateRequest,
    *,
    writer: EmailWriterAgent,
    mailboxes: MailboxSenderFactory,
) -> ProposedMandate:
    """Render the initial emails and record the single mandate approval.

    `writer` and `mailboxes` are what `prepare_send` takes (the RFQ email
    writer and the mailbox sender factory). Nothing is sent here.
    """
    overrides = {limits.rfq_line_id: limits for limits in request.lines}
    async with tenant_session(db, principal.tenant_id) as session:
        rfq = await session.get(Rfq, rfq_id)
        if rfq is None:
            raise NotFound(f"rfq {rfq_id} not found")
        if rfq.status not in ("draft", "open"):
            raise InvalidState(f"rfq {rfq_id} is {rfq.status}")
        if rfq.negotiation_status == "closed":
            raise InvalidState(f"rfq {rfq_id}: the negotiation is closed")
        live = await session.scalar(
            select(NegotiationMandate.id).where(
                NegotiationMandate.rfq_id == rfq_id, NegotiationMandate.status != "ended"
            )
        )
        if live is not None:
            raise InvalidState(f"rfq {rfq_id} already has a live mandate")
        lines = list(
            await session.scalars(
                select(RfqLine).where(RfqLine.rfq_id == rfq_id).order_by(RfqLine.position)
            )
        )
        mandate_lines: list[MandateLine] = []
        missing: list[int] = []
        for line in lines:
            o = overrides.get(line.id)
            target = o.target_unit_price if o else line.target_unit_price
            maximum = o.max_unit_price if o else line.max_unit_price
            lead = o.max_lead_time_days if o else line.max_lead_time_days
            if target is None or maximum is None:
                missing.append(line.position)
                continue
            mandate_lines.append(
                MandateLine(
                    rfq_line_id=line.id,
                    position=line.position,
                    description=line.description[:500],
                    target_unit_price=target,
                    max_unit_price=maximum,
                    max_lead_time_days=lead,
                )
            )
        if missing:
            raise InvalidState(
                "a mandate needs a target and a maximum price on every line; missing on "
                f"line(s) {', '.join(str(p) for p in missing)}"
            )
        unknown = set(overrides) - {line.id for line in lines}
        if unknown:
            raise NotFound(f"line(s) not on rfq {rfq_id}")
        attempts = await session.scalar(
            select(func.count()).where(
                Approval.tool == APPROVE_NEGOTIATION_MANDATE,
                Approval.session_id == f"rfq:{rfq_id}",
            )
        )
        rfq_ref, currency = rfq.ref, rfq.base_currency

    prepared = await prepare_send(db, principal, rfq_id, writer, mailboxes)

    async with tenant_session(db, principal.tenant_id) as session:
        initial: list[MandateInitialEmail] = []
        to_by_invite: dict[UUID, list[str]] = {}
        for item in prepared.approvals:
            approval = await session.get(Approval, item.approval_id)
            if approval is None or approval.status != "pending":
                continue
            args = SendRfqEmailArgs.model_validate(approval.args)
            initial.append(
                MandateInitialEmail(
                    approval_id=approval.id,
                    invite_id=item.invite_id,
                    args_hash=approval.args_hash,
                )
            )
            to_by_invite[item.invite_id] = list(args.to)
        rows = await session.execute(
            select(Invite, Supplier, Contact)
            .join(Supplier, Supplier.id == Invite.supplier_id)
            .join(Contact, Contact.id == Invite.contact_id)
            .where(
                Invite.rfq_id == rfq_id,
                Invite.status.in_(("pending_approval", "sent", "responded")),
            )
            .order_by(Invite.created_at, Invite.id)
        )
        suppliers: list[MandateSupplier] = []
        for invite, supplier, contact in rows:
            to = to_by_invite.get(invite.id)
            if to is None:
                if invite.status == "pending_approval" or not contact.email:
                    # Awaiting a separate, non-mandate approval: not covered.
                    continue
                to = [contact.email]
            suppliers.append(
                MandateSupplier(invite_id=invite.id, supplier_name=supplier.name[:300], to=to)
            )
    if not suppliers:
        raise InvalidState(f"rfq {rfq_id} has no supplier to negotiate with")

    args = ApproveMandateArgs(
        rfq_id=rfq_id,
        rfq_ref=rfq_ref,
        sender_user_id=principal.user_id,
        currency=currency,
        deadline=request.deadline,
        max_rounds=request.max_rounds,
        max_messages_per_supplier_per_day=request.max_messages_per_supplier_per_day,
        disclose_target_price=True,
        disclose_competing_offers=True,
        suppliers=suppliers,
        lines=mandate_lines,
        initial_emails=initial,
    )
    payload = cast(dict[str, JsonValue], args.model_dump(mode="json"))
    run_id, call_id = mandate_proposal_ids(rfq_id, attempts or 0)
    try:
        await PostgresApprovalSink(db).request_approval(
            ApprovalProposal(
                tenant_id=principal.tenant_id,
                run_id=run_id,
                session_id=f"rfq:{rfq_id}",
                tool_call_id=call_id,
                tool=APPROVE_NEGOTIATION_MANDATE,
                args=payload,
                args_hash=args_hash(payload),
                requested_by=principal.user_id,
            )
        )
    except ApprovalConflict:
        logger.info("mandate for rfq %s was already proposed", rfq_id)
    async with tenant_session(db, principal.tenant_id) as session:
        approval_id = await session.scalar(
            select(Approval.id).where(
                Approval.run_id == str(run_id), Approval.tool_call_id == call_id
            )
        )
    if approval_id is None:
        raise NotFound(f"mandate approval for rfq {rfq_id} not found")
    logger.info(
        "mandate proposed tenant=%s rfq=%s approval=%s suppliers=%d lines=%d initial=%d",
        principal.tenant_id,
        rfq_id,
        approval_id,
        len(suppliers),
        len(mandate_lines),
        len(initial),
    )
    return ProposedMandate(
        rfq_id=rfq_id,
        approval_id=approval_id,
        suppliers=len(suppliers),
        lines=len(mandate_lines),
        initial_emails=len(initial),
        deadline=request.deadline,
    )


# --- Executing the approved mandate ------------------------------------------------------


class MandateOutcome(BaseModel):
    mandate_id: UUID
    initial_emails_approved: int


@dataclass(frozen=True, slots=True)
class MandateActDeps:
    db: Db
    # Where the initial emails' execution jobs go. None: the next sweep's
    # `auto_approve_pending` pass enqueues them (approved, not yet executed).
    jobs: JobQueue | None = None


async def _mark_auto_approved(
    session: AsyncSession,
    approval: Approval,
    mandate: NegotiationMandate,
    invite_id: UUID | None,
    now: datetime,
) -> None:
    approval.status = "approved"
    approval.decided_by = mandate.approved_by
    approval.decided_at = now
    session.add(approval_audit(approval, "approval.approved_by_mandate", mandate.approved_by))
    session.add(
        MandateSend(
            tenant_id=mandate.tenant_id,
            mandate_id=mandate.id,
            approval_id=approval.id,
            invite_id=invite_id,
            tool=approval.tool,
            outcome="auto_approved",
        )
    )


def _hold(
    session: AsyncSession,
    approval: Approval,
    mandate: NegotiationMandate,
    invite_id: UUID | None,
    reason: str,
) -> None:
    session.add(
        MandateSend(
            tenant_id=mandate.tenant_id,
            mandate_id=mandate.id,
            approval_id=approval.id,
            invite_id=invite_id,
            tool=approval.tool,
            outcome="held",
            reason=reason,
        )
    )
    session.add(
        AuditEvent(
            tenant_id=mandate.tenant_id,
            actor_user_id=None,
            action="mandate.held",
            payload={
                "mandate_id": str(mandate.id),
                "approval_id": str(approval.id),
                "tool": approval.tool,
                "reason": reason,
            },
        )
    )


class _ApproveMandate:
    def __init__(self, deps: MandateActDeps) -> None:
        self._deps = deps

    async def __call__(self, ctx: ToolContext, args: ApproveMandateArgs) -> MandateOutcome:
        if ctx.user_id is None:
            raise InvalidState("a mandate needs the approving person")
        now = datetime.now(UTC)
        approved: list[UUID] = []
        async with tenant_session(self._deps.db, ctx.tenant_id) as session:
            approval = await session.scalar(
                select(Approval).where(
                    Approval.run_id == str(ctx.run_id),
                    Approval.tool == APPROVE_NEGOTIATION_MANDATE,
                    Approval.status.in_(("approved", "executed")),
                )
            )
            if approval is None:
                raise InvalidState(f"mandate for rfq {args.rfq_id}: approval not found")
            existing = await session.scalar(
                select(NegotiationMandate).where(NegotiationMandate.approval_id == approval.id)
            )
            if existing is not None:
                return MandateOutcome(mandate_id=existing.id, initial_emails_approved=0)
            if approval.requested_by != args.sender_user_id:
                raise InvalidState("the mandate's sender is not its requester")
            live = await session.scalar(
                select(NegotiationMandate.id).where(
                    NegotiationMandate.rfq_id == args.rfq_id, NegotiationMandate.status != "ended"
                )
            )
            if live is not None:
                raise InvalidState(f"rfq {args.rfq_id} already has a live mandate")
            await apply_settings(
                session,
                tenant_id=ctx.tenant_id,
                actor_user_id=ctx.user_id,
                rfq_id=args.rfq_id,
                changes=NegotiationSettingsUpdate(
                    deadline=args.deadline,
                    max_rounds=args.max_rounds,
                    early_close=True,
                    lines=[
                        LineLimitsUpdate(
                            rfq_line_id=line.rfq_line_id,
                            target_unit_price=line.target_unit_price,
                            max_unit_price=line.max_unit_price,
                            max_lead_time_days=line.max_lead_time_days,
                        )
                        for line in args.lines
                    ],
                ),
            )
            mandate = NegotiationMandate(
                tenant_id=ctx.tenant_id,
                rfq_id=args.rfq_id,
                approval_id=approval.id,
                args_hash=approval.args_hash,
                requested_by=approval.requested_by,
                approved_by=ctx.user_id,
                sender_user_id=args.sender_user_id,
                terms=cast(dict[str, object], args.model_dump(mode="json")),
                status="active",
                deadline=args.deadline,
            )
            session.add(mandate)
            await session.flush()
            for email in args.initial_emails:
                row = await session.scalar(
                    select(Approval).where(Approval.id == email.approval_id).with_for_update()
                )
                if (
                    row is None
                    or row.tool != SEND_RFQ_EMAIL
                    or row.status != "pending"
                    or row.args_hash != email.args_hash
                    or row.requested_by != args.sender_user_id
                    or not payload_matches(row)
                ):
                    if row is not None and row.status == "pending":
                        _hold(session, row, mandate, email.invite_id, "initial_email_changed")
                    continue
                await _mark_auto_approved(session, row, mandate, email.invite_id, now)
                approved.append(row.id)
            session.add(
                AuditEvent(
                    tenant_id=ctx.tenant_id,
                    actor_user_id=ctx.user_id,
                    action="mandate.started",
                    payload={
                        "mandate_id": str(mandate.id),
                        "rfq_id": str(args.rfq_id),
                        "approval_id": str(approval.id),
                        "suppliers": len(args.suppliers),
                        "initial_emails": len(approved),
                    },
                )
            )
            await session.flush()
            mandate_id = mandate.id
        await _enqueue(self._deps.jobs, ctx.tenant_id, approved)
        logger.info(
            "mandate started tenant=%s rfq=%s mandate=%s initial=%d",
            ctx.tenant_id,
            args.rfq_id,
            mandate_id,
            len(approved),
        )
        return MandateOutcome(mandate_id=mandate_id, initial_emails_approved=len(approved))


async def _enqueue(jobs: JobQueue | None, tenant_id: UUID, approval_ids: Sequence[UUID]) -> None:
    if jobs is None:
        return
    for approval_id in approval_ids:
        try:
            await jobs.enqueue(approval_execute_envelope(tenant_id, approval_id))
        except Exception:
            # Approved and committed: the next auto-approve pass re-enqueues it.
            logger.exception("mandate approval %s approved but not enqueued", approval_id)


def approve_mandate_tool(deps: MandateActDeps) -> Tool[ApproveMandateArgs, MandateOutcome]:
    return Tool(
        name=APPROVE_NEGOTIATION_MANDATE,
        description=(
            "Start a negotiation under an approved mandate: send the initial RFQ emails "
            "and let in-bounds follow-ups and counter-offers go out automatically."
        ),
        input_model=ApproveMandateArgs,
        output_model=MandateOutcome,
        kind="act",
        handler=_ApproveMandate(deps),
    )


def register_mandate_act_tools(registry: ActToolRegistry, deps: MandateActDeps) -> None:
    registry.register(approve_mandate_tool(deps))


# --- Automatic approval under a mandate ---------------------------------------------------


@dataclass(slots=True)
class AutoApproveResult:
    approved: list[UUID] = field(default_factory=list[UUID])
    held: int = 0
    re_enqueued: int = 0


def _limits_unchanged(rfq: Rfq, lines: Sequence[RfqLine], terms: ApproveMandateArgs) -> bool:
    if rfq.negotiation_deadline != terms.deadline or rfq.negotiation_max_rounds != terms.max_rounds:
        return False
    by_id = {line.id: line for line in lines}
    for limit in terms.lines:
        line = by_id.get(limit.rfq_line_id)
        if (
            line is None
            or line.target_unit_price != limit.target_unit_price
            or line.max_unit_price != limit.max_unit_price
            or line.max_lead_time_days != limit.max_lead_time_days
        ):
            return False
    return True


async def _check(
    session: AsyncSession,
    approval: Approval,
    mandate: NegotiationMandate,
    terms: ApproveMandateArgs,
    now: datetime,
) -> tuple[UUID | None, str | None, bool]:
    """(invite_id, reason to hold or None, retry later). Pure reads."""
    if not payload_matches(approval):
        return None, "integrity_failed", False
    try:
        if approval.tool == SEND_RFQ_EMAIL:
            rfq_args = SendRfqEmailArgs.model_validate(approval.args)
            initial = {e.approval_id: e for e in terms.initial_emails}
            email = initial.get(approval.id)
            if email is None or email.args_hash != approval.args_hash:
                return rfq_args.invite_id, "rfq_email_not_in_mandate", False
            invite_id, sender = rfq_args.invite_id, rfq_args.sender_user_id
            to, cc, texts = rfq_args.to, rfq_args.cc, ()
        elif approval.tool == SEND_FOLLOWUP_EMAIL:
            f_args = SendFollowupEmailArgs.model_validate(approval.args)
            invite_id, sender = f_args.invite_id, f_args.sender_user_id
            to, cc, texts = f_args.to, f_args.cc, (f_args.subject, f_args.body)
        else:
            n_args = SendNegotiationEmailArgs.model_validate(approval.args)
            invite_id, sender = n_args.invite_id, n_args.sender_user_id
            to, cc, texts = n_args.to, n_args.cc, (n_args.subject, n_args.body)
            if n_args.round > terms.max_rounds:
                return invite_id, "rounds_exceeded", False
            row = await session.get(NegotiationMessage, n_args.negotiation_message_id)
            if row is None:
                return invite_id, "terms_unreadable", False
            problem = negotiation_terms_problem(cast(dict[str, JsonValue], row.terms), terms)
            if problem is not None:
                return invite_id, problem, False
    except ValidationError:
        return None, "args_unreadable", False

    supplier = next((s for s in terms.suppliers if s.invite_id == invite_id), None)
    if supplier is None:
        return invite_id, "supplier_not_in_mandate", False
    if sender != terms.sender_user_id or approval.requested_by != terms.sender_user_id:
        return invite_id, "sender_not_in_mandate", False
    problem = recipients_problem(to, cc, supplier.to)
    if problem is not None:
        return invite_id, problem, False
    if texts and commitment_language(*texts):
        return invite_id, "commitment_language", False
    # Daily cap: not a reason to hold; the email waits for the next window.
    sent_today = await session.scalar(
        select(func.count()).where(
            MandateSend.mandate_id == mandate.id,
            MandateSend.invite_id == invite_id,
            MandateSend.outcome == "auto_approved",
            MandateSend.created_at > now - _DAY,
        )
    )
    if (sent_today or 0) >= terms.max_messages_per_supplier_per_day:
        return invite_id, None, True
    return invite_id, None, False


async def auto_approve_pending(
    db: Db,
    tenant_id: UUID,
    *,
    jobs: JobQueue | None,
    now: datetime | None = None,
) -> AutoApproveResult:
    """Approve the in-bounds pending emails of every active mandate in a tenant."""
    at = now or datetime.now(UTC)
    result = AutoApproveResult()
    to_enqueue: list[UUID] = []
    async with tenant_session(db, tenant_id) as session:
        mandates = list(
            await session.scalars(
                select(NegotiationMandate)
                .where(NegotiationMandate.status == "active")
                .with_for_update()
            )
        )
        for mandate in mandates:
            terms = ApproveMandateArgs.model_validate(mandate.terms)
            rfq = await session.get(Rfq, mandate.rfq_id)
            if (
                rfq is None
                or rfq.status != "open"
                or rfq.negotiation_status not in ("open", "ready_to_close")
                or at >= mandate.deadline
            ):
                continue
            lines = list(await session.scalars(select(RfqLine).where(RfqLine.rfq_id == rfq.id)))
            unchanged = _limits_unchanged(rfq, lines, terms)
            decided = select(MandateSend.approval_id).where(MandateSend.mandate_id == mandate.id)
            pending = list(
                await session.scalars(
                    select(Approval)
                    .where(
                        Approval.session_id == f"rfq:{rfq.id}",
                        Approval.tool.in_(AUTO_TOOLS),
                        Approval.status == "pending",
                        Approval.id.not_in(decided),
                    )
                    .order_by(Approval.created_at)
                    .with_for_update(skip_locked=True)
                )
            )
            for approval in pending:
                if not unchanged:
                    _hold(session, approval, mandate, None, "limits_changed")
                    result.held += 1
                    continue
                invite_id, reason, retry = await _check(session, approval, mandate, terms, at)
                if retry:
                    continue
                if reason is not None:
                    _hold(session, approval, mandate, invite_id, reason)
                    result.held += 1
                    logger.info(
                        "mandate held approval=%s mandate=%s reason=%s",
                        approval.id,
                        mandate.id,
                        reason,
                    )
                    continue
                await _mark_auto_approved(session, approval, mandate, invite_id, at)
                await session.flush()
                result.approved.append(approval.id)
                to_enqueue.append(approval.id)
            # Approved by this mandate but not executed yet (a lost enqueue): again.
            stuck = list(
                await session.scalars(
                    select(Approval.id)
                    .join(MandateSend, MandateSend.approval_id == Approval.id)
                    .where(
                        MandateSend.mandate_id == mandate.id,
                        MandateSend.outcome == "auto_approved",
                        Approval.status == "approved",
                        Approval.id.not_in(to_enqueue),
                    )
                )
            )
            to_enqueue.extend(stuck)
            result.re_enqueued += len(stuck)
        await session.flush()
    await _enqueue(jobs, tenant_id, to_enqueue)
    if result.approved or result.held:
        logger.info(
            "mandates swept tenant=%s approved=%d held=%d re_enqueued=%d",
            tenant_id,
            len(result.approved),
            result.held,
            result.re_enqueued,
        )
    return result


# --- Pause, resume, views ---------------------------------------------------------------


def _view(mandate: NegotiationMandate) -> MandateView:
    terms = ApproveMandateArgs.model_validate(mandate.terms)
    return MandateView(
        id=mandate.id,
        rfq_id=mandate.rfq_id,
        status=cast(MandateStatus, mandate.status),
        approval_id=mandate.approval_id,
        approved_by=mandate.approved_by,
        deadline=mandate.deadline,
        max_rounds=terms.max_rounds,
        max_messages_per_supplier_per_day=terms.max_messages_per_supplier_per_day,
        suppliers=[s.supplier_name for s in terms.suppliers],
        created_at=mandate.created_at,
        paused_at=mandate.paused_at,
        ended_at=mandate.ended_at,
    )


async def _live(session: AsyncSession, rfq_id: UUID) -> NegotiationMandate | None:
    return await session.scalar(
        select(NegotiationMandate)
        .where(NegotiationMandate.rfq_id == rfq_id, NegotiationMandate.status != "ended")
        .with_for_update()
    )


async def set_paused(db: Db, principal: Principal, rfq_id: UUID, *, paused: bool) -> MandateView:
    """The kill switch: paused, nothing more is approved on the buyer's behalf."""
    async with tenant_session(db, principal.tenant_id) as session:
        mandate = await _live(session, rfq_id)
        if mandate is None:
            raise NotFound(f"rfq {rfq_id} has no live mandate")
        if paused and mandate.status == "active":
            mandate.status = "paused"
            mandate.paused_at = datetime.now(UTC)
        elif not paused and mandate.status == "paused":
            mandate.status = "active"
            mandate.paused_at = None
        session.add(
            AuditEvent(
                tenant_id=principal.tenant_id,
                actor_user_id=principal.user_id,
                action="mandate.paused" if paused else "mandate.resumed",
                payload={"mandate_id": str(mandate.id), "rfq_id": str(rfq_id)},
            )
        )
        await session.flush()
        view = _view(mandate)
    logger.info(
        "mandate %s tenant=%s rfq=%s by=%s",
        "paused" if paused else "resumed",
        principal.tenant_id,
        rfq_id,
        principal.user_id,
    )
    return view


async def get_activity(db: Db, principal: Principal, rfq_id: UUID) -> MandateActivity:
    async with tenant_session(db, principal.tenant_id) as session:
        rfq = await session.get(Rfq, rfq_id)
        if rfq is None:
            raise NotFound(f"rfq {rfq_id} not found")
        mandate = await session.scalar(
            select(NegotiationMandate)
            .where(NegotiationMandate.rfq_id == rfq_id)
            .order_by(NegotiationMandate.created_at.desc())
            .limit(1)
        )
        if mandate is None:
            return MandateActivity(rfq_id=rfq_id, mandate=None, items=[])
        names = {
            s.invite_id: s.supplier_name
            for s in ApproveMandateArgs.model_validate(mandate.terms).suppliers
        }
        rows = await session.execute(
            select(MandateSend, Approval)
            .join(Approval, Approval.id == MandateSend.approval_id)
            .where(MandateSend.mandate_id == mandate.id)
            .order_by(MandateSend.created_at.desc())
        )
        items = [
            MandateActivityItem(
                approval_id=send.approval_id,
                tool=send.tool,
                outcome=cast(MandateSendOutcome, send.outcome),
                reason=send.reason,
                invite_id=send.invite_id,
                supplier_name=names.get(send.invite_id) if send.invite_id else None,
                approval_status=approval.status,
                subject=_subject(approval),
                created_at=send.created_at,
            )
            for send, approval in rows
        ]
        return MandateActivity(rfq_id=rfq_id, mandate=_view(mandate), items=items)


def _subject(approval: Approval) -> str | None:
    value = approval.args.get("subject")
    return value if isinstance(value, str) else None
