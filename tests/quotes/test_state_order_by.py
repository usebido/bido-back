"""An order deadline in a revised offer is the offer's order-by date, not a basis change.

The RFQ-9 thread (`tests.quotes.rfq9`): Bido countered, the supplier lowered its
price and gave a delivery date "provided we receive the purchase order by
21/10/2026". That sentence is when the offer holds, so the amendment applies;
a real change of the commercial basis still holds the offer, and the change is
kept with both values for the buyer.
"""

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from bido_back.quotes.lead_time import decode_lead_time
from bido_back.quotes.state import OfferEvent, OfferState, reconcile, restore_quote, store_quote
from bido_back.quotes.validation import ValidatedQuote
from tests.quotes import rfq9


def _event(quote: ValidatedQuote, *, revised: bool) -> OfferEvent:
    message_id = uuid4()
    at = rfq9.REVISED_AT if revised else rfq9.QUOTE_AT
    return OfferEvent(
        message_id=message_id,
        message_header=f"<{message_id}@supplier.test>",
        # The revised offer answers Bido's counter-offer, not the supplier's quote.
        in_reply_to=rfq9.COUNTER_HEADER if revised else None,
        occurred_at=at,
        received_at=at + timedelta(minutes=1),
        quote=quote,
    )


def _thread(revised: ValidatedQuote) -> tuple[OfferState, OfferEvent, OfferEvent]:
    first = _event(rfq9.validate(rfq9.first_quote(), rfq9.QUOTE, at=rfq9.QUOTE_AT), revised=False)
    second = _event(revised, revised=True)
    return reconcile([first, second]), first, second


def _revised(**kwargs: str) -> ValidatedQuote:
    return rfq9.validate(rfq9.revised_offer(**kwargs), rfq9.REVISED, at=rfq9.REVISED_AT)


@pytest.mark.parametrize(
    ("deadline_as", "condition"),
    [
        # RFQ-9: the model filed the order deadline as the line's condition.
        ("condition", rfq9.DEADLINE),
        # The whole delivery sentence as the condition.
        ("condition", rfq9.DELIVERY),
        # RFQ-8: the same kind of sentence as a general offer note.
        ("note", rfq9.DEADLINE),
        # Only in the delivery statement the lead time cites.
        ("lead_time", rfq9.DEADLINE),
    ],
)
def test_a_revised_offer_with_an_order_deadline_is_applied(
    deadline_as: str, condition: str
) -> None:
    state, first, second = _thread(_revised(deadline_as=deadline_as, condition=condition))

    assert state.issues == ()
    assert state.held_message_ids == ()
    assert state.held_changes == ()
    assert state.reply_kind == "amendment"
    assert not state.quote.needs_review
    # The revised price and delivery date are the offer; the deadline is its order-by date.
    [line] = state.quote.lines
    assert line.parsed("unit_price") == Decimal("2.95")
    assert line.fields["unit_price"].message_id == second.message_id
    assert line.parsed("conditional_terms") is None
    lead = decode_lead_time(line.parsed("lead_time_terms"))
    assert lead is not None
    assert (lead.unit, lead.delivery_date) == ("date", rfq9.DELIVERED)
    assert state.quote.parsed("order_by") == rfq9.ORDER_BY
    assert state.quote.header["order_by"].message_id == second.message_id
    # What was not restated carries over under "other terms unchanged".
    assert line.parsed("tooling_cost") == Decimal("1250")
    assert state.quote.parsed("incoterm") == "DAP"
    assert state.quote.header["incoterm"].message_id == first.message_id
    # The supplier's words are the evidence, and a condition or note stays visible to the
    # buyer as an offer note.
    source = state.quote.header["order_by"].extracted.source
    assert source is not None
    assert "21/10/2026" in source.quote
    if deadline_as != "lead_time":
        assert any("21/10/2026" in note for note in state.quote.output.offer_notes)
    assert restore_quote(store_quote(state.quote)) == state.quote


def test_the_order_deadline_is_validated_out_of_the_line_conditions() -> None:
    revised = _revised()
    [line] = revised.lines
    assert line.parsed("conditional_terms") is None
    assert "conditional_terms_require_review" not in line.issues
    order_by = revised.header["order_by"]
    assert order_by.parsed == rfq9.ORDER_BY
    assert not order_by.needs_review
    assert order_by.extracted.source is not None
    assert "21/10/2026" in order_by.extracted.source.quote
    assert rfq9.DEADLINE in revised.output.offer_notes


def test_a_first_quote_with_an_order_deadline_has_no_condition_to_review() -> None:
    body = rfq9.QUOTE.replace("valid 30 days.", f"valid 30 days, {rfq9.DEADLINE}.")
    output = rfq9.first_quote()
    output.lines[0].conditional_terms = rfq9.ev(rfq9.DEADLINE, rfq9.DEADLINE)
    quote = rfq9.validate(output, body, at=rfq9.QUOTE_AT)
    assert not quote.needs_review
    assert quote.parsed("order_by") == rfq9.ORDER_BY


