"""Deterministic checks on a quote extraction: the model proposes, this code decides.

For every extracted value the model gives the value, the exact text it read it
from (`source.quote`) with a locator, and a confidence. Here we check, without
any model:

- the quote really is in the cited source (email body, PDF page, sheet cell);
- the value is really in the quote: numbers read locale-aware (`1.234,56` vs
  `1,234.56`) with the separator the supplier's own numbers prove, never the
  language it is registered with (`proven_decimal_separator`); a number that
  still reads two ways (`1,500`) is a requested tier quantity when one reading
  is, else it goes to review. Weeks converted to days, dates in the usual
  written forms, currencies by code or symbol;
- the value is well-formed and in range (ISO currency, Incoterm, ISO date...);
- the confidence is at least `REVIEW_CONFIDENCE`;
- each line maps to a requested RFQ line; an offered MPN that differs from the
  requested one marks the line as an alternative;
- required values are present and the quote is not already expired;
- the offer answers what the RFQ asked: a quoted quantity that is none of the
  requested tiers (`quantity_mismatch`, on the line), an Incoterm, a delivery
  location or a currency other than the RFQ's (`incoterm_mismatch`,
  `delivery_location_mismatch`, `currency_mismatch`, on the proposal). These
  are warnings for the buyer, never rejections: the quote still registers.

Anything that fails marks the field, and through it the line or the proposal,
`needs_review`. Nothing is dropped: the persisted provenance keeps the raw
value, the source, the confidence and the reasons.

Durations are data, not numbers (prompt v6):

- a lead time is an amount and a unit (calendar days, business days, weeks,
  months, or a date), a basis, an approximate flag and a risk note. The
  offer-level lead time applies to every line without its own. Code converts it
  to calendar days from the offer date with the supplier's holiday calendar
  (`bido_back.quotes.lead_time`) and stores that in `lead_time_days`, which is
  always CALENDAR days; a unit other than calendar days leaves its conversion
  (the number as written, the unit, the rule) in that field's provenance
  (`conversion`). The statement itself is kept in the line's `lead_time_terms`
  field and, for the offer-level one, in the header's `lead_time` field
  (`parsed` holds it as JSON). A lead time written the older way, as a
  `lead_time_days` number with its unit in the cited quote (prompts up to v5:
  business days as written, weeks converted), is read as the same statement and
  counted the same way;
- a validity given as a duration ("valid for 15 days") becomes `valid_until`,
  counted from the supplier message's date (or its first offer's, when the text
  says so). The duration is kept in the `validity` header field.

Review issues of a converted value always sit on the field a person can correct
(`lead_time_days`, `valid_until`); the `lead_time_terms`, `lead_time` and
`validity` fields only carry the statement and never need review themselves.

Also deterministic, never the model's call:

- an unverified `min_quantity` equal to a requested quantity was copied from the
  request: it is dropped, not flagged (a single price without tiers applies to
  the requested quantity, see `quantity_bases`);
- a condition repeated on the lines of every requested position is a general
  term of the offer: it becomes an offer note and blocks no line; a condition
  sentence only about delivery timing ("a few additional days for the shafts")
  becomes that line's lead-time risk note;
- an order deadline ("provided we receive the purchase order by 21/10/2026", in a
  line's condition, an offer note or a lead-time statement) is the offer's `order_by`
  header field: the earliest such date the supplier wrote. A condition sentence that
  says nothing else, or names a price only to restate the line's own price ("to hold
  the price of BRL 2.90 per piece, the purchase order must be placed by 24/10/2026"),
  leaves the line's conditions (it becomes an offer note), so an amendment that adds
  one is not a change of the offer's commercial basis;
- the supplier's signals (final offer, declined lines, questions) are grounded
  against its message (`ValidatedQuote.signals`).
"""

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import Decimal
from typing import Final, Literal
from uuid import UUID

from pydantic import JsonValue

from bido_back.agents.catalog.contracts import (
    ExtractedValue,
    LeadTimeBasis,
    LeadTimeValue,
    ProposalLineDraft,
    QuoteExtractionOutput,
    SourceRef,
    ValidityValue,
)
from bido_back.comparison.compute import same_unit
from bido_back.documents import DocumentText
from bido_back.quotes.lead_time import (
    LeadTime,
    LeadTimeScope,
    PeriodUnit,
    ValidityPeriod,
    decode_lead_time,
    encode_lead_time,
    expected_delivery,
    holiday_calendar_for,
    validity_end,
)
from bido_back.quotes.parsing import (
    INCOTERMS,
    ISO_CURRENCIES,
    DecimalSeparator,
    WrittenDate,
    business_days_to_calendar,
    dates_in,
    day_months_in,
    decimal_separator_in,
    detect_currencies,
    find_number,
    incoterms_in,
    is_delivery_timing,
    is_order_deadline_only,
    mentions_approximation,
    mentions_calendar_days,
    mentions_days,
    mentions_months,
    mentions_none,
    mentions_original_offer,
    mentions_stock,
    mentions_weeks,
    mentions_working_days,
    normalize_text,
    numeric_date_order,
    order_deadlines,
    own_words,
    parse_canonical,
    parse_decimal,
    ranges_in,
    restated_price,
    stated_values,
)
from bido_back.quotes.terms import (
    DeclinedLineSignal,
    QuantityTerms,
    QuestionSignal,
    SupplierSignals,
)

REVIEW_CONFIDENCE: Final = 0.8
# Above what the numeric columns can hold (Numeric(18, 6) has 12 integer digits).
MAX_AMOUNT: Final = Decimal(10) ** 12
MAX_LEAD_TIME_DAYS: Final = 3650

FieldKind = Literal[
    "amount",
    "quantity",
    "days",
    "currency",
    "date",
    "incoterm",
    "country",
    "mpn",
    "text",
    # A stated lead time / validity period: `parsed` is its JSON (`quotes.lead_time`).
    "lead_time",
    "validity",
]

HEADER_FIELDS: Final[Mapping[str, FieldKind]] = {
    "currency": "currency",
    "valid_until": "date",
    "incoterm": "incoterm",
    "delivery_location": "text",
    "payment_terms": "text",
    "validity": "validity",
    "lead_time": "lead_time",
    # The date the supplier must receive the order by for its offer to hold ("provided
    # we receive the PO by 21/10/2026"): derived by code, never extracted by the model.
    "order_by": "date",
}
LINE_FIELDS: Final[Mapping[str, FieldKind]] = {
    "offered_mpn": "mpn",
    "min_quantity": "quantity",
    "unit_price": "amount",
    "unit": "text",
    "tooling_cost": "amount",
    "moq": "quantity",
    "lead_time_days": "days",
    "origin_country": "country",
    "qualification": "text",
    "price_basis_quantity": "quantity",
    "freight_cost": "amount",
    "tax_rate_percent": "amount",
    "conditional_terms": "text",
    # The lead time that applies to the line (its own or the offer's), as JSON.
    "lead_time_terms": "lead_time",
}
# Fields checked by `check_field`; the others are derived below.
_GENERIC_HEADER: Final = (
    "currency",
    "valid_until",
    "incoterm",
    "delivery_location",
    "payment_terms",
)
_DERIVED_LINE: Final = frozenset({"lead_time_terms", "lead_time_days"})
# A quantity the model could only have copied from the request.
_UNVERIFIED: Final = frozenset({"not_grounded", "quote_not_found", "no_source"})

type Parsed = Decimal | int | date | str


# --- Inputs ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RequestedLine:
    """What the RFQ asked for on one line: what a quoted line is compared against."""

    id: UUID
    position: int
    mpn: str | None = None
    # The requested quantity tiers: a quoted quantity must be one of them, and a
    # copied `min_quantity` is recognized by them.
    quantities: tuple[Decimal, ...] = ()
    incoterm: str | None = None
    delivery_location: str | None = None


@dataclass(frozen=True, slots=True)
class QuoteSources:
    """The texts the model saw, to check its quotes against."""

    email_subject: str
    email_body: str
    # By marker name (`DocumentText.filename`), as SourceRef.attachment cites them.
    documents: Mapping[str, DocumentText] = field(default_factory=dict[str, DocumentText])

    @property
    def email_text(self) -> str:
        return f"{self.email_subject}\n{self.email_body}"


