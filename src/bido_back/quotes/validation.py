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
- required values are present and the quote is not already expired.

Anything that fails marks the field, and through it the line or the proposal,
`needs_review`. Nothing is dropped: the persisted provenance keeps the raw
value, the source, the confidence and the reasons.
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
    INCOTERMS,
    ISO_CURRENCIES,
    DecimalSeparator,
    dates_in,
    detect_currencies,
    find_number,
    incoterms_in,
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
}

type Parsed = Decimal | int | date | str


# --- Inputs ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RequestedLine:
    id: UUID
    position: int
    mpn: str | None = None


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

    @property
    def needs_review(self) -> bool:
        return bool(self.issues)

    def provenance(self) -> dict[str, JsonValue]:
        source = self.extracted.source
        return {
            "value": self.extracted.value,
            "parsed": _json(self.parsed),
            "source": source.model_dump(mode="json") if source is not None else None,
            "confidence": self.extracted.confidence,
            "needs_review": self.needs_review,
            "issues": list(self.issues),
        }


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
) -> ValidatedQuote:
    """Check an extraction against its sources and the RFQ.

    `decimal` is the supplier's likely decimal separator (from its language);
    without it, numbers like `1,000` are ambiguous and go to review.
    """

    def check(name: str, kind: FieldKind, value: ExtractedValue) -> FieldCheck:
        return check_field(
            name, kind, value, sources, decimal=decimal, review_confidence=review_confidence
        )

    header = {
        name: check(name, kind, getattr(output, name)) for name, kind in HEADER_FIELDS.items()
    }
    if not output.is_quote:
        return ValidatedQuote(output=output, header=header, lines=())

    currency = header["currency"]
    by_position = {line.position: line for line in requested}
    lines: list[LineCheck] = []
    for draft in output.lines:
        fields = {
            name: check(name, kind, getattr(draft, name)) for name, kind in LINE_FIELDS.items()
        }
        lines.append(_check_line(draft, fields, by_position, currency))

    issues: list[str] = []
    if not lines:
        issues.append("no_lines")
    if currency.extracted.value is None and any(
        line.draft.unit_price.value is not None for line in lines
    ):
        issues.append("missing:currency")
    valid_until = header["valid_until"].parsed
    if isinstance(valid_until, date) and valid_until < today:
        issues.append("validity_expired")
    return ValidatedQuote(output=output, header=header, lines=tuple(lines), issues=tuple(issues))


def _check_line(
    draft: ProposalLineDraft,
    fields: dict[str, FieldCheck],
    by_position: Mapping[int, RequestedLine],
    currency: FieldCheck,
) -> LineCheck:
    issues: list[str] = []
    notes: list[str] = []

    position = draft.rfq_line_position
    requested = by_position.get(position) if position is not None else None
    if position is None:
        issues.append("unmatched_rfq_line")
    elif requested is None:
        issues.append("unknown_rfq_line")

    if draft.unit_price.value is None:
        issues.append("missing:unit_price")

    priced = [fields[n] for n in ("unit_price", "tooling_cost") if fields[n].parsed is not None]
    if priced and (currency.parsed is None or currency.needs_review):
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

    return LineCheck(
        draft=draft,
        rfq_line_id=requested.id if requested is not None else None,
        is_alternative=is_alternative,
        fields=fields,
        issues=tuple(issues),
        notes=tuple(notes),
    )


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
    issues.extend(value_issues)
    if parsed is not None and source is not None and source.quote.strip():
        issues.extend(_grounding(kind, parsed, source.quote, decimal))
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
    if quote in normalize_text(document.text):
        # Found, but not at the cited page / cell (or no locator was given).
        has_locator = (source.kind == "pdf" and source.page is not None) or (
            source.kind == "sheet" and bool(source.cell)
        )
        return "wrong_locator" if has_locator else None
    return "quote_not_found"


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


def _grounding(
    kind: FieldKind, parsed: Parsed, quote: str, decimal: DecimalSeparator | None
) -> list[str]:
    match kind:
        case "amount" | "quantity":
            assert isinstance(parsed, Decimal)
            return _number_grounding(parsed, quote, decimal)
        case "days":
            assert isinstance(parsed, int)
            issues: list[str] = []
            direct = find_number(Decimal(parsed), quote, decimal=decimal)
            if direct.found:
                issues.extend(["ambiguous_number"] if direct.ambiguous else [])
            elif not (
                parsed % 7 == 0
                and mentions_weeks(quote)
                and find_number(Decimal(parsed // 7), quote, decimal=decimal).found
            ):
                issues.append("not_grounded")
            if mentions_working_days(quote):
                issues.append("working_days")  # calendar days depend on the supplier's calendar
            return issues
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
