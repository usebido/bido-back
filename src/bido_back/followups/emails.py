"""The follow-up emails as the standard layout's typed input (`bido_back.supplier_email`).

The sweep decides what to ask (`bido_back.followups.missing` and the ledger); these
pure functions turn that decision into a `SupplierEmail`, which code renders in the
thread's language:

- a chase (no offer yet) is the layout's reminder of the RFQ
  (`supplier_email.builders.reminder_email`);
- a clarification asks what the latest offer lacks: its currency and validity, the
  values marked missing or to confirm under each position
  (`supplier_email.builders.clarification_email`), and, for a line whose stated lead
  time cannot be shown to meet its need-by date, "can you deliver by <need-by>? If
  not, what is the earliest date?", under the lead time it quoted;
- a reminder of a clarification the supplier has not answered asks its open items
  again, as they were asked, with a new reply-by date.

That is at most three asks (the offer's terms, the positions' values, the delivery
dates), the layout's limit. A line without a price is asked for its price (the
layout has no "or tell us you do not quote it" for one position yet).
"""

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Final, Literal

from bido_back.agents.catalog.contracts import FollowupItem
from bido_back.quotes.lead_time import StoredLeadTime
from bido_back.supplier_email.builders import (
    Gap,
    Header,
    RequestedLine,
    clarification_email,
    position_of,
)
from bido_back.supplier_email.model import (
    Answered,
    Ask,
    Fact,
    LeadTime,
    OfferField,
    Position,
    QuotedLeadTime,
    ReplyBy,
    RequiredDelivery,
    SupplierEmail,
)

# The ledger's fields the layout marks under a position or asks for the whole offer.
_OFFER_FIELDS: Final[dict[str, OfferField]] = {
    "currency": "currency",
    "valid_until": "valid_until",
    "unit_price": "unit_price",
    "quantity": "quantity",
    "lead_time": "lead_time",
}


def quoted_lead_time(lead: StoredLeadTime | None) -> LeadTime | None:
    """A stated lead time as the email shows it ("approx. 10 business days after order").

    Only whole calendar or business days: a lead time in weeks, months or as a date is
    not restated (the email shows the required date alone).
    """
    if lead is None or lead.amount is None or lead.amount != lead.amount.to_integral_value():
        return None
    basis: Literal["calendar", "business"]
    match lead.unit:
        case "calendar_days":
            basis = "calendar"
        case "business_days":
            basis = "business"
        case _:
            return None
    days = int(lead.amount)
    if days < 0:
        return None
    return LeadTime(days=days, basis=basis, approximate=lead.approximate)


def clarification_of(
    header: Header,
    *,
    answered: Answered | None,
    lines: Sequence[RequestedLine],
    items: Sequence[FollowupItem],
    reply_by: ReplyBy | None,
    quoted: Mapping[int, LeadTime] | None = None,
) -> SupplierEmail:
    """One clarification of exactly `items` (the ledger's asks, see the module docstring).

    `quoted` is the lead time each position's offer states, shown above a delivery
    question.
    """
    gaps = [
        Gap(item.position, _OFFER_FIELDS[item.field], item.problem)
        for item in items
        if item.field in _OFFER_FIELDS
    ]
    email = clarification_email(
        header, answered=answered, lines=lines, gaps=gaps, reply_by=reply_by
    )
    by_position = {line.position: line for line in lines}
    deliveries = sorted(
        {
            item.position: item.need_by
            for item in items
            if item.field == "delivery_date"
            and item.position is not None
            and item.position in by_position
            and item.need_by is not None
        }.items()
    )
    if not deliveries:
        return email
    positions: dict[int, Position] = {position.number: position for position in email.positions}
    for number, need_by in deliveries:
        facts: list[Fact] = []
        lead = (quoted or {}).get(number)
        if lead is not None:
            facts.append(QuotedLeadTime(lead))
        facts.append(RequiredDelivery(need_by))
        current = positions.get(number)
        positions[number] = (
            position_of(by_position[number], facts)
            if current is None
            else replace(current, facts=(*current.facts, *facts))
        )
    shown = tuple(positions[number] for number in sorted(positions))
    return replace(
        email,
        positions=shown,
        asks=(*email.asks, Ask("deliver_by", positions=tuple(n for n, _ in deliveries))),
        subset=0 < len(shown) < len(lines),
    )
