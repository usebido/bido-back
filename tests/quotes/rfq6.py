"""The RFQ-6 thread (2026-10-08, MetalNova Teste Ltda, a Brazilian supplier writing English).

Synthetic test data the buyer wrote for a deployed test. The supplier's two
replies are verbatim; the extractions are what a correct model returns under
prompt v6, and `V4_CATCH_ALL` is the condition text the v4 model copied onto
every line of the first reply.
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

SUBJECT: Final = "Re: [RFQ-6] Request for quotation: Cotação de componentes para motor MTR-100"
# Both replies arrived on 2026-10-09 UTC (23:33 and 23:38 in Brasília, 2026-10-08).
REPLY_1_AT: Final = datetime(2026, 10, 9, 2, 33, tzinfo=UTC)
REPLY_2_AT: Final = datetime(2026, 10, 9, 2, 38, tzinfo=UTC)
OFFER_DATE: Final = date(2026, 10, 9)

LINES: Final = (
    RequestedLine(id=uuid4(), position=1, quantities=(Decimal(70),)),
    RequestedLine(id=uuid4(), position=2, quantities=(Decimal(15),)),
    RequestedLine(id=uuid4(), position=3, quantities=(Decimal("12.5"),)),
)

REPLY_1: Final = """Hello,

Thank you for reaching out!

We can supply the requested items. Our current prices are BRL 19.50 per piece for the bearings (000101), BRL 89.00 per piece for the shafts (000102), and BRL 95.00 per kg for the copper wire (000104).

Our standard payment terms are Net 30, and the quotation is valid for 15 days. The MOQ is 10 pieces for bearings and shafts, and 5 kg for copper wire. All items are supplied in standard industrial packaging.

Lead time is approximately 10 business days after order confirmation. We may need a few additional days for the shafts, depending on production availability.

Let us know if these conditions work for you.

Best regards,
Sales Team
MetalNova Teste"""  # noqa: E501

REPLY_2: Final = """Hello Biduuuuu,

Thank you for your follow-up.

Our quotation is valid for 15 calendar days from the date of our original offer.

The quoted prices apply to the quantities requested in RFQ-6:

For the bearings (000101), BRL 19.50 per piece applies to the requested 70 pieces, with a lead time of 7 business days.

For the shafts (000102), BRL 89.00 per piece applies to the requested 15 pieces, with a lead time of 12 business days due to our current production schedule.

For the copper wire (000104), BRL 95.00 per kg applies to the requested 12.5 kg, with a lead time of 8 business days.

All lead times are counted from purchase order confirmation.

Please let us know if you would like to proceed.

