"""The RFQ-9 thread (dev, 2026-10-12): a counter-offer answered with an order deadline.

Synthetic test data from a deployed test. The supplier's replies are verbatim
in substance; the extractions are what a correct model returns under prompt v6.
The revised offer was held as `changed_commercial_basis_requires_replacement`
because the model filed "provided we receive the purchase order by 21/10/2026"
as the line's `conditional_terms`; on RFQ-8 the same kind of sentence went to
`offer_notes` and passed.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Final
from uuid import uuid4

from bido_back.agents.catalog.contracts import (
    ExtractedValue,
    LeadTimeValue,
    ProposalLineDraft,
    QuoteExtractionOutput,
    SourceRef,
    ValidityValue,
)
from bido_back.quotes.validation import QuoteSources, RequestedLine, ValidatedQuote, validate_quote

SUBJECT: Final = "Re: [RFQ-9] Request for quotation: stamped brackets BRK-200"
QUOTE_AT: Final = datetime(2026, 10, 9, 14, tzinfo=UTC)
# Bido's counter-offer went out in between (target price, delivery date).
COUNTER_HEADER: Final = "<counter-rfq9@bido.test>"
REVISED_AT: Final = datetime(2026, 10, 12, 15, tzinfo=UTC)

LINE: Final = RequestedLine(id=uuid4(), position=1, quantities=(Decimal(2000),))

QUOTE: Final = """Hello,

Thank you for your request.

We can offer 2,000 pcs at BRL 3.20 per piece (total BRL 6,400.00); tooling BRL 1,250.
Lead time 6 weeks after receipt of the purchase order.
DAP Centro 1000, 30 days net, valid 30 days.

Best regards,
Sales"""

DEADLINE: Final = "provided we receive the purchase order by 21/10/2026"
DELIVERY_DATE: Final = "Delivery: all 2,000 pcs by 14/11/2026"
DELIVERY: Final = f"{DELIVERY_DATE}, {DEADLINE}."
UNCHANGED: Final = "Other terms unchanged (DAP Centro 1000, 30 days net, valid 30 days)"

REVISED: Final = f"""Hello,

Thank you for your counter-offer. Our revised offer:

2,000 pcs at BRL 2.95 per piece (total BRL 5,900.00); tooling BRL 1,250 (unchanged).
{DELIVERY}
{UNCHANGED}.

Best regards,
Sales"""

NONE: Final = ExtractedValue(value=None, source=None, confidence=0)
NO_LEAD: Final = LeadTimeValue(value=None, source=None, confidence=0)


def ev(value: str, quote: str, confidence: float = 0.95) -> ExtractedValue:
    return ExtractedValue(
        value=value, source=SourceRef(kind="email_body", quote=quote), confidence=confidence
    )


def lead(value: str, quote: str, unit: str) -> LeadTimeValue:
    return LeadTimeValue.model_validate(
        {
            "value": value,
            "source": {"kind": "email_body", "quote": quote},
            "confidence": 0.95,
            "unit": unit,
            "basis": "order",
        }
    )


def bracket(
    price: str,
    priced: str,
    *,
    unit: str = "piece",
    tooling: str = "tooling BRL 1,250",
    lead_time: LeadTimeValue = NO_LEAD,
    conditional_terms: ExtractedValue = NONE,
) -> ProposalLineDraft:
    return ProposalLineDraft(
        rfq_line_position=1,
        offered_mpn=NONE,
        min_quantity=NONE,
        unit_price=ev(price, priced),
        unit=ev(unit, priced),
        tooling_cost=ev("1250", tooling),
        moq=NONE,
        origin_country=NONE,
        qualification=NONE,
        lead_time=lead_time,
        conditional_terms=conditional_terms,
    )


def first_quote() -> QuoteExtractionOutput:
    return QuoteExtractionOutput(
        is_quote=True,
        currency=ev("BRL", "BRL 3.20"),
        valid_until=NONE,
        validity=ValidityValue(
            value="30",
            unit="calendar_days",
            source=SourceRef(kind="email_body", quote="valid 30 days"),
            confidence=0.95,
        ),
        incoterm=ev("DAP", "DAP Centro 1000"),
        delivery_location=ev("Centro 1000", "DAP Centro 1000"),
        payment_terms=ev("30 days net", "30 days net"),
        lead_time=lead("6", "Lead time 6 weeks after receipt of the purchase order", "weeks"),
        lines=[bracket("3.20", "2,000 pcs at BRL 3.20 per piece")],
    )


def revised_offer(
    *,
    deadline_as: str = "condition",
    unit: str = "piece",
    condition: str = DEADLINE,
) -> QuoteExtractionOutput:
    """The revised offer as an amendment. `deadline_as` says where the model put the
    order deadline: the line's `condition` (RFQ-9), an offer `note` (RFQ-8), or
    nowhere but the delivery sentence (`lead_time`)."""
    return QuoteExtractionOutput(
        is_quote=True,
        currency=ev("BRL", "BRL 2.95"),
        valid_until=NONE,
        incoterm=NONE,
        payment_terms=NONE,
        lines=[
            bracket(
                "2.95",
                "2,000 pcs at BRL 2.95 per piece",
                unit=unit,
                tooling="tooling BRL 1,250 (unchanged)",
                lead_time=lead(
                    "2026-11-14",
                    DELIVERY.rstrip(".") if deadline_as == "lead_time" else DELIVERY_DATE,
                    "date",
                ),
                conditional_terms=ev(condition, condition) if deadline_as == "condition" else NONE,
            )
        ],
        reply_kind="amendment",
        unchanged_terms=ev("Other terms unchanged", UNCHANGED),
        update_evidence=ev("Our revised offer", "Our revised offer"),
        offer_notes=[DELIVERY] if deadline_as == "note" else [],
    )


def validate(
    output: QuoteExtractionOutput, body: str, *, at: datetime, subject: str = SUBJECT
) -> ValidatedQuote:
    """As the extraction job validates it: the supplier writes a point decimal, in BRL."""
    return validate_quote(
        output,
        requested=(LINE,),
        sources=QuoteSources(email_subject=subject, email_body=body),
        today=at.date(),
        decimal=".",
        offer_date=at.date(),
        rfq_currency="BRL",
    )


ORDER_BY: Final = date(2026, 10, 21)
DELIVERED: Final = date(2026, 11, 14)
