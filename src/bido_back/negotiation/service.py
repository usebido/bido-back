"""Supplier negotiation: settings, the sweep, early close, closing at the deadline.

A negotiation starts when the buyer saves its settings (`PUT
/v1/rfqs/{id}/negotiation`) or a mandate is approved: a deadline, a round cap,
and per-line limits (target, maximum, lead time, in the RFQ's base currency).
Until then, nothing is negotiated.

The `negotiation.sweep` job (every 15 minutes, see `bido_back.negotiation.jobs`)
does, per tenant, per RFQ whose negotiation is open:

1. Reconcile: a negotiation email decided or proposed whose reason is gone is
   canceled and its pending approval expired (the negotiation closed, or the
   supplier sent a newer offer).
2. Deadline: past the deadline, the negotiation closes (`close_negotiation`):
   the best eligible offer per line is kept as the outcome, pending emails are
   canceled, follow-ups stop. Nothing is awarded: that is the buyer's step.
3. Plan: one next action per supplier (`bido_back.negotiation.planner`, pure
   code), from its current offer, the clarification ledger, the emails decided
   or sent, the limits and the deadlines. An email is stored FIRST, as a
   `decided` row holding its typed decision (`bido_back.negotiation.decision`);
   accept, final and hold decisions are recorded on the invite, with an audit
   event when they change; the first best-and-final request sets the RFQ's
   common cut-off (and every supplier asked in that sweep gets it).
4. Write: each decided email is written by the negotiation writer from its
   stored decision, checked (`negotiation_email_problems`: the numbers are the
   decision's), and recorded as a PENDING approval of `send_negotiation_email`
   requested on behalf of the buyer who sent the RFQ email. A refused or failed
   draft is tried again from the same decision, `MAX_DRAFT_ATTEMPTS` times at
   most; then the email is `held` with the reason and the buyer decides. The
   same offer version never gets a second decision.
5. Early close: when every invited supplier answered and every line has an
   offer accepted internally, the RFQ becomes `ready_to_close`.

The view (`get_negotiation`) runs the same planner read-only, so what the buyer
sees per supplier is what the next sweep does.

Logs carry ids and counts only.
"""

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Final, cast
from uuid import UUID, uuid4

from pydantic import JsonValue, ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.agents.approvals import ApprovalProposal
from bido_back.agents.catalog.contracts import NegotiationEmailInput
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
    FollowupMessage,
    Invite,
    Message,
    NegotiationMandate,
    NegotiationMessage,
    Proposal,
    ProposalLine,
    Rfq,
    RfqLine,
    Supplier,
    Tenant,
    User,
)
from bido_back.followups.calendar import add_business_days
from bido_back.followups.missing import OfferedLine, open_items
from bido_back.followups.missing import RequestedLine as MissingLine
from bido_back.followups.service import reply_subject
from bido_back.mandates.schemas import ApproveMandateArgs
from bido_back.mandates.waits import MandateWait, approval_waits
from bido_back.negotiation.agents import NegotiationWriter
from bido_back.negotiation.decision import (
    DecisionLine,
    EmailDecision,
    build_decision,
    legacy_lines,
    writer_task,
)
from bido_back.negotiation.history import PriceHistory, quoted_price_history
from bido_back.negotiation.labels import line_reasons, phase_label, warning_text
from bido_back.negotiation.planner import (
    Context,
    EmailRecord,
    EmailState,
    EmailWait,
    LineAsk,
    LinePlan,
    LineQuote,
    LineTerms,
    Offer,
    SupplierFacts,
    SupplierPlan,
    plan_supplier,
    ready_to_close,
)
from bido_back.negotiation.schemas import (
    EmailStateName,
    InviteNegotiation,
    LineDecisionName,
    LineDecisionView,
    LineLimitsView,
    LinePlanView,
    MessageAskView,
    NegotiationActionName,
    NegotiationDecisionView,
    NegotiationMessageStatus,
    NegotiationMessageView,
    NegotiationSettings,
    NegotiationSettingsUpdate,
    NegotiationStatus,
    NegotiationWarningView,
    OutcomeLine,
    RfqNegotiation,
    SupplierStateView,
)
from bido_back.negotiation.send import (
    SEND_NEGOTIATION_EMAIL,
    SendNegotiationEmailArgs,
    negotiation_message_id,
    negotiation_proposal_ids,
)
from bido_back.negotiation.signals import (
    NO_CLARIFICATION,
    ClarificationState,
    ClarificationWait,
    ExpectedDelivery,
    QuotedLeadTime,
    calendar_expected_delivery,
    latest_order_date,
    offer_signals,
)
from bido_back.negotiation.strategy import ask_places
from bido_back.rfqs.errors import AgentFailed, InvalidState, NotFound
from bido_back.rfqs.language import supplier_email_language
from bido_back.rfqs.send import SEND_RFQ_EMAIL, send_proposal_ids
from bido_back.rfqs.service import buyer_display_name

logger = logging.getLogger(__name__)

Db = AsyncEngine | async_sessionmaker[AsyncSession]

_ACTIVE: Final = ("open", "ready_to_close")
_FINAL_INVITE: Final = ("rejected", "closed")
# Emails decided and not sent yet: what reconciliation and closing cancel.
_LIVE: Final = ("decided", "proposed")
_EMAIL_STATES: Final = frozenset({"decided", "proposed", "sent", "canceled", "rejected", "held"})
# A decided email whose drafts keep being refused or failing waits for the buyer after this.
MAX_DRAFT_ATTEMPTS: Final = 3
# Comparison flags the planner judges itself (the buyer's limits and delivery dates).
_PLANNED: Final = frozenset(
    {
        "no_quote_for_line",
        "above_buyer_maximum",
        "buyer_lead_time_unconfirmed",
        "above_buyer_lead_time_limit",
        "delivery_after_required_date",
        "delivery_date_unconfirmed",
    }
)
# Until the clarification ledger gives one: a clarification's nominal reply-by.
_CLARIFICATION_REPLY_DAYS: Final = 2
_DAYS: Final = re.compile(r"\d{1,3}")
# Planner decisions written to the audit log when they change (by action, then phase).
_EVENTS: Final[Mapping[str, str]] = {
    "accept": "rfq.negotiation_accepted",
    "close": "rfq.negotiation_final",
    "needs_decision": "rfq.negotiation_needs_decision",
    "needs_answer": "rfq.negotiation_needs_answer",
}


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    proposed: int = 0
    ready: bool = False
    closed: bool = False
    skipped: int = 0
    approval_ids: tuple[UUID, ...] = ()
    held: int = 0


