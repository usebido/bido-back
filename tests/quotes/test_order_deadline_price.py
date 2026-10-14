"""An order deadline that names the line's own price is still only an order deadline.

The RFQ-9 thread (dev, 2026-10-14), after the best and final offer at BRL 2.90 per
piece: "to hold the price of BRL 2.90 per piece and delivery by 14/11/2026, the
purchase order must be placed by 24/10/2026". The price it names is the line's,
carried forward: the sentence is the offer's order-by date, not a new price
condition. Any other price, currency, unit or basis, a second price or a discount
keeps it a condition, and the amendment is held.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Final
from uuid import uuid4

import pytest

from bido_back.agents.catalog.contracts import (
    ExtractedValue,
    ProposalLineDraft,
    QuoteExtractionOutput,
    SourceRef,
    ValidityValue,
)
from bido_back.quotes.lead_time import decode_lead_time
from bido_back.quotes.state import OfferEvent, OfferState, reconcile
from bido_back.quotes.validation import ValidatedQuote
from tests.quotes import rfq9

FINAL_AT: Final = datetime(2026, 10, 13, 15, tzinfo=UTC)
AMENDED_AT: Final = datetime(2026, 10, 14, 10, tzinfo=UTC)

FINAL: Final = """Hello,

Our best and final offer for RFQ-9:

2,000 pcs at BRL 2.90 per piece (total BRL 5,800.00); tooling BRL 1,250.
Delivery: all 2,000 pcs by 14/11/2026.
DAP Centro 1000, 30 days net, valid 30 days.

Best regards,
Sales"""

# The supplier's sentence, verbatim.
ADDITION: Final = (
    "to hold the price of BRL 2.90 per piece and delivery by 14/11/2026, "
    "the purchase order must be placed by 24/10/2026"
)
# A preservation statement the current reader accepts (the live wording, "All other
# terms of our final offer are unchanged", is read by a separate change).
KEPT: Final = "All other terms unchanged."
ORDER_BY: Final = date(2026, 10, 24)
DELIVERED: Final = date(2026, 11, 14)


def _amendment_body(addition: str, *, restated: bool = False) -> str:
    # `restated`: the line's price is also written on its own, so the line's price stays
    # grounded when the sentence names another one.
    price = "\n2,000 pcs at BRL 2.90 per piece." if restated else ""
    return f"""Hello,

further to our best and final offer for RFQ-9, one addition: {addition}. {KEPT}{price}

Best regards,
Sales"""


def _final_offer() -> QuoteExtractionOutput:
    return QuoteExtractionOutput(
        is_quote=True,
        currency=rfq9.ev("BRL", "BRL 2.90"),
        valid_until=rfq9.NONE,
        validity=ValidityValue(
            value="30",
            unit="calendar_days",
            source=SourceRef(kind="email_body", quote="valid 30 days"),
            confidence=0.95,
        ),
        incoterm=rfq9.ev("DAP", "DAP Centro 1000"),
        delivery_location=rfq9.ev("Centro 1000", "DAP Centro 1000"),
        payment_terms=rfq9.ev("30 days net", "30 days net"),
        lines=[
            rfq9.bracket(
                "2.90",
                "2,000 pcs at BRL 2.90 per piece",
                lead_time=rfq9.lead("2026-11-14", rfq9.DELIVERY_DATE, "date"),
            )
        ],
    )


def _amendment(
    addition: str = ADDITION,
    *,
    price: ExtractedValue | None = None,
    unit: ExtractedValue | None = None,
) -> QuoteExtractionOutput:
    """The addition as the model filed it on RFQ-9: the restated line's condition."""
    line = ProposalLineDraft(
        rfq_line_position=1,
        offered_mpn=rfq9.NONE,
        min_quantity=rfq9.NONE,
        unit_price=price or rfq9.ev("2.90", "BRL 2.90 per piece"),
        unit=unit or rfq9.ev("piece", "BRL 2.90 per piece"),
        tooling_cost=rfq9.NONE,
        moq=rfq9.NONE,
        origin_country=rfq9.NONE,
        qualification=rfq9.NONE,
        lead_time=rfq9.lead("2026-11-14", "delivery by 14/11/2026", "date"),
        conditional_terms=rfq9.ev(addition, addition),
    )
    return QuoteExtractionOutput(
        is_quote=True,
        currency=rfq9.ev("BRL", "BRL 2.90"),
        valid_until=rfq9.NONE,
        incoterm=rfq9.NONE,
        payment_terms=rfq9.NONE,
        lines=[line],
        reply_kind="amendment",
        unchanged_terms=rfq9.ev(KEPT, KEPT),
        update_evidence=rfq9.ev("one addition", "one addition"),
    )


def _event(quote: ValidatedQuote, at: datetime, *, in_reply_to: str | None) -> OfferEvent:
    message_id = uuid4()
    return OfferEvent(
        message_id=message_id,
        message_header=f"<{message_id}@supplier.test>",
        in_reply_to=in_reply_to,
        occurred_at=at,
        received_at=at + timedelta(minutes=1),
        quote=quote,
    )


