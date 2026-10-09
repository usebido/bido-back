"""What a supplier's offer says besides prices, as typed values for the follow-ups and the
negotiation.

The extraction (`bido_back.quotes.extract_job`) stores these in the provenance
JSON of the proposal and its lines. Read them only through this module:

- `offer_terms(proposal.fields)`: the validity period, the offer-level lead
  time, general notes, and the signals of the message the proposal was
  extracted from (final offer, declined lines, the supplier's questions);
- `line_terms(proposal_line.fields)`: the line's lead time as data, and which
  quantity its price applies to.

Proposals extracted before prompt v5 carry none of this: the readers then
return empty values (no lead time, no signals, an unknown quantity basis).
"""

from collections.abc import Mapping
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, ValidationError

from bido_back.agents.catalog.contracts import QuestionTopic
from bido_back.quotes.lead_time import (
    StoredLeadTime,
    ValidityPeriod,
    decode_lead_time,
    decode_validity,
)

# Which quantity a line's price applies to:
# - `requested_quantity`: the only price the supplier gave for this part, with no
#   quantity of its own: it applies to the requested quantity (never ask for a tier);
# - `stated_tier`: the supplier wrote the quantity (tier) the price applies to;
# - `ambiguous`: several prices for the same part and no quantity for this one;
# - `unmatched`: the line answers no requested line.
QuantityBasis = Literal["requested_quantity", "stated_tier", "ambiguous", "unmatched"]

# Provenance keys written by the extraction.
SIGNALS_KEY = "_signals"
LINE_TERMS_KEY = "_terms"
OFFER_NOTES_KEY = "offer_notes"


class QuantityTerms(BaseModel):
    model_config = ConfigDict(frozen=True)

    quantity_basis: QuantityBasis
    # True when the price is valid for (one of) the requested quantities: a single
    # price without tiers, or a stated tier at or below a requested quantity.
    price_applies_to_requested_quantity: bool


class DeclinedLineSignal(BaseModel):
    model_config = ConfigDict(frozen=True)

    rfq_line_position: int
    # The supplier's refusal, verbatim (found in its message).
    quote: str


class QuestionSignal(BaseModel):
    model_config = ConfigDict(frozen=True)

    # The supplier's question, verbatim.
    quote: str
    topic: QuestionTopic
    # The requested line it concerns; None for the whole request.
    rfq_line_position: int | None
    # The quote was found word for word in the supplier's message or attachments.
    grounded: bool


class SupplierSignals(BaseModel):
    """What one supplier message says about the negotiation itself."""

    model_config = ConfigDict(frozen=True)

    # The supplier states this is its final / best offer (grounded statement).
    final_offer: bool = False
    final_offer_quote: str | None = None
    # Requested lines the supplier explicitly will not or cannot quote (grounded,
    # requested positions only, never a line it also prices in the same message).
    declined_lines: tuple[DeclinedLineSignal, ...] = ()
    # Every question the supplier asks; any question holds the offer for the buyer.
    questions: tuple[QuestionSignal, ...] = ()

    @property
    def declined_line_positions(self) -> tuple[int, ...]:
        return tuple(line.rfq_line_position for line in self.declined_lines)


class OfferTerms(BaseModel):
    model_config = ConfigDict(frozen=True)

    # The validity as stated duration and computed end date (`proposal.valid_until`
    # holds the same date). None when the supplier gave a date, gave nothing, or a
    # person corrected `valid_until`.
    validity: ValidityPeriod | None = None
    # The lead time stated for the whole offer (each line also carries the lead
    # time that applies to it: `line_terms`).
    lead_time: StoredLeadTime | None = None
    # General conditions of the whole offer (packaging, warranty...), verbatim. They
    # never block a line.
    notes: tuple[str, ...] = ()
    # The signals of the message this proposal version was extracted from
    # (`proposal.message_id`).
    signals: SupplierSignals = SupplierSignals()


class LineTerms(BaseModel):
    model_config = ConfigDict(frozen=True)

    # The lead time that applies to this line (its own, or the offer-level one with
    # `scope == "offer"`). None when there is none, when `lead_time_days` needs
    # review, or when a person corrected `lead_time_days` (the correction wins).
    lead_time: StoredLeadTime | None = None
    # None for lines extracted before prompt v5.
    quantity: QuantityTerms | None = None


def offer_terms(fields: Mapping[str, object]) -> OfferTerms:
    """The typed terms and signals stored in a proposal's `fields`."""
    validity = (
        decode_validity(_entry(fields, "validity").get("parsed"))
        if _usable(fields, "valid_until")
        else None
    )
    raw_notes = fields.get(OFFER_NOTES_KEY)
    notes = (
        tuple(note for note in cast(list[object], raw_notes) if isinstance(note, str))
        if isinstance(raw_notes, list)
        else ()
    )
    return OfferTerms(
        validity=validity,
        lead_time=decode_lead_time(_entry(fields, "lead_time").get("parsed")),
        notes=notes,
        signals=_signals(fields.get(SIGNALS_KEY)),
    )


def line_terms(fields: Mapping[str, object]) -> LineTerms:
    """The typed terms stored in a proposal line's `fields`."""
    lead_time = (
        decode_lead_time(_entry(fields, "lead_time_terms").get("parsed"))
        if _usable(fields, "lead_time_days")
        else None
    )
    quantity: QuantityTerms | None = None
    raw = fields.get(LINE_TERMS_KEY)
    if isinstance(raw, dict):
        try:
            quantity = QuantityTerms.model_validate(raw)
        except ValidationError:
            quantity = None
    return LineTerms(lead_time=lead_time, quantity=quantity)


def _entry(fields: Mapping[str, object], name: str) -> dict[str, object]:
    entry = fields.get(name)
    return cast(dict[str, object], entry) if isinstance(entry, dict) else {}


def _usable(fields: Mapping[str, object], name: str) -> bool:
    """The derived field carries a value nobody flagged or replaced."""
    entry = _entry(fields, name)
    return (
        entry.get("parsed") is not None
        and entry.get("needs_review") is not True
        and entry.get("corrected") is not True
    )


def _signals(raw: object) -> SupplierSignals:
    if not isinstance(raw, dict):
        return SupplierSignals()
    try:
        return SupplierSignals.model_validate(raw)
    except ValidationError:
        return SupplierSignals()
