"""The negotiation planner: exactly one next action per supplier, pure code.

Bido negotiates in stages, like a disciplined buyer: clarify, evaluate, ask
for a revised offer (price) and/or a delivery date, one best-and-final request
(BAFO) with a common cut-off, then close or hand the supplier to the buyer.
One open request per supplier: nothing new goes out while an email to it waits
for approval, is being written, or waits for the supplier's answer.

`plan_supplier` reads everything known about one supplier (its invite, its
current offer with the delivery dates the lead-time helper computed, the
clarification ledger, every negotiation email decided or sent to it) plus the
buyer's limits and the deadlines, and decides ONE action:

- `wait`: nothing to do now (until a date when there is one);
- `clarify`: open questions come first (the ledger's items; the follow-up
  sweep writes that email);
- `revised_offer` / `delivery_date` / `bafo`: one email, decided here with
  every number and date (`EmailPlan`); the service persists it before any
  draft is written;
- `accept`: accepted internally, nothing to ask (no email);
- `hold`: the buyer decides or answers (`needs_decision` / `needs_answer`);
- `close`: nothing more to negotiate with this supplier.

First match wins:

1. Not open (not started, closed, RFQ not sent, declined, no reply yet).
2. An email already decided for this supplier and not sent (being written,
   awaiting approval, the daily cap, the mailbox): wait. One that could not
   be written, or that the buyer rejected, for the current offer: hold. Never
   a second email for the same offer version.
3. The offer is on hold: a supplier question (needs your answer), a withdrawal
   (close), anything else to review (needs your decision). The buyer may release
   an offer's holds (audited): it is then negotiated as it is.
4. The clarification ledger: an open clarification (to ask, pending, sent and
   unanswered) comes first.
5. Our last email is unanswered: wait for the supplier (until its reply-by
   date, or the cut-off of a BAFO; after a BAFO's cut-off the offer stands).
6. After a BAFO answer, or the supplier's own declared final offer: accept if
   everything is acceptable, else close (the final offer stands). Never a BAFO
   after the supplier's final offer.
7. Nothing to ask: accept internally when price <= target on every quoted
   line, delivery known and on time, validity known and lasting until the
   decision, no MOQ or payment deviation; otherwise the buyer decides (reason).
8. Something to ask, but the supplier did not move on anything we asked:
   stop (needs your decision, `no_progress`). Round cap reached, or the RFQ's
   final round closed: close.
9. The last allowed round, the RFQ already in its final round (a cut-off is
   set), or the deadline within `BAFO_WINDOW`: the BAFO (once, common
   cut-off; not when the cut-off leaves no time). Otherwise a revised-offer
   request (price asks, maybe with dates) or a delivery-date request (dates
   only). A request identical to the last one sent is never repeated: it
   becomes the BAFO.

Rounds count SENT emails only: a canceled or rejected email never consumes a
round, and a replacement repeats the round it replaces.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Final, Literal
from uuid import UUID

from bido_back.followups.calendar import add_business_days
from bido_back.negotiation.signals import ClarificationState, ClarificationWait
from bido_back.negotiation.strategy import (
    DeliveryAsk,
    DeliveryStatus,
    PriceAsk,
    PriceStatus,
    delivery_position,
    price_position,
    required_by,
)
from bido_back.supplier_email.deadlines import reply_by_for

NextAction = Literal[
    "wait", "clarify", "revised_offer", "delivery_date", "bafo", "accept", "hold", "close"
]
Phase = Literal[
    "not_started",
    "waiting_for_supplier",
    "waiting",
    "held_for_approval",
    "accepted_internally",
    "needs_decision",
    "needs_answer",
    "closed",
]
EmailKind = Literal["revised_offer", "delivery_date", "bafo"]
EmailState = Literal["decided", "proposed", "sent", "canceled", "rejected", "held"]
EmailWaitReason = Literal[
    "preparing", "approval", "daily_cap", "mailbox", "mandate_hold", "mandate_paused"
]
InviteStatus = Literal["open", "countered", "bafo_requested", "accepted", "rejected", "closed"]
LineStatus = Literal["not_quoted", "declined", "blocked", "question_open", "priced"]

# A BAFO is due when the negotiation deadline is this close.
BAFO_WINDOW: Final = timedelta(hours=24)
# The common cut-off: this many business days after the first BAFO, never after the deadline.
BAFO_REPLY_BUSINESS_DAYS: Final = 2
# No BAFO when its cut-off is closer than this: the supplier's offer stands.
MIN_BAFO_NOTICE: Final = timedelta(hours=12)
# Emails decided before typed decisions (0036) were answered within this many business
# days; new requests follow the email standard (`reply_by_date`).
REPLY_BUSINESS_DAYS: Final = 2

EMAIL_ACTIONS: Final = frozenset({"revised_offer", "delivery_date", "bafo"})


# --- Inputs --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LineTerms:
    """The buyer's side of one RFQ line (base currency)."""

    rfq_line_id: UUID
    position: int
    quantity: Decimal
    need_by: date | None = None
    target: Decimal | None = None  # disclosed to suppliers
    max_price: Decimal | None = None  # never disclosed
    # A lead-time limit the buyer set (days). A limit derived from the need-by date
    # is not passed: the need-by date itself is compared, at every evaluation.
    max_lead_days: int | None = None
    history_price: Decimal | None = None  # internal reference only, never disclosed


