"""Supplier negotiation: settings, the sweep, early close, closing at the deadline.

A negotiation starts when the buyer saves its settings (`PUT
/v1/rfqs/{id}/negotiation`) or a mandate is approved: a deadline, a round cap,
and per-line limits (target, maximum, lead time, in the RFQ's base currency).
Until then, nothing is negotiated.

The `negotiation.sweep` job (every 15 minutes, see `bido_back.negotiation.jobs`)
does, per tenant, per RFQ whose negotiation is open (a supplier reply runs steps
2-5 for its RFQ right away: `replan_rfq`, the `negotiation.replan` job; the
sweep stays the safety net):

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
   when the supplier's answer also left items of the clarification ledger open,
   the same email asks them first (one email per supplier answer: the decision
   stores them, and they are recorded as asked by it when it goes out); the
   follow-up sweep writes a clarification alone only when the planner says so
   (`planned_clarifications`). Accept, final and hold decisions are recorded on
   the invite (with the plan's detail code), with an audit
   event when they change; the first best-and-final request sets the RFQ's
   common cut-off (and every supplier asked in that sweep gets it).
4. Write: each decided email is rendered by code from its stored decision, in
   the standard layout and the language of the thread's RFQ email
   (`bido_back.supplier_email`): a revised-offer request (a price asked, with or
   without a number), a delivery-date request ("deliver by <date>"), or the
   best-and-final request with the RFQ's common cut-off. The negotiation writer
   may only add an optional note. The send gate (`supplier_email.checks`) checks
   the email, and it is recorded as a PENDING approval of `send_negotiation_email`
   requested on behalf of the buyer who sent the RFQ email. An email the gate
   refuses is `held` at once with the reason codes (a code-level refusal would
   only recur) and the buyer decides; a writer failure only leaves the note out.
   The same offer version never gets a second decision.
5. Ready to close: the RFQ becomes `ready_to_close` (once, audited as
   `rfq.negotiation_ready` with its reason) when every invited supplier answered and
   either every line has an offer accepted internally (early close, only with
   `early_close` on: `every_line_in_target`), or every supplier gave its last word
   (best-and-final answered, its own final offer, round cap, cut-off, declined; none
   held, none with an open question or an email in flight: `every_supplier_final`).
   The second applies whatever `early_close` says: nothing is left to negotiate, and
   waiting for the deadline only delays the buyer's decision. While ready, the view's
   `recommendation` is what closing would keep: the best eligible offer per line and
   tier (never one above the maximum), `above_target` when it is above the target.

The view (`get_negotiation`) runs the same planner read-only, so what the buyer
sees per supplier is what the next sweep does.

An email the send gate refuses (a code bug or an inconsistent decision) is
recorded once for a person and never rendered again by the sweep: its row ends
`canceled` with reason `draft_refused` and the problem codes in its terms, an
`rfq.negotiation_draft_held` audit event the activity feed shows, and the view
says `draft_refused_requires_buyer` (the planner holds that offer: `held_reason`).
A new offer, new limits, or a person asking for counter-offers
(`evaluate_rfq(force=True)`) decide it again. The writer's note never holds an
email: a note that fails or breaks a rule is left out.

The writer does not sign: code renders the buyer's signature, once
(`bido_back.templates.signature`). The email is in the language of the
invite's RFQ email (`bido_back.rfqs.language.thread_language`).

Logs carry ids and counts only.
"""

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Final, Literal, cast
from uuid import UUID, uuid4

from pydantic import JsonValue, ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.agents.approvals import ApprovalProposal
from bido_back.agents.catalog.contracts import NegotiationEmailInput
from bido_back.agents.context import ToolContext
from bido_back.agents.events import args_hash
from bido_back.approvals.audit import approval_audit
from bido_back.approvals.errors import ApprovalConflict
from bido_back.approvals.sink import PostgresApprovalSink
from bido_back.auth import Principal
from bido_back.comparison.compute import Cell as ComparisonCell
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
from bido_back.followups.ledger import (
    Ask,
    SupplierFollowup,
    declined_positions,
    supplier_followups,
)
from bido_back.followups.missing import DEFAULT_EXPECTED_DELIVERY, OpenItem
from bido_back.mandates.schemas import ApproveMandateArgs
from bido_back.mandates.waits import MandateWait, approval_waits
from bido_back.negotiation.agents import NegotiationWriter
from bido_back.negotiation.decision import (
    DecisionLine,
    EmailDecision,
    QuestionDecision,
    QuotedTerms,
    build_decision,
    decision_of,
    legacy_lines,
    reminder_decision,
    supplier_email,
    writer_task,
)
from bido_back.negotiation.history import PriceHistory, quoted_price_history
from bido_back.negotiation.labels import line_reasons, phase_label, warning_text
from bido_back.negotiation.planner import (
    REPLY_BUSINESS_DAYS,
    Context,
    EmailPlan,
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
    end_of_day,
    every_supplier_final,
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
    ClarificationItem,
    ClarificationState,
    ClarificationWait,
    ExpectedDelivery,
    QuotedLeadTime,
    extraction_expected_delivery,
    latest_order_date,
    offer_signals,
    stated_lead_time,
)
from bido_back.negotiation.strategy import ask_places, plain
from bido_back.rfqs.errors import AgentFailed, InvalidState, NotFound
from bido_back.rfqs.language import country_code, thread_language
from bido_back.rfqs.send import SEND_RFQ_EMAIL, send_proposal_ids
from bido_back.rfqs.service import buyer_display_name
from bido_back.supplier_email.builders import Header, email_line
from bido_back.supplier_email.checks import GateContext, problems_text
from bido_back.supplier_email.compose import Refused, compose
from bido_back.supplier_email.locale import Locale
from bido_back.supplier_email.model import Signature, SupplierEmail
from bido_back.supplier_email.render import RenderedEmail
from bido_back.supplier_email.subject import reply_subject

logger = logging.getLogger(__name__)

Db = AsyncEngine | async_sessionmaker[AsyncSession]

_ACTIVE: Final = ("open", "ready_to_close")
_FINAL_INVITE: Final = ("rejected", "closed")
# Emails decided and not sent yet: what reconciliation and closing cancel.
_LIVE: Final = ("decided", "proposed")
_EMAIL_STATES: Final = frozenset({"decided", "proposed", "sent", "canceled", "rejected", "held"})
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
# A decided email the send gate refused: held for a person, with the problem codes.
_REFUSED: Final = "email_refused"
# How a refused email is recorded (N1): `canceled`, this reason, the codes in its terms.
DRAFT_REFUSED: Final = "draft_refused"
DRAFT_REFUSED_REASON: Final = "draft_refused_requires_buyer"
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
    # An earlier reply already declared the supplier's final position ("best and final",
    # "we cannot go any lower"): it stays final, Bido asks nothing more of this supplier.
    final_before: bool = False


