"""The typed decision behind every negotiation email: what the email renderer renders.

The planner (`bido_back.negotiation.planner`) decides an email; the sweep
stores it as an `EmailDecision` in `negotiation_messages.terms["decision"]`
BEFORE any draft exists, and a failed or refused draft is retried from the
same stored decision (never a new one for the same offer version).

Whatever writes the email (today the negotiation writer agent, through
`writer_task`; the code renderer of the email standard later) takes this and
nothing else: the type, one entry per position concerned (the supplier's own
offer, what we need, what we ask), the reply-by date and, for a best-and-final
request, the common cut-off. Every number and date is final here: quantized
when decided, never above the buyer's maximum, never another supplier's. The
maximum is not in it; neither is any other supplier's name or price.

`terms["lines"]` keeps the flat per-line shape older readers use (the mandate
check reads `ask` against the maximum; `ask_lead_days` is the deliver-by date
as days from the order date).
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from bido_back.agents.catalog.contracts import NegotiationEmailInput, NegotiationLineAsk
from bido_back.negotiation.planner import EmailPlan, LinePlan
from bido_back.negotiation.strategy import plain

EmailKindName = Literal["revised_offer", "delivery_date", "bafo"]


class PositionDecision(BaseModel):
    """One position of the email: the supplier's own offer, our need, our ask."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rfq_line_id: UUID
    position: int
    description: str
    part_number: str | None = None
    mpn: str | None = None
    quantity: Decimal
    unit: str
    currency: str
    # The supplier's own offer for this position.
    offered_unit_price: Decimal | None = None
    offered_lead_time_days: int | None = None
    # Its expected delivery if ordered on `EmailDecision.order_date` (estimated when
    # `delivery_exact` is false: an approximate lead time).
    offered_delivery: date | None = None
    delivery_exact: bool = True
    # The approved need-by date.
    need_by: date | None = None
    # What we ask: a unit price (quantized, disclosed), or a better price without a
    # number (`best_price`), and/or delivery by a date.
    target_unit_price: Decimal | None = None
    best_price: bool = False
    deliver_by: date | None = None
    # True: the email may say a lower offer exists for this item (never whose or how much).
    better_offer_exists: bool = False
    # The ask moved from our previous one because the supplier moved (a labeled step).
    conceded: bool = False


class EmailDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: EmailKindName
    # The negotiation round (sent emails before it, plus one) and the email's sequence.
    round: int = Field(ge=1)
    sequence: int = Field(ge=1)
    currency: str
    # Delivery estimates count from this order date.
    order_date: date
    # Answer by this date (a best-and-final request: the cut-off's date).
    reply_by: date
    # The common best-and-final cut-off (date and time, UTC), for every supplier.
    cutoff_at: datetime | None = None
    # The offer this email answers.
    offer_version: int
    offer_received_on: date | None = None
    mention_competition: bool = False
    positions: list[PositionDecision]


class DecisionLine(BaseModel):
    """The RFQ line facts a decision repeats (as requested, never translated)."""

    model_config = ConfigDict(frozen=True)

    position: int
    description: str
    part_number: str | None = None
    mpn: str | None = None
    quantity: Decimal
    unit: str
    need_by: date | None = None