@dataclass(frozen=True, slots=True)
class LineQuote:
    """The supplier's current offer for one line (first tier, base currency)."""

    rfq_line_id: UUID
    unit_price: Decimal | None
    # Decimals an ask on this line may use (currency minor unit, or finer as quoted).
    places: int = 2
    lead_time_days: int | None = None
    # Expected delivery when ordered on the order date (the lead-time helper).
    delivery: date | None = None
    delivery_exact: bool = True
    # Expected delivery when ordered on the RFQ's reply-by date (feasibility warning).
    delivery_at_reply_by: date | None = None
    latest_order_date: date | None = None
    moq: Decimal | None = None
    # Why the offer cannot be negotiated as is (alternative part, review, ...).
    blockers: tuple[str, ...] = ()
    # Another supplier's eligible offer is lower (we may say so, never the number).
    better_offer_exists: bool = False
    # (order date, expected delivery) for the order dates of earlier emails' decisions:
    # whether the supplier moved on a date is judged from the same order date.
    deliveries_then: tuple[tuple[date, date], ...] = ()

    def delivery_on(self, order_date: date | None) -> date | None:
        """The expected delivery if ordered on `order_date` (None: not computed)."""
        return dict(self.deliveries_then).get(order_date) if order_date is not None else None


@dataclass(frozen=True, slots=True)
class Offer:
    """The supplier's current offer (the latest proposal version)."""

    proposal_id: UUID
    version: int
    lines: Mapping[UUID, LineQuote]
    valid_until: date | None = None
    received_on: date | None = None
    holds: tuple[str, ...] = ()
    questions: int = 0
    declared_final: bool = False
    declined_lines: frozenset[UUID] = frozenset()
    # The offer pays sooner than the RFQ's payment terms ask.
    payment_terms_shorter: bool = False
    # The buyer released this offer's holds: negotiate it as it is (a withdrawal still
    # closes it).
    holds_released: bool = False


@dataclass(frozen=True, slots=True)
class LineAsk:
    """What one email asked about one line, and the supplier's numbers it answered."""

    rfq_line_id: UUID
    ask_price: Decimal | None = None
    best_price: bool = False
    deliver_by: date | None = None
    offered_price: Decimal | None = None
    offered_delivery: date | None = None


@dataclass(frozen=True, slots=True)
class EmailWait:
    reason: EmailWaitReason
    until: datetime | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class EmailRecord:
    """One negotiation email decided for this supplier (any state)."""

    sequence: int
    round: int
    kind: EmailKind
    state: EmailState
    proposal_id: UUID
    proposal_version: int
    asks: tuple[LineAsk, ...] = ()
    reply_by: date | None = None
    cutoff_at: datetime | None = None
    held_reason: str | None = None
    wait: EmailWait | None = None
    # The order date the email's delivery dates were computed from.
    order_date: date | None = None


@dataclass(frozen=True, slots=True)
class SupplierFacts:
    invite_status: str  # draft, pending_approval, sent, responded, declined
    offer: Offer | None = None
    emails: tuple[EmailRecord, ...] = ()
    clarification: ClarificationState = field(default_factory=ClarificationState)
    # The RFQ email awaiting approval or a connected mailbox (pending_approval invites).
    rfq_email_wait: EmailWait | None = None
    chases_sent: int = 0
    chases_max: int = 0
    chases_waiting: bool = False


