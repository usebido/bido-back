"""The negotiation strategy: pure code, no model, no database.

Given one supplier's latest offer (per line, in the RFQ's base currency), the
buyer's limits, the competing offers and where the negotiation stands, decide
what Bido proposes to that supplier next. The model only puts the decision
into words (`bido_back.negotiation.agents`); every number comes from here.

Per line (`decide_line`):

- `no_offer`: the supplier did not price the line.
- `blocked`: the offer cannot be considered as is (an alternative part, an
  unauthorized source on an automotive line, an expired or untrustworthy
  quote; see `bido_back.comparison.rules.blockers`). Price is not negotiated
  on it.
- `accept`: price at or below the target and lead time acceptable.
- `above_max`: price above the buyer's maximum.
- `counter`: anything else. We ask for the target; without a target, for the
  5% off its own offer, never a competitor-derived amount.
  If only the lead time is the problem, we ask for the lead time and no price.

The disclosed target is the buyer's (`rfq_lines.target_unit_price`). Without
one, price history (`bido_back.negotiation.history`) is only an internal
attainment reference; asks use this supplier's own offer as their basis.

Per supplier (`decide_supplier`), with `rounds_used` = counter/BAFO emails
already proposed or sent to that supplier:

- every priced line `accept` -> `accept` (hold: no email; awarding is a
  separate, human step);
- the supplier already received a best-and-final request, or the round cap is
  reached -> `final` (no more emails): `rejected` when every priced line is
  still above the maximum, else `closed` (its last offer stands);
- something to negotiate and this is the last allowed round, or the deadline
  is less than `BAFO_WINDOW` away -> `bafo` (best and final offer);
- something to negotiate -> `counter`;
- nothing priced -> `none`.

What we may tell a supplier (enforced by the email checks, documented here):
our target price per line, the lead time we need, the deadline, and THAT a
more competitive offer exists. Never a competitor's name, price or terms, and
never the buyer's maximum. A counter-offer never accepts or commits to buy.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Final, Literal
from uuid import UUID

from bido_back.decimals import plain_decimal

BAFO_WINDOW: Final = timedelta(hours=24)
NO_TARGET_DISCOUNT: Final = Decimal("0.95")
PRICE_STEP: Final = Decimal("0.0001")

LineDecisionKind = Literal["no_offer", "blocked", "accept", "above_max", "counter"]
SupplierActionKind = Literal["none", "accept", "counter", "bafo", "final"]
InviteNegotiationStatus = Literal[
    "open", "countered", "bafo_requested", "accepted", "rejected", "closed"
]


@dataclass(frozen=True, slots=True)
class LineLimits:
    rfq_line_id: UUID
    target: Decimal | None
    max_price: Decimal | None
    max_lead_days: int | None
    history_price: Decimal | None = None

    @property
    def effective_target(self) -> Decimal | None:
        return self.target if self.target is not None else self.history_price


@dataclass(frozen=True, slots=True)
class LineOffer:
    rfq_line_id: UUID
    unit_price: Decimal | None
    lead_time_days: int | None
    blockers: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LineDecision:
    rfq_line_id: UUID
    kind: LineDecisionKind
    offered_price: Decimal | None
    ask_price: Decimal | None
    lead_time_days: int | None
    ask_lead_days: int | None
    better_offer_exists: bool


@dataclass(frozen=True, slots=True)
class SupplierDecision:
    action: SupplierActionKind
    status: InviteNegotiationStatus
    lines: tuple[LineDecision, ...]
    mention_competition: bool
    reasons: tuple[str, ...] = ()
    stalled_rounds: int = 0

    @property
    def sends_email(self) -> bool:
        return self.action in ("counter", "bafo")


@dataclass(frozen=True, slots=True)
class PreviousRound:
    """Supplier terms preceding a counter actually sent, never a pending draft."""

    offers: Mapping[UUID, LineOffer]


def stagnant_rounds(current: Mapping[UUID, LineOffer], previous: Sequence[PreviousRound]) -> int:
    """Count consecutive sent rounds without an evidenced price/delivery improvement.

    Only comparable unblocked line snapshots count. New lines, missing history
    or a changed scope reset the observation rather than inventing a refusal.
    """
    count = 0
    recent = {
        line: offer
        for line, offer in current.items()
        if offer.unit_price is not None and not offer.blockers
    }
    for prior in reversed(previous):
        earlier = {
            line: offer
            for line, offer in prior.offers.items()
            if offer.unit_price is not None and not offer.blockers
        }
        common = set(recent) & set(earlier)
        if not common or set(recent) != set(earlier):
            break
        comparable = [(recent[line], earlier[line]) for line in common]
        improved = any(
            (
                new.unit_price is not None
                and old.unit_price is not None
                and new.unit_price < old.unit_price
            )
            or (
                new.lead_time_days is not None
                and old.lead_time_days is not None
                and new.lead_time_days < old.lead_time_days
            )
            for new, old in comparable
        )
        if improved:
            break
        count += 1
        recent = earlier
    return count


def _round(value: Decimal) -> Decimal:
    # Not `normalize()`: a round ask (90.0000) would become 9E+1 in the writer's
    # input, the approval and the API, and the email would carry it.
    return plain_decimal(value.quantize(PRICE_STEP, rounding=ROUND_HALF_UP))


def _lead_ok(lead: int | None, max_lead: int | None) -> bool:
    return max_lead is None or (lead is not None and lead <= max_lead)


def decide_line(
    limits: LineLimits, offer: LineOffer | None, best_competing: Decimal | None
) -> LineDecision:
    """What to do about one line of one supplier's offer (see the module docstring)."""
    if offer is None or offer.unit_price is None:
        return LineDecision(
            rfq_line_id=limits.rfq_line_id,
            kind="no_offer",
            offered_price=None,
            ask_price=None,
            lead_time_days=offer.lead_time_days if offer else None,
            ask_lead_days=None,
            better_offer_exists=False,
        )
    price = offer.unit_price
    better = best_competing is not None and best_competing < price
    lead_ok = _lead_ok(offer.lead_time_days, limits.max_lead_days)

    def decision(
        kind: LineDecisionKind, ask: Decimal | None = None, ask_lead: int | None = None
    ) -> LineDecision:
        return LineDecision(
            rfq_line_id=limits.rfq_line_id,
            kind=kind,
            offered_price=price,
            ask_price=ask,
            lead_time_days=offer.lead_time_days,
            ask_lead_days=ask_lead,
            better_offer_exists=better,
        )

    if offer.blockers:
        return decision("blocked")
    target = limits.effective_target
    if target is not None and limits.max_price is not None and target > limits.max_price:
        # Historical reference is not permission to disclose or raise the private ceiling.
        target = None
    if limits.max_price is not None and price > limits.max_price:
        return decision(
            "above_max",
            ask=_round(limits.target) if limits.target is not None else None,
            ask_lead=None if lead_ok else limits.max_lead_days,
        )
    if target is not None and price <= target:
        if lead_ok:
            return decision("accept")
        return decision("counter", ask_lead=limits.max_lead_days)
    # Historical and competing prices stay internal. Only a buyer-adopted
    # target or a reduction of this supplier's own offer reaches the writer.
    if limits.target is not None:
        ask = limits.target
    else:
        ask = price * NO_TARGET_DISCOUNT
    ask = _round(min(ask, price))
    if ask >= price and lead_ok:
        # Nothing to ask for: treat as acceptable (no target, best on the table).
        return decision("accept")
    return decision(
        "counter",
        ask=ask if ask < price else None,
        ask_lead=None if lead_ok else limits.max_lead_days,
    )