@dataclass(slots=True)
class _Loaded:
    rfq: Rfq
    lines: list[_Line]
    suppliers: list[_Supplier]
    versions: dict[UUID, int]
    approvals: dict[UUID, Approval]
    waits: dict[UUID, MandateWait]
    # Per invite: the clarification ledger (`followups.ledger.supplier_followups`).
    ledger: dict[UUID, SupplierFollowup] = field(default_factory=dict[UUID, SupplierFollowup])


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
        for proposal in proposals:
            item = by_invite[proposal.invite_id]
            if (
                item.proposal is not None
                and proposal.version < item.proposal.version
                and offer_signals(proposal.fields).final_offer
            ):
                item.final_before = True
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
    ledger = await supplier_followups(
        session, rfq.id, now=now, expected_delivery=DEFAULT_EXPECTED_DELIVERY
    )
    return _Loaded(rfq, lines, suppliers, versions, approvals, waits, ledger)


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
    """The typed decision a negotiation email was written from (None before 0037)."""
    return decision_of(cast(dict[str, object], row.terms))


def _parse_decimal(value: object) -> Decimal | None:
    if not isinstance(value, str):
        return None
    try:
        number = Decimal(value)
    except ArithmeticError:
        return None
    # Older rows stored normalized values ("5E+1"): read them plainly.
    return plain(number) if number.is_finite() else None


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


def _refused_and_held(row: NegotiationMessage) -> bool:
    """The send gate refused this email and it still waits for a person (until a new
    offer, new limits or a person asking for counter-offers release it)."""
    return (
        row.status == "canceled"
        and row.canceled_reason == DRAFT_REFUSED
        and row.held_reason is not None
    )


def _state(row: NegotiationMessage) -> EmailState:
    if _refused_and_held(row):
        return "held"
    return cast(EmailState, row.status) if row.status in _EMAIL_STATES else "canceled"


def _planned_state(row: NegotiationMessage) -> EmailState:
    """What the planner counts: a canceled email whose Message-ID was claimed may have
    reached the supplier (the send was interrupted), so it counts as sent: its round is
    used, a best-and-final is never asked twice."""
    if row.status == "canceled" and row.message_id_header is not None:
        return "sent"
    return _state(row)


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
        order_date: date | None = decision.order_date
    else:
        # Written before typed decisions (0037): a request was answered within
        # `REPLY_BUSINESS_DAYS`, a best-and-final by the negotiation deadline.
        asks = _legacy_asks(row)
        kind = "bafo" if row.action == "bafo" else "revised_offer"
        asked_at = row.sent_at or row.created_at
        reply_by = add_business_days(asked_at, REPLY_BUSINESS_DAYS, None).date()
        cutoff = loaded.rfq.negotiation_deadline if kind == "bafo" else None
        order_date = asked_at.date()
    approval = loaded.approvals.get(row.approval_id) if row.approval_id is not None else None
    return EmailRecord(
        sequence=row.sequence,
        round=row.round,
        kind=kind,
        state=_planned_state(row),
        proposal_id=row.proposal_id,
        proposal_version=loaded.versions.get(row.proposal_id, 0),
        asks=asks,
        reply_by=reply_by,
        cutoff_at=cutoff,
        held_reason=row.held_reason,
        wait=_email_wait(approval, loaded.waits) if row.status == "proposed" else None,
        order_date=order_date,
        reminder_of=decision.reminder_of if decision is not None else None,
        typed=decision is not None,
        deferred=frozenset(decision.deferred_lines) if decision is not None else frozenset(),
    )


def _item_code(position: int | None, item_field: str) -> str:
    return item_field if position is None else f"pos{position}.{item_field}"


def _item_codes(items: Sequence[OpenItem | Ask]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(_item_code(i.position, i.field) for i in items))


def _item_lines(items: Sequence[OpenItem | Ask]) -> frozenset[UUID]:
    return frozenset(i.rfq_line_id for i in items if i.rfq_line_id is not None)


def _clarification(item: _Supplier, loaded: _Loaded, now: datetime) -> ClarificationState:
    """The clarification ledger (`followups.ledger.supplier_followups`) for this supplier,
    as the planner reads it: the same state the follow-up sweep acts on.

    - to ask: what the latest offer lacks and nobody asked (the follow-up sweep asks it);
    - pending: a clarification, or its reminder, waits for approval (a person, the
      mandate's checks or daily cap, a mailbox);
    - awaiting: our clarification is unanswered, until its reply-by date (its
      reminder's, once reminded);
    - exhausted: items answered unusably, asked before and still missing, never
      answered after the reminder, or whose clarification the send gate held: the
      buyer decides on those lines; the rest of the offer is negotiated;
    - asked by our unanswered negotiation email (it asked them with its own asks): their
      lines wait for the answer (the planner waits for that email's answer anyway).
    """
    del now
    state = loaded.ledger.get(item.invite.id)
    if state is None or item.proposal is None or not state.applicable:
        return NO_CLARIFICATION
    by_position = {line.row.position: line.row.id for line in loaded.lines}
    exhausted: list[str] = []
    exhausted_lines: set[UUID] = set()
    for decision in state.decisions:
        if decision.kind in ("unusable_answer", "asked_before") and decision.field is not None:
            exhausted.append(_item_code(decision.position, decision.field))
            if decision.position is not None and decision.position in by_position:
                exhausted_lines.add(by_position[decision.position])
    to_ask: Sequence[OpenItem] = state.to_ask
    if to_ask and any(
        d.kind == "email_held" and "clarification" in d.reasons for d in state.decisions
    ):
        # The send gate refused the clarification: the buyer decides on what it asked.
        exhausted += _item_codes(to_ask)
        exhausted_lines |= _item_lines(to_ask)
        to_ask = ()
    if state.request is not None and state.request.kind != "clarification":
        # Our counter-offer or best-and-final request also asked these: their lines wait.
        settled_lines = _item_lines(state.request.asks)
    else:
        settled_lines = frozenset[UUID]()
    request = state.request if state.request and state.request.kind == "clarification" else None
    if request is not None and any(d.kind == "no_answer" for d in state.decisions):
        # Unanswered after its reminder: those items are the buyer's now.
        exhausted += _item_codes(request.asks)
        exhausted_lines |= _item_lines(request.asks)
        request = None
    settled = ClarificationState(
        exhausted=tuple(dict.fromkeys(exhausted)),
        exhausted_lines=frozenset(exhausted_lines),
        lines=settled_lines,
    )
    waiting = [p for p in state.pending if p.kind in ("clarification", "reminder")]
    if waiting:
        last = max(waiting, key=lambda p: p.created_at)
        approval = loaded.approvals.get(last.approval_id) if last.approval_id else None
        wait, until = _clarification_wait(approval, loaded.waits)
        asked = [a for a in state.asks if a.status in ("proposed", "open")]
        return replace(
            settled, pending=True, pending_wait=wait, pending_until=until, lines=_item_lines(asked)
        )
    if request is not None:
        day = request.reminder_reply_by if request.reminded else request.reply_by
        return replace(
            settled,
            awaiting=True,
            awaiting_until=end_of_day(day) if day is not None else None,
            lines=_item_lines(request.asks),
        )
    if to_ask:
        return replace(
            settled,
            to_ask=_item_codes(to_ask),
            items=tuple(
                dict.fromkeys(ClarificationItem(i.rfq_line_id, i.position, i.field) for i in to_ask)
            ),
            lines=settled_lines | _item_lines(to_ask),
        )
    return settled