@dataclass(frozen=True, slots=True)
class Context:
    now: datetime
    order_date: date
    deadline: datetime | None
    max_rounds: int
    cutoff_at: datetime | None = None  # the RFQ's common BAFO cut-off, once set
    response_due: date | None = None
    negotiation: Literal["not_started", "open", "ready_to_close", "closed"] = "open"
    country: str | None = None  # the supplier's, for business days


# --- Outputs -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LinePlan:
    rfq_line_id: UUID
    position: int
    status: LineStatus
    price: PriceAsk | None = None
    delivery: DeliveryAsk | None = None
    offered_price: Decimal | None = None
    acceptable: bool = False
    reasons: tuple[str, ...] = ()
    latest_order_date: date | None = None

    @property
    def price_status(self) -> PriceStatus | None:
        return self.price.status if self.price is not None else None

    @property
    def delivery_status(self) -> DeliveryStatus | None:
        return self.delivery.status if self.delivery is not None else None

    @property
    def asks(self) -> bool:
        return (self.price is not None and self.price.asks) or (
            self.delivery is not None and self.delivery.deliver_by is not None
        )


@dataclass(frozen=True, slots=True)
class PositionPlan:
    """What one email asks for one line (every value decided here)."""

    rfq_line_id: UUID
    ask_price: Decimal | None
    best_price: bool
    deliver_by: date | None
    offered_price: Decimal | None
    lead_time_days: int | None
    offered_delivery: date | None
    delivery_exact: bool
    better_offer_exists: bool
    conceded: bool


@dataclass(frozen=True, slots=True)
class EmailPlan:
    kind: EmailKind
    round: int
    reply_by: date
    cutoff_at: datetime | None
    order_date: date
    positions: tuple[PositionPlan, ...]
    mention_competition: bool
    # This BAFO sets the RFQ's common cut-off (the first one).
    sets_cutoff: bool = False


@dataclass(frozen=True, slots=True)
class PlanWarning:
    code: str
    position: int | None = None
    on: date | None = None


@dataclass(frozen=True, slots=True)
class SupplierPlan:
    action: NextAction
    phase: Phase
    reason: str
    until: datetime | None = None
    lines: tuple[LinePlan, ...] = ()
    email: EmailPlan | None = None
    status: InviteStatus = "open"
    warnings: tuple[PlanWarning, ...] = ()
    # Accepted internally: order by this date to meet every need-by, valid until.
    order_by: date | None = None
    valid_until: date | None = None
    detail: str | None = None
    # The offer itself is on hold (a question, a withdrawal, something to review) and the
    # buyer has not released it: none of its lines counts as accepted.
    offer_held: bool = False

    @property
    def sends_email(self) -> bool:
        return self.email is not None


# --- Dates ---------------------------------------------------------------------------------


def end_of_day(day: date) -> datetime:
    return datetime.combine(day, time(23, 59), tzinfo=UTC)


def reply_by_date(
    now: datetime,
    deadline: datetime | None,
    country: str | None,
    kind: EmailKind = "delivery_date",
) -> date:
    """A request's reply-by as the email standard sets it (`supplier_email.deadlines`): a
    revised-offer request +3 business days, a delivery-date request +2, in the supplier's
    country; never after the deadline."""
    found = reply_by_for(kind, now=now, country=country, deadline=deadline)
    if found is not None:
        return found.day
    due = add_business_days(now, REPLY_BUSINESS_DAYS, country).date()
    if deadline is not None:
        due = min(due, deadline.date())
    return max(due, now.date())


def bafo_cutoff(now: datetime, deadline: datetime | None, country: str | None = None) -> datetime:
    """The common BAFO cut-off: end of the day `BAFO_REPLY_BUSINESS_DAYS` business days
    after the first BAFO, never after the negotiation deadline."""
    cutoff = end_of_day(add_business_days(now, BAFO_REPLY_BUSINESS_DAYS, country).date())
    return min(cutoff, deadline) if deadline is not None else cutoff


