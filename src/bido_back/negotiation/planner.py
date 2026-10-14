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
- `clarify`: the ledger's open items go out alone (nothing to negotiate yet,
  or no room for it: the follow-up sweep writes that email);
- `revised_offer` / `delivery_date` / `bafo`: one email, decided here with
  every number and date (`EmailPlan`); the service persists it before the
  email is rendered. When the supplier's answer also left open items, the
  same email asks them first (`EmailPlan.clarify`): one email per answer;
- `reminder`: the one reminder of an unanswered revised-offer or delivery-date
  request (the same request, a new reply-by date);
- `accept`: accepted internally, nothing to ask (no email);
- `hold`: the buyer decides or answers (`needs_decision` / `needs_answer`);
- `close`: nothing more to negotiate with this supplier.

First match wins:

1. Not open (not started, closed, RFQ not sent, declined, no reply yet). A reply
   without an offer waits while Bido is still reading it (`reading_reply`); only a
   reply read without an offer (a question, a request) needs the buyer's answer.
2. An email already decided for this supplier and not sent (being written,
   awaiting approval, the daily cap, the mailbox): wait. One that could not
   be written, or that the buyer rejected, for the current offer: hold. Never
   a second email for the same offer version.
3. The offer is on hold: a supplier question (needs your answer), a withdrawal
   (close), anything else to review (needs your decision). The buyer may release
   an offer's holds (audited): it is then negotiated as it is.
4. The clarification ledger: a clarification waiting for approval or for its
   answer comes first (one open request per supplier). Items still to ask go
   out in the next email: in the email steps 5–9 decide, asked first, when
   there is one (one email per supplier answer: the blocking questions first,
   then the negotiation's asks, at most `MAX_ASKS` numbered asks; a price ask
   that does not fit waits for the next email and the decision says so);
   otherwise alone (`clarify`), unless our last request is still unanswered
   (they wait for its answer). Lines the open items concern are negotiated
   once they are answered.
