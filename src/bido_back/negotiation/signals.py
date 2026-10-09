"""What the negotiation planner reads from other workstreams, behind small interfaces.

Three inputs come from outside the negotiation package. Each has a documented
shape here and a default implementation that works with today's data, so the
planner (`bido_back.negotiation.planner`) does not change when the richer
sources land:

1. `ExpectedDelivery` (quote extraction, N2a): when a line would arrive if
   ordered on a given day. Signature: `expected_delivery(line, reference_date)
   -> (date, exact) | None`. `exact` is False for approximate lead times
   ("approximately 10 business days"). The default,
   `calendar_expected_delivery`, reads `lead_time_days` as calendar days (what
   extraction stores today). N2a's helper converts business days and weeks with
   the supplier's holiday calendar and applies an offer-level lead time to
   lines without their own; it receives the same `QuotedLeadTime`.
2. `OfferSignals` (quote extraction, N2a): facts about the supplier's latest
   reply that change what Bido may do, read from the proposal's provenance by
   `offer_signals`. Today's keys are read as they are; the new ones are read
   from `fields["_offer_state"]` when present:
   - `final_offer: bool`: the supplier declared this its best and final offer
     (never ask a best-and-final after it);
   - `declined_line_positions: list[int]`: positions the supplier said it does
     not quote (never asked again);
   - `technical_questions: list[str]` (exists): questions only the buyer can
     answer; Bido counts them and never reads their text into a decision.
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

# --- Delivery dates (N2a) ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QuotedLeadTime:
    """One offered line's lead time, as the extraction stored it.

    `days` is the stored `lead_time_days` (calendar days today). `fields` is the
    line's provenance and `offer_fields` the proposal's: N2a's helper finds the
    unit (business days, weeks), the basis (from order, from payment) and an
    offer-level lead time there. `country` is the supplier's, for holidays.
    """

    days: int | None
    fields: Mapping[str, object] = field(default_factory=dict[str, object])
    offer_fields: Mapping[str, object] = field(default_factory=dict[str, object])
    country: str | None = None


ExpectedDelivery = Callable[[QuotedLeadTime, date], tuple[date, bool] | None]


def calendar_expected_delivery(
    line: QuotedLeadTime, reference_date: date
) -> tuple[date, bool] | None:
    """The default: `lead_time_days` calendar days after the order date, exact."""
    if line.days is None or line.days < 0:
        return None
    return reference_date + timedelta(days=line.days), True


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
    """Read the reply facts the planner uses from a proposal's provenance (see above)."""
    raw = fields.get("_offer_state")
    if not isinstance(raw, dict):
        return OfferSignals()
    state = cast(dict[str, object], raw)
    declined: set[int] = set()
    positions = state.get("declined_line_positions")
    if isinstance(positions, list):
        for value in cast(list[object], positions):
            if isinstance(value, int) and not isinstance(value, bool):
                declined.add(value)
    questions = state.get("technical_questions")
    count = len(cast(list[object], questions)) if isinstance(questions, list) else 0
    return OfferSignals(
        final_offer=state.get("final_offer") is True,
        declined_positions=frozenset(declined),
        questions=count,
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