# --- Lines ---------------------------------------------------------------------------------


def _last_sent_ask(emails: Sequence[EmailRecord], line_id: UUID) -> LineAsk | None:
    """Our latest price ask on a line among the emails that reached the supplier."""
    for email in sorted(emails, key=lambda e: e.sequence, reverse=True):
        if email.state != "sent":
            continue
        for ask in email.asks:
            if ask.rfq_line_id == line_id and (ask.ask_price is not None or ask.best_price):
                return ask
    return None


def _date_asked(emails: Sequence[EmailRecord], line_id: UUID) -> bool:
    return any(
        ask.rfq_line_id == line_id and ask.deliver_by is not None
        for email in emails
        if email.state == "sent"
        for ask in email.asks
    )


def plan_line(
    terms: LineTerms,
    offer: Offer,
    emails: Sequence[EmailRecord],
    clarification: ClarificationState,
    ctx: Context,
    *,
    final_round: bool,
) -> LinePlan:
    """Where one line of the supplier's current offer stands, and what we would ask."""
    quote = offer.lines.get(terms.rfq_line_id)
    if terms.rfq_line_id in offer.declined_lines:
        return LinePlan(terms.rfq_line_id, terms.position, "declined", reasons=("declined",))
    if quote is None or quote.unit_price is None:
        return LinePlan(terms.rfq_line_id, terms.position, "not_quoted", reasons=("not_quoted",))
    offered = quote.unit_price
    if quote.blockers:
        return LinePlan(
            terms.rfq_line_id,
            terms.position,
            "blocked",
            offered_price=offered,
            reasons=quote.blockers,
        )
    if terms.rfq_line_id in clarification.lines:
        return LinePlan(
            terms.rfq_line_id,
            terms.position,
            "question_open",
            offered_price=offered,
            reasons=("clarification_open",),
        )
    previous = _last_sent_ask(emails, terms.rfq_line_id)
    price = price_position(
        offered=offered,
        target=terms.target,
        max_price=terms.max_price,
        places=quote.places,
        history=terms.history_price,
        previous_ask=previous.ask_price if previous is not None else None,
        previous_offer=previous.offered_price if previous is not None else None,
        final_round=final_round,
    )
    required = required_by(terms.need_by, terms.max_lead_days, ctx.order_date)
    delivery = delivery_position(
        required=required,
        expected=quote.delivery,
        order_date=ctx.order_date,
        asked_before=_date_asked(emails, terms.rfq_line_id),
    )
    reasons: list[str] = []
    if price.status in ("in_range", "above_max", "no_target"):
        reasons.append(f"price_{price.status}")
    elif price.status == "ask_met":
        reasons.append("price_meets_last_ask")
    if not delivery.acceptable:
        reasons.append(f"delivery_{delivery.status}")
    if quote.moq is not None and quote.moq > terms.quantity:
        reasons.append("moq_above_quantity")
    if terms.rfq_line_id in clarification.exhausted_lines:
        reasons.append("clarification_unanswered")
    acceptable = (
        price.status == "in_target"
        and delivery.acceptable
        and "moq_above_quantity" not in reasons
        and "clarification_unanswered" not in reasons
    )
    return LinePlan(
        terms.rfq_line_id,
        terms.position,
        "priced",
        price=price,
        delivery=delivery,
        offered_price=offered,
        acceptable=acceptable,
        reasons=tuple(reasons),
        latest_order_date=quote.latest_order_date,
    )


