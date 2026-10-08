"""Supplier negotiation: settings, the sweep, early close, closing at the deadline.

A negotiation starts when the buyer saves its settings (`PUT
/v1/rfqs/{id}/negotiation`): a deadline, a round cap, and per-line limits
(target, maximum, lead time, in the RFQ's base currency). Until then, nothing
is negotiated.

The `negotiation.sweep` job (every 15 minutes, see `bido_back.negotiation.jobs`)
does, per tenant, per RFQ whose negotiation is open:

1. Reconcile: a proposed negotiation email whose reason is gone is canceled
   and its pending approval expired (the negotiation closed, or the supplier
   sent a newer offer).
2. Deadline: past the deadline, the negotiation closes (`close_negotiation`):
   the best eligible offer per line is kept as the outcome, pending emails are
   canceled, follow-ups stop. Nothing is awarded: that is the buyer's step.
3. Evaluate: each supplier's latest offer, not evaluated yet, goes through the
   strategy (`bido_back.negotiation.strategy`, pure code). A counter-offer or a
   best-and-final request is written by the negotiation writer, checked
   (`negotiation_email_problems`: the numbers are the strategy's), and
   recorded as a PENDING approval of `send_negotiation_email` requested on
   behalf of the buyer who sent the RFQ email; another person approves and the
   worker sends it once, in the thread (`bido_back.negotiation.send`).
4. Early close: when every invited supplier answered and every line has an
   offer within target, the RFQ becomes `ready_to_close` (the buyer sees
   "pronto pra fechar" and closes, or waits for the deadline).

Logs carry ids and counts only.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Final, cast
from uuid import UUID, uuid4

from pydantic import JsonValue
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.agents.approvals import ApprovalProposal
from bido_back.agents.catalog.contracts import NegotiationEmailInput, NegotiationLineAsk
from bido_back.agents.catalog.definitions import negotiation_email_problems
from bido_back.agents.context import ToolContext
from bido_back.agents.events import args_hash
from bido_back.approvals.audit import approval_audit
from bido_back.approvals.errors import ApprovalConflict
from bido_back.approvals.sink import PostgresApprovalSink
from bido_back.auth import Principal
from bido_back.comparison.compute import Comparison
from bido_back.comparison.loader import load_comparison
from bido_back.comparison.policy import ComparisonPolicy
from bido_back.comparison.rules import policy_blockers
from bido_back.db import tenant_session
from bido_back.db.models import (
    Approval,
    AuditEvent,
    Contact,
    FieldCorrection,
    Invite,
    Message,
    NegotiationMandate,
    NegotiationMessage,
    Proposal,
    Rfq,
    RfqLine,
    Supplier,
    Tenant,
    User,
)
from bido_back.followups.service import reply_subject
from bido_back.negotiation.agents import NegotiationWriter
from bido_back.negotiation.history import PriceHistory, quoted_price_history
from bido_back.negotiation.schemas import (
    InviteNegotiation,
    InviteNegotiationStatus,
    LineDecisionView,
    LineLimitsView,
    NegotiationActionName,
    NegotiationMessageStatus,
    NegotiationMessageView,
    NegotiationSettings,
    NegotiationSettingsUpdate,
    NegotiationStatus,
    OutcomeLine,
    RfqNegotiation,
)
from bido_back.negotiation.send import (
    SEND_NEGOTIATION_EMAIL,
    SendNegotiationEmailArgs,
    negotiation_message_id,
    negotiation_proposal_ids,
)
from bido_back.negotiation.strategy import (
    LineLimits,
    LineOffer,
    PreviousRound,
    SupplierDecision,
    best_competing_prices,
    decide_supplier,
    ready_to_close,
)
from bido_back.rfqs.errors import AgentFailed, InvalidState, NotFound
from bido_back.rfqs.send import SEND_RFQ_EMAIL, send_proposal_ids
from bido_back.rfqs.service import buyer_display_name

logger = logging.getLogger(__name__)

Db = AsyncEngine | async_sessionmaker[AsyncSession]

# Rounds that count toward `max_rounds`: waiting, sent, or turned down.
_COUNTED: Final = ("proposed", "sent", "rejected")
_ACTIVE: Final = ("open", "ready_to_close")
_FINAL_INVITE: Final = ("rejected", "closed")


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    proposed: int = 0
    ready: bool = False
    closed: bool = False
    skipped: int = 0
    approval_ids: tuple[UUID, ...] = ()


@dataclass(slots=True)
class SweepResult:
    canceled: int = 0
    proposed: int = 0
    ready: int = 0
    closed: int = 0
    skipped: int = 0
    rfqs: list[UUID] = field(default_factory=list[UUID])


# --- Loading what the strategy needs ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Line:
    row: RfqLine
    limits: LineLimits
    quantity: Decimal


async def _lines(session: AsyncSession, rfq: Rfq, history: PriceHistory) -> list[_Line]:
    lines: list[_Line] = []
    for row in await session.scalars(
        select(RfqLine).where(RfqLine.rfq_id == rfq.id).order_by(RfqLine.position)
    ):
        history_price = (
            await history(session, rfq, row.mpn)
            if row.target_unit_price is None and row.mpn
            else None
        )
        quantities = [Decimal(str(q)) for q in row.quantities] or [Decimal(1)]
        lines.append(
            _Line(
                row=row,
                limits=LineLimits(
                    rfq_line_id=row.id,
                    target=row.target_unit_price,
                    max_price=row.max_unit_price,
                    max_lead_days=row.max_lead_time_days,
                    history_price=history_price,
                ),
                quantity=quantities[0],
            )
        )
    return lines


def _offers(comparison: Comparison) -> dict[UUID, dict[UUID, LineOffer]]:
    """Per invite, per line: the first-tier offer in base currency, with its blockers."""
    offers: dict[UUID, dict[UUID, LineOffer]] = {}
    for column in comparison.suppliers:
        mine: dict[UUID, LineOffer] = {}
        for cell in column.cells:
            if cell.tier != 0:
                continue
            mine[cell.rfq_line_id] = LineOffer(
                rfq_line_id=cell.rfq_line_id,
                unit_price=cell.effective_unit_price_base,
                lead_time_days=cell.lead_time_days,
                blockers=tuple(
                    b
                    for b in policy_blockers(cell, column, comparison.policy)
                    if b
                    not in (
                        "no_quote_for_line",
                        "above_buyer_maximum",
                        "buyer_lead_time_unconfirmed",
                        "above_buyer_lead_time_limit",
                    )
                ),
            )
        offers[column.invite_id] = mine
    return offers


def _rounds(messages: Sequence[NegotiationMessage]) -> tuple[int, bool, bool]:
    """(rounds used, a best-and-final was requested, an email awaits approval)."""
    counted = [m for m in messages if m.status in _COUNTED]
    bafo = any(m.action == "bafo" and m.status in ("proposed", "sent") for m in messages)
    pending = any(m.status == "proposed" for m in messages)
    return len(counted), bafo, pending


def _decimal_value(value: JsonValue) -> Decimal | None:
    if not isinstance(value, str):
        return None
    try:
        number = Decimal(value)
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _previous_rounds(messages: Sequence[NegotiationMessage]) -> list[PreviousRound]:
    previous: list[PreviousRound] = []
    for message in sorted(messages, key=lambda message: message.round):
        if message.status != "sent":
            continue
        terms = cast(dict[str, JsonValue], message.terms)
        entries = terms.get("lines")
        if not isinstance(entries, list):
            continue
        offers: dict[UUID, LineOffer] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            line_id = entry.get("rfq_line_id")
            if not isinstance(line_id, str):
                continue
            try:
                line_uuid = UUID(line_id)
            except ValueError:
                continue
            lead = entry.get("offered_lead_days")
            offers[line_uuid] = LineOffer(
                rfq_line_id=line_uuid,
                unit_price=_decimal_value(entry.get("offered")),
                lead_time_days=lead
                if isinstance(lead, int) and not isinstance(lead, bool)
                else None,
                blockers=("previous_offer_blocked",) if entry.get("decision") == "blocked" else (),
            )
        if offers:
            previous.append(PreviousRound(offers=offers))
    return previous


def _decide(
    lines: Sequence[_Line],
    offers: dict[UUID, LineOffer],
    best: dict[UUID, Decimal],
    messages: Sequence[NegotiationMessage],
    rfq: Rfq,
    now: datetime,
    holds: Sequence[str] = (),
) -> SupplierDecision:
    rounds_used, bafo, _ = _rounds(messages)
    return decide_supplier(
        lines=[line.limits for line in lines],
        offers=offers,
        best_competing=best,
        rounds_used=rounds_used,
        bafo_already_requested=bafo,
        max_rounds=rfq.negotiation_max_rounds,
        deadline=rfq.negotiation_deadline,
        now=now,
        previous_rounds=_previous_rounds(messages),
        holds=holds,
    )


# --- The view (API) --------------------------------------------------------------------


def _settings(rfq: Rfq, lines: Sequence[_Line]) -> NegotiationSettings:
    return NegotiationSettings(
        deadline=rfq.negotiation_deadline,
        max_rounds=rfq.negotiation_max_rounds,
        early_close=rfq.negotiation_early_close,
        base_currency=rfq.base_currency,
        comparison_policy=ComparisonPolicy.model_validate(rfq.comparison_policy or {}),
        lines=[
            LineLimitsView(
                rfq_line_id=line.row.id,
                position=line.row.position,
                description=line.row.description,
                mpn=line.row.mpn,
                target_unit_price=line.row.target_unit_price,
                max_unit_price=line.row.max_unit_price,
                max_lead_time_days=line.row.max_lead_time_days,
            )
            for line in lines
        ],
    )


def _line_views(decision: SupplierDecision) -> list[LineDecisionView]:
    return [
        LineDecisionView(
            rfq_line_id=d.rfq_line_id,
            decision=d.kind,
            offered_unit_price_base=d.offered_price,
            ask_unit_price=d.ask_price,
            lead_time_days=d.lead_time_days,
            ask_lead_time_days=d.ask_lead_days,
        )
        for d in decision.lines
    ]


async def _view(
    db: Db, tenant_id: UUID, rfq_id: UUID, now: datetime, history: PriceHistory
) -> RfqNegotiation:
    async with tenant_session(db, tenant_id) as session:
        rfq = await session.get(Rfq, rfq_id)
        if rfq is None:
            raise NotFound(f"rfq {rfq_id} not found")
        created_by = rfq.created_by
    comparison = await load_comparison(
        db, Principal(tenant_id=tenant_id, user_id=created_by), rfq_id, now.date()
    )
    offers = _offers(comparison)
    best = best_competing_prices(offers)
    columns = {c.invite_id: c for c in comparison.suppliers}
    async with tenant_session(db, tenant_id) as session:
        rfq = await session.get(Rfq, rfq_id)
        if rfq is None:
            raise NotFound(f"rfq {rfq_id} not found")
        lines = await _lines(session, rfq, history)
        rows = (
            await session.execute(
                select(Invite, Supplier.name)
                .join(Supplier, Supplier.id == Invite.supplier_id)
                .where(Invite.rfq_id == rfq.id)
                .order_by(Invite.created_at, Invite.id)
            )
        ).all()
        messages = list(
            await session.scalars(
                select(NegotiationMessage)
                .join(Invite, Invite.id == NegotiationMessage.invite_id)
                .where(Invite.rfq_id == rfq.id)
                .order_by(NegotiationMessage.round)
            )
        )
        invites: list[InviteNegotiation] = []
        for invite, supplier_name in rows:
            mine = [m for m in messages if m.invite_id == invite.id]
            column = columns.get(invite.id)
            decision = (
                _decide(
                    lines,
                    offers.get(invite.id, {}),
                    best.get(invite.id, {}),
                    mine,
                    rfq,
                    now,
                    column.negotiation_holds,
                )
                if column is not None
                else None
            )
            invites.append(
                InviteNegotiation(
                    invite_id=invite.id,
                    supplier_name=supplier_name,
                    invite_status=invite.status,
                    status=cast(InviteNegotiationStatus, invite.negotiation_status),
                    rounds_used=_rounds(mine)[0],
                    latest_proposal_id=column.proposal_id if column else None,
                    latest_proposal_version=column.proposal_version if column else None,
                    lines=_line_views(decision) if decision else [],
                    strategy_reasons=list(decision.reasons) if decision else [],
                    stalled_rounds=decision.stalled_rounds if decision else 0,
                    messages=[
                        NegotiationMessageView(
                            id=m.id,
                            round=m.round,
                            action=cast(NegotiationActionName, m.action),
                            status=cast(NegotiationMessageStatus, m.status),
                            approval_id=m.approval_id,
                            proposal_id=m.proposal_id,
                            canceled_reason=m.canceled_reason,
                            created_at=m.created_at,
                            sent_at=m.sent_at,
                        )
                        for m in mine
                    ],
                )
            )
        outcome = (
            [OutcomeLine.model_validate(o) for o in rfq.negotiation_outcome]
            if rfq.negotiation_outcome is not None
            else None
        )
        return RfqNegotiation(
            rfq_id=rfq.id,
            status=cast(NegotiationStatus, rfq.negotiation_status),
            settings=_settings(rfq, lines),
            ready_at=rfq.negotiation_ready_at,
            closed_at=rfq.negotiation_closed_at,
            invites=invites,
            outcome=outcome,
        )


async def get_negotiation(
    db: Db,
    principal: Principal,
    rfq_id: UUID,
    *,
    now: datetime | None = None,
    history: PriceHistory = quoted_price_history,
) -> RfqNegotiation:
    return await _view(db, principal.tenant_id, rfq_id, now or datetime.now(UTC), history)


async def apply_settings(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    actor_user_id: UUID | None,
    rfq_id: UUID,
    changes: NegotiationSettingsUpdate,
) -> None:
    """Save the settings and open the negotiation, in the caller's transaction."""
    rfq = await session.scalar(select(Rfq).where(Rfq.id == rfq_id).with_for_update())
    if rfq is None:
        raise NotFound(f"rfq {rfq_id} not found")
    if rfq.negotiation_status == "closed":
        raise InvalidState(f"rfq {rfq_id}: the negotiation is closed")
    if rfq.status not in ("draft", "open"):
        raise InvalidState(f"rfq {rfq_id} is {rfq.status}")
    lines = {
        line.id: line
        for line in await session.scalars(
            select(RfqLine).where(RfqLine.rfq_id == rfq_id).with_for_update()
        )
    }
    for limits in changes.lines:
        line = lines.get(limits.rfq_line_id)
        if line is None:
            raise NotFound(f"line {limits.rfq_line_id} is not on rfq {rfq_id}")
        line.target_unit_price = limits.target_unit_price
        line.max_unit_price = limits.max_unit_price
        line.max_lead_time_days = limits.max_lead_time_days
    if changes.comparison_policy is not None:
        await _save_comparison_policy(session, rfq, actor_user_id, changes.comparison_policy)
    rfq.negotiation_deadline = changes.deadline
    rfq.negotiation_max_rounds = changes.max_rounds
    rfq.negotiation_early_close = changes.early_close
    rfq.negotiation_status = "open"
    rfq.negotiation_ready_at = None
    # New limits: the current offers are evaluated again on the next sweep
    # (suppliers with an email awaiting approval keep it; it is reconciled).
    await session.execute(
        update(Invite)
        .where(
            Invite.rfq_id == rfq_id,
            Invite.negotiation_status.not_in(_FINAL_INVITE),
        )
        .values(negotiation_evaluated_proposal_id=None)
    )
    session.add(
        AuditEvent(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="rfq.negotiation_settings",
            payload={"rfq_id": str(rfq_id), "lines": len(changes.lines)},
        )
    )
    await session.flush()