@dataclass(slots=True)
class SweepResult:
    canceled: int = 0
    proposed: int = 0
    ready: int = 0
    closed: int = 0
    skipped: int = 0
    held: int = 0
    rfqs: list[UUID] = field(default_factory=list[UUID])


# --- Loading what the planner needs ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Line:
    row: RfqLine
    terms: LineTerms
    facts: DecisionLine


@dataclass(slots=True)
class _Supplier:
    invite: Invite
    supplier: Supplier
    proposal: Proposal | None = None
    proposal_lines: list[ProposalLine] = field(default_factory=list[ProposalLine])
    received_on: date | None = None
    rows: list[NegotiationMessage] = field(default_factory=list[NegotiationMessage])
    followups: list[FollowupMessage] = field(default_factory=list[FollowupMessage])


@dataclass(slots=True)
class _Loaded:
    rfq: Rfq
    lines: list[_Line]
    suppliers: list[_Supplier]
    versions: dict[UUID, int]
    approvals: dict[UUID, Approval]
    waits: dict[UUID, MandateWait]


async def _derived_lead_lines(session: AsyncSession, rfq: Rfq) -> set[UUID]:
    """Lines whose lead-time limit a mandate derived from the need-by date.

    Their day count was frozen when the mandate was proposed; the planner
    compares the need-by date itself instead, at every evaluation.
    """
    mandate = await session.scalar(
        select(NegotiationMandate)
        .where(NegotiationMandate.rfq_id == rfq.id)
        .order_by(NegotiationMandate.created_at.desc())
        .limit(1)
    )
    if mandate is None:
        return set()
    try:
        terms = ApproveMandateArgs.model_validate(mandate.terms)
    except ValidationError:
        return set()
    return {line.rfq_line_id for line in terms.lines if line.lead_time_need_by is not None}


async def _lines(session: AsyncSession, rfq: Rfq, history: PriceHistory) -> list[_Line]:
    derived = await _derived_lead_lines(session, rfq)
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
                terms=LineTerms(
                    rfq_line_id=row.id,
                    position=row.position,
                    quantity=quantities[0],
                    need_by=row.need_by,
                    target=row.target_unit_price,
                    max_price=row.max_unit_price,
                    max_lead_days=None if row.id in derived else row.max_lead_time_days,
                    history_price=history_price,
                ),
                facts=DecisionLine(
                    position=row.position,
                    description=row.description,
                    part_number=row.part_number,
                    mpn=row.mpn,
                    quantity=quantities[0],
                    unit=row.unit,
                    need_by=row.need_by,
                ),
            )
        )
    return lines


async def _load(
    session: AsyncSession, rfq: Rfq, history: PriceHistory, now: datetime, *, lock: bool
) -> _Loaded:
    lines = await _lines(session, rfq, history)
    query = (
        select(Invite, Supplier)
        .join(Supplier, Supplier.id == Invite.supplier_id)
        .where(Invite.rfq_id == rfq.id)
        .order_by(Invite.created_at, Invite.id)
    )
    if lock:
        query = query.with_for_update(of=Invite)
    suppliers = [_Supplier(invite=i, supplier=s) for i, s in (await session.execute(query)).all()]
    by_invite = {s.invite.id: s for s in suppliers}
    versions: dict[UUID, int] = {}
    if by_invite:
        proposals = list(
            await session.scalars(select(Proposal).where(Proposal.invite_id.in_(list(by_invite))))
        )
        versions = {p.id: p.version for p in proposals}
        for proposal in proposals:
            item = by_invite[proposal.invite_id]
            if item.proposal is None or proposal.version > item.proposal.version:
                item.proposal = proposal
        latest = {s.proposal.id: s for s in suppliers if s.proposal is not None}
        if latest:
            for line in await session.scalars(
                select(ProposalLine)
                .where(ProposalLine.proposal_id.in_(list(latest)))
                .order_by(ProposalLine.created_at, ProposalLine.id)
            ):
                latest[line.proposal_id].proposal_lines.append(line)
            for proposal_id, occurred_at in (
                await session.execute(
                    select(Proposal.id, Message.occurred_at)
                    .join(Message, Message.id == Proposal.message_id)
                    .where(Proposal.id.in_(list(latest)))
                )
            ).all():
                latest[proposal_id].received_on = occurred_at.date()
        rows_query = (
            select(NegotiationMessage)
            .where(NegotiationMessage.invite_id.in_(list(by_invite)))
            .order_by(NegotiationMessage.sequence)
        )
        if lock:
            rows_query = rows_query.with_for_update()
        for row in await session.scalars(rows_query):
            by_invite[row.invite_id].rows.append(row)
        for followup in await session.scalars(
            select(FollowupMessage)
            .where(FollowupMessage.invite_id.in_(list(by_invite)))
            .order_by(FollowupMessage.created_at, FollowupMessage.sequence)
        ):
            by_invite[followup.invite_id].followups.append(followup)
    approval_ids = [
        a
        for s in suppliers
        for a in (
            *(r.approval_id for r in s.rows if r.status in _LIVE),
            *(f.approval_id for f in s.followups if f.status == "proposed"),
        )
        if a is not None
    ]
    approvals: dict[UUID, Approval] = {}
    if approval_ids:
        approvals = {
            a.id: a
            for a in await session.scalars(select(Approval).where(Approval.id.in_(approval_ids)))
        }
    waiting_rfq = [s.invite.id for s in suppliers if s.invite.status == "pending_approval"]
    if waiting_rfq:
        for approval in await session.scalars(
            select(Approval).where(
                Approval.tool == SEND_RFQ_EMAIL,
                Approval.tool_call_id.in_([send_proposal_ids(i)[1] for i in waiting_rfq]),
                Approval.status.in_(("pending", "approved")),
            )
        ):
            approvals[approval.id] = approval
    waits = await approval_waits(session, rfq.id, list(approvals.values()), now)
    return _Loaded(rfq, lines, suppliers, versions, approvals, waits)


def _email_wait(approval: Approval | None, waits: Mapping[UUID, MandateWait]) -> EmailWait:
    """What a proposed email waits for: a person, the mandate's checks, its cap, a mailbox."""
    if approval is None or approval.status in ("approved", "executed"):
        return EmailWait("preparing")
    wait = waits.get(approval.id)
    if wait is None:
        return EmailWait("approval")
    if wait.reason == "held":
        return EmailWait("mandate_hold", detail=wait.detail)
    if wait.reason == "paused":
        return EmailWait("mandate_paused")
    if wait.reason == "mailbox":
        return EmailWait("mailbox")
    if wait.reason == "daily_cap":
        return EmailWait("daily_cap", until=wait.until)
    return EmailWait("preparing")