def _warnings(
    lines: Sequence[LineTerms], offer: Offer, ctx: Context, plans: Sequence[LinePlan]
) -> list[PlanWarning]:
    found: list[PlanWarning] = []
    by_id = {line.rfq_line_id: line for line in lines}
    for plan in plans:
        terms = by_id[plan.rfq_line_id]
        quote = offer.lines.get(plan.rfq_line_id)
        if plan.status != "priced" or quote is None:
            continue
        if (
            terms.need_by is not None
            and ctx.response_due is not None
            and ctx.response_due > ctx.order_date
            and quote.delivery_at_reply_by is not None
            and quote.delivery_at_reply_by > terms.need_by
        ):
            # Waiting for every quote (the reply-by date) before ordering makes it late.
            found.append(
                PlanWarning("reply_by_misses_need_by", plan.position, quote.delivery_at_reply_by)
            )
        decided_by = ctx.cutoff_at or ctx.deadline
        if (
            plan.latest_order_date is not None
            and decided_by is not None
            and plan.latest_order_date < decided_by.date()
        ):
            # Waiting for the end of the negotiation makes this line late: decide earlier.
            found.append(
                PlanWarning("order_by_before_decision", plan.position, plan.latest_order_date)
            )
        if "moq_above_quantity" in plan.reasons:
            found.append(PlanWarning("moq_above_quantity", plan.position))
        if plan.delivery is not None and plan.delivery.status == "need_by_unreachable":
            found.append(PlanWarning("need_by_unreachable", plan.position, plan.delivery.expected))
        if quote.delivery is not None and not quote.delivery_exact:
            found.append(PlanWarning("delivery_estimated", plan.position, quote.delivery))
    if offer.valid_until is None:
        found.append(PlanWarning("validity_unknown"))
    elif ctx.deadline is not None and offer.valid_until < ctx.deadline.date():
        found.append(PlanWarning("validity_ends_before_decision", on=offer.valid_until))
    if offer.payment_terms_shorter:
        found.append(PlanWarning("payment_terms_shorter"))
    not_quoted = [p.position for p in plans if p.status == "not_quoted"]
    for position in not_quoted:
        found.append(PlanWarning("line_not_quoted", position))
    return found


# --- The supplier ---------------------------------------------------------------------------


def _wait_phase(wait: EmailWait | None) -> tuple[Phase, str, datetime | None]:
    if wait is None or wait.reason == "approval":
        return "held_for_approval", "email_awaiting_approval", None
    if wait.reason == "mandate_hold":
        return "held_for_approval", f"held_by_mandate:{wait.detail or 'out_of_bounds'}", None
    if wait.reason == "mandate_paused":
        return "held_for_approval", "mandate_paused", None
    if wait.reason == "daily_cap":
        return "waiting", "daily_limit", wait.until
    if wait.reason == "mailbox":
        return "waiting", "mailbox_not_connected", None
    return "waiting", "preparing_email", None


def _clarification_phase(
    wait: ClarificationWait | None, until: datetime | None
) -> tuple[Phase, str, datetime | None]:
    if wait is None or wait == "approval":
        return "held_for_approval", "clarification_awaiting_approval", None
    if wait == "mandate_hold":
        return "held_for_approval", "clarification_held_by_mandate", None
    if wait == "mandate_paused":
        return "held_for_approval", "mandate_paused", None
    if wait == "daily_cap":
        return "waiting", "clarification_daily_limit", until
    return "waiting", "mailbox_not_connected", None


def _moved(offer: Offer, last: EmailRecord) -> bool:
    """Whether the supplier improved on anything our last email asked for.

    A price is compared with the price the supplier had then; a delivery with the date
    the supplier offered then, both counted from the order date of that email (a lead
    time cut from 20 to 17 days is a move, whatever day it arrives).
    """
    for ask in last.asks:
        quote = offer.lines.get(ask.rfq_line_id)
        if quote is None or quote.unit_price is None:
            continue
        if (
            (ask.ask_price is not None or ask.best_price)
            and ask.offered_price is not None
            and quote.unit_price < ask.offered_price
        ):
            return True
        if ask.deliver_by is not None and quote.delivery is not None:
            if quote.delivery <= ask.deliver_by:
                return True
            then = quote.delivery_on(last.order_date) or quote.delivery
            if ask.offered_delivery is not None and then < ask.offered_delivery:
                return True
    return False


def _verdict(plans: Sequence[LinePlan]) -> InviteStatus:
    priced = [p for p in plans if p.status == "priced"]
    if priced and all(p.price_status == "above_max" for p in priced):
        return "rejected"
    return "closed"


def _first_reason(plans: Sequence[LinePlan], warnings: Sequence[PlanWarning]) -> str:
    """Why an offer with nothing to ask is not accepted internally (most important first)."""
    priority = (
        "delivery_need_by_unreachable",
        "delivery_still_late",
        "delivery_unknown",
        "clarification_unanswered",
        "price_meets_last_ask",
        "moq_above_quantity",
    )
    reasons = {r for p in plans for r in p.reasons}
    for code in priority:
        if code in reasons:
            return code
    codes = {w.code for w in warnings}
    for code in ("validity_unknown", "validity_ends_before_decision", "payment_terms_shorter"):
        if code in codes:
            return code
    if any(p.status == "blocked" for p in plans):
        return "offer_blocked"
    return "needs_review"