def build_decision(
    plan: EmailPlan,
    *,
    sequence: int,
    currency: str,
    offer_version: int,
    offer_received_on: date | None,
    lines: dict[UUID, DecisionLine],
) -> EmailDecision:
    """The stored decision for an email the planner decided."""
    positions: list[PositionDecision] = []
    for position in plan.positions:
        line = lines[position.rfq_line_id]
        positions.append(
            PositionDecision(
                rfq_line_id=position.rfq_line_id,
                position=line.position,
                description=line.description,
                part_number=line.part_number,
                mpn=line.mpn,
                quantity=plain(line.quantity),
                unit=line.unit,
                currency=currency,
                offered_unit_price=position.offered_price,
                offered_lead_time_days=position.lead_time_days,
                offered_delivery=position.offered_delivery,
                delivery_exact=position.delivery_exact,
                need_by=line.need_by,
                target_unit_price=position.ask_price,
                best_price=position.best_price,
                deliver_by=position.deliver_by,
                better_offer_exists=position.better_offer_exists,
                conceded=position.conceded,
            )
        )
    return EmailDecision(
        kind=plan.kind,
        round=plan.round,
        sequence=sequence,
        currency=currency,
        order_date=plan.order_date,
        reply_by=plan.reply_by,
        cutoff_at=plan.cutoff_at,
        offer_version=offer_version,
        offer_received_on=offer_received_on,
        mention_competition=plan.mention_competition,
        positions=sorted(positions, key=lambda p: p.position),
    )


def legacy_lines(decision: EmailDecision, plans: tuple[LinePlan, ...]) -> list[JsonValue]:
    """The flat `terms["lines"]` entries: every RFQ line, what was decided and asked."""
    asked: dict[UUID, PositionDecision] = {p.rfq_line_id: p for p in decision.positions}
    entries: list[JsonValue] = []
    for plan in plans:
        position = asked.get(plan.rfq_line_id)
        ask = position.target_unit_price if position is not None else None
        deliver_by = position.deliver_by if position is not None else None
        entry: dict[str, JsonValue] = {
            "rfq_line_id": str(plan.rfq_line_id),
            "decision": _line_decision(plan, position),
            "offered": str(plan.offered_price) if plan.offered_price is not None else None,
            "ask": str(ask) if ask is not None else None,
            "best_price": position.best_price if position is not None else False,
            "ask_lead_days": (deliver_by - decision.order_date).days
            if deliver_by is not None
            else None,
            "deliver_by": deliver_by.isoformat() if deliver_by is not None else None,
            "offered_lead_days": position.offered_lead_time_days if position is not None else None,
            "offered_delivery": position.offered_delivery.isoformat()
            if position is not None and position.offered_delivery is not None
            else None,
        }
        entries.append(entry)
    return entries


def _line_decision(plan: LinePlan, position: PositionDecision | None) -> str:
    if plan.status != "priced":
        return {"not_quoted": "no_offer", "declined": "no_offer"}.get(plan.status, "blocked")
    if position is None:
        return "accept" if plan.acceptable else "hold"
    if plan.price_status == "above_max":
        return "above_max"
    return "counter"


def writer_task(
    decision: EmailDecision,
    *,
    rfq_ref: str,
    rfq_title: str,
    buyer_company: str,
    buyer_name: str,
    supplier_name: str,
    supplier_language: str,
) -> NegotiationEmailInput:
    """The negotiation writer's task for a decision (until code renders the email).

    A delivery-date ask becomes `required_lead_time_days`: the days from the
    decision's order date to the deliver-by date (the writer's v1 contract
    has no date per line); the decision itself keeps the date.
    """
    lines: list[NegotiationLineAsk] = []
    for position in decision.positions:
        label = position.description + (f" ({position.mpn})" if position.mpn else "")
        lines.append(
            NegotiationLineAsk(
                position=position.position,
                line=label,
                quantity=position.quantity,
                unit=position.unit,
                currency=position.currency,
                offered_unit_price=position.offered_unit_price,
                target_unit_price=position.target_unit_price,
                offered_lead_time_days=position.offered_lead_time_days,
                required_lead_time_days=(position.deliver_by - decision.order_date).days
                if position.deliver_by is not None
                else None,
            )
        )
    return NegotiationEmailInput(
        action="bafo" if decision.kind == "bafo" else "counter",
        rfq_ref=rfq_ref,
        rfq_title=rfq_title,
        buyer_company=buyer_company,
        buyer_name=buyer_name,
        supplier_name=supplier_name,
        supplier_language=supplier_language,
        round=decision.round,
        reply_by=decision.reply_by,
        mention_competition=decision.mention_competition,
        lines=lines,
    )