def _thread(amended: ValidatedQuote) -> tuple[OfferState, OfferEvent, OfferEvent]:
    """RFQ-9 from the first quote: quote, best and final offer, the amendment."""
    first = _event(
        rfq9.validate(rfq9.first_quote(), rfq9.QUOTE, at=rfq9.QUOTE_AT),
        rfq9.QUOTE_AT,
        in_reply_to=None,
    )
    final = _event(
        rfq9.validate(_final_offer(), FINAL, at=FINAL_AT),
        FINAL_AT,
        in_reply_to=rfq9.COUNTER_HEADER,
    )
    amendment = _event(amended, AMENDED_AT, in_reply_to=final.message_header)
    return reconcile([first, final, amendment]), final, amendment


def _amended(output: QuoteExtractionOutput, body: str) -> ValidatedQuote:
    return rfq9.validate(output, body, at=AMENDED_AT)


def test_the_rfq9_sentence_restating_the_price_is_the_order_by_date() -> None:
    amended = _amended(_amendment(), _amendment_body(ADDITION))
    [line] = amended.lines
    assert line.parsed("conditional_terms") is None
    assert "conditional_terms_require_review" not in line.issues
    assert amended.parsed("order_by") == ORDER_BY
    assert ADDITION in amended.output.offer_notes

    state, final, amendment = _thread(amended)

    assert state.issues == ()
    assert state.held_message_ids == ()
    assert state.held_changes == ()
    assert not state.quote.needs_review
    [merged] = state.quote.lines
    assert merged.parsed("unit_price") == Decimal("2.90")
    assert merged.parsed("conditional_terms") is None
    assert state.quote.parsed("order_by") == ORDER_BY
    assert state.quote.header["order_by"].message_id == amendment.message_id
    lead = decode_lead_time(merged.parsed("lead_time_terms"))
    assert lead is not None
    assert lead.delivery_date == DELIVERED
    # What the amendment did not restate carries over from the final offer.
    assert merged.parsed("tooling_cost") == Decimal("1250")
    assert state.quote.header["incoterm"].message_id == final.message_id


@pytest.mark.parametrize(
    "addition",
    [
        # Another price than the line's.
        ADDITION.replace("BRL 2.90", "BRL 2.85"),
        # Another currency than the offer's.
        ADDITION.replace("BRL 2.90", "EUR 2.90"),
        # Two prices.
        ADDITION.replace(
            "BRL 2.90 per piece", "BRL 2.90 per piece, or BRL 2.80 per piece for 4,000 pcs,"
        ),
        # Another unit, another basis.
        ADDITION.replace("per piece", "per kg"),
        ADDITION.replace("BRL 2.90 per piece", "BRL 290 per 100 pcs"),
        # A quantity beside the price.
        ADDITION.replace("per piece", "per piece for 5,000 pcs"),
        # A bare price that is not the line's.
        ADDITION.replace("BRL 2.90", "2.85"),
    ],
)
def test_an_order_deadline_naming_another_price_holds_the_amendment(addition: str) -> None:
    state, _, amendment = _thread(
        _amended(_amendment(addition), _amendment_body(addition, restated=True))
    )

    assert state.issues == ("changed_commercial_basis_requires_replacement",)
    assert state.held_message_ids == (amendment.message_id,)
    [change] = state.held_changes
    assert (change.code, change.previous, change.stated) == (
        "pos1.conditional_terms",
        None,
        addition,
    )
    # The offer stays the final offer, without an order deadline.
    assert state.quote.lines[0].parsed("unit_price") == Decimal("2.90")
    assert state.quote.parsed("order_by") is None


def test_a_discount_with_an_order_deadline_holds_the_amendment() -> None:
    addition = "a discount of 5% applies if ordered by 24/10/2026"
    state, _, _ = _thread(_amended(_amendment(addition), _amendment_body(addition, restated=True)))
    assert state.issues == ("changed_commercial_basis_requires_replacement",)
    [change] = state.held_changes
    assert change.code == "pos1.conditional_terms"
    assert state.quote.parsed("order_by") is None


def test_the_same_price_bare_or_per_unit_in_other_words_is_the_order_by_date() -> None:
    for addition in (
        ADDITION.replace("BRL 2.90", "2.90"),
        ADDITION.replace("per piece", "per pc"),
        "BRL 2.90 per piece if ordered by 24/10/2026",
    ):
        amended = _amended(_amendment(addition), _amendment_body(addition, restated=True))
        assert amended.lines[0].parsed("conditional_terms") is None, addition
        assert amended.parsed("order_by") == ORDER_BY, addition


def test_without_the_line_price_to_compare_the_sentence_stays_a_condition() -> None:
    # The restated line has no price of its own: nothing proves the sentence's price is
    # the line's, so it fails closed.
    output = _amendment(price=rfq9.NONE)
    amended = _amended(output, _amendment_body(ADDITION))
    assert amended.lines[0].parsed("conditional_terms") == ADDITION
    assert amended.parsed("order_by") is None
