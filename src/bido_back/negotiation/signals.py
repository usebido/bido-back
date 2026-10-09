"""What the negotiation planner reads from other workstreams, behind small interfaces.

Three inputs come from outside the negotiation package. Each has a documented
shape here, so the planner (`bido_back.negotiation.planner`) stays pure:

1. `ExpectedDelivery` (quote extraction, N2a): when a line would arrive if
   ordered on a given day. Signature: `expected_delivery(line, reference_date)
   -> (date, exact) | None`. `exact` is False when the supplier hedged
   ("approximately"), the period starts after payment or approval, or ends ex
   works. The default, `extraction_expected_delivery`, is the follow-ups' own
   helper (`followups.missing.stored_expected_delivery`), so the clarification
   ledger and the planner never disagree on a date: it reads the lead time the
   extraction stored as data (`quotes.terms.line_terms`: amount, unit, basis,
   the supplier's holiday calendar, the offer-level lead time for lines without
   their own) and converts it with `quotes.lead_time.expected_delivery` from the
   reference date (today: a hypothetical order placed now); a stated lead time
   that needs review gives no date. `proposal_lines.lead_time_days` (calendar
   days from the offer date) is only read for offers extracted before prompt v5
   (never exact) or corrected by a person.
2. `OfferSignals` (quote extraction, N2a): facts about the supplier's latest
   reply that change what Bido may do, read by `offer_signals` from the
   proposal's typed terms (`quotes.terms.offer_terms(...).signals`):
   - `final_offer`: the supplier declared this its best and final offer
     (never ask a best-and-final after it);
   - `declined_line_positions`: positions the supplier said it does not quote
     (never asked again);
   - `questions`: the supplier's questions (and `_offer_state.technical_questions`);
     only the buyer can answer them. Bido counts them and never reads their
     text into a decision.
3. `ClarificationState` (the clarification ledger, N2b): per supplier, what is
   still to ask (`to_ask` codes, `items` typed), what was asked and not
   answered, and what was asked once and is still unusable. One open request
   per supplier: `pending` and `awaiting` block every other email; items
   `to_ask` go out in the next email, alone (the follow-up sweep's
   clarification) or, when the offer also has something to negotiate, in the
   planner's own email with its asks (one email per supplier answer). Lines
   with an open item are not negotiated until it is answered; `exhausted`
   items are buyer decisions for their lines. The service maps it from the
   ledger (`followups.ledger.supplier_followups`), the same state the
   follow-up sweep acts on, so the two agree by construction (both read
   `to_ask`). A delivery date that cannot be told, or only approximately
   misses the need-by date, is the clarification's question; a definite late
   date is the negotiation's delivery-date request ("deliver by <need-by>"),
   once.

Nothing here reads or returns supplier text: codes, ids, numbers and dates only.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Final, Literal, cast
from uuid import UUID

from bido_back.followups.missing import OfferedLine, stored_expected_delivery
from bido_back.quotes.lead_time import StoredLeadTime
from bido_back.quotes.terms import line_terms, offer_terms

# --- Delivery dates (N2a) ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QuotedLeadTime:
    """One offered line's lead time, as the extraction stored it.

    `days` is the stored `lead_time_days` (calendar days from the offer date: the
    fallback only). `fields` is the line's provenance, where the extraction keeps
    the lead time as data (`quotes.terms.line_terms`); `offer_fields` the
    proposal's. `country` is the supplier's, for holidays.
    """

    days: int | None
    fields: Mapping[str, object] = field(default_factory=dict[str, object])
    offer_fields: Mapping[str, object] = field(default_factory=dict[str, object])
    country: str | None = None


ExpectedDelivery = Callable[[QuotedLeadTime, date], tuple[date, bool] | None]


def calendar_expected_delivery(
    line: QuotedLeadTime, reference_date: date
) -> tuple[date, bool] | None:
    """`lead_time_days` calendar days after the order date, exact (the fallback)."""
    if line.days is None or line.days < 0:
        return None
    return reference_date + timedelta(days=line.days), True


def stated_lead_time(line: QuotedLeadTime) -> StoredLeadTime | None:
    """The lead time the supplier stated for this line, as data (N2a), when usable."""
    return line_terms(line.fields).lead_time


def extraction_expected_delivery(
    line: QuotedLeadTime, reference_date: date
) -> tuple[date, bool] | None:
    """The default: the follow-ups' helper (`followups.missing.stored_expected_delivery`):
    the stated lead time converted from the order date (business days skip the
    supplier's holidays, a stated date stays that date); None when it needs review."""
    try:
        return stored_expected_delivery(
            OfferedLine(
                rfq_line_id=None,
                values={"lead_time_days": line.days},
                fields=line.fields,
                offer_fields=line.offer_fields,
                supplier_country=line.country,
            ),
            reference_date,
        )
    except ValueError:
        return None


def latest_order_date(
    expected_delivery: ExpectedDelivery, line: QuotedLeadTime, need_by: date, today: date
) -> date | None:
    """The last order date whose expected delivery still meets `need_by`, or None.

    A later order never arrives earlier, so this is a binary search between
    today and `need_by`, with any helper (business days included). None when
    even an order today is late, or the lead time is unknown.
    """
    first = expected_delivery(line, today)
    if first is None or first[0] > need_by:
        return None
    low, high = 0, max((need_by - today).days, 0)
    while low < high:
        middle = (low + high + 1) // 2
        found = expected_delivery(line, today + timedelta(days=middle))
        if found is not None and found[0] <= need_by:
            low = middle
        else:
            high = middle - 1
    return today + timedelta(days=low)


# --- Offer signals (N2a) ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OfferSignals:
    final_offer: bool = False
    declined_positions: frozenset[int] = frozenset()
    questions: int = 0


def offer_signals(fields: Mapping[str, object]) -> OfferSignals:
    """The reply facts the planner uses, from a proposal's typed terms (see above)."""
    signals = offer_terms(fields).signals
    technical = 0
    raw = fields.get("_offer_state")
    if isinstance(raw, dict):
        questions = cast(dict[str, object], raw).get("technical_questions")
        if isinstance(questions, list):
            technical = len(cast(list[object], questions))
    return OfferSignals(
        final_offer=signals.final_offer,
        declined_positions=frozenset(signals.declined_line_positions),
        questions=max(len(signals.questions), technical),
    )


# --- The clarification ledger (N2b) -------------------------------------------------------

ClarificationWait = Literal["approval", "daily_cap", "mailbox", "mandate_hold", "mandate_paused"]
# The offer-level fields an item may concern (the rest concern one requested line).
OFFER_ITEM_FIELDS: Final = frozenset({"currency", "valid_until"})


@dataclass(frozen=True, slots=True)
class ClarificationItem:
    """One item to ask (the ledger's): the whole offer (no line) or one requested line,
    and the field (`currency`, `valid_until`, `unit_price`, `quantity`, `lead_time`,
    `delivery_date`). Codes and ids only."""

    rfq_line_id: UUID | None
    position: int | None
    field: str

    @property
    def code(self) -> str:
        return self.field if self.position is None else f"pos{self.position}.{self.field}"


@dataclass(frozen=True, slots=True)
class ClarificationState:
    """Where the supplier's clarification stands for its current offer.

    Items are codes such as `valid_until` or `pos2.lead_time` (never text); `items`
    are the items `to_ask`, typed. `lines` are the RFQ lines that open items concern
    (to ask, pending, awaiting, or asked by a negotiation email still unanswered);
    `exhausted_lines` those whose items were asked once and remain unusable.
    """

    to_ask: tuple[str, ...] = ()
    items: tuple[ClarificationItem, ...] = ()
    pending: bool = False
    pending_wait: ClarificationWait | None = None
    pending_until: datetime | None = None
    awaiting: bool = False
    awaiting_until: datetime | None = None
    exhausted: tuple[str, ...] = ()
    lines: frozenset[UUID] = frozenset()
    exhausted_lines: frozenset[UUID] = frozenset()

    @property
    def blocking(self) -> bool:
        return bool(self.to_ask) or self.pending or self.awaiting


NO_CLARIFICATION = ClarificationState()