Best regards,
Sales Team
MetalNova Teste"""  # noqa: E501

SHAFT_RISK: Final = (
    "We may need a few additional days for the shafts, depending on production availability."
)
# What the v4 model put in `conditional_terms` on every line of the first reply.
V4_CATCH_ALL: Final = (
    "Our standard payment terms are Net 30, and the quotation is valid for 15 days. The MOQ is "
    "10 pieces for bearings and shafts, and 5 kg for copper wire. All items are supplied in "
    "standard industrial packaging.\n\nLead time is approximately 10 business days after order "
    "confirmation."
)

NONE: Final = ExtractedValue(value=None, source=None, confidence=0)


def ev(value: str, quote: str, confidence: float = 0.95) -> ExtractedValue:
    return ExtractedValue(
        value=value, source=SourceRef(kind="email_body", quote=quote), confidence=confidence
    )


def lead(
    value: str | None,
    quote: str | None,
    *,
    unit: str | None = "business_days",
    basis: str | None = None,
    approximate: bool = False,
    risk_note: str | None = None,
    confidence: float = 0.95,
) -> LeadTimeValue:
    return LeadTimeValue.model_validate(
        {
            "value": value,
            "source": {"kind": "email_body", "quote": quote} if quote is not None else None,
            "confidence": confidence if value is not None else 0,
            "unit": unit if value is not None else None,
            "basis": basis,
            "approximate": approximate,
            "risk_note": risk_note,
        }
    )


NO_LEAD: Final = LeadTimeValue(value=None, source=None, confidence=0)


def line(
    position: int,
    part: str,
    price: str,
    per: str,
    *,
    moq: ExtractedValue = NONE,
    min_quantity: ExtractedValue = NONE,
    lead_time: LeadTimeValue = NO_LEAD,
    conditional_terms: ExtractedValue = NONE,
) -> ProposalLineDraft:
    priced = f"BRL {price} per {per}"
    return ProposalLineDraft(
        rfq_line_position=position,
        offered_mpn=ev(part, f"({part})"),
        min_quantity=min_quantity,
        unit_price=ev(price, priced),
        unit=ev(per, priced),
        tooling_cost=NONE,
        moq=moq,
        origin_country=NONE,
        qualification=NONE,
        lead_time=lead_time,
        conditional_terms=conditional_terms,
    )


def first_reply(*, conditions: str | None = None) -> QuoteExtractionOutput:
    """Reply 1 as prompt v6 reads it; `conditions` copies a text onto every line (v4)."""
    moq_pieces = "The MOQ is 10 pieces for bearings and shafts"
    terms = ev(conditions, conditions) if conditions is not None else NONE
    shaft_terms = (
        ev(f"{conditions} {SHAFT_RISK}", f"{conditions} {SHAFT_RISK}")
        if conditions is not None
        else NONE
    )
    return QuoteExtractionOutput(
        is_quote=True,
        currency=ev("BRL", "BRL 19.50"),
        valid_until=NONE,
        validity=ValidityValue(
            value="15",
            unit="calendar_days",
            source=SourceRef(kind="email_body", quote="the quotation is valid for 15 days"),
            confidence=0.95,
        ),
        incoterm=NONE,
        payment_terms=ev("Net 30", "Our standard payment terms are Net 30"),
        lead_time=lead(
            "10",
            "Lead time is approximately 10 business days after order confirmation",
            basis="order",
            approximate=True,
        ),
        lines=[
            line(1, "000101", "19.50", "piece", moq=ev("10", moq_pieces), conditional_terms=terms),
            line(
                2,
                "000102",
                "89.00",
                "piece",
                moq=ev("10", moq_pieces),
                lead_time=lead(None, None, risk_note=SHAFT_RISK),
                conditional_terms=shaft_terms,
            ),
            line(
                3,
                "000104",
                "95.00",
                "kg",
                moq=ev("5", "5 kg for copper wire"),
                conditional_terms=terms,
            ),
        ],
        offer_notes=["All items are supplied in standard industrial packaging."],
    )


def second_reply() -> QuoteExtractionOutput:
    """Reply 2 as prompt v6 reads it: per-line business days, the basis stated once."""

    def answered(
        position: int, part: str, price: str, per: str, qty: str, days: int
    ) -> ProposalLineDraft:
        return line(
            position,
            part,
            price,
            per,
            min_quantity=ev(qty, f"the requested {qty}"),
            lead_time=lead(str(days), f"a lead time of {days} business days"),
        )

    return QuoteExtractionOutput(
        is_quote=True,
        currency=ev("BRL", "BRL 19.50"),
        valid_until=NONE,
        validity=ValidityValue(
            value="15",
            unit="calendar_days",
            source=SourceRef(
                kind="email_body",
                quote="valid for 15 calendar days from the date of our original offer",
            ),
            confidence=0.95,
        ),
        incoterm=NONE,
        payment_terms=NONE,
        lead_time=LeadTimeValue(
            value=None,
            source=None,
            confidence=0,
            basis="order",
        ),
        lines=[
            answered(1, "000101", "19.50", "piece", "70", 7),
            answered(2, "000102", "89.00", "piece", "15", 12),
            answered(3, "000104", "95.00", "kg", "12.5", 8),
        ],
        reply_kind="amendment",
        update_evidence=ev(
            "The quoted prices apply to the quantities requested in RFQ-6",
            "The quoted prices apply to the quantities requested in RFQ-6",
        ),
    )


def validate(
    output: QuoteExtractionOutput,
    body: str,
    *,
    offer_date: date = OFFER_DATE,
    original_offer_date: date | None = None,
    country: str | None = None,
) -> ValidatedQuote:
    """As the extraction job validates it: MetalNova was created without a country."""
    return validate_quote(
        output,
        requested=LINES,
        sources=QuoteSources(email_subject=SUBJECT, email_body=body),
        today=OFFER_DATE,
        decimal=".",
        offer_date=offer_date,
        original_offer_date=original_offer_date,
        country=country,
    )