async def _save_comparison_policy(
    session: AsyncSession, rfq: Rfq, actor_user_id: UUID | None, policy: ComparisonPolicy
) -> None:
    if ComparisonPolicy.model_validate(rfq.comparison_policy or {}) == policy:
        return
    rfq.comparison_policy = policy.model_dump(mode="json")
    pending = list(
        await session.scalars(
            select(NegotiationMessage)
            .join(Invite, Invite.id == NegotiationMessage.invite_id)
            .where(Invite.rfq_id == rfq.id, NegotiationMessage.status == "proposed")
            .with_for_update(of=NegotiationMessage)
        )
    )
    if pending:
        await _cancel(session, pending, "comparison_policy_changed")
    await session.execute(
        update(Invite)
        .where(Invite.rfq_id == rfq.id, Invite.negotiation_status.not_in(_FINAL_INVITE))
        .values(negotiation_evaluated_proposal_id=None)
    )
    if rfq.negotiation_status == "ready_to_close":
        rfq.negotiation_status = "open"
        rfq.negotiation_ready_at = None
    session.add(
        AuditEvent(
            tenant_id=rfq.tenant_id,
            actor_user_id=actor_user_id,
            action="rfq.comparison_policy_updated",
            payload={"rfq_id": str(rfq.id), "canceled_messages": len(pending)},
        )
    )