def _clarification_wait(
    approval: Approval | None, waits: Mapping[UUID, MandateWait]
) -> tuple[ClarificationWait | None, datetime | None]:
    wait = _email_wait(approval, waits)
    if wait.reason == "preparing":
        return None, None
    reasons: dict[str, ClarificationWait] = {
        "approval": "approval",
        "mandate_hold": "mandate_hold",
        "mandate_paused": "mandate_paused",
        "daily_cap": "daily_cap",
        "mailbox": "mailbox",
    }
    return reasons.get(wait.reason), wait.until


def stored_decision(row: NegotiationMessage) -> EmailDecision | None:
    """The typed decision a negotiation email was written from (None before 0034)."""
    raw = cast(dict[str, JsonValue], row.terms).get("decision")
    if not isinstance(raw, dict):
        return None
    try:
        return EmailDecision.model_validate(raw)
    except ValidationError:
        return None


def _parse_decimal(value: object) -> Decimal | None:
    if not isinstance(value, str):
        return None
    try:
        number = Decimal(value)
    except ArithmeticError:
        return None
    return number if number.is_finite() else None


def _legacy_asks(row: NegotiationMessage) -> tuple[LineAsk, ...]:
    """What an email written before typed decisions asked (its flat terms)."""
    asks: list[LineAsk] = []
    entries = cast(dict[str, object], row.terms).get("lines")
    for entry in cast(list[object], entries) if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        values = cast(dict[str, object], entry)
        try:
            line_id = UUID(str(values.get("rfq_line_id")))
        except ValueError:
            continue
        ask = _parse_decimal(values.get("ask"))
        lead = values.get("ask_lead_days")
        deliver_by = (
            row.created_at.date() + timedelta(days=lead)
            if isinstance(lead, int) and not isinstance(lead, bool)
            else None
        )
        if ask is None and deliver_by is None:
            continue
        asks.append(
            LineAsk(
                rfq_line_id=line_id,
                ask_price=ask,
                deliver_by=deliver_by,
                offered_price=_parse_decimal(values.get("offered")),
            )
        )
    return tuple(asks)


def _state(row: NegotiationMessage) -> EmailState:
    return cast(EmailState, row.status) if row.status in _EMAIL_STATES else "canceled"


def _record(row: NegotiationMessage, loaded: _Loaded) -> EmailRecord:
    decision = stored_decision(row)
    if decision is not None:
        asks = tuple(
            LineAsk(
                rfq_line_id=p.rfq_line_id,
                ask_price=p.target_unit_price,
                best_price=p.best_price,
                deliver_by=p.deliver_by,
                offered_price=p.offered_unit_price,
                offered_delivery=p.offered_delivery,
            )
            for p in decision.positions
        )
        kind = decision.kind
        reply_by, cutoff = decision.reply_by, decision.cutoff_at
    else:
        asks = _legacy_asks(row)
        kind = "bafo" if row.action == "bafo" else "revised_offer"
        reply_by, cutoff = None, None
    approval = loaded.approvals.get(row.approval_id) if row.approval_id is not None else None
    return EmailRecord(
        sequence=row.sequence,
        round=row.round,
        kind=kind,
        state=_state(row),
        proposal_id=row.proposal_id,
        proposal_version=loaded.versions.get(row.proposal_id, 0),
        asks=asks,
        reply_by=reply_by,
        cutoff_at=cutoff,
        held_reason=row.held_reason,
        wait=_email_wait(approval, loaded.waits) if row.status == "proposed" else None,
    )


def _item_code(position: int | None, item_field: str) -> str:
    return item_field if position is None else f"pos{position}.{item_field}"


def _clarification(item: _Supplier, loaded: _Loaded, now: datetime) -> ClarificationState:
    """The default clarification ledger: today's follow-up sweep, mirrored.

    The follow-up sweep proposes one clarification per offer version with open
    items (`followups.missing.open_items`). So, for the current offer: items
    and no clarification yet -> to ask (the follow-up sweep writes it); one
    proposed -> pending; sent -> awaiting the answer; canceled or rejected ->
    the items are the buyer's (never asked again for this version). N2b's
    ledger replaces this with the same `ClarificationState`.
    """
    proposal = item.proposal
    if proposal is None or item.invite.status != "responded":
        return NO_CLARIFICATION
    review = proposal.fields.get("_review")
    if isinstance(review, dict) and cast(dict[str, object], review).get("declined") is True:
        return NO_CLARIFICATION
    by_position = {line.row.position: line.row.id for line in loaded.lines}
    items = open_items(
        header_values={"currency": proposal.currency, "valid_until": proposal.valid_until},
        header_fields=proposal.fields,
        requested=[
            MissingLine(
                id=line.row.id,
                position=line.row.position,
                description=line.row.description,
                mpn=line.row.mpn,
            )
            for line in loaded.lines
        ],
        offered=[
            OfferedLine(
                rfq_line_id=line.rfq_line_id,
                values={
                    "unit_price": line.unit_price,
                    "min_quantity": line.min_quantity,
                    "lead_time_days": line.lead_time_days,
                },
                fields=line.fields,
            )
            for line in item.proposal_lines
        ],
    )
    if not items:
        return NO_CLARIFICATION
    codes = tuple(_item_code(i.position, i.field) for i in items)
    concerned = frozenset(
        by_position[i.position]
        for i in items
        if i.position is not None and i.position in by_position
    )
    current = [
        f for f in item.followups if f.kind == "clarification" and f.proposal_id == proposal.id
    ]
    if not current:
        return ClarificationState(to_ask=codes, lines=concerned)
    latest = current[-1]
    if latest.status == "proposed":
        approval = loaded.approvals.get(latest.approval_id) if latest.approval_id else None
        wait, until = _clarification_wait(approval, loaded.waits)
        return ClarificationState(
            pending=True, pending_wait=wait, pending_until=until, lines=concerned
        )
    if latest.status == "sent":
        sent_at = latest.sent_at or now
        country = item.supplier.country
        return ClarificationState(
            awaiting=True,
            awaiting_until=add_business_days(sent_at, _CLARIFICATION_REPLY_DAYS, country),
            lines=concerned,
        )
    return ClarificationState(exhausted=codes, exhausted_lines=concerned)


def _payment_days(text: str | None) -> int | None:
    if not text:
        return None
    found = _DAYS.search(text)
    return int(found.group(0)) if found is not None else None