5. Our last email is unanswered: wait for the supplier (until its reply-by
   date, or the cut-off of a BAFO; after a BAFO's cut-off the offer stands). A
   revised-offer or delivery-date request gets ONE reminder after its reply-by
   date (the same request again, with a new reply-by date; never after the
   negotiation deadline, never for a BAFO); unanswered after it, the offer
   stands (closed).
6. After a BAFO answer, or the supplier's own declared final offer: accept if
   everything is acceptable, else close (the final offer stands). Never a BAFO
   after the supplier's final offer.
7. Nothing to ask: accept internally when price <= target on every quoted
   line, delivery known and on time, validity known and lasting until the
   decision, no MOQ or payment deviation, and the supplier's order deadline
   (`Offer.order_by`) not passed; otherwise the buyer decides (reason). An
   accepted offer's `order_by` is the earliest of the lines' latest order dates
   and the supplier's order deadline. An offer that would be accepted but whose
   one-off charges put a line's effective price above the maximum is the
   supplier's last word for the buyer's decision (`effective_above_max`), never
   accepted internally.
8. Something to ask, but the supplier did not move on anything we asked:
   stop (needs your decision, `no_progress`); a price ask our last email left
   for later (its questions came first) is no repeat. Round cap reached, or
   the RFQ's final round closed: close.
9. The last allowed round, the RFQ already in its final round (a cut-off is
   set), or the deadline within `BAFO_WINDOW`: the BAFO (once, common
   cut-off; not when the cut-off leaves no time). Otherwise a revised-offer
   request (price asks, maybe with dates) or a delivery-date request (dates
   only). A request identical to the last one sent is never repeated: it
   becomes the BAFO.

Rounds count SENT requests only: a canceled or rejected email never consumes a
round, a replacement repeats the round it replaces, and a reminder is no round.

Prices: the buyer's target and maximum are UNIT prices, compared with the
supplier's offered unit price (`UnitPrice`), never with the effective price
(`EffectivePrice`: the unit price plus one-off charges such as tooling spread over
the quantity). The emails ask for a unit price, so what is asked and what is judged
are the same number. When the unit price is within the maximum but the effective
price is above it, the line says so (`effective_above_max`, a reason and a
warning): the buyer decides knowingly; it is neither accepted internally nor
rejected.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Final, Literal
from uuid import UUID

from bido_back.followups.calendar import add_business_days
from bido_back.negotiation.signals import (
    OFFER_ITEM_FIELDS,
    ClarificationItem,
    ClarificationState,
    ClarificationWait,
)
from bido_back.negotiation.strategy import (
    DeliveryAsk,
    DeliveryStatus,
    EffectivePrice,
    PriceAsk,
    PriceStatus,
    UnitPrice,
    delivery_position,
    price_position,
    required_by,
)
from bido_back.supplier_email.deadlines import reply_by_for
from bido_back.supplier_email.model import MAX_ASKS

NextAction = Literal[
    "wait",
    "clarify",
    "revised_offer",
    "delivery_date",
    "bafo",
    "reminder",
    "accept",
    "hold",
    "close",
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
# Emails decided before typed decisions (0037) were answered within this many business
# days; new requests follow the email standard (`reply_by_date`).
REPLY_BUSINESS_DAYS: Final = 2

EMAIL_ACTIONS: Final = frozenset({"revised_offer", "delivery_date", "bafo", "reminder"})


# --- Inputs --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LineTerms:
    """The buyer's side of one RFQ line (base currency)."""

    rfq_line_id: UUID
    position: int
    quantity: Decimal
    need_by: date | None = None
    # Unit prices (per piece), compared with the offered unit price only.
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
    # The offered price per unit: what the target and the maximum apply to.
    unit_price: UnitPrice | None
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
    # The unit price plus one-off charges (tooling) spread over the quantity: shown, and
    # checked against the maximum only to warn (`effective_above_max`). None: no one-off
    # charge known, or not computable.
    effective_price: EffectivePrice | None = None

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
    # The supplier's order deadline: its offer (delivery promise) holds only for an
    # order on or before this day ("provided we receive the PO by 21/10/2026").
    order_by: date | None = None
    # Why the offer is held for review (the offer state's reason codes) and which
    # commercial terms the held reply changed (`pos1.unit`, `currency`): codes only.
    hold_reasons: tuple[str, ...] = ()
    held_fields: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LineAsk:
    """What one email asked about one line, and the supplier's numbers it answered."""

    rfq_line_id: UUID
    ask_price: Decimal | None = None
    best_price: bool = False
    deliver_by: date | None = None
    # The supplier's unit price when the email was decided.
    offered_unit_price: UnitPrice | None = None
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
    # A reminder: the sequence of the request it repeats (no round of its own).
    reminder_of: int | None = None
    # It was decided as a typed decision (a reminder repeats one; not older emails).
    typed: bool = True
    # Lines whose price ask did not fit in it (its questions came first): never asked by it.
    deferred: frozenset[UUID] = frozenset()


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
    # A reply from the supplier is stored and not read yet (quote extraction is running
    # or queued): whether it holds an offer is not known yet.
    reply_unread: bool = False


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
    # What the price status judges: the offered unit price.
    offered_unit_price: UnitPrice | None = None
    acceptable: bool = False
    reasons: tuple[str, ...] = ()
    latest_order_date: date | None = None
    # The unit price plus one-off charges per unit (shown, never judged against the
    # target), and whether it is above the maximum while the unit price is within it.
    effective_price: EffectivePrice | None = None
    effective_above_max: bool = False

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
    offered_unit_price: UnitPrice | None
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
    # A reminder of the request with this sequence: the same email, a new reply-by date.
    reminder_of: int | None = None
    # The clarification ledger's items this email asks too, before its own asks (one email
    # per supplier answer).
    clarify: tuple[ClarificationItem, ...] = ()
    # Lines whose price ask did not fit in this email (at most `MAX_ASKS` asks, the
    # questions first): the next email asks them.
    deferred: tuple[UUID, ...] = ()


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
    # For an offer held for review: why (the offer state's reason codes) and which
    # commercial terms the held reply changed (`pos1.unit`, `currency`).
    hold_reasons: tuple[str, ...] = ()
    held_fields: tuple[str, ...] = ()
    # Items of the clarification ledger are open for this supplier (to ask, waiting for
    # approval, or asked and unanswered): none of its lines counts as accepted yet.
    questions_open: bool = False

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
            offered_unit_price=offered,
            effective_price=quote.effective_price,
            reasons=quote.blockers,
        )
    if terms.rfq_line_id in clarification.lines:
        return LinePlan(
            terms.rfq_line_id,
            terms.position,
            "question_open",
            offered_unit_price=offered,
            effective_price=quote.effective_price,
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
        previous_offer=previous.offered_unit_price if previous is not None else None,
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
    # The unit price is within the maximum, the one-off charges put the effective price
    # above it: the buyer decides knowingly (never accepted internally, never rejected).
    effective_above_max = (
        price.status != "above_max"
        and terms.max_price is not None
        and quote.effective_price is not None
        and offered <= terms.max_price < quote.effective_price
    )
    if effective_above_max:
        reasons.append("effective_above_max")
    acceptable = (
        price.status == "in_target"
        and delivery.acceptable
        and "moq_above_quantity" not in reasons
        and "clarification_unanswered" not in reasons
        and not effective_above_max
    )
    return LinePlan(
        terms.rfq_line_id,
        terms.position,
        "priced",
        price=price,
        delivery=delivery,
        offered_unit_price=offered,
        acceptable=acceptable,
        reasons=tuple(reasons),
        latest_order_date=quote.latest_order_date,
        effective_price=quote.effective_price,
        effective_above_max=effective_above_max,
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
        if plan.effective_above_max:
            found.append(PlanWarning("effective_above_max", plan.position))
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
    if offer.order_by is not None:
        # The supplier's offer holds only when ordered by this day.
        passed = offer.order_by < ctx.order_date
        found.append(
            PlanWarning("order_deadline_passed" if passed else "order_deadline", on=offer.order_by)
        )
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
            and ask.offered_unit_price is not None
            and quote.unit_price < ask.offered_unit_price
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
    for code in (*_ACCEPT_BLOCKERS, "effective_above_max"):
        if code in codes:
            return code
    if any(p.status == "blocked" for p in plans):
        return "offer_blocked"
    return "needs_review"


# Warnings that keep an offer from being accepted internally, most important first.
_ACCEPT_BLOCKERS: Final = (
    "order_deadline_passed",
    "validity_unknown",
    "validity_ends_before_decision",
    "payment_terms_shorter",
)


def _accept_blockers(warnings: Sequence[PlanWarning]) -> bool:
    return any(w.code in _ACCEPT_BLOCKERS for w in warnings)


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
            if supplier.reply_unread:
                # Bido is still reading the reply: it may well be the quote.
                return SupplierPlan("wait", "waiting", "reading_reply")
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
    sent = sorted(
        (e for e in emails if e.state == "sent" and e.reminder_of is None),
        key=lambda e: e.sequence,
    )
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
    clarification = supplier.clarification
    base = SupplierPlan(
        "wait",
        "waiting",
        "",
        lines=plans,
        warnings=warnings,
        status="bafo_requested" if bafo_sent else "countered" if sent else "open",
        valid_until=offer.valid_until,
        questions_open=clarification.blocking or bool(clarification.lines),
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
                base,
                action="hold",
                phase="needs_decision",
                reason=reason,
                offer_held=True,
                hold_reasons=offer.hold_reasons,
                held_fields=offer.held_fields,
            )

    # 4. One open request per supplier: a clarification waiting for approval or its answer.
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
    stage = _Stage(
        lines=tuple(lines),
        offer=offer,
        emails=emails,
        ctx=ctx,
        last=last,
        counted=counted,
        sent=bool(sent),
        bafo_sent=bafo_sent,
        final_round=final_round,
    )
    planned = _negotiate(base, stage, clarification)
    if not clarification.to_ask:
        return planned
    if planned.phase == "waiting_for_supplier" or planned.action == "reminder":
        # Our request for this offer is unanswered: the items wait for its answer (one
        # open request per supplier).
        return planned
    # What the offer still lacks goes out in the next email: with the negotiation's own
    # asks when there are any (one email per supplier answer), else alone.
    return _with_questions(planned, clarification, ctx) or _clarify(base, planned)


@dataclass(frozen=True, slots=True)
class _Stage:
    """What the steps after the open questions read about one supplier."""

    lines: tuple[LineTerms, ...]
    offer: Offer
    emails: tuple[EmailRecord, ...]
    ctx: Context
    last: EmailRecord | None
    counted: int
    sent: bool
    bafo_sent: bool
    final_round: bool


def _negotiate(
    base: SupplierPlan, stage: _Stage, clarification: ClarificationState
) -> SupplierPlan:
    """Steps 5 to 9: wait, accept, stop, or the next email (see the module docstring)."""
    plans, warnings = base.lines, base.warnings
    offer, emails, ctx, last = stage.offer, stage.emails, stage.ctx, stage.last
    counted, bafo_sent, final_round = stage.counted, stage.bafo_sent, stage.final_round

    # 5. Our last email is not answered yet.
    if last is not None and offer.version <= last.proposal_version:
        if last.kind == "bafo":
            cutoff = last.cutoff_at or ctx.cutoff_at
            if cutoff is not None and ctx.now < cutoff:
                return replace(base, phase="waiting_for_supplier", reason="bafo_sent", until=cutoff)
            return _final(base, plans, warnings, "final_round_ended")
        return _unanswered(base, plans, warnings, last, emails, ctx)

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
        (
            *(p.latest_order_date for p in priced if p.latest_order_date is not None),
            *((offer.order_by,) if offer.order_by is not None else ()),
        ),
        default=None,
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
        # Only the price stands between this offer and an acceptance: the supplier met
        # what we last asked, or one-off charges put a line's effective price above the
        # maximum while its unit price is within it. Nothing more to ask; the buyer
        # decides at award time (the effective price is named, never hidden).
        settled = (
            all(
                p.price_status in ("in_target", "ask_met")
                and p.delivery is not None
                and p.delivery.acceptable
                and not {"moq_above_quantity", "clarification_unanswered"} & set(p.reasons)
                for p in priced
            )
            and not any(p.status == "blocked" for p in plans)
            and not _accept_blockers(warnings)
        )
        if settled and any(p.price_status == "ask_met" for p in priced):
            return _final(base, plans, warnings, "offer_meets_last_ask")
        if settled and any(p.effective_above_max for p in priced):
            return _final(base, plans, warnings, "effective_above_max")
        return replace(
            base,
            action="hold",
            phase="needs_decision",
            reason=_first_reason(plans, warnings),
            status="closed" if stage.sent else "open",
        )

    # 8. Stop rules. A price ask our last email left for later (its questions came first)
    # was never asked: asking it now is no repeat.
    deferred = last is not None and any(p.rfq_line_id in last.deferred for p in asking)
    if last is not None and not deferred and not _moved(offer, last):
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
                plan_line(t, offer, emails, clarification, ctx, final_round=True)
                for t in stage.lines
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


# --- The open questions in the next email ----------------------------------------------------

AskSlot = Literal["offer_terms", "complete_positions", "deliver_by", "revise_price", "final"]


def ask_slots(
    items: Sequence[ClarificationItem], positions: Sequence[PositionPlan], kind: EmailKind
) -> list[AskSlot]:
    """The numbered asks of an email asking `items` (the ledger's) and `positions` (the
    negotiation's), in the order the email numbers them (`supplier_email.builders.
    with_questions`): the blocking questions first, then the negotiation's own ask.

    - `offer_terms`: the offer's currency or validity;
    - `complete_positions`: values missing or to confirm under the positions;
    - `deliver_by`: "can you deliver by D?", for the delivery dates the clarification asks
      and the late lines a revised-offer or delivery-date request asks, together;
    - `revise_price`: the revised offer, one ask covering its positions;
    - `final`: the best-and-final request (it covers its positions' dates too).
    """
    slots: list[AskSlot] = []
    if any(i.position is None and i.field in OFFER_ITEM_FIELDS for i in items):
        slots.append("offer_terms")
    if any(i.position is not None and i.field != "delivery_date" for i in items):
        slots.append("complete_positions")
    dated = kind != "bafo" and any(p.deliver_by is not None for p in positions)
    if dated or any(i.field == "delivery_date" for i in items):
        slots.append("deliver_by")
    if kind == "bafo":
        slots.append("final")
    elif any(p.ask_price is not None or p.best_price for p in positions):
        slots.append("revise_price")
    return slots


def _with_questions(
    planned: SupplierPlan, clarification: ClarificationState, ctx: Context
) -> SupplierPlan | None:
    """The negotiation's next email asking the open items too, or None: nothing to
    negotiate now (or no room for it), so the questions go alone.

    At most `MAX_ASKS` numbered asks, the questions first: a price ask that does not fit
    is left for the next email (`EmailPlan.deferred`); a best-and-final request that does
    not fit waits for the answers.
    """
    email = planned.email
    items = clarification.items
    if email is None or email.reminder_of is not None or not items:
        return None
    slots = ask_slots(items, email.positions, email.kind)
    kept = slots[:MAX_ASKS]
    if email.kind == "bafo":
        if "final" not in kept:
            return None
        combined = replace(email, clarify=items)
    elif "revise_price" in slots and "revise_price" not in kept:
        deferred = tuple(
            p.rfq_line_id for p in email.positions if p.ask_price is not None or p.best_price
        )
        positions = tuple(
            replace(p, ask_price=None, best_price=False, better_offer_exists=False, conceded=False)
            for p in email.positions
            if p.deliver_by is not None
        )
        if not positions:
            return None
        combined = replace(
            email,
            kind="delivery_date",
            reply_by=reply_by_date(ctx.now, ctx.deadline, ctx.country, "delivery_date"),
            positions=positions,
            mention_competition=False,
            clarify=items,
            deferred=deferred,
        )
    else:
        combined = replace(email, clarify=items)
    return replace(
        planned,
        action=combined.kind,
        reason=f"{combined.kind}_with_questions_due",
        email=combined,
    )


def _clarify(base: SupplierPlan, planned: SupplierPlan) -> SupplierPlan:
    """The open items go alone (the follow-up sweep writes that email). When the
    negotiation had an email to send, it waits for the answers (no room for it)."""
    waiting = planned.email is not None and planned.email.reminder_of is None
    return replace(
        base,
        action="clarify",
        phase="waiting",
        reason="clarification_due",
        detail=f"{planned.email.kind}_after_answers" if waiting and planned.email else None,
    )


def _unanswered(
    base: SupplierPlan,
    plans: Sequence[LinePlan],
    warnings: Sequence[PlanWarning],
    last: EmailRecord,
    emails: Sequence[EmailRecord],
    ctx: Context,
) -> SupplierPlan:
    """Our revised-offer or delivery-date request is unanswered: wait until its reply-by
    date, then remind once (the same request, a new reply-by date), then the offer
    stands."""
    reminders = [
        e for e in emails if e.reminder_of == last.sequence and e.state not in ("canceled",)
    ]
    reminder = max(reminders, key=lambda e: e.sequence) if reminders else None
    if reminder is not None and reminder.state == "sent":
        due = end_of_day(reminder.reply_by) if reminder.reply_by is not None else None
        if due is not None and ctx.now <= due:
            return replace(base, phase="waiting_for_supplier", reason="reminder_sent", until=due)
        return _final(base, plans, warnings, "no_reply_after_reminder")
    due = end_of_day(last.reply_by) if last.reply_by is not None else None
    if due is not None and ctx.now <= due:
        return replace(base, phase="waiting_for_supplier", reason=f"{last.kind}_sent", until=due)
    if reminder is None and last.typed and (ctx.deadline is None or ctx.now < ctx.deadline):
        email = EmailPlan(
            kind=last.kind,
            round=last.round,
            reply_by=reply_by_date(ctx.now, ctx.deadline, ctx.country, last.kind),
            cutoff_at=None,
            order_date=last.order_date or ctx.order_date,
            positions=(),
            mention_competition=False,
            reminder_of=last.sequence,
        )
        return replace(base, action="reminder", phase="waiting", reason="reminder_due", email=email)
    return replace(base, phase="waiting_for_supplier", reason="reply_overdue", until=ctx.deadline)


# Problems a final offer can still have besides its price, most important first: no more
# emails go out, but the buyer is told what is still wrong instead of reading "closed".
# A late line that was never asked about can no longer be asked: it reads as late_final.
_FINAL_OPEN_PROBLEMS: Final = (
    ("delivery_need_by_unreachable", "delivery_need_by_unreachable"),
    ("delivery_still_late", "delivery_still_late"),
    ("delivery_late", "delivery_late_final"),
    ("delivery_unknown", "delivery_unknown"),
    ("clarification_unanswered", "clarification_unanswered"),
    ("moq_above_quantity", "moq_above_quantity"),
    ("validity_unknown", "validity_unknown"),
    ("validity_ends_before_decision", "validity_ends_before_decision"),
    ("payment_terms_shorter", "payment_terms_shorter"),
    # The unit price is within the maximum, the effective price (with one-off charges)
    # above it: the buyer decides knowingly.
    ("effective_above_max", "effective_above_max"),
)


def _final(
    base: SupplierPlan, plans: Sequence[LinePlan], warnings: Sequence[PlanWarning], reason: str
) -> SupplierPlan:
    """The supplier's last word: no more emails. A delivery, validity, MOQ or payment
    problem that remains is the buyer's decision and is named; otherwise the offer stands."""
    codes = {r for p in plans if p.status == "priced" for r in p.reasons} | {
        w.code for w in warnings
    }
    for code, shown in _FINAL_OPEN_PROBLEMS:
        if code in codes:
            return replace(
                base, action="close", phase="needs_decision", reason=shown, status=_verdict(plans)
            )
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
        offered_unit_price=plan.offered_unit_price,
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
            or plan.questions_open
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


def _final_word(plan: SupplierPlan) -> bool:
    """Nothing more is negotiated with this supplier and nothing waits for the buyer
    before it could be: accepted internally, or closed (its best-and-final answered, its
    own final offer, the round cap, the final round's cut-off, no reply after the reminder,
    declined, rejected). A hold, an open question or an email in flight is not final."""
    return (
        plan.action in ("accept", "close")
        and not plan.offer_held
        and not plan.questions_open
        and plan.phase not in ("needs_answer", "held_for_approval")
    )


def every_supplier_final(*, waiting_suppliers: int, plans: Sequence[SupplierPlan]) -> bool:
    """Every invited supplier answered and gave its last word: nothing is left to
    negotiate, whatever the prices. Lines within the maximum but above the target are the
    buyer's decision (the outcome says `above_target`); an offer above the maximum is never
    recommended."""
    if waiting_suppliers > 0 or not plans:
        return False
    return all(_final_word(plan) for plan in plans)
