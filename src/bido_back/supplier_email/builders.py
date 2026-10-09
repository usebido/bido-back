"""Build a `SupplierEmail` from what the services decided: pure functions, no I/O.

Each builder takes neutral data (the requested lines, what the supplier offered, what
code decided to ask) and returns the email's typed input. The services load the rows
and call them; other workstreams (the clarification ledger, the negotiation strategy) may
build `SupplierEmail` directly or feed these with their own decisions.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from typing import Literal

from bido_back.db.models import RfqLine
from bido_back.supplier_email.locale import Locale
from bido_back.supplier_email.model import (
    OFFER_FIELDS,
    OFFER_TERMS,
    Answered,
    Ask,
    EmailType,
    Fact,
    LeadTime,
    MissingValues,
    Money,
    OfferedTerms,
    OfferField,
    Position,
    ReplyBy,
    RequiredDelivery,
    RequiredLeadTime,
    Signature,
    SupplierEmail,
    TargetPrice,
    ValuesToConfirm,
)
from bido_back.supplier_email.render import clean_text


@dataclass(frozen=True, slots=True)
class Header:
    """What every email of one thread shares."""

    rfq_ref: str
    # `Re: ` plus the RFQ email's subject (`subject.reply_subject`).
    subject: str
    locale: Locale
    supplier_name: str
    buyer_company: str
    signature: Signature


@dataclass(frozen=True, slots=True)
class RequestedLine:
    """One RFQ line as the RFQ email showed it."""

    position: int
    description: str
    part_number: str | None
    mpn: str | None
    quantities: tuple[Decimal, ...]
    unit: str
    need_by: date | None = None


@dataclass(frozen=True, slots=True)
class Gap:
    """Something the offer lacks (`missing`) or that needs confirming (`unclear`)."""

    position: int | None
    field: OfferField
    problem: Literal["missing", "unclear"]


@dataclass(frozen=True, slots=True)
class LineAsk:
    """What a counter-offer or best-and-final request asks for one line.

    `offered` is the supplier's own quote as quoted (currency, price basis); `target` the
    price we ask for, `lead_days` or `need_by` the lead time or date we need. None: that
    part is not asked.
    """

    position: int
    offered: Money | None = None
    offered_lead: LeadTime | None = None
    target: Money | None = None
    lead_days: int | None = None
    need_by: date | None = None

    @property
    def asks_delivery(self) -> bool:
        return self.lead_days is not None or self.need_by is not None


def email_line(row: RfqLine) -> RequestedLine:
    """An RFQ line as its email shows it: the requested quantity tiers, the unit, the date."""
    return RequestedLine(
        position=row.position,
        description=row.description,
        part_number=row.part_number,
        mpn=row.mpn,
        quantities=tuple(Decimal(str(q)) for q in row.quantities) or (Decimal(1),),
        unit=row.unit,
        need_by=row.need_by,
    )


def line_code(part_number: str | None, mpn: str | None) -> str | None:
    """The code the RFQ email showed: the buyer's own, a different MPN in brackets."""
    if part_number and mpn and mpn != part_number:
        return f"{part_number} ({mpn})"
    return part_number or mpn or None


def position_of(line: RequestedLine, facts: Iterable[Fact] = ()) -> Position:
    code = line_code(line.part_number, line.mpn)
    return Position(
        number=line.position,
        description=clean_text(line.description, 200),
        code=clean_text(code, 100) if code else None,
        quantities=line.quantities or (Decimal(1),),
        unit=line.unit,
        facts=tuple(facts),
    )


def _email(header: Header, kind: EmailType) -> SupplierEmail:
    return SupplierEmail(
        type=kind,
        locale=header.locale,
        rfq_ref=header.rfq_ref,
        subject=header.subject,
        supplier_name=header.supplier_name,
        buyer_company=header.buyer_company,
        signature=header.signature,
    )


def reminder_email(
    header: Header, *, requested_on: date | None, reply_by: ReplyBy | None
) -> SupplierEmail:
    """A chase: the supplier has not answered the RFQ yet."""
    return replace(
        _email(header, "reminder"),
        answered=Answered("request", requested_on) if requested_on else None,
        asks=(Ask("quote_or_decline"),),
        reply_by=reply_by,
    )