# --- Results -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FieldCheck:
    name: str
    extracted: ExtractedValue
    # The validated value; None when absent or unusable (see `issues`).
    parsed: Parsed | None
    issues: tuple[str, ...] = ()
    message_id: UUID | None = None
    retained_by: tuple[dict[str, JsonValue], ...] = ()
    corrected: bool = False
    correction_id: str | None = None
    # How `parsed` was derived from what the supplier wrote when the units differ (a
    # lead time in business days, weeks, months or a date): the number as written,
    # its unit and the rule applied (`_conversion`).
    conversion: Mapping[str, JsonValue] | None = None

    @property
    def needs_review(self) -> bool:
        return bool(self.issues)

    def provenance(self) -> dict[str, JsonValue]:
        source = self.extracted.source
        data: dict[str, JsonValue] = {
            "value": self.extracted.value,
            "parsed": _json(self.parsed),
            "source": source.model_dump(mode="json") if source is not None else None,
            "confidence": self.extracted.confidence,
            "needs_review": self.needs_review,
            "issues": list(self.issues),
        }
        if self.conversion is not None:
            data["conversion"] = dict(self.conversion)
        if self.message_id is not None:
            data["message_id"] = str(self.message_id)
        if self.retained_by:
            data["retained_by"] = list(self.retained_by)
        if self.corrected:
            data["corrected"] = True
        if self.correction_id is not None:
            data["correction_id"] = self.correction_id
        return data


@dataclass(frozen=True, slots=True)
class LineCheck:
    draft: ProposalLineDraft
    rfq_line_id: UUID | None
    is_alternative: bool
    fields: Mapping[str, FieldCheck]
    issues: tuple[str, ...] = ()
    # Informational, not a reason for review (e.g. an alternative part was offered).
    notes: tuple[str, ...] = ()

    @property
    def needs_review(self) -> bool:
        return bool(self.issues) or any(f.needs_review for f in self.fields.values())

    def parsed(self, name: str) -> Parsed | None:
        return self.fields[name].parsed

    def provenance(self) -> dict[str, JsonValue]:
        data: dict[str, JsonValue] = {name: f.provenance() for name, f in self.fields.items()}
        data["_review"] = {
            "needs_review": self.needs_review,
            "issues": list(self.issues),
            "notes": list(self.notes),
            "rfq_line_position": self.draft.rfq_line_position,
            "model_is_alternative": self.draft.is_alternative,
        }
        return data


@dataclass(frozen=True, slots=True)
class ValidatedQuote:
    # The model's output, with `technical_questions` completed from
    # `supplier_questions` and `offer_notes` reduced to verified sentences.
    output: QuoteExtractionOutput
    header: Mapping[str, FieldCheck]
    lines: tuple[LineCheck, ...]
    issues: tuple[str, ...] = ()
    # The signals of this message only. Not part of a stored offer state: the
    # extraction job stores them per proposal (`bido_back.quotes.terms`).
    signals: SupplierSignals = field(default_factory=SupplierSignals)

    @property
    def is_quote(self) -> bool:
        return self.output.is_quote

    @property
    def needs_review(self) -> bool:
        return (
            bool(self.issues)
            or any(f.needs_review for f in self.header.values())
            or any(line.needs_review for line in self.lines)
        )

    def parsed(self, name: str) -> Parsed | None:
        return self.header[name].parsed

    def provenance(self) -> dict[str, JsonValue]:
        data: dict[str, JsonValue] = {name: f.provenance() for name, f in self.header.items()}
        data["_review"] = {
            "needs_review": self.needs_review,
            "issues": list(self.issues),
            "missing_fields": list(self.output.missing_fields),
            "declined": self.output.declined,
        }
        return data


# --- Validation ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Context:
    sources: QuoteSources
    decimal: DecimalSeparator | None
    review_confidence: float
    # The supplier message's date: durations are converted from it.
    reference: date
    original_offer_date: date | None
    holiday_calendar: str | None


def validate_quote(
    output: QuoteExtractionOutput,
    *,
    requested: Sequence[RequestedLine],
    sources: QuoteSources,
    today: date,
    decimal: DecimalSeparator | None = None,
    review_confidence: float = REVIEW_CONFIDENCE,
    offer_date: date | None = None,
    original_offer_date: date | None = None,
    country: str | None = None,
    rfq_currency: str | None = None,
) -> ValidatedQuote:
    """Check an extraction against its sources and the RFQ.

    `decimal` is the decimal separator the supplier's own writing proves
    (`proven_decimal_separator`, never its registered language); without it, numbers
    like `1,000` are ambiguous and go to review, unless a tier quantity can be read
    as one of the requested quantities.
    `offer_date` is the date of the supplier's message (default `today`):
    lead times and validity periods run from it. `original_offer_date` is the
    date of the supplier's first offer in this thread, for a validity "from the
    date of our original offer". `country` is the supplier's ISO country, whose
    public holidays business days skip; without it, the offer currency's country
    when only one country uses that currency (BRL: Brazil).
    `rfq_currency` is the RFQ's base currency: a quote in another one is a
    `currency_mismatch` (a warning, so the buyer sees it before comparing).
    """

    def check(name: str, kind: FieldKind, value: ExtractedValue) -> FieldCheck:
        return check_field(
            name, kind, value, sources, decimal=decimal, review_confidence=review_confidence
        )

    generic = {
        name: check(name, HEADER_FIELDS[name], getattr(output, name)) for name in _GENERIC_HEADER
    }
    currency = generic["currency"]
    trusted_currency = currency.parsed if isinstance(currency.parsed, str) else None
    ctx = _Context(
        sources=sources,
        decimal=decimal,
        review_confidence=review_confidence,
        reference=offer_date or today,
        original_offer_date=original_offer_date,
        holiday_calendar=holiday_calendar_for(
            country, trusted_currency if not currency.needs_review else None
        ),
    )
    offer_lead = _check_lead_time(output.lead_time, ctx, scope="offer")
    validity = _check_validity(output.validity, ctx)
    derived = {
        "valid_until": _valid_until(generic["valid_until"], validity),
        "validity": _validity_field(validity),
        "lead_time": _lead_time_field(
            "lead_time", offer_lead, offer_lead.lead_time if offer_lead else None, ctx
        ),
        "order_by": absent_field("order_by"),
    }
    merged = {**generic, **derived}
    header = {name: merged[name] for name in HEADER_FIELDS}
    by_position = {line.position: line for line in requested}
    notes = _verified_notes(output.offer_notes, sources)
    signals = _signals(output, by_position, sources, review_confidence)
    if not output.is_quote and output.reply_kind == "replacement":
        return ValidatedQuote(
            output=_normalized(output, notes), header=header, lines=(), signals=signals
        )

    drafts: list[ProposalLineDraft] = []
    line_fields: list[dict[str, FieldCheck]] = []
    for draft in output.lines:
        position = draft.rfq_line_position
        fields = {
            name: check(name, kind, getattr(draft, name))
            for name, kind in LINE_FIELDS.items()
            if name not in _DERIVED_LINE
        }
        fields.update(_line_lead_time(draft, offer_lead, output.lead_time, ctx))
        asked = by_position.get(position) if position is not None else None
        fields["min_quantity"] = _as_requested_tier(fields["min_quantity"], asked, sources, decimal)
        fields["min_quantity"], draft = _without_copied_quantity(
            fields["min_quantity"], draft, asked
        )
        drafts.append(draft)
        line_fields.append({name: fields[name] for name in LINE_FIELDS})
    drafts, line_fields, general = _general_terms(drafts, line_fields, notes)
    drafts, line_fields = _timing_out_of_conditions(drafts, line_fields, ctx)
    notes = _unique([*notes, *general])
    drafts, line_fields, deadline_notes, order_by = _order_by_out_of_conditions(
        output, drafts, line_fields, notes, currency, ctx
    )
    notes = _unique([*notes, *deadline_notes])
    header = {**header, "order_by": order_by}
    lines = [
        _check_line(
            draft, fields, by_position, currency, partial=output.reply_kind != "replacement"
        )
        for draft, fields in zip(drafts, line_fields, strict=True)
    ]

    issues: list[str] = []
    if not lines and output.reply_kind == "replacement":
        issues.append("no_lines")
    if (
        output.reply_kind == "replacement"
        and currency.extracted.value is None
        and any(line.draft.unit_price.value is not None for line in lines)
    ):
        issues.append("missing:currency")
    valid_until = header["valid_until"].parsed
    if isinstance(valid_until, date) and valid_until < today:
        issues.append("validity_expired")
    issues.extend(_rfq_mismatches(header, lines, requested, rfq_currency))
    for conflict in output.conflicts:
        # Even an ungrounded alleged conflict is held; neither claim wins silently.
        issues.append(f"conflicting_sources:{conflict.field}")
    for name in ("unchanged_terms", "update_evidence"):
        evidence = check(name, "text", getattr(output, name))
        if evidence.needs_review:
            issues.append(f"unverified:{name}")
    return ValidatedQuote(
        output=_normalized(output, notes),
        header=header,
        lines=tuple(lines),
        issues=tuple(issues),
        signals=signals,
    )