async def set_comparison_policy(
    db: Db, principal: Principal, rfq_id: UUID, policy: ComparisonPolicy
) -> ComparisonPolicy:
    """Change review priorities without opening negotiation or changing its mandate.

    The frozen outcome of a closed negotiation retains the old policy and
    evidence. The live comparison applies the new policy for a fresh review.
    """
    async with tenant_session(db, principal.tenant_id) as session:
        rfq = await session.scalar(select(Rfq).where(Rfq.id == rfq_id).with_for_update())
        if rfq is None:
            raise NotFound(f"rfq {rfq_id} not found")
        await _save_comparison_policy(session, rfq, principal.user_id, policy)
        await session.flush()
    return policy


async def update_negotiation(
    db: Db,
    principal: Principal,
    rfq_id: UUID,
    changes: NegotiationSettingsUpdate,
    *,
    now: datetime | None = None,
    history: PriceHistory = quoted_price_history,
) -> RfqNegotiation:
    """Save the settings and open the negotiation. Current offers are re-evaluated.

    Refused while a negotiation mandate is live: its bounds are what a person
    approved; pause and replace the mandate to change them.
    """
    at = now or datetime.now(UTC)
    async with tenant_session(db, principal.tenant_id) as session:
        live = await session.scalar(
            select(NegotiationMandate.id).where(
                NegotiationMandate.rfq_id == rfq_id, NegotiationMandate.status != "ended"
            )
        )
        if live is not None:
            raise InvalidState(
                f"rfq {rfq_id}: a negotiation mandate is live; its limits cannot change"
            )
        await apply_settings(
            session,
            tenant_id=principal.tenant_id,
            actor_user_id=principal.user_id,
            rfq_id=rfq_id,
            changes=changes,
        )
    logger.info(
        "negotiation settings saved tenant=%s rfq=%s by=%s",
        principal.tenant_id,
        rfq_id,
        principal.user_id,
    )
    return await _view(db, principal.tenant_id, rfq_id, at, history)


