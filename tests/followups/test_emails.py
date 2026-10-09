"""The follow-up emails in the standard layout: what the ledger asks, rendered by code. No
database."""

from datetime import date
from decimal import Decimal

import pytest

from bido_back.agents.catalog.contracts import FollowupField, FollowupItem, FollowupQuestion
from bido_back.followups.emails import clarification_of, quoted_lead_time
from bido_back.quotes.lead_time import LeadTime as StatedLeadTime
from bido_back.quotes.lead_time import StoredLeadTime, decode_lead_time, encode_lead_time
from bido_back.supplier_email.checks import email_problems
from bido_back.supplier_email.model import (
    Answered,
    LeadTime,
    MissingValues,
    QuotedLeadTime,
    ReplyBy,
    RequiredDelivery,
)
from bido_back.supplier_email.render import render
from tests.supplier_email import rfq6

LANGUAGES = ("pt", "en", "de", "fr", "it", "es")
NEED_BY = date(2026, 10, 22)
REPLY_BY = ReplyBy(date(2026, 10, 13))
APPROX = LeadTime(10, "business", approximate=True)


def item(
    position: int | None,
    field: FollowupField,
    question: FollowupQuestion = "value",
    *,
    need_by: date | None = None,
) -> FollowupItem:
    return FollowupItem(
        position=position,
        line=None,
        field=field,
        problem="missing" if question in ("value", "price_or_decline") else "unclear",
        question=question,
        need_by=need_by,
    )


def delivery(position: int) -> FollowupItem:
    return item(position, "delivery_date", "deliver_by", need_by=NEED_BY)


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_rfq6_delivery_question_is_the_layouts_clarification(language: str) -> None:
    # After the first RFQ-6 reply the ledger asks one thing: can the bearings and the
    # shafts arrive by 22 Oct? It is the layout's own clarification A, to the byte.
    email = clarification_of(
        rfq6.header(language),
        answered=Answered("offer", rfq6.OFFER_DAY),
        lines=rfq6.LINES,
        items=[delivery(1), delivery(2)],
        reply_by=REPLY_BY,
        quoted={1: APPROX, 2: LeadTime(10, "business", approximate=True, plus_a_few_days=True)},
    )
    assert email == rfq6.clarification_a(language)
    assert email_problems(email, render(email), rfq6.gate(language)) == []


@pytest.mark.parametrize("language", LANGUAGES)
def test_terms_values_and_dates_go_in_one_email_of_at_most_three_asks(language: str) -> None:
    items = [
        item(None, "valid_until"),
        item(2, "quantity"),
        item(3, "unit_price", "price_or_decline"),
        delivery(1),
        delivery(2),
    ]
    email = clarification_of(
        rfq6.header(language),
        answered=Answered("offer", rfq6.OFFER_DAY),
        lines=rfq6.LINES,
        items=items,
        reply_by=REPLY_BY,
        quoted={1: APPROX},
    )
    assert [ask.kind for ask in email.asks] == ["offer_terms", "complete_positions", "deliver_by"]
    assert [ask.positions for ask in email.asks] == [(), (2, 3), (1, 2)]
    # Every position asked about once, its facts merged: the quoted lead time where known.
    assert [p.number for p in email.positions] == [1, 2, 3] and not email.subset
    facts = {p.number: p.facts for p in email.positions}
    assert facts[1] == (QuotedLeadTime(APPROX), RequiredDelivery(NEED_BY))
    assert facts[2] == (MissingValues(("quantity",)), RequiredDelivery(NEED_BY))
    assert facts[3] == (MissingValues(("unit_price",)),)
    assert email_problems(email, render(email), rfq6.gate(language)) == []


def test_without_a_delivery_question_it_is_the_layouts_clarification_of_gaps() -> None:
    email = clarification_of(
        rfq6.header("en"),
        answered=Answered("offer", rfq6.OFFER_DAY),
        lines=rfq6.LINES,
        items=[item(None, "valid_until"), item(1, "lead_time")],
        reply_by=REPLY_BY,
    )
    assert [ask.kind for ask in email.asks] == ["offer_terms", "complete_positions"]
    assert [p.number for p in email.positions] == [1] and email.subset


def _stored(unit: str, amount: str, *, approximate: bool = False) -> StoredLeadTime:
    """A lead time as the extraction stores it with a line."""
    stated = StatedLeadTime.model_validate(
        {"unit": unit, "amount": Decimal(amount), "approximate": approximate}
    )
    stored = decode_lead_time(encode_lead_time(stated, date(2026, 10, 9)))
    assert stored is not None
    return stored


def test_a_quoted_lead_time_is_shown_only_in_whole_days() -> None:
    assert quoted_lead_time(_stored("business_days", "10", approximate=True)) == APPROX
    assert quoted_lead_time(_stored("calendar_days", "21")) == LeadTime(21, "calendar")
    # Weeks, months, a date or half days are not restated: the required date says enough.
    assert quoted_lead_time(_stored("weeks", "3")) is None
    assert quoted_lead_time(_stored("business_days", "2.5")) is None
    assert quoted_lead_time(None) is None