def _check_line(
    draft: ProposalLineDraft,
    fields: dict[str, FieldCheck],
    by_position: Mapping[int, RequestedLine],
    currency: FieldCheck,
    *,
    partial: bool = False,
) -> LineCheck:
    issues: list[str] = []
    notes: list[str] = []

    position = draft.rfq_line_position
    requested = by_position.get(position) if position is not None else None
    if position is None:
        issues.append("unmatched_rfq_line")
    elif requested is None:
        issues.append("unknown_rfq_line")

    if draft.unit_price.value is None and not partial:
        issues.append("missing:unit_price")

    priced = [
        fields[n]
        for n in ("unit_price", "tooling_cost", "freight_cost")
        if fields[n].parsed is not None
    ]
    if priced and (currency.needs_review or (currency.parsed is None and not partial)):
        # A price is meaningless without a trusted currency.
        issues.append("currency_unverified")
    for check in priced:
        source = check.extracted.source
        named: set[str] = detect_currencies(source.quote) if source is not None else set()
        if named and currency.parsed is not None and currency.parsed not in named:
            issues.append(f"currency_mismatch:{check.name}")

    is_alternative = draft.is_alternative
    offered = fields["offered_mpn"].parsed
    if requested is not None and requested.mpn and isinstance(offered, str):
        if _alnum(offered) != _alnum(requested.mpn):
            is_alternative = True
    if is_alternative:
        notes.append("alternative_offered")
    quantity = fields["min_quantity"].parsed
    if (
        requested is not None
        and requested.quantities
        and isinstance(quantity, Decimal)
        and quantity not in requested.quantities
    ):
        # 50 pcs quoted for a 1,500 pcs request: the price may not hold for the
        # quantity the buyer needs. A warning, never a rejection. (A quantity the
        # model copied from the request is dropped before: it matches a tier anyway.)
        issues.append("quantity_mismatch")
    tax = fields["tax_rate_percent"].parsed
    if isinstance(tax, Decimal) and tax > 100:
        issues.append("invalid_tax_rate")
    if fields["conditional_terms"].parsed is not None:
        issues.append("conditional_terms_require_review")

    return LineCheck(
        draft=draft,
        rfq_line_id=requested.id if requested is not None else None,
        is_alternative=is_alternative,
        fields=fields,
        issues=tuple(issues),
        notes=tuple(notes),
    )


def _rfq_mismatches(
    header: Mapping[str, FieldCheck],
    lines: Sequence[LineCheck],
    requested: Sequence[RequestedLine],
    rfq_currency: str | None,
) -> list[str]:
    """The quote's terms against the RFQ's: Incoterm, delivery location, currency.

    Compared with the requested lines the quote answers (every requested line
    when none could be matched). Only parsed, grounded values are compared: a
    value the quote does not state is not a mismatch.
    """
    issues: list[str] = []
    matched = {line.rfq_line_id for line in lines if line.rfq_line_id is not None}
    against = [line for line in requested if line.id in matched] or list(requested)

    incoterm = _trusted(header["incoterm"])
    if incoterm is not None and any(
        line.incoterm and line.incoterm.upper() != incoterm for line in against
    ):
        issues.append("incoterm_mismatch")

    location = _trusted(header["delivery_location"])
    if location is not None and any(
        line.delivery_location and not _same_place(location, line.delivery_location)
        for line in against
    ):
        issues.append("delivery_location_mismatch")

    currency = _trusted(header["currency"])
    if currency is not None and rfq_currency and currency != rfq_currency.upper():
        issues.append("currency_mismatch")
    return issues


def _trusted(check: FieldCheck) -> str | None:
    """The value when the quote backs it; an unverified value is already on review."""
    return check.parsed if isinstance(check.parsed, str) and not check.issues else None


def _same_place(quoted: str, requested: str) -> bool:
    """`Joinville/SC` is `Joinville-SC` and `Joinville`; `Curitiba` is not.

    Place names are compared as sets of words without accents, case or
    punctuation (Incoterm codes dropped): one must contain the other.
    """
    a, b = _place_words(quoted), _place_words(requested)
    return not a or not b or a <= b or b <= a


def _place_words(text: str) -> set[str]:
    plain = "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    ).casefold()
    return {w for w in re.split(r"[^0-9a-z]+", plain) if w and w.upper() not in INCOTERMS}


def absent_field(name: str) -> FieldCheck:
    """A field the supplier did not state."""
    return FieldCheck(
        name=name, extracted=ExtractedValue(value=None, source=None, confidence=0), parsed=None
    )


# --- Lead times ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _LeadCheck:
    raw: LeadTimeValue
    # None when the value cannot be used (no unit, not a number or a date).
    lead_time: LeadTime | None
    issues: tuple[str, ...]
    # The model wrote the number itself in `lead_time_days` (prompts up to v5): that
    # field keeps the value as written.
    written: bool = False


def _check_lead_time(
    value: LeadTimeValue, ctx: _Context, *, scope: LeadTimeScope
) -> _LeadCheck | None:
    """The stated lead time checked against its quote: the number, and its unit word."""
    raw = (value.value or "").strip()
    if not raw:
        return None
    issues = _evidence_issues(value, ctx)
    source = value.source
    quote = (_spanned_cell(source, ctx.sources) or source.quote) if source is not None else ""
    header = _column_header(source, ctx.sources) if source is not None else None
    unit_text = f"{quote}\n{header}" if header else quote
    risk = _verbatim(value.risk_note, ctx.sources)
    approximate = value.approximate or mentions_approximation(quote)
    basis: LeadTimeBasis = value.basis or "unspecified"

    def done(lead: LeadTime | None, *more: str) -> _LeadCheck:
        return _LeadCheck(value, lead, tuple(dict.fromkeys((*issues, *more))))

    unit = value.unit
    if unit is None:
        return done(None, "unit_missing")
    if unit == "date":
        try:
            delivery = date.fromisoformat(raw)
        except ValueError:
            return done(None, "invalid_value")
        lead = LeadTime(
            unit="date",
            delivery_date=delivery,
            basis=basis,
            approximate=approximate,
            risk_note=risk,
            scope=scope,
            holiday_calendar=ctx.holiday_calendar,
        )
        if quote.strip() and not _date_grounded(delivery, quote, ctx.reference):
            return done(lead, "not_grounded")
        return done(lead)

    amount = parse_canonical(raw)
    if amount is None:
        amount = parse_decimal(raw, decimal=ctx.decimal)
    whole_days = unit in ("calendar_days", "business_days")
    if amount is None or amount < 0 or (whole_days and amount != amount.to_integral_value()):
        return done(None, "invalid_value")
    more: list[str] = []
    if quote.strip():
        match = find_number(amount, quote, decimal=ctx.decimal)
        if not match.found and not (amount == 0 and mentions_stock(quote)):
            more.append("not_grounded")
        elif match.ambiguous:
            more.append("ambiguous_number")
        if not _unit_grounded(unit, unit_text):
            more.append("unit_not_grounded")
        highs = [high for low, high in ranges_in(quote) if amount in (low, high)]
        if highs:
            # A range is approximate, and the buyer plans with its end.
            approximate = True
            amount = max(highs)
    if _rough_days(amount, unit) > MAX_LEAD_TIME_DAYS:
        return done(None, *more, "out_of_range")
    lead = LeadTime(
        unit=unit,
        amount=amount,
        basis=basis,
        approximate=approximate,
        risk_note=risk,
        scope=scope,
        holiday_calendar=ctx.holiday_calendar,
    )
    return done(lead, *more)