# --- Closing ---------------------------------------------------------------------------


def _outcome(comparison: Comparison, targets: dict[UUID, Decimal | None]) -> list[OutcomeLine]:
    result: list[OutcomeLine] = []
    for rec in comparison.recommendations:
        target = targets.get(rec.rfq_line_id)
        price = rec.effective_unit_price_base
        result.append(
            OutcomeLine(
                rfq_line_id=rec.rfq_line_id,
                tier=rec.tier,
                quantity=rec.quantity,
                invite_id=rec.invite_id,
                supplier_name=rec.supplier_name,
                effective_unit_price_base=price,
                lead_time_days=rec.lead_time_days,
                within_target=(price <= target)
                if price is not None and target is not None
                else None,
                justification=rec.justification,
                readiness=rec.readiness,
                policy=comparison.policy,
                policy_reasons=rec.policy_reasons,
                source_evidence=rec.source_evidence,
                offer=rec.offer,
                evidence_gaps=rec.evidence_gaps,
                alternatives=rec.alternatives,
                excluded=rec.excluded,
                performance=rec.performance,
                proposal_id=rec.proposal_id,
                proposal_line_id=rec.proposal_line_id,
                evidence_message_ids=rec.evidence_message_ids,
                landed_total_base=rec.landed_total_base,
            )
        )
    return result


