"""Deterministic checks on a quote extraction: the model proposes, this code decides.

For every extracted value the model gives the value, the exact text it read it
from (`source.quote`) with a locator, and a confidence. Here we check, without
any model:

- the quote really is in the cited source (email body, PDF page, sheet cell);
- the value is really in the quote: numbers read locale-aware (`1.234,56` vs
  `1,234.56`), weeks converted to days, dates in the usual written forms,
  currencies by code or symbol;
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

Lead times: `lead_time_days` is always stored as CALENDAR days. The model
writes the number as the supplier wrote it, with its unit in the cited quote;
this code reads the unit from the quote and converts (weeks x 7; business days
by `parsing.BUSINESS_DAYS_RULE`, `ceil(n * 7 / 5)`), recording the raw number
and the unit in the field's provenance (`conversion`).
"""

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Final, Literal
from uuid import UUID

from pydantic import JsonValue

from bido_back.agents.catalog.contracts import (
    ExtractedValue,
    ProposalLineDraft,
    QuoteExtractionOutput,
    SourceRef,
)
from bido_back.documents import DocumentText
from bido_back.quotes.parsing import (
    BUSINESS_DAYS_RULE,
    INCOTERMS,
    ISO_CURRENCIES,
    DecimalSeparator,
    business_days_to_calendar,
    dates_in,
    detect_currencies,
    find_number,
    incoterms_in,
    mentions_days,
    mentions_none,
    mentions_weeks,
    mentions_working_days,
    normalize_text,
    parse_canonical,
    parse_decimal,
)

REVIEW_CONFIDENCE: Final = 0.8
# Above what the numeric columns can hold (Numeric(18, 6) has 12 integer digits).
MAX_AMOUNT: Final = Decimal(10) ** 12
MAX_LEAD_TIME_DAYS: Final = 3650

FieldKind = Literal[
    "amount", "quantity", "days", "currency", "date", "incoterm", "country", "mpn", "text"
]

HEADER_FIELDS: Final[Mapping[str, FieldKind]] = {
    "currency": "currency",
    "valid_until": "date",
    "incoterm": "incoterm",
    "delivery_location": "text",
    "payment_terms": "text",
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
}

type Parsed = Decimal | int | date | str


# --- Inputs ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RequestedLine:
    """What the RFQ asked for on one line: what a quoted line is compared against."""

    id: UUID
    position: int
    mpn: str | None = None
    # Requested quantity tiers; a quoted quantity must be one of them.
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
    # How `parsed` was derived from `value` when they differ (a lead time written
    # in weeks or business days): the raw number, its unit and the rule applied.
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
    output: QuoteExtractionOutput
    header: Mapping[str, FieldCheck]
    lines: tuple[LineCheck, ...]
    issues: tuple[str, ...] = ()

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


def validate_quote(
    output: QuoteExtractionOutput,
    *,
    requested: Sequence[RequestedLine],
    sources: QuoteSources,
    today: date,
    decimal: DecimalSeparator | None = None,
    review_confidence: float = REVIEW_CONFIDENCE,
    rfq_currency: str | None = None,
) -> ValidatedQuote:
    """Check an extraction against its sources and the RFQ.

    `decimal` is the supplier's likely decimal separator (from its language);
    without it, numbers like `1,000` are ambiguous and go to review.
    `rfq_currency` is the RFQ's base currency: a quote in another one is a
    `currency_mismatch` (a warning, so the buyer sees it before comparing).
    """

    def check(name: str, kind: FieldKind, value: ExtractedValue) -> FieldCheck:
        return check_field(
            name, kind, value, sources, decimal=decimal, review_confidence=review_confidence
        )

    header = {
        name: check(name, kind, getattr(output, name)) for name, kind in HEADER_FIELDS.items()
    }
    if not output.is_quote and output.reply_kind == "replacement":
        return ValidatedQuote(output=output, header=header, lines=())

    currency = header["currency"]
    by_position = {line.position: line for line in requested}
    lines: list[LineCheck] = []
    for draft in output.lines:
        fields = {
            name: check(name, kind, getattr(draft, name)) for name, kind in LINE_FIELDS.items()
        }
        lines.append(
            _check_line(
                draft, fields, by_position, currency, partial=output.reply_kind != "replacement"
            )
        )

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
    return ValidatedQuote(output=output, header=header, lines=tuple(lines), issues=tuple(issues))


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
        # quantity the buyer needs. A warning, never a rejection.
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
    conversion: dict[str, JsonValue] | None = None
    if parsed is not None and source is not None and source.quote.strip():
        if kind == "days":
            assert isinstance(parsed, int)
            parsed, conversion, grounding = _lead_time(
                parsed, source.quote, decimal, header=_column_header(source, sources)
            )
        else:
            grounding = _grounding(kind, parsed, source.quote, decimal)
        issues.extend(grounding)
    return FieldCheck(
        name=name, extracted=extracted, parsed=parsed, issues=tuple(issues), conversion=conversion
    )


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
    if quote in normalize_text(document.text):
        # Found, but not at the cited page / cell (or no locator was given).
        has_locator = (source.kind == "pdf" and source.page is not None) or (
            source.kind == "sheet" and bool(source.cell)
        )
        return "wrong_locator" if has_locator else None
    return "quote_not_found"


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
        case "mpn" | "text":
            return value, []