def _accept_blockers(warnings: Sequence[PlanWarning]) -> bool:
    return any(
        w.code in ("validity_unknown", "validity_ends_before_decision", "payment_terms_shorter")
        for w in warnings
    )


def plan_supplier(
    lines: Sequence[LineTerms], supplier: SupplierFacts, ctx: Context
) -> SupplierPlan:
    """The one next action for one supplier (see the module docstring)."""
    if ctx.negotiation == "closed":
        return SupplierPlan("close", "closed", "negotiation_closed", status="closed")
    status = supplier.invite_status
    if status == "draft":
        return SupplierPlan("wait", "not_started", "rfq_not_sent")
    if status == "pending_approval":
        phase, reason, until = _wait_phase(supplier.rfq_email_wait)
        return SupplierPlan("wait", phase, f"rfq_email:{reason}", until)
    if status == "declined":
        return SupplierPlan("close", "closed", "supplier_declined", status="rejected")
    offer = supplier.offer
    if offer is None:
        if status == "responded":
            return SupplierPlan("hold", "needs_answer", "reply_without_offer")
        until = end_of_day(ctx.response_due) if ctx.response_due is not None else ctx.deadline
        if until is not None and until < ctx.now:
            until = ctx.deadline
        reason = "awaiting_quote"
        if supplier.chases_max and supplier.chases_sent >= supplier.chases_max:
            reason = "awaiting_quote_reminders_exhausted"
        elif ctx.response_due is not None and ctx.response_due < ctx.now.date():
            reason = "quote_overdue"
        return SupplierPlan("wait", "waiting_for_supplier", reason, until)
    if ctx.negotiation == "not_started":
        return SupplierPlan("wait", "not_started", "negotiation_not_started")

    emails = supplier.emails
    sent = sorted((e for e in emails if e.state == "sent"), key=lambda e: e.sequence)
    counted = len(sent)
    last = sent[-1] if sent else None
    bafo_sent = any(e.kind == "bafo" for e in sent)
    final_round = (
        counted + 1 >= ctx.max_rounds
        or ctx.cutoff_at is not None
        or (ctx.deadline is not None and ctx.deadline - ctx.now <= BAFO_WINDOW)
    )
    plans = tuple(
        plan_line(t, offer, emails, supplier.clarification, ctx, final_round=final_round)
        for t in lines
    )
    warnings = tuple(_warnings(lines, offer, ctx, plans))
    base = SupplierPlan(
        "wait",
        "waiting",
        "",
        lines=plans,
        warnings=warnings,
        status="bafo_requested" if bafo_sent else "countered" if sent else "open",
        valid_until=offer.valid_until,
    )

    # 2. An email already decided and not sent; never a second one for the same offer.
    live = [e for e in emails if e.state in ("decided", "proposed")]
    if live:
        email = max(live, key=lambda e: e.sequence)
        if email.state == "decided":
            return replace(base, phase="waiting", reason="preparing_email")
        phase, reason, until = _wait_phase(email.wait)
        return replace(base, phase=phase, reason=reason, until=until)
    current = [e for e in emails if e.proposal_id == offer.proposal_id]
    if any(e.state == "held" for e in current):
        held = next(e for e in current if e.state == "held")
        return replace(
            base,
            action="hold",
            phase="needs_decision",
            reason="email_not_prepared",
            detail=held.held_reason,
        )
    if any(e.state == "rejected" for e in current):
        return replace(base, action="hold", phase="needs_decision", reason="email_rejected")

    # 3. The offer is on hold (unless the buyer released it: negotiate it as it is).
    if "offer_withdrawn" in offer.holds:
        return replace(
            base,
            action="close",
            phase="closed",
            reason="offer_withdrawn",
            status="closed",
            offer_held=True,
        )
    if not offer.holds_released:
        if offer.questions or (
            "supplier_clarification_requires_buyer" in offer.holds
            and "offer_state_requires_review" not in offer.holds
        ):
            return replace(
                base,
                action="hold",
                phase="needs_answer",
                reason="supplier_question",
                offer_held=True,
            )
        if offer.holds:
            review = "offer_state_requires_review"
            reason = review if review in offer.holds else offer.holds[0]
            return replace(
                base, action="hold", phase="needs_decision", reason=reason, offer_held=True
            )

    # 4. Open questions first.
    clarification = supplier.clarification
    if clarification.pending:
        phase, reason, until = _clarification_phase(
            clarification.pending_wait, clarification.pending_until
        )
        return replace(base, phase=phase, reason=reason, until=until)
    if clarification.awaiting:
        return replace(
            base,
            phase="waiting_for_supplier",
            reason="clarification_sent",
            until=clarification.awaiting_until,
        )
    if clarification.to_ask:
        return replace(base, action="clarify", phase="waiting", reason="clarification_due")

    # 5. Our last email is not answered yet.
    if last is not None and offer.version <= last.proposal_version:
        if last.kind == "bafo":
            cutoff = last.cutoff_at or ctx.cutoff_at
            if cutoff is not None and ctx.now < cutoff:
                return replace(base, phase="waiting_for_supplier", reason="bafo_sent", until=cutoff)
            return _final(base, plans, warnings, "final_round_ended")
        due = end_of_day(last.reply_by) if last.reply_by is not None else None
        if due is not None and ctx.now <= due:
            return replace(
                base, phase="waiting_for_supplier", reason=f"{last.kind}_sent", until=due
            )
        return replace(
            base, phase="waiting_for_supplier", reason="reply_overdue", until=ctx.deadline
        )

    quoted = [p for p in plans if p.status in ("priced", "blocked", "question_open")]
    if not quoted:
        if plans and all(p.status == "declined" for p in plans):
            return replace(
                base, action="close", phase="closed", reason="supplier_declined", status="rejected"
            )
        return replace(base, action="hold", phase="needs_decision", reason="no_priced_line")
    priced = [p for p in plans if p.status == "priced"]
    if not priced:
        return replace(base, action="hold", phase="needs_decision", reason="offer_blocked")
    accept = (
        all(p.acceptable for p in priced)
        and not any(p.status == "blocked" for p in plans)
        and not _accept_blockers(warnings)
    )
    order_by = min(
        (p.latest_order_date for p in priced if p.latest_order_date is not None), default=None
    )

    # 6. Final: after a BAFO answer, or the supplier's own final offer.
    if bafo_sent or offer.declared_final:
        if accept:
            return replace(
                base,
                action="accept",
                phase="accepted_internally",
                reason="final_offer_in_target",
                status="accepted",
                order_by=order_by,
            )
        reason = "bafo_answered" if bafo_sent else "supplier_declared_final"
        return _final(base, plans, warnings, reason)

    asking = [p for p in priced if p.asks]
    # 7. Nothing to ask.
    if not asking:
        if accept:
            return replace(
                base,
                action="accept",
                phase="accepted_internally",
                reason="nothing_to_ask",
                status="accepted",
                order_by=order_by,
            )
        # Only the price stands between this offer and an acceptance, and the supplier
        # met what we last asked: nothing more to ask; the buyer decides on the above-
        # target price at award time.
        met_only = (
            any(p.price_status == "ask_met" for p in priced)
            and all(
                p.price_status in ("in_target", "ask_met")
                and p.delivery is not None
                and p.delivery.acceptable
                and not {"moq_above_quantity", "clarification_unanswered"} & set(p.reasons)
                for p in priced
            )
            and not any(p.status == "blocked" for p in plans)
            and not _accept_blockers(warnings)
        )
        if met_only:
            return _final(base, plans, warnings, "offer_meets_last_ask")
        return replace(
            base,
            action="hold",
            phase="needs_decision",
            reason=_first_reason(plans, warnings),
            status="closed" if sent else "open",
        )

    # 8. Stop rules.
    if last is not None and not _moved(offer, last):
        return replace(
            base, action="hold", phase="needs_decision", reason="no_progress", status="closed"
        )
    if counted >= ctx.max_rounds:
        return _final(base, plans, warnings, "round_limit_reached")
    if ctx.cutoff_at is not None and ctx.now >= ctx.cutoff_at:
        return _final(base, plans, warnings, "final_round_closed")

    # 9. The next email.
    positions = tuple(_position(p, offer) for p in asking)
    kind: EmailKind = (
        "revised_offer"
        if any(p.ask_price is not None or p.best_price for p in positions)
        else "delivery_date"
    )
    bafo = final_round or (last is not None and _same_asks(positions, last))
    if bafo:
        cutoff = ctx.cutoff_at or bafo_cutoff(ctx.now, ctx.deadline, ctx.country)
        if cutoff - ctx.now < MIN_BAFO_NOTICE:
            return _final(base, plans, warnings, "no_time_for_final_round")
        if not final_round:
            # A repeated request becomes the final round: the asks move to its rules.
            plans = tuple(
                plan_line(t, offer, emails, clarification, ctx, final_round=True) for t in lines
            )
            positions = tuple(_position(p, offer) for p in plans if p.status == "priced" and p.asks)
        email = EmailPlan(
            kind="bafo",
            round=counted + 1,
            reply_by=cutoff.date(),
            cutoff_at=cutoff,
            order_date=ctx.order_date,
            positions=positions,
            mention_competition=any(p.better_offer_exists for p in positions),
            sets_cutoff=ctx.cutoff_at is None,
        )
        return replace(
            base, action="bafo", phase="waiting", reason="bafo_due", email=email, lines=plans
        )
    email = EmailPlan(
        kind=kind,
        round=counted + 1,
        reply_by=reply_by_date(ctx.now, ctx.deadline, ctx.country, kind),
        cutoff_at=None,
        order_date=ctx.order_date,
        positions=positions,
        mention_competition=any(p.better_offer_exists for p in positions),
    )
    return replace(base, action=kind, phase="waiting", reason=f"{kind}_due", email=email)