def clarification_email(
    header: Header,
    *,
    answered: Answered | None,
    lines: Sequence[RequestedLine],
    gaps: Sequence[Gap],
    reply_by: ReplyBy | None,
) -> SupplierEmail:
    """What the offer lacks: its currency and validity, then the values of each position."""
    asks: list[Ask] = []
    terms = [g for g in gaps if g.position is None and g.field in OFFER_TERMS]
    if terms:
        fields: tuple[OfferField, ...] = tuple(
            f for f in OFFER_FIELDS if any(g.field == f for g in terms)
        )
        asks.append(
            Ask("offer_terms", fields=fields, confirm=all(g.problem == "unclear" for g in terms))
        )
    positions: list[Position] = []
    for line in sorted(lines, key=lambda line: line.position):
        mine = [g for g in gaps if g.position == line.position]
        if not mine:
            continue
        missing: tuple[OfferField, ...] = tuple(
            f for f in OFFER_FIELDS if any(g.field == f and g.problem == "missing" for g in mine)
        )
        unclear: tuple[OfferField, ...] = tuple(
            f
            for f in OFFER_FIELDS
            if f not in missing and any(g.field == f and g.problem == "unclear" for g in mine)
        )
        facts: list[Fact] = []
        if missing:
            facts.append(MissingValues(missing))
        if unclear:
            facts.append(ValuesToConfirm(unclear))
        positions.append(position_of(line, facts))
    if positions:
        asks.append(Ask("complete_positions", positions=tuple(p.number for p in positions)))
    return replace(
        _email(header, "clarification"),
        answered=answered,
        positions=tuple(positions),
        asks=tuple(asks),
        reply_by=reply_by,
        subset=0 < len(positions) < len(lines),
    )


def negotiation_type(action: Literal["counter", "bafo"], asks: Sequence[LineAsk]) -> EmailType:
    """`bafo`; a counter asking a price is a `revised_offer`, one asking only lead times or
    dates a `delivery_date` request."""
    if action == "bafo":
        return "bafo"
    return "revised_offer" if any(a.target is not None for a in asks) else "delivery_date"


def negotiation_email(
    header: Header,
    *,
    action: Literal["counter", "bafo"],
    round: int,
    answered: Answered | None,
    lines: Sequence[RequestedLine],
    asks: Sequence[LineAsk],
    mention_competition: bool,
    reply_by: ReplyBy | None,
) -> SupplierEmail:
    """A revised-offer request (a price is asked), a delivery-date request (only lead times
    or dates), or the best-and-final request."""
    requested = {line.position for line in lines}
    asks = [a for a in asks if a.position in requested]
    by_position = {a.position: a for a in asks}
    kind = negotiation_type(action, asks)
    positions: list[Position] = []
    for line in sorted(lines, key=lambda line: line.position):
        ask = by_position.get(line.position)
        if ask is None:
            continue
        facts: list[Fact] = []
        # The quoted lead time shows where a lead time is asked (or the request is about one).
        lead = ask.offered_lead if ask.asks_delivery or kind == "delivery_date" else None
        if ask.offered is not None or lead is not None:
            facts.append(OfferedTerms(price=ask.offered, lead_time=lead))
        if ask.target is not None:
            facts.append(TargetPrice(ask.target))
        if ask.need_by is not None:
            facts.append(RequiredDelivery(ask.need_by))
        elif ask.lead_days is not None:
            facts.append(RequiredLeadTime(ask.lead_days))
        positions.append(position_of(line, facts))
    numbers = tuple(p.number for p in positions)
    priced = tuple(n for n in numbers if by_position[n].target is not None)
    dated = tuple(n for n in numbers if by_position[n].need_by is not None)
    timed = tuple(
        n
        for n in numbers
        if by_position[n].need_by is None and by_position[n].lead_days is not None
    )
    email_asks: list[Ask] = []
    if kind == "bafo":
        email_asks.append(Ask("best_and_final", positions=numbers))
    else:
        if priced:
            email_asks.append(Ask("revise_price", positions=priced))
        if dated:
            email_asks.append(Ask("deliver_by", positions=dated))
        if timed:
            email_asks.append(Ask("meet_lead_time", positions=timed))
    return replace(
        _email(header, kind),
        answered=answered,
        positions=tuple(positions),
        asks=tuple(email_asks),
        reply_by=reply_by,
        round=round if kind == "revised_offer" else None,
        mention_competition=mention_competition and kind in ("revised_offer", "bafo"),
        subset=0 < len(positions) < len(lines),
    )


def not_selected_email(header: Header, *, answered: Answered | None) -> SupplierEmail:
    """The outcome notice: no winner, prices, reasons or invitation to bid again."""
    return replace(_email(header, "not_selected"), answered=answered)