def _lead_time(
    written: int, quote: str, decimal: DecimalSeparator | None, *, header: str | None
) -> tuple[int | None, dict[str, JsonValue] | None, list[str]]:
    """Calendar days from a lead time the model wrote, in the unit its quote states.

    `header` is the column header of a cited sheet cell (`Lieferzeit (Wochen)`),
    which may carry the unit the cell omits. The model may write the number as
    the supplier did (`25` for "25 dias úteis", `6` for "6 Wochen") or already
    converted (`35`, `42`): both ground, and the stored value is the same.
    Returns the calendar days (None when out of range), the conversion applied
    (raw number, unit, rule) and the grounding issues.
    """
    unit_text = quote if header is None else f"{quote}\n{header}"
    business = mentions_working_days(unit_text)
    weeks = mentions_weeks(unit_text) and not mentions_days(unit_text)
    direct = find_number(Decimal(written), quote, decimal=decimal)
    if direct.found:
        issues = ["ambiguous_number"] if direct.ambiguous else []
        if business:
            return _converted(business_days_to_calendar(written), written, "business_days", issues)
        if weeks:
            return _converted(written * 7, written, "weeks", issues)
        return written, None, issues
    # Not written as such: the model converted. The number it read must be there.
    if business:
        for raw in range(max(0, written * 5 // 7 - 1), written * 5 // 7 + 2):
            if (
                business_days_to_calendar(raw) == written
                and find_number(Decimal(raw), quote, decimal=decimal).found
            ):
                return _converted(written, raw, "business_days", [])
    if (
        weeks
        and written % 7 == 0
        and find_number(Decimal(written // 7), quote, decimal=decimal).found
    ):
        return _converted(written, written // 7, "weeks", [])
    return written, None, ["not_grounded"]


def _converted(
    days: int, raw: int, unit: Literal["business_days", "weeks"], issues: list[str]
) -> tuple[int | None, dict[str, JsonValue] | None, list[str]]:
    conversion: dict[str, JsonValue] = {"quoted": raw, "unit": unit, "calendar_days": days}
    if unit == "business_days":
        conversion["rule"] = BUSINESS_DAYS_RULE
    if days > MAX_LEAD_TIME_DAYS:
        return None, conversion, [*issues, "out_of_range"]
    return days, conversion, issues


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
            raise AssertionError("lead times are grounded by _lead_time")
        case "currency":
            return [] if parsed in detect_currencies(quote) else ["not_grounded"]
        case "date":
            return [] if parsed in dates_in(quote) else ["not_grounded"]
        case "incoterm":
            return [] if parsed in incoterms_in(quote) else ["not_grounded"]
        case "mpn":
            assert isinstance(parsed, str)
            return [] if _alnum(parsed) in _alnum(quote) else ["not_grounded"]
        case "country" | "text":
            # Free text or a code for a country name: the located quote is the evidence.
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