def decide_supplier(
    *,
    lines: Sequence[LineLimits],
    offers: Mapping[UUID, LineOffer],
    best_competing: Mapping[UUID, Decimal],
    rounds_used: int,
    bafo_already_requested: bool,
    max_rounds: int,
    deadline: datetime | None,
    now: datetime,
    previous_rounds: Sequence[PreviousRound] = (),
    holds: Sequence[str] = (),
) -> SupplierDecision:
    """What Bido proposes to one supplier next (see the module docstring)."""
    decisions = tuple(
        decide_line(limits, offers.get(limits.rfq_line_id), best_competing.get(limits.rfq_line_id))
        for limits in lines
    )
    if holds:
        return SupplierDecision("none", "open", decisions, False, tuple(holds))
    priced = [d for d in decisions if d.kind not in ("no_offer", "blocked")]
    mention = any(d.better_offer_exists for d in priced if d.kind != "accept")
    if not priced:
        return SupplierDecision("none", "open", decisions, False)
    if all(d.kind == "accept" for d in priced):
        incomplete = any(d.kind in ("no_offer", "blocked") for d in decisions)
        return SupplierDecision(
            "none" if incomplete else "accept",
            "open" if incomplete else "accepted",
            decisions,
            False,
            ("partial_coverage_requires_review",) if incomplete else (),
        )
    stagnant = stagnant_rounds(offers, previous_rounds)
    if stagnant >= 2:
        status: InviteNegotiationStatus = (
            "rejected" if all(d.kind == "above_max" for d in priced) else "closed"
        )
        return SupplierDecision(
            "final", status, decisions, False, ("stalled_negotiation_requires_buyer",), stagnant
        )
    if bafo_already_requested or rounds_used >= max_rounds:
        status = "rejected" if all(d.kind == "above_max" for d in priced) else "closed"
        return SupplierDecision("final", status, decisions, False, ("round_limit_reached",))
    if not any(d.ask_price is not None or d.ask_lead_days is not None for d in priced):
        return SupplierDecision("none", "open", decisions, False, ("buyer_target_required",))
    last_round = rounds_used + 1 >= max_rounds
    near_deadline = deadline is not None and deadline - now <= BAFO_WINDOW
    if last_round or near_deadline:
        return SupplierDecision("bafo", "bafo_requested", decisions, mention)
    return SupplierDecision("counter", "countered", decisions, mention)


def best_competing_prices(
    offers_by_supplier: Mapping[UUID, Mapping[UUID, LineOffer]],
) -> dict[UUID, dict[UUID, Decimal]]:
    """Per supplier, per line: the lowest price any OTHER supplier offered (unblocked)."""
    result: dict[UUID, dict[UUID, Decimal]] = {}
    for supplier, _ in offers_by_supplier.items():
        best: dict[UUID, Decimal] = {}
        for other, other_offers in offers_by_supplier.items():
            if other == supplier:
                continue
            for line_id, offer in other_offers.items():
                if offer.unit_price is None or offer.blockers:
                    continue
                current = best.get(line_id)
                if current is None or offer.unit_price < current:
                    best[line_id] = offer.unit_price
        result[supplier] = best
    return result


def ready_to_close(
    *,
    line_ids: Sequence[UUID],
    waiting_suppliers: int,
    decisions: Sequence[SupplierDecision],
) -> bool:
    """Every invited supplier answered and every line has an offer within target."""
    if waiting_suppliers > 0 or not line_ids:
        return False
    accepted = {
        d.rfq_line_id
        for supplier in decisions
        if not any(
            reason
            in (
                "supplier_clarification_requires_buyer",
                "offer_state_requires_review",
                "offer_withdrawn",
            )
            for reason in supplier.reasons
        )
        for d in supplier.lines
        if d.kind == "accept"
    }
    return all(line_id in accepted for line_id in line_ids)