def _quotes(
    comparison: Comparison,
    loaded: _Loaded,
    order_date: date,
    expected_delivery: ExpectedDelivery,
) -> dict[UUID, dict[UUID, LineQuote]]:
    """Per invite, per line: the current offer (first tier, base currency) as the planner
    reads it, with the delivery dates the lead-time helper computes."""
    rfq = loaded.rfq
    need_by = {line.row.id: line.row.need_by for line in loaded.lines}
    by_invite = {s.invite.id: s for s in loaded.suppliers}
    quotes: dict[UUID, dict[UUID, LineQuote]] = {}
    for column in comparison.suppliers:
        item = by_invite.get(column.invite_id)
        if item is None or item.proposal is None:
            continue
        line_fields = {line.id: line.fields for line in item.proposal_lines}
        holds = set(column.negotiation_holds)
        mine: dict[UUID, LineQuote] = {}
        for cell in column.cells:
            if cell.tier != 0:
                continue
            same_basis = (cell.currency or "").upper() == rfq.base_currency.upper() and not (
                cell.tooling_cost
            )
            lead = QuotedLeadTime(
                days=cell.lead_time_days,
                fields=line_fields.get(cell.proposal_line_id, {})
                if cell.proposal_line_id is not None
                else {},
                offer_fields=item.proposal.fields,
                country=item.supplier.country,
            )
            delivery = expected_delivery(lead, order_date)
            at_reply_by = (
                expected_delivery(lead, rfq.response_due)
                if rfq.response_due is not None and rfq.response_due > order_date
                else None
            )
            needed = need_by.get(cell.rfq_line_id)
            mine[cell.rfq_line_id] = LineQuote(
                rfq_line_id=cell.rfq_line_id,
                unit_price=cell.effective_unit_price_base,
                places=ask_places(rfq.base_currency, cell.unit_price if same_basis else None),
                lead_time_days=cell.lead_time_days,
                delivery=delivery[0] if delivery is not None else None,
                delivery_exact=delivery[1] if delivery is not None else True,
                delivery_at_reply_by=at_reply_by[0] if at_reply_by is not None else None,
                latest_order_date=latest_order_date(expected_delivery, lead, needed, order_date)
                if needed is not None
                else None,
                moq=cell.moq,
                blockers=tuple(
                    b
                    for b in policy_blockers(cell, column, comparison.policy)
                    if b not in _PLANNED and b not in holds
                ),
            )
        quotes[column.invite_id] = mine
    # A lower eligible offer elsewhere may be mentioned (never whose, never how much).
    for invite_id, mine in quotes.items():
        for line_id, quote in list(mine.items()):
            if quote.unit_price is None or quote.blockers:
                continue
            others = [
                q.unit_price
                for other, theirs in quotes.items()
                if other != invite_id
                for q in (theirs.get(line_id),)
                if q is not None and q.unit_price is not None and not q.blockers
            ]
            if others and min(others) < quote.unit_price:
                mine[line_id] = replace(quote, better_offer_exists=True)
    return quotes


def _offer(
    item: _Supplier, quotes: Mapping[UUID, LineQuote], loaded: _Loaded, holds: Sequence[str]
) -> Offer | None:
    proposal = item.proposal
    if proposal is None:
        return None
    signals = offer_signals(proposal.fields)
    positions = {line.row.position: line.row.id for line in loaded.lines}
    offered, requested = (
        _payment_days(proposal.payment_terms),
        _payment_days(loaded.rfq.payment_terms),
    )
    return Offer(
        proposal_id=proposal.id,
        version=proposal.version,
        lines=quotes,
        valid_until=proposal.valid_until,
        received_on=item.received_on,
        holds=tuple(holds),
        questions=signals.questions,
        declared_final=signals.final_offer,
        declined_lines=frozenset(
            positions[p] for p in signals.declined_positions if p in positions
        ),
        payment_terms_shorter=offered is not None and requested is not None and offered < requested,
    )


def _context(rfq: Rfq, item: _Supplier, now: datetime, cutoff: datetime | None) -> Context:
    negotiation = rfq.negotiation_status
    if rfq.status in ("closed", "cancelled"):
        negotiation = "closed"
    elif rfq.status != "open" and negotiation != "closed":
        negotiation = "not_started"
    return Context(
        now=now,
        order_date=now.date(),
        deadline=rfq.negotiation_deadline,
        max_rounds=rfq.negotiation_max_rounds,
        cutoff_at=cutoff,
        response_due=rfq.response_due,
        negotiation=cast(NegotiationStatus, negotiation),
        country=item.supplier.country,
    )


@dataclass(slots=True)
class _Planned:
    loaded: _Loaded
    plans: dict[UUID, SupplierPlan]
    cutoff: datetime | None
    # Per invite, the offer (proposal) the comparison read; the quotes it gave.
    columns: dict[UUID, UUID]
    quotes: dict[UUID, dict[UUID, LineQuote]]

    def current(self, item: "_Supplier") -> bool:
        """Whether the plan read this supplier's latest offer (none arrived in between)."""
        if item.proposal is None:
            return item.invite.id not in self.columns
        return self.columns.get(item.invite.id) == item.proposal.id


def _plan(
    loaded: _Loaded,
    comparison: Comparison,
    now: datetime,
    expected_delivery: ExpectedDelivery,
) -> _Planned:
    """Every supplier's next action. The first best-and-final request sets the RFQ's
    common cut-off; every supplier is then planned again with it."""
    rfq = loaded.rfq
    quotes = _quotes(comparison, loaded, now.date(), expected_delivery)
    holds = {c.invite_id: c.negotiation_holds for c in comparison.suppliers}
    terms = [line.terms for line in loaded.lines]
    facts: dict[UUID, SupplierFacts] = {}
    for item in loaded.suppliers:
        offer = _offer(item, quotes.get(item.invite.id, {}), loaded, holds.get(item.invite.id, []))
        rfq_wait = None
        if item.invite.status == "pending_approval":
            original = next(
                (
                    a
                    for a in loaded.approvals.values()
                    if a.tool == SEND_RFQ_EMAIL
                    and a.tool_call_id == send_proposal_ids(item.invite.id)[1]
                ),
                None,
            )
            rfq_wait = _email_wait(original, loaded.waits)
        chases = [f for f in item.followups if f.kind == "chase"]
        facts[item.invite.id] = SupplierFacts(
            invite_status=item.invite.status,
            offer=offer,
            emails=tuple(_record(row, loaded) for row in item.rows),
            clarification=_clarification(item, loaded, now),
            rfq_email_wait=rfq_wait,
            chases_sent=sum(1 for c in chases if c.status == "sent"),
            chases_max=rfq.followup_max_chases if rfq.followups_enabled else 0,
            chases_waiting=any(c.status == "proposed" for c in chases),
        )
    cutoff = rfq.negotiation_cutoff_at

    def plan_all(at: datetime | None) -> dict[UUID, SupplierPlan]:
        return {
            item.invite.id: plan_supplier(
                terms, facts[item.invite.id], _context(rfq, item, now, at)
            )
            for item in loaded.suppliers
        }

    plans = plan_all(cutoff)
    if cutoff is None:
        firsts = [
            p.email.cutoff_at
            for p in plans.values()
            if p.email is not None and p.email.sets_cutoff and p.email.cutoff_at is not None
        ]
        if firsts:
            cutoff = min(firsts)
            plans = plan_all(cutoff)
    columns = {c.invite_id: c.proposal_id for c in comparison.suppliers}
    return _Planned(loaded, plans, cutoff, columns, quotes)