def _final(
    base: SupplierPlan, plans: Sequence[LinePlan], warnings: Sequence[PlanWarning], reason: str
) -> SupplierPlan:
    del warnings
    return replace(base, action="close", phase="closed", reason=reason, status=_verdict(plans))


def _position(plan: LinePlan, offer: Offer) -> PositionPlan:
    quote = offer.lines[plan.rfq_line_id]
    price = plan.price
    delivery = plan.delivery
    asks_price = price is not None and price.asks
    return PositionPlan(
        rfq_line_id=plan.rfq_line_id,
        ask_price=price.ask if price is not None else None,
        best_price=price.best_price if price is not None else False,
        deliver_by=delivery.deliver_by if delivery is not None else None,
        # Unrounded: what the next offer is compared with (the email shows the quote as
        # the supplier wrote it, see `decision.QuotedTerms`).
        offered_price=plan.offered_price,
        lead_time_days=quote.lead_time_days,
        offered_delivery=quote.delivery,
        delivery_exact=quote.delivery_exact,
        better_offer_exists=quote.better_offer_exists and asks_price,
        conceded=price.conceded if price is not None else False,
    )


def _same_asks(positions: Sequence[PositionPlan], last: EmailRecord) -> bool:
    """Whether these asks repeat exactly what the last sent email asked."""
    if last.kind == "bafo":
        return False
    ours = {(p.rfq_line_id, p.ask_price, p.best_price, p.deliver_by) for p in positions}
    theirs = {(a.rfq_line_id, a.ask_price, a.best_price, a.deliver_by) for a in last.asks}
    return ours == theirs


# --- The RFQ --------------------------------------------------------------------------------


def ready_to_close(
    *, line_ids: Sequence[UUID], waiting_suppliers: int, plans: Sequence[SupplierPlan]
) -> bool:
    """Every invited supplier answered and every line has an offer accepted internally
    (in target, on time, validity known, no open question) from a supplier not on hold."""
    if waiting_suppliers > 0 or not line_ids:
        return False
    accepted: set[UUID] = set()
    for plan in plans:
        if (
            plan.offer_held
            or plan.phase in ("needs_answer", "held_for_approval")
            or plan.action == "clarify"
        ):
            continue
        if plan.reason in ("clarification_sent", "clarification_due") or _accept_blockers(
            plan.warnings
        ):
            continue
        accepted |= {line.rfq_line_id for line in plan.lines if line.acceptable}
    return all(line_id in accepted for line_id in line_ids)