def _holds(
    column_holds: Sequence[str], item: _Supplier, ledger: SupplierFollowup | None
) -> tuple[str, ...]:
    """The comparison's holds on the offer, less a question the buyer answered.

    A supplier question holds the offer until the buyer answers or dismisses it in the
    ledger (`followups.questions`); a reply that could not be applied stays held.
    """
    holds = list(column_holds)
    question = "supplier_clarification_requires_buyer"
    if ledger is None or question not in holds or ledger.open_questions:
        return tuple(holds)
    raw = item.proposal.fields.get("_offer_state") if item.proposal is not None else None
    state = cast(dict[str, object], raw) if isinstance(raw, dict) else {}
    if state.get("reply_kind") == "clarification" or state.get("requires_clarification") is True:
        return tuple(holds)
    return tuple(h for h in holds if h != question)


def _payment_days(text: str | None) -> int | None:
    if not text:
        return None
    found = _DAYS.search(text)
    return int(found.group(0)) if found is not None else None


def _shown(cell: ComparisonCell, lead: QuotedLeadTime) -> QuotedTerms:
    """The line as the supplier quoted it: what the email shows as "your offer"."""
    stated = stated_lead_time(lead)
    days: int | None = cell.lead_time_days
    basis: Literal["calendar", "business"] = "calendar"
    approximate = False
    if stated is not None:
        approximate = stated.approximate
        amount = stated.amount
        days = None
        if amount is not None and stated.unit in ("calendar_days", "business_days", "weeks"):
            total = amount * 7 if stated.unit == "weeks" else amount
            if total == total.to_integral_value():
                days = int(total)
                basis = "business" if stated.unit == "business_days" else "calendar"
        # Months, a date or part days are not restated as days: the email shows the date
        # we need instead.
    return QuotedTerms(
        unit_price=cell.unit_price,
        currency=cell.currency,
        per=cell.price_basis_quantity,
        lead_days=days,
        lead_basis=basis,
        lead_approximate=approximate,
    )


def _quotes(
    comparison: Comparison,
    loaded: _Loaded,
    order_date: date,
    expected_delivery: ExpectedDelivery,
) -> tuple[dict[UUID, dict[UUID, LineQuote]], dict[UUID, dict[UUID, QuotedTerms]]]:
    """Per invite, per line: the current offer (first tier, base currency) as the planner
    reads it, with the delivery dates the lead-time helper computes; and the line as
    the supplier quoted it (what an email shows)."""
    rfq = loaded.rfq
    need_by = {line.row.id: line.row.need_by for line in loaded.lines}
    by_invite = {s.invite.id: s for s in loaded.suppliers}
    quotes: dict[UUID, dict[UUID, LineQuote]] = {}
    shown: dict[UUID, dict[UUID, QuotedTerms]] = {}
    for column in comparison.suppliers:
        item = by_invite.get(column.invite_id)
        if item is None or item.proposal is None:
            continue
        line_fields = {line.id: line.fields for line in item.proposal_lines}
        holds = set(column.negotiation_holds)
        mine: dict[UUID, LineQuote] = {}
        quoted: dict[UUID, QuotedTerms] = {}
        asked_on = _asked_order_dates(item)
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
            quoted[cell.rfq_line_id] = _shown(cell, lead)
            # The delivery as of the order dates of the emails that asked for a date: the
            # supplier moved when it is earlier than what it offered then.
            then = {day: expected_delivery(lead, day) for day in asked_on}
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
                deliveries_then=tuple(
                    (day, found[0]) for day, found in then.items() if found is not None
                ),
            )
        quotes[column.invite_id] = mine
        shown[column.invite_id] = quoted
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
    return quotes, shown


def _asked_order_dates(item: "_Supplier") -> tuple[date, ...]:
    """The order dates of the decisions behind the emails sent to this supplier."""
    days: set[date] = set()
    for row in item.rows:
        if row.status != "sent":
            continue
        decision = stored_decision(row)
        if decision is not None:
            days.add(decision.order_date)
    return tuple(sorted(days))


