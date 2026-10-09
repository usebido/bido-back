"""Reading the stored terms and signals back (what follow-ups and negotiation consume)."""

from datetime import date
from decimal import Decimal

from pydantic import JsonValue

from bido_back.agents.catalog.contracts import SupplierQuestion
from bido_back.quotes.terms import (
    LINE_TERMS_KEY,
    OFFER_NOTES_KEY,
    SIGNALS_KEY,
    LineTerms,
    OfferTerms,
    SupplierSignals,
    line_terms,
    offer_terms,
)
from bido_back.quotes.validation import ValidatedQuote, quantity_bases
from tests.quotes import rfq6


def stored(result: ValidatedQuote) -> tuple[dict[str, JsonValue], list[dict[str, JsonValue]]]:
    """The proposal and line `fields` as the extraction job stores them."""
    proposal = result.provenance()
    proposal[OFFER_NOTES_KEY] = list(result.output.offer_notes)
    proposal[SIGNALS_KEY] = result.signals.model_dump(mode="json")
    bases = quantity_bases(result.lines, {line.id: line.quantities for line in rfq6.LINES})
    lines: list[dict[str, JsonValue]] = []
    for line, basis in zip(result.lines, bases, strict=True):
        fields = line.provenance()
        fields[LINE_TERMS_KEY] = basis.model_dump(mode="json")
        lines.append(fields)
    return proposal, lines


def flagged(fields: dict[str, JsonValue], name: str, flag: str) -> dict[str, JsonValue]:
    entry = fields[name]
    assert isinstance(entry, dict)
    return {**fields, name: {**entry, flag: True}}


def test_rfq6_terms_read_back() -> None:
    proposal, lines = stored(rfq6.validate(rfq6.first_reply(), rfq6.REPLY_1))

    offer = offer_terms(proposal)
    assert offer.validity is not None
    assert (offer.validity.amount, offer.validity.valid_until) == (Decimal(15), date(2026, 10, 24))
    assert offer.lead_time is not None and offer.lead_time.scope == "offer"
    assert offer.notes == ("All items are supplied in standard industrial packaging.",)
    assert offer.signals == SupplierSignals()

    shaft = line_terms(lines[1])
    assert shaft.lead_time is not None
    assert shaft.lead_time.risk_note == rfq6.SHAFT_RISK
    assert shaft.lead_time.expected.delivery_date == date(2026, 10, 26)
    assert shaft.quantity is not None
    assert shaft.quantity.quantity_basis == "requested_quantity"
    assert shaft.quantity.price_applies_to_requested_quantity


def test_a_correction_or_a_doubt_hides_the_converted_value() -> None:
    proposal, lines = stored(rfq6.validate(rfq6.first_reply(), rfq6.REPLY_1))
    corrected = flagged(lines[0], "lead_time_days", "corrected")
    doubtful = flagged(lines[0], "lead_time_days", "needs_review")
    fixed_date = flagged(proposal, "valid_until", "corrected")

    # A person's lead time (calendar days) wins over the supplier's statement.
    assert line_terms(corrected).lead_time is None
    assert line_terms(doubtful).lead_time is None
    assert offer_terms(fixed_date).validity is None


def test_signals_read_back() -> None:
    asked = "Can you send the drawing of the shaft?"
    output = rfq6.first_reply().model_copy(
        update={
            "supplier_questions": [
                SupplierQuestion(quote=asked, topic="technical", rfq_line_position=2)
            ]
        }
    )
    proposal, _ = stored(rfq6.validate(output, rfq6.REPLY_1 + "\n" + asked))

    signals = offer_terms(proposal).signals
    assert [(q.quote, q.topic, q.rfq_line_position, q.grounded) for q in signals.questions] == [
        (asked, "technical", 2, True)
    ]


def test_proposals_before_v5_read_as_empty() -> None:
    legacy_line: dict[str, object] = {
        "lead_time_days": {"value": "42", "parsed": 42, "needs_review": False, "issues": []},
        "_review": {"needs_review": False, "issues": []},
    }
    legacy_proposal: dict[str, object] = {"currency": {"value": "EUR", "parsed": "EUR"}}

    assert line_terms(legacy_line) == LineTerms()
    assert offer_terms(legacy_proposal) == OfferTerms()
    assert offer_terms({SIGNALS_KEY: {"final_offer": "maybe"}}).signals == SupplierSignals()
    assert line_terms({LINE_TERMS_KEY: {"quantity_basis": "unknown"}}).quantity is None