async def _cancel(session: AsyncSession, rows: Sequence[NegotiationMessage], reason: str) -> int:
    approval_ids = [r.approval_id for r in rows if r.approval_id is not None]
    for row in rows:
        row.status = "canceled"
        row.canceled_reason = reason
    if approval_ids:
        expired = list(
            await session.scalars(
                update(Approval)
                .where(Approval.id.in_(approval_ids), Approval.status == "pending")
                .values(status="expired")
                .returning(Approval)
            )
        )
        for approval in expired:
            session.add(approval_audit(approval, "approval.expired", None))
    await session.flush()
    return len(rows)


async def close_negotiation(
    db: Db,
    tenant_id: UUID,
    rfq_id: UUID,
    *,
    now: datetime,
    closed_by: UUID | None,
    reason: str,
) -> list[OutcomeLine]:
    """Close the negotiation; the best eligible offer per line and tier is the outcome.

    Idempotent: a closed negotiation returns the outcome stored when it closed.
    Nothing is awarded and nothing is sent.
    """
    async with tenant_session(db, tenant_id) as session:
        rfq = await session.get(Rfq, rfq_id)
        if rfq is None:
            raise NotFound(f"rfq {rfq_id} not found")
        if rfq.negotiation_status == "closed" and rfq.negotiation_outcome is not None:
            return [OutcomeLine.model_validate(o) for o in rfq.negotiation_outcome]
        created_by = rfq.created_by
    comparison = await load_comparison(
        db, Principal(tenant_id=tenant_id, user_id=created_by), rfq_id, now.date()
    )
    async with tenant_session(db, tenant_id) as session:
        rfq = await session.scalar(select(Rfq).where(Rfq.id == rfq_id).with_for_update())
        if rfq is None:
            raise NotFound(f"rfq {rfq_id} not found")
        if rfq.negotiation_status == "closed" and rfq.negotiation_outcome is not None:
            return [OutcomeLine.model_validate(o) for o in rfq.negotiation_outcome]
        targets = {
            line.id: line.target_unit_price
            for line in await session.scalars(select(RfqLine).where(RfqLine.rfq_id == rfq_id))
        }
        outcome = _outcome(comparison, targets)
        rfq.negotiation_status = "closed"
        rfq.negotiation_closed_at = now
        rfq.negotiation_outcome = [o.model_dump(mode="json") for o in outcome]
        await session.execute(
            update(Invite)
            .where(Invite.rfq_id == rfq_id, Invite.negotiation_status != "rejected")
            .values(negotiation_status="closed")
        )
        pending = list(
            await session.scalars(
                select(NegotiationMessage)
                .join(Invite, Invite.id == NegotiationMessage.invite_id)
                .where(Invite.rfq_id == rfq_id, NegotiationMessage.status == "proposed")
                .with_for_update(of=NegotiationMessage)
            )
        )
        if pending:
            await _cancel(session, pending, "negotiation_closed")
        # A closed negotiation ends its mandate: nothing more goes out on its behalf.
        await session.execute(
            update(NegotiationMandate)
            .where(NegotiationMandate.rfq_id == rfq_id, NegotiationMandate.status != "ended")
            .values(status="ended", ended_at=now)
        )
        session.add(
            AuditEvent(
                tenant_id=tenant_id,
                actor_user_id=closed_by,
                action="rfq.negotiation_closed",
                payload={"rfq_id": str(rfq_id), "reason": reason, "lines": len(outcome)},
            )
        )
        await session.flush()
    logger.info("negotiation closed tenant=%s rfq=%s reason=%s", tenant_id, rfq_id, reason)
    return outcome


async def close(
    db: Db, principal: Principal, rfq_id: UUID, *, now: datetime | None = None
) -> list[OutcomeLine]:
    """The buyer closes the negotiation (e.g. when it is ready to close). For awarding:
    the outcome is the best eligible offer per line and tier."""
    return await close_negotiation(
        db,
        principal.tenant_id,
        rfq_id,
        now=now or datetime.now(UTC),
        closed_by=principal.user_id,
        reason="buyer",
    )


# --- The sweep -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Due:
    invite_id: UUID
    rfq_id: UUID
    proposal_id: UUID
    round: int
    action: NegotiationActionName
    status: InviteNegotiationStatus
    sender_user_id: UUID
    to: str
    subject: str
    task: NegotiationEmailInput
    terms: dict[str, JsonValue]


