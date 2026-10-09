"""What the negotiation planner reads from other workstreams, behind small interfaces.

Three inputs come from outside the negotiation package. Each has a documented
shape here, so the planner (`bido_back.negotiation.planner`) stays pure:

1. `ExpectedDelivery` (quote extraction, N2a): when a line would arrive if
   ordered on a given day. Signature: `expected_delivery(line, reference_date)
   -> (date, exact) | None`. `exact` is False when the supplier hedged
   ("approximately"), the period starts after payment or approval, or ends ex
   works. The default, `extraction_expected_delivery`, reads the lead time the
   extraction stored as data (`quotes.terms.line_terms`: amount, unit, basis,
   the supplier's holiday calendar, the offer-level lead time for lines without
   their own) and converts it with `quotes.lead_time.expected_delivery` from the
   reference date (today: a hypothetical order placed now). Never from
   `proposal_lines.lead_time_days`, which counts from the offer date and goes
   stale; that value (calendar days) is only the fallback for offers extracted
   before prompt v5 or corrected by a person (`calendar_expected_delivery`).
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
   still to ask, what was asked and not answered, and what was asked once and
   is still unusable. The planner treats `to_ask`, `pending` and `awaiting` as
   blocking (one open request per supplier: clarify first, then negotiate) and
   `exhausted` items as buyer decisions for their lines. The default adapter
   (`followup_clarifications` in the service) mirrors today's follow-up sweep:
   it reads `followup_messages` and recomputes the open items with
   `followups.missing.open_items`, so a negotiation email never overlaps a
   clarification of the same offer. N2b's ledger replaces it with the same
   shape; the follow-up sweep and the planner then agree by construction
   (both read `to_ask`).

Nothing here reads or returns supplier text: codes, ids, numbers and dates only.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Literal, cast
from uuid import UUID

from bido_back.quotes.lead_time import StoredLeadTime
from bido_back.quotes.lead_time import expected_delivery as lead_time_delivery
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
    """The default: the stated lead time converted from the order date (N2a's helper:
    business days skip the supplier's holidays, a stated date stays that date);
    calendar days from `lead_time_days` for offers without it."""
    stated = stated_lead_time(line)
    if stated is None:
        return calendar_expected_delivery(line, reference_date)
    try:
        found = lead_time_delivery(stated, reference_date)
    except ValueError:
        return calendar_expected_delivery(line, reference_date)
    return found.delivery_date, found.exact


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


@dataclass(frozen=True, slots=True)
class ClarificationState:
    """Where the supplier's clarification stands for its current offer.

    Items are codes such as `valid_until` or `pos2.lead_time` (never text).
    `lines` are the RFQ lines that open items concern (to ask, pending or
    awaiting); `exhausted_lines` those whose items were asked once and remain
    unusable.
    """

    to_ask: tuple[str, ...] = ()
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