# --- The view (API) ----------------------------------------------------------------------


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


def _days_to(deliver_by: date | None, order_date: date) -> int | None:
    return (deliver_by - order_date).days if deliver_by is not None else None


def _line_decision(plan: LinePlan) -> LineDecisionName:
    if plan.status in ("not_quoted", "declined"):
        return "no_offer"
    if plan.status != "priced":
        return "blocked"
    if plan.acceptable:
        return "accept"
    if plan.price_status == "above_max":
        return "above_max"
    if plan.asks:
        return "counter"
    return "blocked"


def _line_views(
    plan: SupplierPlan, quotes: Mapping[UUID, LineQuote], today: date
) -> list[LineDecisionView]:
    views: list[LineDecisionView] = []
    for line in plan.lines:
        quote = quotes.get(line.rfq_line_id)
        views.append(
            LineDecisionView(
                rfq_line_id=line.rfq_line_id,
                decision=_line_decision(line),
                offered_unit_price_base=line.offered_price,
                ask_unit_price=line.price.ask if line.price is not None else None,
                lead_time_days=quote.lead_time_days if quote is not None else None,
                ask_lead_time_days=_days_to(
                    line.delivery.deliver_by if line.delivery is not None else None, today
                ),
            )
        )
    return views


def _line_plan_views(plan: SupplierPlan) -> list[LinePlanView]:
    return [
        LinePlanView(
            rfq_line_id=line.rfq_line_id,
            position=line.position,
            status=line.status,
            price=line.price_status,
            delivery=line.delivery_status,
            offered_unit_price_base=line.offered_price,
            ask_unit_price=line.price.ask if line.price is not None else None,
            asks_best_price=line.price.best_price if line.price is not None else False,
            deliver_by=line.delivery.deliver_by if line.delivery is not None else None,
            required_by=line.delivery.required_by if line.delivery is not None else None,
            expected_delivery=line.delivery.expected if line.delivery is not None else None,
            latest_order_date=line.latest_order_date,
            acceptable=line.acceptable,
            reasons=line_reasons(line),
        )
        for line in plan.lines
    ]


def state_view(plan: SupplierPlan, *, chases_sent: int = 0) -> SupplierStateView:
    """What the buyer reads about one supplier (API, Copilot)."""
    return SupplierStateView(
        phase=plan.phase,
        label=phase_label(plan, chases_sent=chases_sent),
        reason=plan.reason,
        until=plan.until,
        next_action=plan.action,
        order_by=plan.order_by,
        valid_until=plan.valid_until if plan.phase == "accepted_internally" else None,
        warnings=[warning_text(w) for w in plan.warnings],
    )


def _last_decision(invite: Invite) -> NegotiationDecisionView | None:
    raw = invite.negotiation_decision
    if not isinstance(raw, dict):
        return None
    try:
        return NegotiationDecisionView.model_validate(raw)
    except ValidationError:
        return None


def _message_views(
    rows: Sequence[NegotiationMessage], lines: Sequence[_Line]
) -> list[NegotiationMessageView]:
    positions = {line.row.id: line.row.position for line in lines}
    views: list[NegotiationMessageView] = []
    for row in rows:
        decision = stored_decision(row)
        state: EmailStateName = _state(row)
        status: NegotiationMessageStatus = "proposed" if state in ("decided", "held") else state
        asks = (
            [
                MessageAskView(
                    position=p.position,
                    ask_unit_price=p.target_unit_price,
                    best_price=p.best_price,
                    deliver_by=p.deliver_by,
                )
                for p in decision.positions
            ]
            if decision is not None
            else [
                MessageAskView(
                    position=positions.get(a.rfq_line_id, 0),
                    ask_unit_price=a.ask_price,
                    deliver_by=a.deliver_by,
                )
                for a in _legacy_asks(row)
            ]
        )
        views.append(
            NegotiationMessageView(
                id=row.id,
                round=row.round,
                action=cast(NegotiationActionName, row.action),
                status=status,
                approval_id=row.approval_id,
                proposal_id=row.proposal_id,
                canceled_reason=row.canceled_reason,
                created_at=row.created_at,
                sent_at=row.sent_at,
                sequence=row.sequence,
                kind=decision.kind
                if decision is not None
                else ("bafo" if row.action == "bafo" else "revised_offer"),
                state=state,
                reply_by=decision.reply_by if decision is not None else None,
                cutoff_at=decision.cutoff_at if decision is not None else None,
                attempts=row.attempts,
                held_reason=row.held_reason,
                asks=asks,
            )
        )
    return views


def _rfq_warnings(planned: _Planned) -> list[NegotiationWarningView]:
    names = {s.invite.id: s.supplier.name for s in planned.loaded.suppliers}
    found: list[NegotiationWarningView] = []
    for invite_id, plan in planned.plans.items():
        for warning in plan.warnings:
            if warning.code in ("line_not_quoted", "validity_unknown"):
                continue
            found.append(
                NegotiationWarningView(
                    code=warning.code,
                    supplier_name=names.get(invite_id),
                    position=warning.position,
                    on=warning.on,
                    text=warning_text(warning),
                )
            )
    return found


async def _planned(
    db: Db,
    tenant_id: UUID,
    rfq_id: UUID,
    now: datetime,
    history: PriceHistory,
    expected_delivery: ExpectedDelivery,
) -> tuple[_Planned, Comparison]:
    async with tenant_session(db, tenant_id) as session:
        rfq = await session.get(Rfq, rfq_id)
        if rfq is None:
            raise NotFound(f"rfq {rfq_id} not found")
        created_by = rfq.created_by
    comparison = await load_comparison(
        db, Principal(tenant_id=tenant_id, user_id=created_by), rfq_id, now.date()
    )
    async with tenant_session(db, tenant_id) as session:
        rfq = await session.get(Rfq, rfq_id)
        if rfq is None:
            raise NotFound(f"rfq {rfq_id} not found")
        loaded = await _load(session, rfq, history, now, lock=False)
    return _plan(loaded, comparison, now, expected_delivery), comparison