async def _reconcile(session: AsyncSession) -> int:
    """Cancel proposed negotiation emails whose reason is gone."""
    latest = (
        select(Proposal.invite_id, func.max(Proposal.version).label("version"))
        .group_by(Proposal.invite_id)
        .subquery()
    )
    closed = list(
        await session.scalars(
            select(NegotiationMessage)
            .join(Invite, Invite.id == NegotiationMessage.invite_id)
            .join(Rfq, Rfq.id == Invite.rfq_id)
            .where(
                NegotiationMessage.status == "proposed",
                (Rfq.negotiation_status == "closed") | (Rfq.status != "open"),
            )
            .with_for_update(of=NegotiationMessage)
        )
    )
    newer = list(
        await session.scalars(
            select(NegotiationMessage)
            .join(Proposal, Proposal.id == NegotiationMessage.proposal_id)
            .join(latest, latest.c.invite_id == Proposal.invite_id)
            .where(
                NegotiationMessage.status == "proposed",
                Proposal.version < latest.c.version,
            )
            .with_for_update(of=NegotiationMessage)
        )
    )
    count = 0
    if closed:
        count += await _cancel(session, closed, "negotiation_closed")
    newer = [row for row in newer if row.status == "proposed"]
    if newer:
        count += await _cancel(session, newer, "newer_offer")
    return count


def _asks(
    lines: Sequence[_Line], decision: SupplierDecision, currency: str
) -> list[NegotiationLineAsk]:
    by_id = {line.row.id: line for line in lines}
    asks: list[NegotiationLineAsk] = []
    for d in decision.lines:
        if d.kind not in ("counter", "above_max"):
            continue
        if d.ask_price is None and d.ask_lead_days is None:
            continue
        line = by_id[d.rfq_line_id]
        label = line.row.description + (f" ({line.row.mpn})" if line.row.mpn else "")
        asks.append(
            NegotiationLineAsk(
                position=line.row.position,
                line=label,
                quantity=line.quantity,
                unit=line.row.unit,
                currency=currency,
                offered_unit_price=d.offered_price.normalize() if d.offered_price else None,
                target_unit_price=d.ask_price,
                offered_lead_time_days=d.lead_time_days,
                required_lead_time_days=d.ask_lead_days,
            )
        )
    return asks


def _terms(decision: SupplierDecision, currency: str) -> dict[str, JsonValue]:
    lines: list[JsonValue] = []
    for d in decision.lines:
        line: dict[str, JsonValue] = {
            "rfq_line_id": str(d.rfq_line_id),
            "decision": d.kind,
            "offered": str(d.offered_price) if d.offered_price is not None else None,
            "ask": str(d.ask_price) if d.ask_price is not None else None,
            "ask_lead_days": d.ask_lead_days,
            "offered_lead_days": d.lead_time_days,
        }
        lines.append(line)
    return {"currency": currency, "lines": lines}


async def _sender(session: AsyncSession, invite: Invite) -> UUID | None:
    """The buyer who requested the RFQ email (its executed approval): their mailbox."""
    original = await session.scalar(
        select(Approval).where(
            Approval.tool == SEND_RFQ_EMAIL,
            Approval.tool_call_id == send_proposal_ids(invite.id)[1],
            Approval.status == "executed",
        )
    )
    if original is None:
        return None
    try:
        return UUID(str(original.args.get("sender_user_id")))
    except ValueError:
        return None


async def _due(
    session: AsyncSession,
    rfq: Rfq,
    invite: Invite,
    supplier: Supplier,
    lines: Sequence[_Line],
    decision: SupplierDecision,
    proposal_id: UUID,
    next_round: int,
    correction_revision: int,
) -> _Due | None:
    if invite.thread_message_id is None:
        return None
    contact_email = await session.scalar(
        select(Contact.email).where(Contact.id == invite.contact_id)
    )
    sender_user_id = await _sender(session, invite)
    tenant = await session.get(Tenant, rfq.tenant_id)
    if contact_email is None or sender_user_id is None or tenant is None:
        logger.warning("negotiation: cannot address invite=%s", invite.id)
        return None
    user = await session.get(User, sender_user_id)
    if user is None:
        return None
    asks = _asks(lines, decision, rfq.base_currency)
    if not asks:
        return None
    first_subject = await session.scalar(
        select(Message.subject).where(
            Message.invite_id == invite.id,
            Message.direction == "out",
            Message.message_id_header == invite.thread_message_id,
        )
    )
    action = cast(NegotiationActionName, decision.action)
    round_number = next_round
    return _Due(
        invite_id=invite.id,
        rfq_id=rfq.id,
        proposal_id=proposal_id,
        round=round_number,
        action=action,
        status=decision.status,
        sender_user_id=sender_user_id,
        to=contact_email,
        subject=reply_subject(first_subject, rfq.ref, rfq.title),
        task=NegotiationEmailInput(
            action=action,
            rfq_ref=rfq.ref,
            rfq_title=rfq.title,
            buyer_company=tenant.name,
            buyer_name=await buyer_display_name(session, user, tenant.name),
            supplier_name=supplier.name,
            supplier_language=supplier.language,
            round=round_number,
            reply_by=rfq.negotiation_deadline.date() if rfq.negotiation_deadline else None,
            mention_competition=decision.mention_competition,
            lines=asks,
        ),
        terms={
            **_terms(decision, rfq.base_currency),
            "correction_revision": correction_revision,
            "comparison_policy_hash": args_hash(
                cast(
                    dict[str, JsonValue],
                    ComparisonPolicy.model_validate(rfq.comparison_policy or {}).model_dump(
                        mode="json"
                    ),
                )
            ),
        },
    )