def _offer(
    item: _Supplier, quotes: Mapping[UUID, LineQuote], loaded: _Loaded, holds: Sequence[str]
) -> Offer | None:
    proposal = item.proposal
    if proposal is None:
        return None
    signals = offer_signals(proposal.fields)
    ledger = loaded.ledger.get(item.invite.id)
    positions = {line.row.position: line.row.id for line in loaded.lines}
    offered, requested = (
        _payment_days(proposal.payment_terms),
        _payment_days(loaded.rfq.payment_terms),
    )
    # Declined or withdrawn positions, those of earlier replies included (the ledger's).
    declined = signals.declined_positions | declined_positions(proposal.fields)
    return Offer(
        proposal_id=proposal.id,
        version=proposal.version,
        lines=quotes,
        valid_until=proposal.valid_until,
        received_on=item.received_on,
        holds=tuple(holds),
        # The supplier's questions the buyer has not answered or dismissed yet (the
        # ledger's); the offer's own count without a ledger.
        questions=len(ledger.open_questions) if ledger is not None else signals.questions,
        declared_final=signals.final_offer or item.final_before,
        declined_lines=frozenset(positions[p] for p in declined if p in positions),
        payment_terms_shorter=offered is not None and requested is not None and offered < requested,
        holds_released=item.invite.negotiation_released_proposal_id == proposal.id,
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


def _live_cutoff(loaded: _Loaded) -> datetime | None:
    """The RFQ's common best-and-final cut-off, while a best-and-final request carrying it
    is decided, waiting or sent. One that never reached a supplier (canceled, rejected,
    held) binds nobody: the next best-and-final sets a new one."""
    live = any(
        row.kind == "bafo" and row.status in ("decided", "proposed", "sent")
        for item in loaded.suppliers
        for row in item.rows
    )
    return loaded.rfq.negotiation_cutoff_at if live else None


@dataclass(slots=True)
class _Planned:
    loaded: _Loaded
    plans: dict[UUID, SupplierPlan]
    cutoff: datetime | None
    # Per invite, the offer (proposal) the comparison read; the quotes it gave.
    columns: dict[UUID, UUID]
    quotes: dict[UUID, dict[UUID, LineQuote]]
    # Per invite, per line: the offer as the supplier quoted it (what an email shows).
    shown: dict[UUID, dict[UUID, QuotedTerms]] = field(
        default_factory=dict[UUID, dict[UUID, QuotedTerms]]
    )

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
    quotes, shown = _quotes(comparison, loaded, now.date(), expected_delivery)
    by_invite = {item.invite.id: item for item in loaded.suppliers}
    holds = {
        c.invite_id: _holds(
            c.negotiation_holds, by_invite[c.invite_id], loaded.ledger.get(c.invite_id)
        )
        for c in comparison.suppliers
        if c.invite_id in by_invite
    }
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
    cutoff = _live_cutoff(loaded)

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
    return _Planned(loaded, plans, cutoff, columns, quotes, shown)


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


def _strategy_reasons(plan: SupplierPlan) -> list[str]:
    """The planner's reason code; an email the send gate refused reads as before."""
    if plan.reason == "email_not_prepared" and (plan.detail or "").startswith(_REFUSED):
        return [DRAFT_REFUSED_REASON]
    return [plan.reason] if plan.reason else []


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
        # The legacy status: an email being prepared is "proposed"; one held for a person
        # will not go out as it is ("canceled", with its reason).
        status: NegotiationMessageStatus = (
            "proposed" if state == "decided" else "canceled" if state == "held" else state
        )
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
                kind="reminder"
                if row.kind == "reminder"
                else decision.kind
                if decision is not None
                else ("bafo" if row.action == "bafo" else "revised_offer"),
                reminder_of=decision.reminder_of if decision is not None else None,
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
    expected_delivery: ExpectedDelivery = extraction_expected_delivery,
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
        sent = [r for r in item.rows if r.status == "sent" and r.kind != "reminder"]
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
                strategy_reasons=_strategy_reasons(plan),
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
    # Ready to close: what closing would keep now, so the buyer decides without closing.
    recommendation = (
        _outcome(comparison, {line.row.id: line.row.target_unit_price for line in loaded.lines})
        if rfq.negotiation_status == "ready_to_close"
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
        recommendation=recommendation,
        cutoff_at=_live_cutoff(loaded),
        warnings=_rfq_warnings(planned),
    )


async def get_negotiation(
    db: Db,
    principal: Principal,
    rfq_id: UUID,
    *,
    now: datetime | None = None,
    history: PriceHistory = quoted_price_history,
    expected_delivery: ExpectedDelivery = extraction_expected_delivery,
) -> RfqNegotiation:
    return await _view(
        db, principal.tenant_id, rfq_id, now or datetime.now(UTC), history, expected_delivery
    )


async def planned_clarifications(
    db: Db,
    tenant_id: UUID,
    rfq_id: UUID,
    *,
    now: datetime,
    history: PriceHistory = quoted_price_history,
    expected_delivery: ExpectedDelivery = extraction_expected_delivery,
) -> frozenset[UUID]:
    """The invites whose next action is a clarification on its own (`clarify`): the only
    ones the follow-up sweep writes one for while the RFQ's negotiation is open. When the
    offer also has something to negotiate, the negotiation's own email asks the items
    (one email per supplier answer), and while one of its emails is being prepared,
    waits for approval or for its answer, nothing else goes out. Read-only: the same
    planner the sweep and the view run."""
    try:
        planned, _ = await _planned(db, tenant_id, rfq_id, now, history, expected_delivery)
    except NotFound:
        return frozenset()
    return frozenset(i for i, plan in planned.plans.items() if plan.action == "clarify")


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
    # New limits: an email the gate held is decided again with them.
    await _release_held(session, rfq_id, "settings_changed")
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
    approved. To change them, propose a new mandate (`POST .../negotiation/mandate`):
    it replaces the live one once approved (`bido_back.mandates`).
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
                f"rfq {rfq_id}: a negotiation mandate is live; propose a new mandate "
                "to replace it with these limits"
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


@dataclass(frozen=True, slots=True)
class OfferHold:
    """What holds a supplier's latest offer in the negotiation, as `release_hold` would
    release it. Read-only: the same planner the sweep and the view run."""

    invite_id: UUID
    supplier_name: str
    proposal_id: UUID | None
    proposal_version: int | None
    # The offer itself is on hold for the buyer and not released yet: releasing it
    # changes what the planner does next.
    held: bool
    # Where the supplier stands now (phase, reason code, one sentence written by code).
    phase: str
    reason: str
    label: str
    # The comparison's hold codes on the offer (e.g. offer_state_requires_review).
    holds: tuple[str, ...]


async def offer_hold(
    db: Db,
    principal: Principal,
    rfq_id: UUID,
    invite_id: UUID,
    *,
    now: datetime | None = None,
    history: PriceHistory = quoted_price_history,
    expected_delivery: ExpectedDelivery = extraction_expected_delivery,
) -> OfferHold:
    """What `release_hold` would release for one supplier, and why it is held."""
    at = now or datetime.now(UTC)
    planned, comparison = await _planned(
        db, principal.tenant_id, rfq_id, at, history, expected_delivery
    )
    item = next((i for i in planned.loaded.suppliers if i.invite.id == invite_id), None)
    if item is None:
        raise NotFound(f"supplier {invite_id} is not on rfq {rfq_id}")
    plan = planned.plans[invite_id]
    column = next((c for c in comparison.suppliers if c.invite_id == invite_id), None)
    holds = (
        _holds(column.negotiation_holds, item, planned.loaded.ledger.get(invite_id))
        if column is not None
        else ()
    )
    chases_sent = sum(1 for f in item.followups if f.kind == "chase" and f.status == "sent")
    return OfferHold(
        invite_id=invite_id,
        supplier_name=item.supplier.name,
        proposal_id=item.proposal.id if item.proposal is not None else None,
        proposal_version=item.proposal.version if item.proposal is not None else None,
        held=plan.offer_held,
        phase=plan.phase,
        reason=plan.reason,
        label=phase_label(plan, chases_sent=chases_sent),
        holds=tuple(holds),
    )


async def release_hold(
    db: Db,
    principal: Principal,
    rfq_id: UUID,
    invite_id: UUID,
    proposal_id: UUID,
    *,
    now: datetime | None = None,
    history: PriceHistory = quoted_price_history,
) -> RfqNegotiation:
    """The buyer releases the holds on a supplier's offer: negotiate it as it is.

    For an offer held for review (a reply that could not be applied, a question the
    buyer answered outside Bido): from the next sweep the planner treats that offer
    version as clean. `proposal_id` is the offer the buyer reviewed; when a newer one
    arrived meanwhile nothing is released (InvalidState: review that one). Audited; a
    newer offer is held again if it needs to be. A withdrawn offer stays closed.
    """
    at = now or datetime.now(UTC)
    async with tenant_session(db, principal.tenant_id) as session:
        invite = await session.scalar(
            select(Invite).where(Invite.id == invite_id, Invite.rfq_id == rfq_id).with_for_update()
        )
        if invite is None:
            raise NotFound(f"supplier {invite_id} is not on rfq {rfq_id}")
        rfq = await session.get(Rfq, rfq_id)
        if rfq is None or rfq.negotiation_status not in _ACTIVE:
            raise InvalidState(f"rfq {rfq_id}: the negotiation is not open")
        proposal = await session.scalar(
            select(Proposal)
            .where(Proposal.invite_id == invite_id)
            .order_by(Proposal.version.desc())
            .limit(1)
        )
        if proposal is None:
            raise InvalidState(f"supplier {invite_id} has no offer to release")
        if proposal.id != proposal_id:
            raise InvalidState(
                f"supplier {invite_id}: a newer offer arrived; review it before releasing it"
            )
        invite.negotiation_released_proposal_id = proposal.id
        session.add(
            AuditEvent(
                tenant_id=principal.tenant_id,
                actor_user_id=principal.user_id,
                action="rfq.negotiation_hold_released",
                payload={
                    "rfq_id": str(rfq_id),
                    "invite_id": str(invite_id),
                    "proposal_version": proposal.version,
                },
            )
        )
    logger.info(
        "negotiation hold released tenant=%s rfq=%s invite=%s by=%s",
        principal.tenant_id,
        rfq_id,
        invite_id,
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
                above_target=price is not None and target is not None and price > target,
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


async def withdraw_live_emails(session: AsyncSession, rfq_id: UUID, reason: str) -> int:
    """Withdraw the RFQ's negotiation emails decided, awaiting approval or approved and not
    sent yet, in the caller's transaction: each row ends `canceled` (`reason`; a canceled
    email uses no round), its pending approval expires, and an approved one is skipped by
    the send handler. The planner decides those offers again. An email whose Message-ID
    was already claimed may be with the supplier: it is left to finish."""
    rows = list(
        await session.scalars(
            select(NegotiationMessage)
            .join(Invite, Invite.id == NegotiationMessage.invite_id)
            .where(
                Invite.rfq_id == rfq_id,
                NegotiationMessage.status.in_(_LIVE),
                NegotiationMessage.message_id_header.is_(None),
            )
            .with_for_update(of=NegotiationMessage)
        )
    )
    if not rows:
        return 0
    await session.execute(
        update(Invite)
        .where(Invite.rfq_id == rfq_id, Invite.negotiation_status.not_in(_FINAL_INVITE))
        .values(negotiation_evaluated_proposal_id=None)
    )
    return await _cancel(session, rows, reason)


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


async def _rfq_email(session: AsyncSession, invite: Invite) -> Approval | None:
    """The executed approval of the invite's RFQ email: who sent it, in which language."""
    return await session.scalar(
        select(Approval).where(
            Approval.tool == SEND_RFQ_EMAIL,
            Approval.tool_call_id == send_proposal_ids(invite.id)[1],
            Approval.status == "executed",
        )
    )


def _sender(original: Approval | None) -> UUID | None:
    """The buyer who requested the RFQ email (its executed approval): their mailbox."""
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
    snapshot: dict[str, JsonValue] = {
        "action": plan.action,
        "phase": plan.phase,
        "reason": plan.reason,
        "proposal_version": offer_version,
        "decided_at": now.isoformat(),
    }
    if plan.detail is not None:
        # A code, never supplier text (e.g. the revised-offer request waits for the answers).
        snapshot["detail"] = plan.detail
    return snapshot


def _changed(invite: Invite, plan: SupplierPlan, offer_version: int | None) -> bool:
    previous = invite.negotiation_decision
    if not isinstance(previous, dict):
        return True
    return (
        previous.get("action") != plan.action
        or previous.get("phase") != plan.phase
        or previous.get("reason") != plan.reason
        or previous.get("proposal_version") != offer_version
        or previous.get("detail") != plan.detail
    )


def _event(plan: SupplierPlan) -> str | None:
    return _EVENTS.get(plan.action) or _EVENTS.get(plan.phase)


def _questions(
    items: Sequence[ClarificationItem],
    open_items: Sequence[OpenItem],
    quoted: Mapping[UUID, QuotedTerms],
) -> list[QuestionDecision]:
    """The ledger's items an email asks too (the planner's `EmailPlan.clarify`), as its
    decision stores them: how each is asked comes from the ledger's open item, a delivery
    question shows the lead time the position's offer states."""
    by_key = {(i.rfq_line_id, i.field): i for i in open_items}
    found: list[QuestionDecision] = []
    for item in items:
        open_item = by_key.get((item.rfq_line_id, item.field))
        if open_item is None:
            continue
        shown = quoted.get(open_item.rfq_line_id) if open_item.rfq_line_id else None
        lead = shown if open_item.field == "delivery_date" and shown is not None else None
        found.append(
            QuestionDecision(
                rfq_line_id=open_item.rfq_line_id,
                position=open_item.position,
                field=open_item.field,
                problem=open_item.problem,
                question=open_item.question,
                need_by=open_item.need_by,
                quoted_lead_days=lead.lead_days if lead is not None else None,
                quoted_lead_basis=lead.lead_basis if lead is not None else "calendar",
                quoted_lead_approximate=lead.lead_approximate if lead is not None else False,
            )
        )
    return found


async def _new_decision(
    session: AsyncSession,
    item: _Supplier,
    email: EmailPlan,
    lines: Sequence[_Line],
    quoted: Mapping[UUID, QuotedTerms],
    sequence: int,
    rfq: Rfq,
    open_items: Sequence[OpenItem] = (),
) -> tuple[EmailDecision, NegotiationMessage | None] | None:
    """The typed decision of an email the planner decided, and for a reminder the row of
    the request it repeats (None: nothing typed to repeat, an email from before 0037).
    `open_items` are what the offer lacks and nobody asked (the ledger's): the items the
    email asks too are read from them."""
    if email.reminder_of is not None:
        original = next((r for r in item.rows if r.sequence == email.reminder_of), None)
        repeated = stored_decision(original) if original is not None else None
        if original is None or repeated is None:
            return None
        return reminder_decision(repeated, sequence=sequence, reply_by=email.reply_by), original
    proposal = item.proposal
    assert proposal is not None
    thread = await _thread(session, item.invite, item.supplier)
    decision = build_decision(
        email,
        sequence=sequence,
        currency=rfq.base_currency,
        offer_version=proposal.version,
        offer_received_on=item.received_on,
        language=thread.language if thread is not None else None,
        lines={line.row.id: line.facts for line in lines},
        quoted=quoted,
        questions=_questions(email.clarify, open_items, quoted),
    )
    return decision, None


async def _record_decision(
    session: AsyncSession,
    rfq: Rfq,
    item: _Supplier,
    plan: SupplierPlan,
    lines: Sequence[_Line],
    correction_revision: int,
    now: datetime,
    quoted: Mapping[UUID, QuotedTerms],
    open_items: Sequence[OpenItem] = (),
) -> NegotiationMessage | None:
    """Store what the planner decided for one supplier; a decided email as a row first."""
    invite = item.invite
    proposal = item.proposal
    version = proposal.version if proposal is not None else None
    row: NegotiationMessage | None = None
    if plan.email is not None and proposal is not None:
        sequence = max((r.sequence for r in item.rows), default=0) + 1
        made = await _new_decision(
            session, item, plan.email, lines, quoted, sequence, rfq, open_items
        )
        if made is not None:
            decision, original = made
            row_id = negotiation_message_id(invite.id, sequence)
            kind = "reminder" if original is not None else decision.kind
            terms: dict[str, JsonValue] = {
                "decision": cast(dict[str, JsonValue], decision.model_dump(mode="json")),
                # What the clarification ledger reads of an open request (its reply-by).
                "reply_by": decision.reply_by.isoformat(),
                "currency": rfq.base_currency,
                "lines": cast(dict[str, JsonValue], original.terms).get("lines", [])
                if original is not None
                else legacy_lines(decision, plan.lines),
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
                    round=decision.round,
                    action=original.action
                    if original is not None
                    else "bafo"
                    if decision.kind == "bafo"
                    else "counter",
                    kind=kind,
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
                            "kind": kind,
                            "round": decision.round,
                            "sequence": sequence,
                            "proposal_version": proposal.version,
                            # One email per answer: the ledger's items it asks too, and
                            # the lines whose price ask waits for the next email (ids).
                            "questions": len(decision.questions),
                            "deferred_lines": [str(i) for i in decision.deferred_lines],
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
    expected_delivery: ExpectedDelivery = extraction_expected_delivery,
) -> EvaluationResult:
    """Plan every supplier, store what is decided, write the decided emails, check early close.

    `force` (a person asked for counter-offers now) plans right away and decides
    again an email the send gate held; it never creates a second email for an
    offer that already has one (decided, waiting, sent or rejected): the planner
    waits, or hands it to the buyer.
    """
    at = now or datetime.now(UTC)
    if force:
        # A person asks for counter-offers now: an email the gate held is decided again.
        async with tenant_session(db, tenant_id) as session:
            await _release_held(session, rfq_id, "retried_by_buyer")
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
            ledger = loaded.ledger.get(item.invite.id)
            await _record_decision(
                session,
                rfq,
                item,
                plan,
                loaded.lines,
                revisions.get(item.invite.id, 0),
                at,
                planned.shown.get(item.invite.id, {}),
                ledger.to_ask if ledger is not None else (),
            )
        # Set by the first best-and-final decided; dropped when none carrying it is left.
        rfq.negotiation_cutoff_at = planned.cutoff
        waiting = sum(1 for item in loaded.suppliers if item.invite.status == "sent")
        answered = [
            planned.plans[item.invite.id]
            for item in loaded.suppliers
            if item.invite.status not in ("draft", "pending_approval")
        ]
        ready: str | None = None
        if rfq.negotiation_status == "open":
            if rfq.negotiation_early_close and ready_to_close(
                line_ids=[line.row.id for line in loaded.lines],
                waiting_suppliers=waiting,
                plans=answered,
            ):
                ready = "every_line_in_target"
            elif every_supplier_final(waiting_suppliers=waiting, plans=answered):
                # Nothing is left to negotiate: early_close or not, the buyer decides now.
                ready = "every_supplier_final"
        if ready is not None:
            rfq.negotiation_status = "ready_to_close"
            rfq.negotiation_ready_at = at
            became_ready = True
            session.add(
                AuditEvent(
                    tenant_id=tenant_id,
                    actor_user_id=None,
                    action="rfq.negotiation_ready",
                    payload={"rfq_id": str(rfq_id), "reason": ready},
                )
            )
        await session.flush()

    written = await _write_decided(db, tenant_id, rfq_id, writer=writer, now=at)
    if became_ready:
        logger.info(
            "negotiation ready to close tenant=%s rfq=%s reason=%s", tenant_id, rfq_id, ready
        )
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
    """One decided email to render and propose: the email, its gate and the writer's task."""

    row_id: UUID
    invite_id: UUID
    rfq_id: UUID
    proposal_id: UUID
    sender_user_id: UUID
    to: str
    decision: EmailDecision
    email: SupplierEmail
    gate: GateContext
    # What the writer agent sees when it may add its optional note.
    task: NegotiationEmailInput
    attempts: int
    correction_revision: JsonValue
    policy_hash: JsonValue


@dataclass(slots=True)
class _Written:
    approval_ids: list[UUID] = field(default_factory=list[UUID])
    refused: int = 0
    held: int = 0


@dataclass(frozen=True, slots=True)
class _Thread:
    """The invite's thread as the RFQ email opened it: who sends, in which language."""

    sender_user_id: UUID
    first_subject: str | None
    language: str


async def _thread(session: AsyncSession, invite: Invite, supplier: Supplier) -> _Thread | None:
    original = await _rfq_email(session, invite)
    sender_user_id = _sender(original)
    if original is None or sender_user_id is None:
        return None
    first_subject = await session.scalar(
        select(Message.subject).where(
            Message.invite_id == invite.id,
            Message.direction == "out",
            Message.message_id_header == invite.thread_message_id,
        )
    )
    # The language its RFQ email went out in (`rfqs.language.thread_language`).
    return _Thread(sender_user_id, first_subject, thread_language(original.args, supplier))


async def _due(session: AsyncSession, rfq: Rfq, row: NegotiationMessage) -> _Due | str:
    """The email of a decided row, rendered from its stored decision, or why it cannot be."""
    decision = stored_decision(row)
    if decision is None:
        return "decision_unreadable"
    invite = await session.get(Invite, row.invite_id)
    if invite is None or invite.thread_message_id is None:
        return "no_thread"
    supplier = await session.get(Supplier, invite.supplier_id)
    contact_email = await session.scalar(
        select(Contact.email).where(Contact.id == invite.contact_id)
    )
    thread = await _thread(session, invite, supplier) if supplier is not None else None
    if supplier is None or contact_email is None or thread is None:
        return "no_recipient_or_sender"
    tenant = await session.get(Tenant, rfq.tenant_id)
    user = await session.get(User, thread.sender_user_id)
    if tenant is None or user is None:
        return "no_recipient_or_sender"
    buyer_name = await buyer_display_name(session, user, tenant.name)
    requested = [
        email_line(line)
        for line in await session.scalars(
            select(RfqLine).where(RfqLine.rfq_id == rfq.id).order_by(RfqLine.position)
        )
    ]
    # Every other supplier invited to the RFQ: the note may name none of them.
    others = tuple(
        await session.scalars(
            select(Supplier.name)
            .join(Invite, Invite.supplier_id == Supplier.id)
            .where(Invite.rfq_id == rfq.id, Invite.id != invite.id)
        )
    )
    email = supplier_email(
        decision,
        header=Header(
            rfq_ref=rfq.ref,
            subject=reply_subject(thread.first_subject, rfq.ref, rfq.title),
            # The thread's language with the supplier's number format (es-MX, de-CH).
            locale=Locale.of(thread.language, country_code(supplier.country)),
            supplier_name=supplier.name,
            buyer_company=tenant.name,
            signature=Signature(name=buyer_name, company=tenant.name),
        ),
        lines=requested,
    )
    terms = cast(dict[str, JsonValue], row.terms)
    return _Due(
        row_id=row.id,
        invite_id=invite.id,
        rfq_id=rfq.id,
        proposal_id=row.proposal_id,
        sender_user_id=thread.sender_user_id,
        to=contact_email,
        decision=decision,
        email=email,
        gate=GateContext(
            thread_language=thread.language,
            first_subject=thread.first_subject,
            rfq_title=rfq.title,
            other_suppliers=others,
        ),
        task=writer_task(
            decision,
            rfq_ref=rfq.ref,
            rfq_title=rfq.title,
            supplier_name=supplier.name,
            language=email.language,
        ),
        attempts=row.attempts,
        correction_revision=terms.get("correction_revision"),
        policy_hash=terms.get("comparison_policy_hash"),
    )


async def _write_decided(
    db: Db, tenant_id: UUID, rfq_id: UUID, *, writer: NegotiationWriter, now: datetime
) -> _Written:
    """Render every decided email of the RFQ from its stored decision; propose or hold it.

    Code renders the whole email; the writer may only add a note. The send gate
    refusing the email is a code-level problem that would only recur: the email is
    held at once, with the reason codes, for the buyer. A writer failure, or a note
    that breaks a rule, only leaves the note out.
    """
    del now
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
                row.attempts += 1
                await _hold_row(session, row, built)
                result.held += 1
                continue
            due.append(built)
    sink = PostgresApprovalSink(db)
    for item in due:
        context = ToolContext(tenant_id=tenant_id, user_id=item.sender_user_id, run_id=uuid4())

        async def note(item: _Due = item, context: ToolContext = context) -> str:
            try:
                return (await writer(item.task, context)).note
            except AgentFailed:
                return ""

        composed = await compose(item.email, item.gate, note)
        if isinstance(composed, Refused):
            logger.warning(
                "negotiation email refused tenant=%s invite=%s row=%s kind=%s problems=%s",
                tenant_id,
                item.invite_id,
                item.row_id,
                item.decision.kind,
                problems_text(composed.problems),
            )
            async with tenant_session(db, tenant_id) as session:
                row = await session.scalar(
                    select(NegotiationMessage)
                    .where(NegotiationMessage.id == item.row_id)
                    .with_for_update()
                )
                if row is not None and row.status == "decided":
                    row.attempts = item.attempts + 1
                    await _record_refusal(session, row, composed.problems)
                    result.held += 1
                else:
                    # Canceled meanwhile (a newer offer): nothing to hold.
                    result.refused += 1
            continue
        if composed.note_dropped:
            logger.info(
                "negotiation note left out tenant=%s invite=%s problems=%s",
                tenant_id,
                item.invite_id,
                problems_text(composed.note_dropped),
            )
        approval_id = await _propose(db, sink, tenant_id, item, composed.rendered)
        if approval_id is not None:
            result.approval_ids.append(approval_id)
    return result


def _hold_payload(row: NegotiationMessage, rfq_id: UUID | None) -> dict[str, JsonValue]:
    return {
        "rfq_id": str(rfq_id) if rfq_id is not None else None,
        "invite_id": str(row.invite_id),
        "negotiation_message_id": str(row.id),
        "proposal_id": str(row.proposal_id),
        "action": row.action,
        "round": row.round,
        "attempts": row.attempts,
    }


async def _record_refusal(
    session: AsyncSession, row: NegotiationMessage, problems: Sequence[str]
) -> None:
    """The send gate refused the decided email: recorded once, for a person (N1's record).

    The row ends `canceled` with reason `draft_refused` and the problem codes in its
    terms (no approval: nothing was proposed); `held_reason` keeps it waiting for a
    person, so the planner never decides that offer again on its own; an
    `rfq.negotiation_draft_held` audit event puts it in the activity feed.
    """
    codes = sorted(set(problems))
    row.status = "canceled"
    row.canceled_reason = DRAFT_REFUSED
    row.held_reason = f"{_REFUSED}:{','.join(codes)}"[:300]
    row.terms = {**cast(dict[str, JsonValue], row.terms), "refused": list(codes)}
    invite = await session.get(Invite, row.invite_id)
    payload = _hold_payload(row, invite.rfq_id if invite is not None else None)
    payload["problems"] = list(codes)
    session.add(
        AuditEvent(
            tenant_id=row.tenant_id,
            actor_user_id=None,
            action="rfq.negotiation_draft_held",
            payload=payload,
        )
    )
    await session.flush()


async def _hold_row(session: AsyncSession, row: NegotiationMessage, reason: str) -> None:
    """The decided email cannot be rendered (no thread, no sender, an unreadable
    decision): it waits for the buyer, with the reason (`rfq.negotiation_held`)."""
    row.status = "held"
    row.held_reason = reason
    invite = await session.get(Invite, row.invite_id)
    payload = _hold_payload(row, invite.rfq_id if invite is not None else None)
    payload["reason"] = reason.split(":", 1)[0]
    session.add(
        AuditEvent(
            tenant_id=row.tenant_id,
            actor_user_id=None,
            action="rfq.negotiation_held",
            payload=payload,
        )
    )
    await session.flush()


async def _release_held(session: AsyncSession, rfq_id: UUID, reason: str) -> int:
    """A person released the RFQ's held emails: the planner decides those offers again.

    A held row ends `canceled` (`reason`); a refused one keeps its record (`canceled`,
    `draft_refused`) and stops waiting for a person."""
    rows = list(
        await session.scalars(
            select(NegotiationMessage)
            .join(Invite, Invite.id == NegotiationMessage.invite_id)
            .where(
                Invite.rfq_id == rfq_id,
                (NegotiationMessage.status == "held")
                | (
                    (NegotiationMessage.canceled_reason == DRAFT_REFUSED)
                    & NegotiationMessage.held_reason.is_not(None)
                ),
            )
            .with_for_update(of=NegotiationMessage)
        )
    )
    for row in rows:
        if row.status == "held":
            row.status = "canceled"
            row.canceled_reason = reason
        else:
            row.held_reason = None
    await session.flush()
    return len(rows)


async def _propose(
    db: Db,
    sink: PostgresApprovalSink,
    tenant_id: UUID,
    item: _Due,
    rendered: RenderedEmail,
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
        subject=rendered.subject,
        body=rendered.text,
        html_body=rendered.html,
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
        # The approval first: a person deciding it meanwhile waits for this row's state.
        approval = await session.scalar(
            select(Approval)
            .where(Approval.run_id == str(run_id), Approval.tool_call_id == call_id)
            .with_for_update()
        )
        if approval is None:
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
        if row is None or (row.status == "proposed" and row.approval_id == approval.id):
            return approval.id if row is not None else None
        if stale or row.status != "decided":
            if approval.status == "pending":
                approval.status = "expired"
                session.add(approval_audit(approval, "approval.expired", None))
            if row.status == "decided":
                row.status = "canceled"
                row.canceled_reason = "stale_before_proposal"
            return None
        row.attempts = item.attempts + 1
        row.approval_id = approval.id
        if approval.status in ("pending", "approved"):
            row.status = "proposed"
            return approval.id
        # Decided before the row was marked: the executor skipped it (nothing went out).
        # A rejection is the buyer's answer; anything else is decided again.
        if approval.status == "rejected":
            row.status = "rejected"
        else:
            row.status = "canceled"
            row.canceled_reason = "approval_resolved_early"
    return None


async def _settle_replan(db: Db, tenant_id: UUID, rfq_id: UUID, seen: datetime) -> datetime | None:
    """The re-plan asked at `seen` ran: clear the request, unless a reply asked again
    meanwhile. Returns that newer request (still pending), else None."""
    async with tenant_session(db, tenant_id) as session:
        cleared = await session.scalar(
            update(Rfq)
            .where(Rfq.id == rfq_id, Rfq.negotiation_replan_requested_at == seen)
            .values(negotiation_replan_requested_at=None)
            .returning(Rfq.id)
        )
        if cleared is not None:
            return None
        return await session.scalar(
            select(Rfq.negotiation_replan_requested_at).where(Rfq.id == rfq_id)
        )


@dataclass(frozen=True, slots=True)
class ReplanResult:
    # False: no re-plan was pending (another delivery, or the sweep, already ran it).
    ran: bool = False
    evaluation: EvaluationResult = field(default_factory=EvaluationResult)
    # A reply asked again while this one ran: the request still pending.
    again: datetime | None = None


async def replan_rfq(
    db: Db,
    tenant_id: UUID,
    rfq_id: UUID,
    *,
    writer: NegotiationWriter,
    now: datetime | None = None,
    history: PriceHistory = quoted_price_history,
) -> ReplanResult:
    """Plan one RFQ now because a supplier replied: the sweep's per-RFQ unit.

    Runs only while a request is pending (`negotiation_replan_requested_at`, set by
    quote extraction), so a duplicate delivery or a job the sweep already covered does
    nothing. `evaluate_rfq` takes the same locks as the sweep and never decides an offer
    twice, so running beside the sweep is safe. The request is cleared afterwards unless
    a newer reply bumped it meanwhile (`again`: the caller asks for one more run)."""
    async with tenant_session(db, tenant_id) as session:
        seen = await session.scalar(
            select(Rfq.negotiation_replan_requested_at).where(Rfq.id == rfq_id)
        )
    if seen is None:
        return ReplanResult()
    evaluation = await evaluate_rfq(db, tenant_id, rfq_id, writer=writer, now=now, history=history)
    again = await _settle_replan(db, tenant_id, rfq_id, seen)
    logger.info(
        "negotiation replanned tenant=%s rfq=%s proposed=%d held=%d again=%s",
        tenant_id,
        rfq_id,
        evaluation.proposed,
        evaluation.held,
        again is not None,
    )
    return ReplanResult(ran=True, evaluation=evaluation, again=again)


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
        rows = list(
            await session.execute(
                select(Rfq.id, Rfq.negotiation_replan_requested_at)
                .where(Rfq.status == "open", Rfq.negotiation_status.in_(_ACTIVE))
                .order_by(Rfq.created_at)
            )
        )
    result = SweepResult(canceled=canceled)
    for rfq_id, replan_requested in rows:
        evaluation = await evaluate_rfq(
            db, tenant_id, rfq_id, writer=writer, now=at, history=history
        )
        if replan_requested is not None:
            # The safety net: this run planned what a reply asked for (a lost job too).
            await _settle_replan(db, tenant_id, rfq_id, replan_requested)
        result.proposed += evaluation.proposed
        result.skipped += evaluation.skipped
        result.held += evaluation.held
        result.ready += int(evaluation.ready)
        result.closed += int(evaluation.closed)
        result.rfqs.append(rfq_id)
    if (
        result.canceled
        or result.proposed
        or result.ready
        or result.closed
        or result.skipped
        or result.held
    ):
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