async def _view(
    db: Db,
    tenant_id: UUID,
    rfq_id: UUID,
    now: datetime,
    history: PriceHistory,
    expected_delivery: ExpectedDelivery = calendar_expected_delivery,
) -> RfqNegotiation:
    planned, comparison = await _planned(db, tenant_id, rfq_id, now, history, expected_delivery)
    loaded = planned.loaded
    rfq = loaded.rfq
    columns = {c.invite_id: c for c in comparison.suppliers}
    quotes = planned.quotes
    invites: list[InviteNegotiation] = []
    for item in loaded.suppliers:
        plan = planned.plans[item.invite.id]
        column = columns.get(item.invite.id)
        sent = [r for r in item.rows if r.status == "sent"]
        chases_sent = sum(1 for f in item.followups if f.kind == "chase" and f.status == "sent")
        invites.append(
            InviteNegotiation(
                invite_id=item.invite.id,
                supplier_name=item.supplier.name,
                invite_status=item.invite.status,
                status=plan.status,
                rounds_used=len(sent),
                latest_proposal_id=column.proposal_id if column else None,
                latest_proposal_version=column.proposal_version if column else None,
                lines=_line_views(plan, quotes.get(item.invite.id, {}), now.date())
                if column is not None
                else [],
                strategy_reasons=[plan.reason] if plan.reason else [],
                stalled_rounds=1 if plan.reason == "no_progress" else 0,
                messages=_message_views(item.rows, loaded.lines),
                state=state_view(plan, chases_sent=chases_sent),
                line_plans=_line_plan_views(plan),
                last_decision=_last_decision(item.invite),
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
        settings=_settings(rfq, loaded.lines),
        ready_at=rfq.negotiation_ready_at,
        closed_at=rfq.negotiation_closed_at,
        invites=invites,
        outcome=outcome,
        cutoff_at=rfq.negotiation_cutoff_at,
        warnings=_rfq_warnings(planned),
    )


async def get_negotiation(
    db: Db,
    principal: Principal,
    rfq_id: UUID,
    *,
    now: datetime | None = None,
    history: PriceHistory = quoted_price_history,
    expected_delivery: ExpectedDelivery = calendar_expected_delivery,
) -> RfqNegotiation:
    return await _view(
        db, principal.tenant_id, rfq_id, now or datetime.now(UTC), history, expected_delivery
    )


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
            .where(Invite.rfq_id == rfq.id, NegotiationMessage.status.in_(_LIVE))
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
                .where(Invite.rfq_id == rfq_id, NegotiationMessage.status.in_(_LIVE))
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


async def _reconcile(session: AsyncSession) -> int:
    """Cancel negotiation emails decided or proposed whose reason is gone."""
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
                NegotiationMessage.status.in_(_LIVE),
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
                NegotiationMessage.status.in_(_LIVE),
                Proposal.version < latest.c.version,
            )
            .with_for_update(of=NegotiationMessage)
        )
    )
    count = 0
    if closed:
        count += await _cancel(session, closed, "negotiation_closed")
    newer = [row for row in newer if row.status in _LIVE]
    if newer:
        count += await _cancel(session, newer, "newer_offer")
    return count


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


def _policy_hash(rfq: Rfq) -> str:
    return args_hash(
        cast(
            dict[str, JsonValue],
            ComparisonPolicy.model_validate(rfq.comparison_policy or {}).model_dump(mode="json"),
        )
    )


def _decision_snapshot(
    plan: SupplierPlan, offer_version: int | None, now: datetime
) -> dict[str, JsonValue]:
    return {
        "action": plan.action,
        "phase": plan.phase,
        "reason": plan.reason,
        "proposal_version": offer_version,
        "decided_at": now.isoformat(),
    }


def _changed(invite: Invite, plan: SupplierPlan, offer_version: int | None) -> bool:
    previous = invite.negotiation_decision
    if not isinstance(previous, dict):
        return True
    return (
        previous.get("action") != plan.action
        or previous.get("phase") != plan.phase
        or previous.get("reason") != plan.reason
        or previous.get("proposal_version") != offer_version
    )


def _event(plan: SupplierPlan) -> str | None:
    return _EVENTS.get(plan.action) or _EVENTS.get(plan.phase)


async def _record_decision(
    session: AsyncSession,
    rfq: Rfq,
    item: _Supplier,
    plan: SupplierPlan,
    lines: Sequence[_Line],
    correction_revision: int,
    now: datetime,
) -> NegotiationMessage | None:
    """Store what the planner decided for one supplier; a decided email as a row first."""
    invite = item.invite
    proposal = item.proposal
    version = proposal.version if proposal is not None else None
    row: NegotiationMessage | None = None
    if plan.email is not None and proposal is not None:
        sequence = max((r.sequence for r in item.rows), default=0) + 1
        decision = build_decision(
            plan.email,
            sequence=sequence,
            currency=rfq.base_currency,
            offer_version=proposal.version,
            offer_received_on=item.received_on,
            lines={line.row.id: line.facts for line in lines},
        )
        row_id = negotiation_message_id(invite.id, sequence)
        terms: dict[str, JsonValue] = {
            "decision": cast(dict[str, JsonValue], decision.model_dump(mode="json")),
            "currency": rfq.base_currency,
            "lines": legacy_lines(decision, plan.lines),
            "correction_revision": correction_revision,
            "comparison_policy_hash": _policy_hash(rfq),
        }
        inserted = await session.scalar(
            insert(NegotiationMessage)
            .values(
                id=row_id,
                tenant_id=rfq.tenant_id,
                invite_id=invite.id,
                proposal_id=proposal.id,
                sequence=sequence,
                round=plan.email.round,
                action="bafo" if plan.email.kind == "bafo" else "counter",
                kind=plan.email.kind,
                status="decided",
                terms=terms,
            )
            .on_conflict_do_nothing()
            .returning(NegotiationMessage.id)
        )
        if inserted is not None:
            row = await session.get(NegotiationMessage, row_id)
            session.add(
                AuditEvent(
                    tenant_id=rfq.tenant_id,
                    actor_user_id=None,
                    action="rfq.negotiation_decided",
                    payload={
                        "rfq_id": str(rfq.id),
                        "invite_id": str(invite.id),
                        "negotiation_message_id": str(row_id),
                        "kind": plan.email.kind,
                        "round": plan.email.round,
                        "sequence": sequence,
                        "proposal_version": proposal.version,
                    },
                )
            )
    if _changed(invite, plan, version):
        event = _event(plan)
        if event is not None:
            session.add(
                AuditEvent(
                    tenant_id=rfq.tenant_id,
                    actor_user_id=None,
                    action=event,
                    payload={
                        "rfq_id": str(rfq.id),
                        "invite_id": str(invite.id),
                        "reason": plan.reason,
                        "phase": plan.phase,
                        "proposal_version": version,
                    },
                )
            )
        invite.negotiation_decision = _decision_snapshot(plan, version, now)
    invite.negotiation_status = plan.status
    if proposal is not None:
        invite.negotiation_evaluated_proposal_id = proposal.id
    return row