async def evaluate_rfq(
    db: Db,
    tenant_id: UUID,
    rfq_id: UUID,
    *,
    writer: NegotiationWriter,
    now: datetime | None = None,
    history: PriceHistory = quoted_price_history,
    force: bool = False,
) -> EvaluationResult:
    """Evaluate every supplier's latest offer once; propose what is due; check early close.

    `force` re-evaluates offers already evaluated (the buyer asked for counter-
    offers now); suppliers with an email awaiting approval are left alone.
    """
    at = now or datetime.now(UTC)
    async with tenant_session(db, tenant_id) as session:
        rfq = await session.get(Rfq, rfq_id)
        if rfq is None or rfq.status != "open" or rfq.negotiation_status not in _ACTIVE:
            return EvaluationResult()
        deadline = rfq.negotiation_deadline
        created_by = rfq.created_by
    if deadline is not None and at >= deadline:
        await close_negotiation(db, tenant_id, rfq_id, now=at, closed_by=None, reason="deadline")
        return EvaluationResult(closed=True)

    comparison = await load_comparison(
        db, Principal(tenant_id=tenant_id, user_id=created_by), rfq_id, at.date()
    )
    offers = _offers(comparison)
    best = best_competing_prices(offers)
    columns = {c.invite_id: c for c in comparison.suppliers}

    due: list[_Due] = []
    became_ready = False
    async with tenant_session(db, tenant_id) as session:
        rfq = await session.scalar(select(Rfq).where(Rfq.id == rfq_id).with_for_update())
        if rfq is None or rfq.negotiation_status not in _ACTIVE:
            return EvaluationResult()
        if ComparisonPolicy.model_validate(rfq.comparison_policy or {}) != comparison.policy:
            # A concurrent policy edit invalidated the comparison before this lock.
            # Leave the offer unevaluated; the next sweep will load the new policy.
            return EvaluationResult(skipped=1)
        lines = await _lines(session, rfq, history)
        rows = (
            await session.execute(
                select(Invite, Supplier)
                .join(Supplier, Supplier.id == Invite.supplier_id)
                .where(Invite.rfq_id == rfq_id)
                .order_by(Invite.created_at, Invite.id)
                .with_for_update(of=Invite)
            )
        ).all()
        messages = list(
            await session.scalars(
                select(NegotiationMessage)
                .join(Invite, Invite.id == NegotiationMessage.invite_id)
                .where(Invite.rfq_id == rfq_id)
            )
        )
        decisions: list[SupplierDecision] = []
        waiting = 0
        for invite, supplier in rows:
            if invite.status == "sent":
                waiting += 1
                continue
            if invite.status == "declined":
                invite.negotiation_status = "rejected"
                continue
            column = columns.get(invite.id)
            if column is None:
                continue
            mine = [m for m in messages if m.invite_id == invite.id]
            _, _, pending = _rounds(mine)
            decision = _decide(
                lines,
                offers.get(invite.id, {}),
                best.get(invite.id, {}),
                mine,
                rfq,
                at,
                column.negotiation_holds,
            )
            decisions.append(decision)
            if pending or invite.negotiation_status in _FINAL_INVITE:
                continue
            if invite.negotiation_evaluated_proposal_id == column.proposal_id and not force:
                continue
            if decision.sends_email:
                built = await _due(
                    session,
                    rfq,
                    invite,
                    supplier,
                    lines,
                    decision,
                    column.proposal_id,
                    max((m.round for m in mine), default=0) + 1,
                    column.correction_revision,
                )
                if built is not None:
                    due.append(built)
                continue
            if decision.action != "none":
                invite.negotiation_status = decision.status
            invite.negotiation_evaluated_proposal_id = column.proposal_id

        if (
            rfq.negotiation_status == "open"
            and rfq.negotiation_early_close
            and ready_to_close(
                line_ids=[line.row.id for line in lines],
                waiting_suppliers=waiting,
                decisions=decisions,
            )
        ):
            rfq.negotiation_status = "ready_to_close"
            rfq.negotiation_ready_at = at
            became_ready = True
            session.add(
                AuditEvent(
                    tenant_id=tenant_id,
                    actor_user_id=None,
                    action="rfq.negotiation_ready",
                    payload={"rfq_id": str(rfq_id)},
                )
            )
        await session.flush()

    proposed: list[UUID] = []
    skipped = 0
    sink = PostgresApprovalSink(db)
    for item in due:
        context = ToolContext(tenant_id=tenant_id, user_id=item.sender_user_id, run_id=uuid4())
        try:
            draft = await writer(item.task, context)
        except AgentFailed:
            skipped += 1
            continue
        problems = negotiation_email_problems(item.task, draft)
        if problems:
            logger.warning(
                "negotiation draft refused tenant=%s invite=%s action=%s problems=%s",
                tenant_id,
                item.invite_id,
                item.action,
                ",".join(problems),
            )
            skipped += 1
            continue
        approval_id = await _propose(db, sink, tenant_id, item, draft.body.strip())
        if approval_id is not None:
            proposed.append(approval_id)
    if became_ready:
        logger.info("negotiation ready to close tenant=%s rfq=%s", tenant_id, rfq_id)
    return EvaluationResult(
        proposed=len(proposed),
        ready=became_ready,
        skipped=skipped,
        approval_ids=tuple(proposed),
    )


