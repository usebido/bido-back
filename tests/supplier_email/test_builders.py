"""The builders: what the services decided becomes the email's typed input, nothing more."""

from datetime import date
from decimal import Decimal

from bido_back.supplier_email.builders import (
    Gap,
    LineAsk,
    clarification_email,
    line_code,
    negotiation_email,
    negotiation_type,
)
from bido_back.supplier_email.model import (
    Ask,
    LeadTime,
    MissingValues,
    Money,
    OfferedTerms,
    RequiredDelivery,
    RequiredLeadTime,
    TargetPrice,
    ValuesToConfirm,
)
from tests.supplier_email import rfq6


def test_a_clarification_asks_the_offer_terms_then_the_positions_concerned() -> None:
    email = clarification_email(
        rfq6.header("pt"),
        answered=None,
        lines=rfq6.LINES,
        gaps=(
            Gap(None, "valid_until", "missing"),
            Gap(None, "currency", "unclear"),
            Gap(2, "lead_time", "missing"),
            Gap(2, "unit_price", "unclear"),
            Gap(2, "lead_time", "unclear"),
        ),
        reply_by=None,
    )
    assert email.asks == (
        Ask("offer_terms", fields=("currency", "valid_until"), confirm=False),
        Ask("complete_positions", positions=(2,)),
    )
    [position] = email.positions
    assert position.number == 2 and position.code == "000102"
    # Missing wins over unclear for the same field; each field once, in a fixed order.
    assert position.facts == (MissingValues(("lead_time",)), ValuesToConfirm(("unit_price",)))
    assert email.subset


def test_a_counter_is_a_revised_offer_when_a_price_is_asked_else_a_delivery_date_request() -> None:
    price = LineAsk(1, offered=Money(Decimal("19.50"), "BRL"), target=Money(Decimal(18), "BRL"))
    lead = LineAsk(2, offered_lead=LeadTime(15), lead_days=10)
    assert negotiation_type("counter", [price, lead]) == "revised_offer"
    assert negotiation_type("counter", [lead]) == "delivery_date"
    assert negotiation_type("bafo", [lead]) == "bafo"

    revised = negotiation_email(
        rfq6.header("en"),
        action="counter",
        round=2,
        answered=None,
        lines=rfq6.LINES,
        asks=[price, lead],
        mention_competition=True,
        reply_by=None,
    )
    assert revised.type == "revised_offer" and revised.round == 2
    assert revised.mention_competition
    assert revised.asks == (
        Ask("revise_price", positions=(1,)),
        Ask("meet_lead_time", positions=(2,)),
    )
    first, second = revised.positions
    # The supplier's quoted lead time shows only where a lead time is asked.
    assert first.facts == (
        OfferedTerms(price=Money(Decimal("19.50"), "BRL")),
        TargetPrice(Money(Decimal(18), "BRL")),
    )
    assert second.facts == (OfferedTerms(lead_time=LeadTime(15)), RequiredLeadTime(10))

    dated = negotiation_email(
        rfq6.header("en"),
        action="counter",
        round=1,
        answered=None,
        lines=rfq6.LINES,
        asks=[LineAsk(2, need_by=date(2026, 10, 22))],
        mention_competition=True,
        reply_by=None,
    )
    assert dated.type == "delivery_date" and dated.round is None
    # Competitiveness is about prices: never said in a delivery-date request.
    assert not dated.mention_competition
    assert dated.asks == (Ask("deliver_by", positions=(2,)),)
    assert dated.positions[0].facts == (RequiredDelivery(date(2026, 10, 22)),)


def test_asks_for_lines_the_rfq_does_not_have_are_ignored() -> None:
    email = negotiation_email(
        rfq6.header("en"),
        action="bafo",
        round=3,
        answered=None,
        lines=rfq6.LINES[:1],
        asks=[LineAsk(1, target=Money(Decimal(18), "BRL")), LineAsk(9, lead_days=3)],
        mention_competition=False,
        reply_by=None,
    )
    assert [p.number for p in email.positions] == [1]
    assert email.asks == (Ask("best_and_final", positions=(1,)),)
    assert not email.subset


def test_the_code_is_the_buyers_own_with_a_different_mpn_in_brackets() -> None:
    assert line_code("000101", None) == "000101"
    assert line_code("TH-1", "NCP18XH103") == "TH-1 (NCP18XH103)"
    assert line_code("NCP18XH103", "NCP18XH103") == "NCP18XH103"
    assert line_code(None, "NCP18XH103") == "NCP18XH103"
    assert line_code(None, None) is None