def test_an_order_deadline_with_another_condition_stays_a_condition() -> None:
    condition = "Price valid for a minimum of 2,000 pcs, if ordered by 21/10/2026"
    body = rfq9.REVISED.replace(rfq9.DEADLINE, condition)
    output = rfq9.revised_offer(condition=condition)
    state, _, second = _thread(rfq9.validate(output, body, at=rfq9.REVISED_AT))

    assert "changed_commercial_basis_requires_replacement" in state.issues
    [change] = state.held_changes
    assert (change.code, change.previous, change.stated) == (
        "pos1.conditional_terms",
        None,
        condition,
    )
    assert change.message_id == second.message_id
    assert not change.applied
    # The offer stays as it was: the earlier price and lead time.
    assert state.quote.lines[0].parsed("unit_price") == Decimal("3.20")
    assert state.quote.parsed("order_by") is None


def test_a_date_that_reads_two_ways_is_no_deadline() -> None:
    # 05/10/2027: 5 October or 10 May; nothing in the message settles it.
    condition = "provided we receive the purchase order by 05/10/2027"
    body = (
        "Revised: 2,000 pcs at BRL 2.95 per piece; tooling BRL 1,250 (unchanged).\n"
        f"Delivery in 6 weeks, {condition}.\n{rfq9.UNCHANGED}."
    )
    output = rfq9.revised_offer(condition=condition)
    output.lines[0].lead_time = rfq9.lead("6", "Delivery in 6 weeks", "weeks")
    quote = rfq9.validate(output, body, at=rfq9.REVISED_AT)
    assert quote.parsed("order_by") is None
    assert quote.lines[0].parsed("conditional_terms") == condition


def test_a_changed_unit_still_holds_the_offer_and_keeps_both_values() -> None:
    body = rfq9.REVISED.replace("BRL 2.95 per piece", "BRL 2.95 per kg")
    output = rfq9.revised_offer(unit="kg")
    output.lines[0].unit_price = rfq9.ev("2.95", "2,000 pcs at BRL 2.95 per kg")
    output.lines[0].unit = rfq9.ev("kg", "2,000 pcs at BRL 2.95 per kg")
    state, _, second = _thread(rfq9.validate(output, body, at=rfq9.REVISED_AT))

    assert state.issues == ("changed_commercial_basis_requires_replacement",)
    assert state.held_message_ids == (second.message_id,)
    [change] = state.held_changes
    assert (change.code, change.previous, change.stated, change.applied) == (
        "pos1.unit",
        "piece",
        "kg",
        False,
    )
    assert state.provenance()["held_changes"] == [change.provenance()]
    # The offer as it was: the first price, no order deadline.
    assert state.quote.lines[0].parsed("unit_price") == Decimal("3.20")
    assert state.quote.parsed("order_by") is None
    # The held reply's own values are kept with it.
    assert second.quote.lines[0].parsed("unit_price") == Decimal("2.95")


def test_the_same_unit_in_other_words_is_no_change() -> None:
    body = rfq9.REVISED.replace("BRL 2.95 per piece", "BRL 2.95 per pc")
    output = rfq9.revised_offer(unit="pcs")
    state, _, _ = _thread(rfq9.validate(output, body, at=rfq9.REVISED_AT))
    assert state.issues == ()
    assert state.quote.lines[0].parsed("unit_price") == Decimal("2.95")


def test_a_replacement_clears_the_held_changes() -> None:
    body = rfq9.REVISED.replace("BRL 2.95 per piece", "BRL 2.95 per kg")
    output = rfq9.revised_offer(unit="kg")
    state, first, held = _thread(rfq9.validate(output, body, at=rfq9.REVISED_AT))
    assert state.held_changes
    replacement = rfq9.validate(rfq9.first_quote(), rfq9.QUOTE, at=rfq9.REVISED_AT)
    later = replace(
        _event(replacement, revised=True),
        occurred_at=rfq9.REVISED_AT + timedelta(days=1),
        received_at=rfq9.REVISED_AT + timedelta(days=1, minutes=1),
        in_reply_to=held.message_header,
    )
    assert reconcile([first, held, later]).held_changes == ()


def test_an_order_deadline_without_a_year_is_the_next_such_day() -> None:
    condition = "if ordered by 21/10"
    body = rfq9.REVISED.replace(rfq9.DEADLINE, condition)
    quote = rfq9.validate(rfq9.revised_offer(condition=condition), body, at=rfq9.REVISED_AT)
    assert quote.parsed("order_by") == date(2026, 10, 21)
    assert quote.lines[0].parsed("conditional_terms") is None