async def evaluate_rfq(
    db: Db,
    tenant_id: UUID,
    rfq_id: UUID,
    *,
    writer: NegotiationWriter,
    now: datetime | None = None,
    history: PriceHistory = quoted_price_history,
    force: bool = False,
    expected_delivery: ExpectedDelivery = calendar_expected_delivery,
) -> EvaluationResult:
    """Plan every supplier, store what is decided, write the decided emails, check early close.

    `force` (the buyer asked for counter-offers now) plans right away; it never
    creates a second email for an offer that already has one (decided, waiting,
    sent, held or rejected): the planner waits, or hands it to the buyer.
    """
    del force  # Every sweep plans every supplier; kept for the Copilot's call.
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
    became_ready = False
    async with tenant_session(db, tenant_id) as session:
        rfq = await session.scalar(select(Rfq).where(Rfq.id == rfq_id).with_for_update())
        if rfq is None or rfq.negotiation_status not in _ACTIVE:
            return EvaluationResult()
        if ComparisonPolicy.model_validate(rfq.comparison_policy or {}) != comparison.policy:
            # A concurrent policy edit invalidated the comparison before this lock.
            # Leave the offers unplanned; the next sweep loads the new policy.
            return EvaluationResult(skipped=1)
        loaded = await _load(session, rfq, history, at, lock=True)
        planned = _plan(loaded, comparison, at, expected_delivery)
        revisions = {c.invite_id: c.correction_revision for c in comparison.suppliers}
        for item in loaded.suppliers:
            plan = planned.plans[item.invite.id]
            if not planned.current(item):
                # A newer offer arrived after the comparison was read: the next sweep plans it.
                continue
            await _record_decision(
                session, rfq, item, plan, loaded.lines, revisions.get(item.invite.id, 0), at
            )
        if (
            planned.cutoff is not None
            and rfq.negotiation_cutoff_at is None
            and any(p.email is not None and p.email.kind == "bafo" for p in planned.plans.values())
        ):
            rfq.negotiation_cutoff_at = planned.cutoff
        waiting = sum(1 for item in loaded.suppliers if item.invite.status == "sent")
        if (
            rfq.negotiation_status == "open"
            and rfq.negotiation_early_close
            and ready_to_close(
                line_ids=[line.row.id for line in loaded.lines],
                waiting_suppliers=waiting,
                plans=[
                    planned.plans[item.invite.id]
                    for item in loaded.suppliers
                    if item.invite.status not in ("draft", "pending_approval")
                ],
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

    written = await _write_decided(db, tenant_id, rfq_id, writer=writer, now=at)
    if became_ready:
        logger.info("negotiation ready to close tenant=%s rfq=%s", tenant_id, rfq_id)
    return EvaluationResult(
        proposed=len(written.approval_ids),
        ready=became_ready,
        skipped=written.refused,
        approval_ids=tuple(written.approval_ids),
        held=written.held,
    )


# --- Writing the decided emails ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Due:
    """One decided email to write: everything the writer and the approval need."""

    row_id: UUID
    invite_id: UUID
    rfq_id: UUID
    proposal_id: UUID
    sender_user_id: UUID
    to: str
    subject: str
    decision: EmailDecision
    attempts: int
    correction_revision: JsonValue
    policy_hash: JsonValue


@dataclass(slots=True)
class _Written:
    approval_ids: list[UUID] = field(default_factory=list[UUID])
    refused: int = 0
    held: int = 0


async def _due(session: AsyncSession, rfq: Rfq, row: NegotiationMessage) -> _Due | str:
    """What writing a decided email needs, or why it cannot be written."""
    decision = stored_decision(row)
    if decision is None:
        return "decision_unreadable"
    invite = await session.get(Invite, row.invite_id)
    if invite is None or invite.thread_message_id is None:
        return "no_thread"
    contact_email = await session.scalar(
        select(Contact.email).where(Contact.id == invite.contact_id)
    )
    sender_user_id = await _sender(session, invite)
    if contact_email is None or sender_user_id is None:
        return "no_recipient_or_sender"
    first_subject = await session.scalar(
        select(Message.subject).where(
            Message.invite_id == invite.id,
            Message.direction == "out",
            Message.message_id_header == invite.thread_message_id,
        )
    )
    return _Due(
        row_id=row.id,
        invite_id=invite.id,
        rfq_id=rfq.id,
        proposal_id=row.proposal_id,
        sender_user_id=sender_user_id,
        to=contact_email,
        subject=reply_subject(first_subject, rfq.ref, rfq.title),
        decision=decision,
        attempts=row.attempts,
        correction_revision=cast(dict[str, JsonValue], row.terms).get("correction_revision"),
        policy_hash=cast(dict[str, JsonValue], row.terms).get("comparison_policy_hash"),
    )


async def _task(db: Db, tenant_id: UUID, item: _Due) -> NegotiationEmailInput | None:
    """The writer's task for a decided email (supplier, buyer and language read now)."""
    async with tenant_session(db, tenant_id) as session:
        rfq = await session.get(Rfq, item.rfq_id)
        invite = await session.get(Invite, item.invite_id)
        supplier = await session.get(Supplier, invite.supplier_id) if invite else None
        tenant = await session.get(Tenant, tenant_id)
        user = await session.get(User, item.sender_user_id)
        if rfq is None or supplier is None or tenant is None or user is None:
            return None
        return writer_task(
            item.decision,
            rfq_ref=rfq.ref,
            rfq_title=rfq.title,
            buyer_company=tenant.name,
            buyer_name=await buyer_display_name(session, user, tenant.name),
            supplier_name=supplier.name,
            supplier_language=supplier_email_language(supplier),
        )


async def _write_decided(
    db: Db, tenant_id: UUID, rfq_id: UUID, *, writer: NegotiationWriter, now: datetime
) -> _Written:
    """Write every decided email of the RFQ from its stored decision; propose or hold it."""
    result = _Written()
    async with tenant_session(db, tenant_id) as session:
        rfq = await session.get(Rfq, rfq_id)
        if rfq is None:
            return result
        rows = list(
            await session.scalars(
                select(NegotiationMessage)
                .join(Invite, Invite.id == NegotiationMessage.invite_id)
                .where(Invite.rfq_id == rfq_id, NegotiationMessage.status == "decided")
                .order_by(NegotiationMessage.created_at, NegotiationMessage.id)
            )
        )
        due: list[_Due] = []
        for row in rows:
            built = await _due(session, rfq, row)
            if isinstance(built, str):
                await _hold_row(session, row, built)
                result.held += 1
                continue
            due.append(built)
    sink = PostgresApprovalSink(db)
    for item in due:
        task = await _task(db, tenant_id, item)
        attempts = item.attempts
        problems: list[str] = [] if task is not None else ["task_unavailable"]
        body: str | None = None
        while task is not None and attempts < MAX_DRAFT_ATTEMPTS:
            attempts += 1
            context = ToolContext(tenant_id=tenant_id, user_id=item.sender_user_id, run_id=uuid4())
            try:
                draft = await writer(task, context)
            except AgentFailed:
                # Likely transient (the model or its provider): the next sweep tries again.
                problems = ["writer_failed"]
                break
            problems = negotiation_email_problems(task, draft)
            if not problems:
                body = draft.body.strip()
                break
            # Refused: another draft of the same decision may pass the check.
            logger.warning(
                "negotiation draft refused tenant=%s invite=%s row=%s attempt=%d problems=%s",
                tenant_id,
                item.invite_id,
                item.row_id,
                attempts,
                ",".join(problems),
            )
        if body is None or task is None:
            result.refused += 1
            async with tenant_session(db, tenant_id) as session:
                row = await session.scalar(
                    select(NegotiationMessage)
                    .where(NegotiationMessage.id == item.row_id)
                    .with_for_update()
                )
                if row is None or row.status != "decided":
                    continue
                row.attempts = attempts
                reason = (
                    "draft_refused"
                    if task is not None and problems != ["writer_failed"]
                    else problems[0]
                )
                if task is None or attempts >= MAX_DRAFT_ATTEMPTS:
                    await _hold_row(session, row, f"{reason}:{','.join(problems)}"[:300])
                    result.held += 1
            continue
        approval_id = await _propose(db, sink, tenant_id, item, body, attempts)
        if approval_id is not None:
            result.approval_ids.append(approval_id)
    return result


async def _hold_row(session: AsyncSession, row: NegotiationMessage, reason: str) -> None:
    """The decided email cannot be written: it waits for the buyer, with the reason."""
    row.status = "held"
    row.held_reason = reason
    invite = await session.get(Invite, row.invite_id)
    session.add(
        AuditEvent(
            tenant_id=row.tenant_id,
            actor_user_id=None,
            action="rfq.negotiation_held",
            payload={
                "rfq_id": str(invite.rfq_id) if invite is not None else None,
                "invite_id": str(row.invite_id),
                "negotiation_message_id": str(row.id),
                "reason": reason.split(":", 1)[0],
                "attempts": row.attempts,
            },
        )
    )
    await session.flush()


async def _propose(
    db: Db,
    sink: PostgresApprovalSink,
    tenant_id: UUID,
    item: _Due,
    body: str,
    attempts: int,
) -> UUID | None:
    """Record the approval, then mark the decided row proposed; once per row."""
    action: NegotiationActionName = "bafo" if item.decision.kind == "bafo" else "counter"
    args = SendNegotiationEmailArgs(
        negotiation_message_id=item.row_id,
        invite_id=item.invite_id,
        action=action,
        round=item.decision.round,
        sender_user_id=item.sender_user_id,
        to=[item.to],
        subject=item.subject,
        body=body,
    )
    payload = cast(dict[str, JsonValue], args.model_dump(mode="json"))
    run_id, call_id = negotiation_proposal_ids(item.row_id)
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
        logger.info("negotiation email %s was already proposed", item.row_id)

    async with tenant_session(db, tenant_id) as session:
        approval_id = await session.scalar(
            select(Approval.id).where(
                Approval.run_id == str(run_id), Approval.tool_call_id == call_id
            )
        )
        if approval_id is None:
            return None
        rfq = await session.scalar(select(Rfq).where(Rfq.id == item.rfq_id).with_for_update())
        row = await session.scalar(
            select(NegotiationMessage).where(NegotiationMessage.id == item.row_id).with_for_update()
        )
        source = await session.scalar(
            select(Proposal).where(Proposal.id == item.proposal_id).with_for_update()
        )
        latest_version = await session.scalar(
            select(func.max(Proposal.version)).where(Proposal.invite_id == item.invite_id)
        )
        revision = await session.scalar(
            select(func.count(FieldCorrection.id)).where(
                FieldCorrection.proposal_id == item.proposal_id,
            )
        )
        stale = (
            rfq is None
            or rfq.status != "open"
            or rfq.negotiation_status not in _ACTIVE
            or source is None
            or source.version != latest_version
            or _policy_hash(rfq) != item.policy_hash
            or revision != item.correction_revision
        )
        if row is None or (row.status == "proposed" and row.approval_id == approval_id):
            return approval_id if row is not None else None
        if stale or row.status != "decided":
            expired = await session.scalar(
                update(Approval)
                .where(Approval.id == approval_id, Approval.status == "pending")
                .values(status="expired")
                .returning(Approval)
            )
            if expired is not None:
                session.add(approval_audit(expired, "approval.expired", None))
            if row.status == "decided":
                row.status = "canceled"
                row.canceled_reason = "stale_before_proposal"
            return None
        row.status = "proposed"
        row.approval_id = approval_id
        row.attempts = attempts
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
        result.held += evaluation.held
        result.ready += int(evaluation.ready)
        result.closed += int(evaluation.closed)
        result.rfqs.append(rfq_id)
    if result.canceled or result.proposed or result.ready or result.closed or result.skipped:
        logger.info(
            "negotiation swept tenant=%s canceled=%d proposed=%d ready=%d closed=%d "
            "skipped=%d held=%d",
            tenant_id,
            result.canceled,
            result.proposed,
            result.ready,
            result.closed,
            result.skipped,
            result.held,
        )
    return result