def _line_lead_time(
    draft: ProposalLineDraft, offer: _LeadCheck | None, offer_raw: LeadTimeValue, ctx: _Context
) -> dict[str, FieldCheck]:
    """The line's `lead_time_terms` and `lead_time_days`: its own lead time, else the
    offer's.

    The basis and the risk note complete each other: "All lead times are counted
    from purchase order confirmation" gives every line its basis, and "a few
    additional days for the shafts" stays on the shafts' line.
    """
    own = _check_lead_time(draft.lead_time, ctx, scope="line")
    if own is None:
        # Prompts up to v5 wrote a line's lead time as a `lead_time_days` number.
        own = _written_lead_time(draft.lead_time_days, ctx)
    chosen = own if own is not None else offer
    if chosen is None:
        return {
            "lead_time_terms": absent_field("lead_time_terms"),
            "lead_time_days": absent_field("lead_time_days"),
        }
    lead = chosen.lead_time
    if lead is not None:
        other = offer_raw if chosen is own else draft.lead_time
        basis = lead.basis if lead.basis != "unspecified" else other.basis or "unspecified"
        risk = _verbatim(draft.lead_time.risk_note, ctx.sources) or lead.risk_note
        lead = lead.model_copy(update={"basis": basis, "risk_note": risk})
    return {
        "lead_time_terms": _lead_time_field("lead_time_terms", chosen, lead, ctx),
        "lead_time_days": _lead_time_days_field(chosen, lead, ctx),
    }


def _lead_time_field(
    name: str, chosen: _LeadCheck | None, lead: LeadTime | None, ctx: _Context
) -> FieldCheck:
    """The stated lead time, as evidence; review issues go on `lead_time_days`."""
    if chosen is None:
        return absent_field(name)
    raw = chosen.raw
    return FieldCheck(
        name=name,
        extracted=ExtractedValue(
            value=_describe(lead) if lead is not None else raw.value,
            source=raw.source,
            confidence=raw.confidence,
        ),
        parsed=encode_lead_time(lead, ctx.reference) if lead is not None else None,
    )


def _lead_time_days_field(chosen: _LeadCheck, lead: LeadTime | None, ctx: _Context) -> FieldCheck:
    """Calendar days from the offer date to the expected delivery: what comparisons use.

    A lead time in another unit records how it was converted (`conversion`).
    """
    issues = list(chosen.issues)
    days: int | None = None
    if lead is not None:
        days = expected_delivery(lead, ctx.reference).calendar_days
        if not 0 <= days <= MAX_LEAD_TIME_DAYS:
            issues.append("out_of_range")
            days = None
    raw = chosen.raw
    return FieldCheck(
        name="lead_time_days",
        extracted=ExtractedValue(
            value=str(days) if days is not None and not chosen.written else raw.value,
            source=raw.source,
            confidence=raw.confidence,
        ),
        parsed=days,
        issues=tuple(dict.fromkeys(issues)),
        conversion=(
            _conversion(lead, days, ctx.reference)
            if lead is not None and days is not None
            else None
        ),
    )


# How `lead_time_days` (calendar days) is derived from a lead time in another unit.
_CONVERSION_RULES: Final[Mapping[str, str]] = {
    "business_days": (
        "business days counted from the reference date, skipping weekends and the "
        "holiday calendar's public holidays"
    ),
    "weeks": "weeks * 7",
    "months": "calendar months from the reference date; part months 30 days each",
    "date": "days from the reference date to the stated date",
}


def _conversion(lead: LeadTime, days: int, reference: date) -> dict[str, JsonValue] | None:
    """What the supplier wrote and the rule that made it calendar days; None when it was
    calendar days already."""
    if lead.unit == "calendar_days":
        return None
    quoted: JsonValue = None
    if lead.unit == "date":
        quoted = lead.delivery_date.isoformat() if lead.delivery_date is not None else None
    elif lead.amount is not None:
        whole = lead.amount == lead.amount.to_integral_value()
        quoted = int(lead.amount) if whole else format(lead.amount, "f")
    conversion: dict[str, JsonValue] = {
        "quoted": quoted,
        "unit": lead.unit,
        "calendar_days": days,
        "reference_date": reference.isoformat(),
        "rule": _CONVERSION_RULES[lead.unit],
    }
    if lead.unit == "business_days":
        conversion["holiday_calendar"] = lead.holiday_calendar
    return conversion


def _written_lead_time(value: ExtractedValue, ctx: _Context) -> _LeadCheck | None:
    """A line's lead time as prompts up to v5 wrote it: a `lead_time_days` number with
    the supplier's unit in its cited quote (v5: business days as written, never
    converted; v4: calendar days, weeks converted to days).

    It is read as the statement prompt v6 writes, unit and all, and counted the same
    way. A number the model converted itself (42 for "6 Wochen", 35 for "25 dias
    úteis") is traced back to the number in the quote.
    """
    text = (value.value or "").strip()
    if not text:
        return None
    issues = _evidence_issues(value, ctx)
    parsed, value_issues = _parse("days", text, ctx.decimal)
    source = value.source
    quote = (_spanned_cell(source, ctx.sources) or source.quote) if source is not None else ""
    if not isinstance(parsed, int):
        raw = LeadTimeValue(value=value.value, source=source, confidence=value.confidence)
        return _LeadCheck(raw, None, tuple(dict.fromkeys((*issues, *value_issues))), written=True)
    unit: PeriodUnit = "calendar_days"
    amount = Decimal(parsed)
    approximate = False
    if quote.strip():
        header = _column_header(source, ctx.sources) if source is not None else None
        unit, amount, grounding = _written_unit(parsed, quote, ctx.decimal, header=header)
        issues.extend(grounding)
        approximate = mentions_approximation(quote)
        highs = [high for low, high in ranges_in(quote) if amount in (low, high)]
        if highs:
            # A range is approximate, and the buyer plans with its end.
            approximate = True
            amount = max(highs)
    raw = LeadTimeValue(value=value.value, source=source, confidence=value.confidence, unit=unit)
    if _rough_days(amount, unit) > MAX_LEAD_TIME_DAYS:
        return _LeadCheck(raw, None, tuple(dict.fromkeys((*issues, "out_of_range"))), written=True)
    lead = LeadTime(
        unit=unit,
        amount=amount,
        approximate=approximate,
        scope="line",
        holiday_calendar=ctx.holiday_calendar,
    )
    return _LeadCheck(raw, lead, tuple(dict.fromkeys(issues)), written=True)