async def _propose(
    db: Db, sink: PostgresApprovalSink, tenant_id: UUID, item: _Due, body: str
) -> UUID | None:
    """Record the approval, then the row and the supplier's state; once per (invite, round)."""
    row_id = negotiation_message_id(item.invite_id, item.round)
    args = SendNegotiationEmailArgs(
        negotiation_message_id=row_id,
        invite_id=item.invite_id,
        action=item.action,
        round=item.round,
        sender_user_id=item.sender_user_id,
        to=[item.to],
        subject=item.subject,
        body=body,
    )
    payload = cast(dict[str, JsonValue], args.model_dump(mode="json"))
    run_id, call_id = negotiation_proposal_ids(row_id)
    try:
        await sink.request_approval(
            ApprovalProposal(
                tenant_id=tenant_id,
                run_id=run_id,
                session_id=f"rfq:{item.rfq_id}",
                tool_call_id=call_id,
                tool=SEND_NEGOTIATION_EMAIL,
                args=payload,
                args_hash=args_hash(payload),
                requested_by=item.sender_user_id,
            )
        )
    except ApprovalConflict:
        logger.info("negotiation email %s was already proposed", row_id)

    async with tenant_session(db, tenant_id) as session:
        approval_id = await session.scalar(
            select(Approval.id).where(
                Approval.run_id == str(run_id), Approval.tool_call_id == call_id
            )
        )
        if approval_id is None:
            return None
        rfq = await session.scalar(select(Rfq).where(Rfq.id == item.rfq_id).with_for_update())
        current_hash = (
            args_hash(
                cast(
                    dict[str, JsonValue],
                    ComparisonPolicy.model_validate(rfq.comparison_policy or {}).model_dump(
                        mode="json"
                    ),
                )
            )
            if rfq
            else None
        )
        source = await session.scalar(
            select(Proposal).where(Proposal.id == item.proposal_id).with_for_update()
        )
        latest_version = await session.scalar(
            select(func.max(Proposal.version)).where(
                Proposal.invite_id == item.invite_id,
            )
        )
        revision = await session.scalar(
            select(func.count(FieldCorrection.id)).where(
                FieldCorrection.proposal_id == item.proposal_id,
            )
        )
        if (
            rfq is None
            or rfq.status != "open"
            or rfq.negotiation_status not in _ACTIVE
            or source is None
            or source.version != latest_version
            or current_hash != item.terms.get("comparison_policy_hash")
            or revision != item.terms.get("correction_revision")
        ):
            expired = await session.scalar(
                update(Approval)
                .where(
                    Approval.id == approval_id,
                    Approval.status == "pending",
                )
                .values(status="expired")
                .returning(Approval)
            )
            if expired is not None:
                session.add(approval_audit(expired, "approval.expired", None))
            return None
        await session.execute(
            insert(NegotiationMessage)
            .values(
                id=row_id,
                tenant_id=tenant_id,
                invite_id=item.invite_id,
                proposal_id=item.proposal_id,
                round=item.round,
                action=item.action,
                status="proposed",
                approval_id=approval_id,
                terms=item.terms,
            )
            .on_conflict_do_nothing()
        )
        await session.execute(
            update(Invite)
            .where(Invite.id == item.invite_id)
            .values(
                negotiation_status=item.status,
                negotiation_evaluated_proposal_id=item.proposal_id,
            )
        )
    return approval_id


async def sweep_tenant(
    db: Db,
    tenant_id: UUID,
    *,
    writer: NegotiationWriter,
    now: datetime | None = None,
    history: PriceHistory = quoted_price_history,
) -> SweepResult:
    at = now or datetime.now(UTC)
    async with tenant_session(db, tenant_id) as session:
        canceled = await _reconcile(session)
        rfq_ids = list(
            await session.scalars(
                select(Rfq.id)
                .where(Rfq.status == "open", Rfq.negotiation_status.in_(_ACTIVE))
                .order_by(Rfq.created_at)
            )
        )
    result = SweepResult(canceled=canceled)
    for rfq_id in rfq_ids:
        evaluation = await evaluate_rfq(
            db, tenant_id, rfq_id, writer=writer, now=at, history=history
        )
        result.proposed += evaluation.proposed
        result.skipped += evaluation.skipped
        result.ready += int(evaluation.ready)
        result.closed += int(evaluation.closed)
        result.rfqs.append(rfq_id)
    if result.canceled or result.proposed or result.ready or result.closed or result.skipped:
        logger.info(
            "negotiation swept tenant=%s canceled=%d proposed=%d ready=%d closed=%d skipped=%d",
            tenant_id,
            result.canceled,
            result.proposed,
            result.ready,
            result.closed,
            result.skipped,
        )
    return result