def _written_unit(
    written: int, quote: str, decimal: DecimalSeparator | None, *, header: str | None
) -> tuple[PeriodUnit, Decimal, list[str]]:
    """The unit and number a `lead_time_days` value stands for, read from its quote.

    `header` is the column header of a cited sheet cell (`Lieferzeit (Wochen)`),
    which may carry the unit the cell omits. Returns the unit, the number of those
    units the quote states, and the grounding issues.
    """
    unit_text = quote if header is None else f"{quote}\n{header}"
    business = mentions_working_days(unit_text)
    weeks = mentions_weeks(unit_text) and not mentions_days(unit_text)
    direct = find_number(Decimal(written), quote, decimal=decimal)
    if direct.found:
        issues = ["ambiguous_number"] if direct.ambiguous else []
        if business:
            return "business_days", Decimal(written), issues
        if weeks:
            return "weeks", Decimal(written), issues
        return "calendar_days", Decimal(written), issues
    # Not written as such: the model converted. The number it read must be there.
    if business:
        for raw in range(max(0, written * 5 // 7 - 1), written * 5 // 7 + 2):
            if (
                business_days_to_calendar(raw) == written
                and find_number(Decimal(raw), quote, decimal=decimal).found
            ):
                return "business_days", Decimal(raw), []
    if (
        weeks
        and written % 7 == 0
        and find_number(Decimal(written // 7), quote, decimal=decimal).found
    ):
        return "weeks", Decimal(written // 7), []
    return "calendar_days", Decimal(written), ["not_grounded"]


def _describe(lead: LeadTime) -> str:
    if lead.unit == "date" and lead.delivery_date is not None:
        return lead.delivery_date.isoformat()
    amount = format(lead.amount, "f") if lead.amount is not None else "?"
    return f"{amount} {lead.unit}"


def _unit_grounded(unit: PeriodUnit, text: str) -> bool:
    """The unit word is in the quote (or its sheet column header). Plain days are calendar."""
    match unit:
        case "calendar_days":
            if mentions_working_days(text) and not mentions_calendar_days(text):
                return False
            return mentions_days(text) or not (mentions_weeks(text) or mentions_months(text))
        case "business_days":
            return mentions_working_days(text)
        case "weeks":
            return mentions_weeks(text)
        case "months":
            return mentions_months(text)


def _rough_days(amount: Decimal, unit: PeriodUnit) -> Decimal:
    """An upper estimate in calendar days, for the range check only."""
    match unit:
        case "calendar_days":
            return amount
        case "business_days":
            return amount * 2
        case "weeks":
            return amount * 7
        case "months":
            return amount * 31


def _date_grounded(value: date, quote: str, reference: date) -> bool:
    """The date is written in the quote; without a year, it is the next such day."""
    if value in dates_in(quote):
        return True
    if (value.month, value.day) not in day_months_in(quote):
        return False
    return value == _next_day_month(value.month, value.day, reference - timedelta(days=1))


def _next_day_month(month: int, day: int, start: date) -> date | None:
    for year in range(start.year, start.year + 5):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue  # 29 February
        if candidate >= start:
            return candidate
    return None


# --- Validity -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ValidityCheck:
    raw: ValidityValue
    period: ValidityPeriod | None
    issues: tuple[str, ...]


def _check_validity(value: ValidityValue, ctx: _Context) -> _ValidityCheck | None:
    """A validity duration checked like a lead time, then turned into an end date."""
    raw = (value.value or "").strip()
    if not raw:
        return None
    issues = _evidence_issues(value, ctx)
    quote = value.source.quote if value.source is not None else ""

    def done(period: ValidityPeriod | None, *more: str) -> _ValidityCheck:
        return _ValidityCheck(value, period, tuple(dict.fromkeys((*issues, *more))))

    unit = value.unit
    if unit is None:
        return done(None, "unit_missing")
    amount = parse_canonical(raw)
    if amount is None:
        amount = parse_decimal(raw, decimal=ctx.decimal)
    whole_days = unit in ("calendar_days", "business_days")
    if amount is None or amount <= 0 or (whole_days and amount != amount.to_integral_value()):
        return done(None, "invalid_value")
    if _rough_days(amount, unit) > MAX_LEAD_TIME_DAYS:
        return done(None, "out_of_range")
    more: list[str] = []
    if quote.strip():
        match = find_number(amount, quote, decimal=ctx.decimal)
        if not match.found:
            more.append("not_grounded")
        elif match.ambiguous:
            more.append("ambiguous_number")
        if not _unit_grounded(unit, quote):
            more.append("unit_not_grounded")
    original = ctx.original_offer_date
    from_original = original is not None and mentions_original_offer(quote)
    anchor = (
        min(original, ctx.reference) if original is not None and from_original else ctx.reference
    )
    valid_until, exact = validity_end(amount, unit, anchor, ctx.holiday_calendar)
    period = ValidityPeriod(
        amount=amount,
        unit=unit,
        anchor=anchor,
        anchor_kind="original_offer" if from_original else "message",
        holiday_calendar=ctx.holiday_calendar,
        valid_until=valid_until,
        exact=exact,
    )
    return done(period, *more)


def _valid_until(written: FieldCheck, validity: _ValidityCheck | None) -> FieldCheck:
    """A clean written date wins; else the end of the stated period, with its issues."""
    if validity is None or validity.period is None:
        return written
    if written.parsed is not None and not written.issues:
        return written
    raw = validity.raw
    end = validity.period.valid_until
    return FieldCheck(
        name="valid_until",
        extracted=ExtractedValue(
            value=end.isoformat(), source=raw.source, confidence=raw.confidence
        ),
        parsed=end,
        issues=validity.issues,
    )


def _validity_field(validity: _ValidityCheck | None) -> FieldCheck:
    """The stated period, as evidence; review issues go on `valid_until`."""
    if validity is None:
        return absent_field("validity")
    raw = validity.raw
    period = validity.period
    return FieldCheck(
        name="validity",
        extracted=ExtractedValue(
            value=f"{format(period.amount, 'f')} {period.unit}"
            if period is not None
            else raw.value,
            source=raw.source,
            confidence=raw.confidence,
        ),
        parsed=period.model_dump_json() if period is not None else None,
    )


# --- Quantities ---------------------------------------------------------------------------


def _without_copied_quantity(
    check: FieldCheck, draft: ProposalLineDraft, requested: RequestedLine | None
) -> tuple[FieldCheck, ProposalLineDraft]:
    """Drop a tier quantity the supplier never wrote but the request did.

    A single price without tiers applies to the requested quantity; a model that
    copies that quantity into `min_quantity` must not put the line on review.
    """
    if (
        requested is None
        or not isinstance(check.parsed, Decimal)
        or not _UNVERIFIED & set(check.issues)
        or check.parsed not in requested.quantities
    ):
        return check, draft
    absent = absent_field(check.name)
    return absent, draft.model_copy(update={check.name: absent.extracted})


def quantity_bases(
    lines: Sequence[LineCheck], requested_quantities: Mapping[UUID, Sequence[Decimal]]
) -> list[QuantityTerms]:
    """Per line, which quantity its price applies to (see `terms.QuantityBasis`).

    Lines are tiers of one part when they answer the same requested line with the
    same part (the requested one, or the same alternative MPN).
    """

    def part(line: LineCheck) -> tuple[UUID | None, str | None]:
        offered = line.fields["offered_mpn"].parsed if "offered_mpn" in line.fields else None
        alternative = _alnum(offered) if isinstance(offered, str) else "?"
        return line.rfq_line_id, alternative if line.is_alternative else None

    tiers: dict[tuple[UUID | None, str | None], int] = {}
    for line in lines:
        tiers[part(line)] = tiers.get(part(line), 0) + 1
    result: list[QuantityTerms] = []
    for line in lines:
        if line.rfq_line_id is None:
            result.append(
                QuantityTerms(quantity_basis="unmatched", price_applies_to_requested_quantity=False)
            )
            continue
        check = line.fields.get("min_quantity")
        stated = check.parsed if check is not None else None
        quantities = requested_quantities.get(line.rfq_line_id, ())
        if isinstance(stated, Decimal) and check is not None and not check.needs_review:
            applies = any(stated <= quantity for quantity in quantities)
            result.append(
                QuantityTerms(
                    quantity_basis="stated_tier", price_applies_to_requested_quantity=applies
                )
            )
        elif stated is None and tiers[part(line)] == 1:
            result.append(
                QuantityTerms(
                    quantity_basis="requested_quantity", price_applies_to_requested_quantity=True
                )
            )
        else:
            result.append(
                QuantityTerms(quantity_basis="ambiguous", price_applies_to_requested_quantity=False)
            )
    return result


# --- General terms and notes ---------------------------------------------------------------

_SENTENCE_BREAK = re.compile(r"(?<=[.;!?])\s+|\n+")


def _sentences(text: str) -> list[str]:
    return [sentence.strip() for sentence in _SENTENCE_BREAK.split(text) if sentence.strip()]


def _sentence_key(text: str) -> str:
    return normalize_text(text).rstrip(" .;:!?,")


def _general_terms(
    drafts: list[ProposalLineDraft], line_fields: list[dict[str, FieldCheck]], notes: list[str]
) -> tuple[list[ProposalLineDraft], list[dict[str, FieldCheck]], list[str]]:
    """Take the offer's general terms out of the lines' `conditional_terms`.

    A sentence on the lines of every requested position (two or more), or one the
    supplier's general notes already hold, is a term of the whole offer (payment,
    validity, packaging...), not a condition on one line's price: it becomes an
    offer note and blocks nothing. Returns the drafts, the fields and the moved
    sentences.
    """
    texts = [fields["conditional_terms"].parsed for fields in line_fields]
    positions = {draft.rfq_line_position for draft in drafts if draft.rfq_line_position is not None}
    common: set[str] = set()
    if len(positions) >= 2 and all(isinstance(text, str) for text in texts):
        keys = [{_sentence_key(s) for s in _sentences(str(text))} for text in texts]
        common = keys[0].intersection(*keys[1:])
    general = common | {_sentence_key(note) for note in notes}
    if not general - {""}:
        return drafts, line_fields, []
    moved: list[str] = []
    out_drafts: list[ProposalLineDraft] = []
    out_fields: list[dict[str, FieldCheck]] = []
    for draft, fields in zip(drafts, line_fields, strict=True):
        current = fields["conditional_terms"]
        text = current.parsed
        sentences = _sentences(text) if isinstance(text, str) else []
        keep = [s for s in sentences if _sentence_key(s) not in general]
        if len(keep) == len(sentences):
            out_drafts.append(draft)
            out_fields.append(fields)
            continue
        moved.extend(s for s in sentences if _sentence_key(s) in common)
        if keep:
            remaining = " ".join(keep)
            replacement = replace(
                current,
                extracted=current.extracted.model_copy(update={"value": remaining}),
                parsed=remaining,
            )
        else:
            replacement = absent_field("conditional_terms")
        out_fields.append({**fields, "conditional_terms": replacement})
        out_drafts.append(draft.model_copy(update={"conditional_terms": replacement.extracted}))
    return out_drafts, out_fields, moved


def _timing_out_of_conditions(
    drafts: list[ProposalLineDraft], line_fields: list[dict[str, FieldCheck]], ctx: _Context
) -> tuple[list[ProposalLineDraft], list[dict[str, FieldCheck]]]:
    """A condition sentence only about delivery timing is the line's lead-time risk.

    "We may need a few additional days for the shafts, depending on production
    availability" filed as a condition would block the line; it belongs with the
    line's lead time as its risk note (verbatim, and only when the line has a lead
    time to qualify). Anything about price, payment or validity stays a condition.
    """
    out_drafts: list[ProposalLineDraft] = []
    out_fields: list[dict[str, FieldCheck]] = []
    for draft, fields in zip(drafts, line_fields, strict=True):
        current = fields["conditional_terms"]
        terms = fields["lead_time_terms"]
        stored = decode_lead_time(terms.parsed)
        text = current.parsed
        if not isinstance(text, str) or stored is None:
            out_drafts.append(draft)
            out_fields.append(fields)
            continue
        sentences = _sentences(text)
        timing = [s for s in sentences if is_delivery_timing(s) and _found(s, ctx.sources)]
        if not timing:
            out_drafts.append(draft)
            out_fields.append(fields)
            continue
        keep = [s for s in sentences if s not in timing]
        risk = " ".join(_unique([*([stored.risk_note] if stored.risk_note else []), *timing]))
        lead = LeadTime.model_validate(
            {**stored.model_dump(include=set(LeadTime.model_fields)), "risk_note": risk}
        )
        if keep:
            remaining = " ".join(keep)
            condition = replace(
                current,
                extracted=current.extracted.model_copy(update={"value": remaining}),
                parsed=remaining,
            )
        else:
            condition = absent_field("conditional_terms")
        out_fields.append(
            {
                **fields,
                "conditional_terms": condition,
                "lead_time_terms": replace(terms, parsed=encode_lead_time(lead, ctx.reference)),
            }
        )
        out_drafts.append(draft.model_copy(update={"conditional_terms": condition.extracted}))
    return out_drafts, out_fields


def _order_by_out_of_conditions(
    output: QuoteExtractionOutput,
    drafts: list[ProposalLineDraft],
    line_fields: list[dict[str, FieldCheck]],
    notes: list[str],
    currency: FieldCheck,
    ctx: _Context,
) -> tuple[list[ProposalLineDraft], list[dict[str, FieldCheck]], list[str], FieldCheck]:
    """The offer's order deadline, taken out of the lines' conditions.

    "Delivery by 14/11/2026, provided we receive the purchase order by 21/10/2026" says
    until when the offer holds, not a condition on one line's price: it is the offer's
    `order_by` (the earliest such date in a line's condition, an offer note or a
    lead-time statement, each found word for word in the supplier's message). A
    condition sentence that says nothing else becomes an offer note, and so does one
    that names a price only to restate the line's own (`_restates_line_price`); one that
    also sets another condition (a minimum, a bundle, stock, a discount, another price)
    stays a condition. A date that reads two ways the message does not settle
    (`05/10/2026`) is no deadline. Returns the drafts, the fields, the moved sentences
    and the `order_by` field.
    """
    if not output.is_quote:
        return drafts, line_fields, [], absent_field("order_by")
    found: list[tuple[date, SourceRef | None]] = []

    def deadline(text: str) -> date | None:
        days = [d for written in order_deadlines(text) if (d := _read_date(written, ctx))]
        return min(days) if days else None

    out_drafts: list[ProposalLineDraft] = []
    out_fields: list[dict[str, FieldCheck]] = []
    moved: list[str] = []
    for draft, fields in zip(drafts, line_fields, strict=True):
        current = fields["conditional_terms"]
        text = current.parsed
        sentences = _sentences(text) if isinstance(text, str) else []
        routed: list[str] = []
        for sentence in sentences:
            day = deadline(sentence)
            if (
                day is None
                or not (
                    is_order_deadline_only(sentence)
                    or _restates_line_price(sentence, fields, currency, ctx)
                )
                or not _found(sentence, ctx.sources)
            ):
                continue
            source = current.extracted.source
            found.append((day, source.model_copy(update={"quote": sentence}) if source else None))
            routed.append(sentence)
        if not routed:
            out_drafts.append(draft)
            out_fields.append(fields)
            continue
        moved.extend(routed)
        keep = [s for s in sentences if s not in routed]
        if keep:
            remaining = " ".join(keep)
            condition = replace(
                current,
                extracted=current.extracted.model_copy(update={"value": remaining}),
                parsed=remaining,
            )
        else:
            condition = absent_field("conditional_terms")
        out_fields.append({**fields, "conditional_terms": condition})
        out_drafts.append(draft.model_copy(update={"conditional_terms": condition.extracted}))
    stated = list(notes)
    stated.extend(
        lead.source.quote
        for lead in (output.lead_time, *(draft.lead_time for draft in drafts))
        if lead.source is not None and _found(lead.source.quote, ctx.sources)
    )
    for text in stated:
        day = deadline(text)
        if day is not None:
            found.append((day, _source_of(text, ctx.sources)))
    if not found:
        return out_drafts, out_fields, [], absent_field("order_by")
    day, source = min(found, key=lambda item: item[0])
    order_by = FieldCheck(
        name="order_by",
        extracted=ExtractedValue(value=day.isoformat(), source=source, confidence=1.0),
        parsed=day,
    )
    return out_drafts, out_fields, moved, order_by


def _restates_line_price(
    sentence: str, fields: Mapping[str, FieldCheck], currency: FieldCheck, ctx: _Context
) -> bool:
    """An order-deadline sentence that names a price only to carry the line's own price
    forward: "to hold the price of BRL 2.90 per piece and delivery by 14/11/2026, the
    purchase order must be placed by 24/10/2026", on a line this reply prices at BRL 2.90
    per piece.

    Every number in it besides its dates is that price, read with the separator the
    supplier's numbers prove (a number that still reads two ways fails); any currency it
    names is the offer's (none named: the offer's, as for the line's own bare price); a
    unit it names is the line's; no other basis, discount, minimum or second price.
    Without a verified price, unit basis and currency on the line there is nothing to
    compare with, and the sentence stays a condition.
    """
    price = fields["unit_price"]
    unit = fields["unit"].parsed
    basis = fields["price_basis_quantity"].parsed
    if (
        not isinstance(price.parsed, Decimal)
        or price.needs_review
        or not isinstance(currency.parsed, str)
        or currency.needs_review
        or (basis is not None and basis != 1)
    ):
        return False
    stated = restated_price(sentence, decimal=ctx.decimal)
    return (
        stated is not None
        and bool(stated.values)
        and all(value == price.parsed for value in stated.values)
        and stated.currencies <= {currency.parsed}
        and all(isinstance(unit, str) and same_unit(word, unit) for word in stated.units)
    )


def _read_date(written: WrittenDate, ctx: _Context) -> date | None:
    """The one date a supplier meant: the only reading, or the one the message's own
    numeric dates settle (`14/11/2026` proves day first); without a year, the next
    such day from the message date."""
    readings: tuple[date, ...] | tuple[tuple[int, int], ...] = written.dates or written.day_months
    if not readings:
        return None
    if len(readings) == 1:
        chosen = readings[0]
    else:
        order = numeric_date_order(ctx.sources.email_text)
        if order is None:
            return None
        chosen = readings[0] if order == "day_first" else readings[1]
    if isinstance(chosen, date):
        return chosen
    month, day = chosen
    return _next_day_month(month, day, ctx.reference - timedelta(days=1))


def _source_of(text: str, sources: QuoteSources) -> SourceRef | None:
    """Where a verified sentence is: the email, or the first attachment that has it."""
    wanted = normalize_text(text)
    if wanted in normalize_text(sources.email_text):
        return SourceRef(kind="email_body", quote=text)
    for name, document in sources.documents.items():
        if document.readable and wanted in normalize_text(document.text):
            lowered = name.casefold()
            kind: Literal["pdf", "sheet", "email_body"] = (
                "pdf"
                if lowered.endswith(".pdf")
                else "sheet"
                if lowered.endswith((".xlsx", ".xls", ".csv"))
                else "email_body"
            )
            return SourceRef(kind=kind, attachment=name, quote=text)
    return None


def _verified_notes(notes: Sequence[str], sources: QuoteSources) -> list[str]:
    return _unique([note.strip() for note in notes if _found(note, sources)])


def _unique(texts: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for text in texts:
        key = _sentence_key(text)
        if key and key not in seen:
            seen.add(key)
            result.append(text)
    return result


def _normalized(output: QuoteExtractionOutput, notes: list[str]) -> QuoteExtractionOutput:
    """Every supplier question also holds the offer (`technical_questions`)."""
    asked = [question.quote.strip() for question in output.supplier_questions]
    technical = list(dict.fromkeys(q for q in (*output.technical_questions, *asked) if q.strip()))
    return output.model_copy(update={"technical_questions": technical, "offer_notes": notes})


# --- Signals ---------------------------------------------------------------------------------


def _signals(
    output: QuoteExtractionOutput,
    by_position: Mapping[int, RequestedLine],
    sources: QuoteSources,
    review_confidence: float,
) -> SupplierSignals:
    """The final-offer statement, declined lines and questions, each found in the message."""
    final = output.final_offer
    final_quote: str | None = None
    if (
        (final.value or "").strip()
        and final.source is not None
        and final.confidence >= review_confidence
        and _locate(final.source, sources) is None
    ):
        final_quote = final.source.quote.strip()
    priced = {
        draft.rfq_line_position
        for draft in output.lines
        if draft.rfq_line_position is not None and draft.unit_price.value is not None
    }
    declined: dict[int, DeclinedLineSignal] = {}
    for line in output.declined_lines:
        position = line.rfq_line_position
        if (
            position in by_position
            and position not in priced
            and position not in declined
            and _found(line.quote, sources)
        ):
            declined[position] = DeclinedLineSignal(
                rfq_line_position=position, quote=line.quote.strip()
            )
    questions = tuple(
        QuestionSignal(
            quote=question.quote.strip(),
            topic=question.topic,
            rfq_line_position=(
                question.rfq_line_position if question.rfq_line_position in by_position else None
            ),
            grounded=_found(question.quote, sources),
        )
        for question in output.supplier_questions
        if question.quote.strip()
    )
    return SupplierSignals(
        final_offer=final_quote is not None,
        final_offer_quote=final_quote,
        declined_lines=tuple(declined[position] for position in sorted(declined)),
        questions=questions,
    )


# --- Evidence ---------------------------------------------------------------------------------


def _evidence_issues(value: ExtractedValue, ctx: _Context) -> list[str]:
    issues: list[str] = []
    if value.confidence < ctx.review_confidence:
        issues.append("low_confidence")
    if value.source is None:
        issues.append("no_source")
    else:
        located = _locate(value.source, ctx.sources)
        if located is not None:
            issues.append(located)
    return issues


def _found(text: str, sources: QuoteSources) -> bool:
    """`text` appears word for word in the message or a readable attachment."""
    wanted = normalize_text(text)
    if not wanted:
        return False
    if wanted in normalize_text(sources.email_text):
        return True
    return any(
        wanted in normalize_text(document.text)
        for document in sources.documents.values()
        if document.readable
    )


def _verbatim(text: str | None, sources: QuoteSources) -> str | None:
    if text is None or not _found(text, sources):
        return None
    return text.strip()


def check_field(
    name: str,
    kind: FieldKind,
    extracted: ExtractedValue,
    sources: QuoteSources,
    *,
    decimal: DecimalSeparator | None = None,
    review_confidence: float = REVIEW_CONFIDENCE,
) -> FieldCheck:
    raw = extracted.value
    if raw is None or not raw.strip():
        # Absent. Whether that matters is decided per line / per proposal.
        return FieldCheck(name=name, extracted=extracted, parsed=None)

    issues: list[str] = []
    if extracted.confidence < review_confidence:
        issues.append("low_confidence")
    source = extracted.source
    if source is None:
        issues.append("no_source")
    else:
        located = _locate(source, sources)
        if located is not None:
            issues.append(located)

    parsed, value_issues = _parse(kind, raw, decimal)
    if name == "price_basis_quantity" and parsed == 1 and not value_issues:
        # A basis of one unit is the default ("0,039 EUR pro Stück", "each"): it cannot
        # change a price and the "1" is rarely written, so it never puts an offer on review.
        return FieldCheck(name=name, extracted=extracted, parsed=parsed)
    issues.extend(value_issues)
    if parsed is not None and source is not None and source.quote.strip():
        # A sheet value is grounded in the cell it cites when the quote spans more cells
        # than that one: the value must be that cell's, not any number of the span.
        cell = _spanned_cell(source, sources)
        scope = cell or source.quote
        # A workbook's numeric cell is in machine form: read with a point, whatever the
        # supplier's separator.
        numeric = _numeric_cell(source, sources)
        machine = numeric is not None and _FORMAT_HINT.sub("", scope).strip() == numeric
        issues.extend(_grounding(kind, parsed, scope, "." if machine else decimal))
    return FieldCheck(name=name, extracted=extracted, parsed=parsed, issues=tuple(issues))


# --- Is the quote in the source? ------------------------------------------------------


def _locate(source: SourceRef, sources: QuoteSources) -> str | None:
    """None when `source.quote` is where it says it is, else the reason it is not."""
    quote = normalize_text(source.quote)
    if not quote:
        return "empty_quote"
    if source.kind == "image":
        return "source_not_read"  # images are never read in the prototype

    if source.attachment is None:
        if source.kind != "email_body":
            return "unknown_attachment"
        return None if quote in normalize_text(sources.email_text) else "quote_not_found"

    document = sources.documents.get(source.attachment)
    if document is None:
        return "unknown_attachment"
    if not document.readable:
        return "source_not_read"

    scope: str | None = None
    if source.kind == "pdf" and source.page is not None:
        scope = pdf_page_text(document, source.page)
    elif source.kind == "sheet" and source.cell:
        scope = cell_text(document, source.sheet, source.cell)
    if scope is not None and quote in normalize_text(scope):
        return None
    if _spanned_cell(source, sources) is not None:
        # The quote runs over several cells (a header, the row) and the cited cell is
        # one of them, in the file: located. The value is then grounded in that cell.
        return None
    if quote in normalize_text(document.text):
        # Found, but not at the cited page / cell (or no locator was given).
        has_locator = (source.kind == "pdf" and source.page is not None) or (
            source.kind == "sheet" and bool(source.cell)
        )
        return "wrong_locator" if has_locator else None
    return "quote_not_found"


def _spanned_cell(source: SourceRef, sources: QuoteSources) -> str | None:
    """The cited sheet cell's text when the quote spans several cells, this one among
    them, and that span is in the workbook; else None.

    Seen live (2026-10-10, a 2-line xlsx quote): the model cited `Proposta!G6` (21 days)
    with a quote running from the column header through the row, so the quote was in the
    file but not inside G6, and every lead time came back `wrong_locator`."""
    if source.kind != "sheet" or not source.cell or source.attachment is None:
        return None
    document = sources.documents.get(source.attachment)
    if document is None or not document.readable:
        return None
    cell = cell_text(document, source.sheet, source.cell)
    if cell is None or not normalize_text(cell):
        return None
    quote = normalize_text(source.quote)
    if quote == normalize_text(cell) or normalize_text(cell) not in quote:
        return None
    if quote not in normalize_text(document.text):
        return None
    return cell


_CELL = re.compile(r"([A-Za-z]+)(\d+)")


def _column_header(source: SourceRef, sources: QuoteSources) -> str | None:
    """The first text cell above a cited sheet cell, in the same column."""
    if source.kind != "sheet" or not source.cell or source.attachment is None:
        return None
    document = sources.documents.get(source.attachment)
    match = _CELL.fullmatch(source.cell.strip())
    if document is None or match is None:
        return None
    column, row = match.group(1).upper(), int(match.group(2))
    for above in range(1, row):
        text = cell_text(document, source.sheet, f"{column}{above}")
        if text is not None and any(c.isalpha() for c in text):
            return text
    return None


def pdf_page_text(document: DocumentText, page: int) -> str | None:
    """The text of page `page` of an extracted PDF, or None if it has no such page."""
    marker = re.compile(rf"^\[{re.escape(document.filename)} p\.(\d+)(?: table \d+)?\]")
    current: int | None = None
    found = False
    lines: list[str] = []
    for line in document.text.splitlines():
        match = marker.match(line)
        if match is not None:
            current = int(match.group(1))
            found = found or current == page
        if current == page:
            lines.append(line)
    return "\n".join(lines) if found else None


def cell_text(document: DocumentText, sheet: str | None, cell: str) -> str | None:
    """The text of one cell of an extracted workbook or CSV, or None if it is empty or absent."""
    locator = f"{sheet}!{cell.upper()}" if sheet else cell.upper()
    pattern = re.compile(
        rf"\[{re.escape(document.filename)} {re.escape(locator)}\] (.*?)(?= \| \[|$)",
        re.MULTILINE,
    )
    match = pattern.search(document.text)
    return match.group(1) if match is not None else None


# --- Which decimal separator? -----------------------------------------------------------

# A locator marker of the extracted text: `[quote.pdf p.2]`, `[offer.xlsx Prices!C14]`.
_MARKER: Final = re.compile(r"\[[^\]\n]*\]")
# One cell of an extracted workbook: its marker and its text (`documents.extract`).
_CELL_TEXT: Final = re.compile(r"\[[^\]\n]*\] (.*?)(?= \| \[|$)", re.MULTILINE)
# The number format the extraction appends to a cell whose format names a currency.
_FORMAT_HINT: Final = re.compile(r" \(format: .*\)$")
# How the extraction renders a workbook's numeric cell: `1234.5`, in machine form.
_NUMERIC_CELL: Final = re.compile(r"-?\d+(?:\.\d+)?")


def proven_decimal_separator(sources: QuoteSources) -> DecimalSeparator | None:
    """The decimal separator the supplier's own numbers prove, or None (`decimal_separator_in`).

    Never the language the supplier is registered with: a supplier registered as Italian
    may quote in English ("2,000 pcs at BRL 3.20"). The evidence is the subject, the body
    above the quoted RFQ email (Bido wrote that one, in the registered language) and the
    readable attachments, without their locator markers. A workbook's numeric cells do not
    count: the extraction renders them in machine form (`3.2`), so they were never written
    with a separator and prove none; its text cells do.
    """
    parts = [sources.email_subject, own_words(sources.email_body)]
    for document in sources.documents.values():
        if not document.readable:
            continue
        if document.kind != "sheet":
            parts.append(_MARKER.sub(" ", document.text))
            continue
        for match in _CELL_TEXT.finditer(document.text):
            text = _FORMAT_HINT.sub("", match.group(1)).strip()
            if _NUMERIC_CELL.fullmatch(text) is None:
                parts.append(text)
    return decimal_separator_in("\n".join(parts))


def _numeric_cell(source: SourceRef, sources: QuoteSources) -> str | None:
    """The cited workbook cell's value when it is a numeric cell (`_NUMERIC_CELL`), else None."""
    if source.kind != "sheet" or not source.cell or source.attachment is None:
        return None
    document = sources.documents.get(source.attachment)
    if document is None or document.kind != "sheet" or not document.readable:
        return None
    text = cell_text(document, source.sheet, source.cell)
    if text is None:
        return None
    value = _FORMAT_HINT.sub("", text).strip()
    return value if _NUMERIC_CELL.fullmatch(value) is not None else None


def _as_requested_tier(
    check: FieldCheck,
    requested: RequestedLine | None,
    sources: QuoteSources,
    decimal: DecimalSeparator | None,
) -> FieldCheck:
    """A tier quantity that reads two ways, read as the requested quantity it can be.

    `1,500` with no separator proven is 1500 or 1.5: for a 1,500 pcs request it is
    1500. When no reading (or more than one) is a requested quantity it stays
    `ambiguous_number`, for a person: code never picks a reading on its own.
    """
    source = check.extracted.source
    raw = check.extracted.value
    if (
        requested is None
        or not requested.quantities
        or "ambiguous_number" not in check.issues
        or source is None
        or raw is None
    ):
        return check
    words = _spanned_cell(source, sources) or source.quote
    tiers = stated_values(raw, words, decimal=decimal) & set(requested.quantities)
    if len(tiers) != 1:
        return check
    issues = tuple(issue for issue in check.issues if issue != "ambiguous_number")
    return replace(check, parsed=tiers.pop(), issues=issues)


# --- Is the value well-formed, and in the quote? ----------------------------------------

_CURRENCY_CODE = re.compile(r"[A-Z]{3}")
_COUNTRY_CODE = re.compile(r"[A-Z]{2}")


def _parse(
    kind: FieldKind, raw: str, decimal: DecimalSeparator | None
) -> tuple[Parsed | None, list[str]]:
    value = raw.strip()
    match kind:
        case "amount" | "quantity" | "days":
            number = parse_canonical(value)
            if number is None:
                number = parse_decimal(value, decimal=decimal)
            if number is None:
                return None, ["invalid_value"]
            if abs(number) >= MAX_AMOUNT:
                return None, ["out_of_range"]
            if kind == "days":
                if number != number.to_integral_value() or not 0 <= number <= MAX_LEAD_TIME_DAYS:
                    return None, ["invalid_value"]
                return int(number), []
            if kind == "amount" and number < 0:
                return None, ["negative"]
            if kind == "quantity" and number <= 0:
                return None, ["not_positive"]
            return number, []
        case "currency":
            code = value.upper()
            if _CURRENCY_CODE.fullmatch(code) is None:
                return None, ["invalid_value"]
            return code, [] if code in ISO_CURRENCIES else ["unknown_currency"]
        case "date":
            try:
                return date.fromisoformat(value), []
            except ValueError:
                return None, ["invalid_value"]
        case "incoterm":
            code = value.split()[0].upper().rstrip(",;")
            if code not in INCOTERMS:
                return None, ["invalid_value"]
            return code, []
        case "country":
            code = value.upper()
            if _COUNTRY_CODE.fullmatch(code) is None:
                return None, ["invalid_value"]
            return code, []
        case "mpn" | "text" | "lead_time" | "validity":
            return value, []


def _grounding(
    kind: FieldKind, parsed: Parsed, quote: str, decimal: DecimalSeparator | None
) -> list[str]:
    match kind:
        case "amount" if parsed == 0 and mentions_none(quote):
            return []  # "Werkzeugkosten: keine"
        case "amount" | "quantity":
            assert isinstance(parsed, Decimal)
            return _number_grounding(parsed, quote, decimal)
        case "days":
            raise AssertionError("lead times are read by _written_lead_time / _check_lead_time")
        case "currency":
            return [] if parsed in detect_currencies(quote) else ["not_grounded"]
        case "date":
            return [] if parsed in dates_in(quote) else ["not_grounded"]
        case "incoterm":
            return [] if parsed in incoterms_in(quote) else ["not_grounded"]
        case "mpn":
            assert isinstance(parsed, str)
            return [] if _alnum(parsed) in _alnum(quote) else ["not_grounded"]
        case "country" | "text" | "lead_time" | "validity":
            # Free text or a code for a country name: the located quote is the evidence.
            # (Lead times and validity periods are checked by their own functions.)
            return []


def _number_grounding(value: Decimal, quote: str, decimal: DecimalSeparator | None) -> list[str]:
    match = find_number(value, quote, decimal=decimal)
    if not match.found:
        return ["not_grounded"]
    return ["ambiguous_number"] if match.ambiguous else []


def _alnum(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", text).upper() if c.isalnum())


def _json(value: Parsed | None) -> JsonValue:
    match value:
        case None:
            return None
        case Decimal():
            return format(value, "f")
        case date():
            return value.isoformat()
        case int() | str():
            return value
