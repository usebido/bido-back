"""Validated source values and evidence utilities shared by assessment paths."""

import re
import unicodedata
from collections import defaultdict
from collections.abc import Sequence
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Final

from bido_back.purchasing.importers import norm
from bido_back.purchasing.sources.contracts import Issue, Record, SourceRef
from bido_back.purchasing.sources.formats import read_date, read_number
from bido_back.purchasing.sources.vocabulary import UNIT_VOCABULARY, built_in, vocabulary_key

NUMBER_FIELDS: Final = frozenset(
    {
        "available", "on_order", "safety_stock", "blocked", "reserved", "total", "unit_price",
        "quantity", "quantity_per", "base_quantity", "rejected", "otif_pct", "quality_pct",
        "rating", "lead_time_days", "demand", "projected", "reported_to_buy",
        "reported_estimated_cost", "received_quantity", "rejected_quantity", "open_quantity",
    }
)  # fmt: skip
DATE_FIELDS: Final = frozenset(
    {
        "as_of", "receipt_date", "need_by", "ordered_at", "promised_at", "received_at",
        "expected_at", "valid_from", "valid_to",
    }
)  # fmt: skip
# Business identifiers matched across reports with `join_key`, by namespace.
IDENTIFIER_FIELDS: Final[dict[str, str]] = {
    "part_number": "item",
    "product_code": "item",
    "supplier_code": "supplier",
    "po_number": "order",
    "po_line": "order line",
    "reference": "reference",
    "receipt_id": "receipt",
    "location": "location",
    "warehouse": "warehouse",
}
_NUMBER_BASES: Final = frozenset({"native_number", "decimal_separator"})
_DATE_BASES: Final = frozenset({"date_order", "declared_date_order"})


def source_text(record: Record | None, field: str) -> str | None:
    if record is None:
        return None
    return record.values.get(field, "").strip() or None


def source_refs(records: Sequence[Record], *fields: str) -> list[SourceRef]:
    result: list[SourceRef] = []
    for record in records:
        for field, ref in record.evidence.items():
            if (not fields or field in fields) and ref not in result:
                result.append(ref)
    return result


def add_issue(
    issues: list[Issue],
    code: str,
    message: str,
    records: Sequence[Record],
    *fields: str,
    warning: bool = False,
) -> None:
    issues.append(
        Issue(
            code=code,
            severity="warning" if warning else "blocker",
            message=message,
            evidence=source_refs(records, *fields),
        )
    )


def _derived(record: Record, field: str, raw: str, bases: frozenset[str]) -> str | None:
    """A canonical reading code derived for exactly this source spelling, if any."""
    canonical = record.canonical.get(field)
    if canonical is None or canonical.source != raw or canonical.basis not in bases:
        return None
    return canonical.value


def source_number(
    record: Record | None,
    field: str,
    issues: list[Issue],
    *,
    negative: bool = False,
    warning: bool = False,
) -> Decimal | None:
    raw = source_text(record, field)
    if raw is None or record is None:
        return None
    derived = _derived(record, field, raw, _NUMBER_BASES)
    value: Decimal | None
    try:
        value = Decimal(derived) if derived is not None else read_number(raw)
    except InvalidOperation:
        value = None
    if (
        value is None
        or not value.is_finite()
        or abs(value) >= Decimal(10) ** 12
        or (value < 0 and not negative)
    ):
        add_issue(
            issues,
            "invalid_number",
            f"The source {field} is not an unambiguous valid number.",
            [record],
            field,
            warning=warning,
        )
        return None
    return value


def source_date(record: Record | None, field: str, issues: list[Issue]) -> date | None:
    raw = source_text(record, field)
    if raw is None or record is None:
        return None
    derived = _derived(record, field, raw, _DATE_BASES)
    if derived is not None:
        return date.fromisoformat(derived)
    reading = read_date(raw)
    if reading.value is not None:
        return reading.value
    if reading.ambiguous:
        add_issue(
            issues,
            "ambiguous_date",
            f"The source {field} can be read day-first or month-first; no other date "
            "in its column, sheet or source establishes the order.",
            [record],
            field,
        )
        return None
    add_issue(issues, "invalid_date", f"The source {field} is not a valid date.", [record], field)
    return None


def normalize_unit(value: str | None) -> str | None:
    """A unit spelling's canonical unit (UN, EA, pcs, Stk, pç -> count), else its text."""
    if value is None:
        return None
    return built_in("", UNIT_VOCABULARY, value) or norm(value)


def unit_key(record: Record | None, field: str = "unit") -> str | None:
    """A record's unit, as interpreted for its section by the mapper or else built in."""
    raw = source_text(record, field)
    if raw is None or record is None:
        return None
    canonical = record.canonical.get(field)
    if canonical is not None and canonical.basis == "value_map" and canonical.source == raw:
        return canonical.value
    return normalize_unit(raw)


def categorical(record: Record | None, field: str) -> str | None:
    """A categorical field's canonical term (status, movement, basis), or None if unknown.

    The mapper's checked interpretation of the section's spelling wins; otherwise the
    built-in multilingual vocabulary applies. Conflicting interpretations stay unknown.
    """
    raw = source_text(record, field)
    if raw is None or record is None:
        return None
    canonical = record.canonical.get(field)
    if canonical is not None and canonical.basis == "value_map" and canonical.source == raw:
        return canonical.value
    return built_in(record.kind, field, raw)


def join_key(value: str | None) -> str | None:
    """How business identifiers are compared across reports.

    Case, surrounding and repeated spaces are ignored; numeric-only codes are compared
    without leading zeros and without a float rendering's ".0" ("000101", "101" and
    "101.0" are one code). Displayed values keep their source spelling.
    """
    if value is None:
        return None
    text = " ".join(unicodedata.normalize("NFKC", value).casefold().split())
    if not text:
        return None
    if re.fullmatch(r"[0-9]+\.0+", text):
        text = text.split(".", 1)[0]
    if re.fullmatch(r"[0-9]+", text):
        text = text.lstrip("0") or "0"
    return text


def same_key(left: str | None, right: str | None) -> bool:
    key = join_key(left)
    return key is not None and key == join_key(right)


def fact_key(record: Record, field: str) -> str:
    """Equal keys denote the same business fact, however each report formats it."""
    raw = source_text(record, field)
    if raw is None:
        return ""
    if field in IDENTIFIER_FIELDS:
        return join_key(raw) or ""
    if field in DATE_FIELDS:
        day = source_date(record, field, [])
        return day.isoformat() if day is not None else raw
    if field in NUMBER_FIELDS:
        number = source_number(record, field, [], negative=True)
        return format(number.normalize(), "f") if number is not None else raw
    if vocabulary_key(record.kind, field) == UNIT_VOCABULARY:
        return unit_key(record, field) or raw
    if vocabulary_key(record.kind, field) is not None:
        return categorical(record, field) or norm(raw)
    return " ".join(raw.casefold().split())


def identifier_variants(records: Sequence[Record]) -> list[Issue]:
    """Disclose identifiers whose spellings differ but are matched as one code.

    Across reports, "000101" and "101" are one numeric code written with and without
    padding. Inside ONE table they are two different keys (an ERP with lexicographic
    material numbers): that collision is a blocker, never silently merged.
    """
    spellings: dict[tuple[str, str], dict[str, SourceRef | None]] = defaultdict(dict)
    per_table: dict[tuple[str, str, str, str], dict[str, SourceRef]] = defaultdict(dict)
    for record in records:
        for field, namespace in IDENTIFIER_FIELDS.items():
            raw = source_text(record, field)
            key = join_key(raw)
            if raw is not None and key is not None:
                ref = record.evidence.get(field)
                spellings[namespace, key].setdefault(raw, ref)
                if ref is not None:
                    spelled = " ".join(raw.casefold().split())
                    per_table[ref.source_id, ref.table, namespace, key].setdefault(spelled, ref)
    issues: list[Issue] = []
    collisions = [found for found in per_table.values() if len(found) > 1]
    if collisions:
        issues.append(
            Issue(
                code="identifier_collision",
                severity="blocker",
                message=f"{len(collisions)} identifier(s) appear in one table both with and "
                "without leading zeros (or as a float); they may be different codes, so the "
                "assessment is held for review.",
                evidence=[ref for found in collisions for ref in found.values()][:20],
            )
        )
    for namespace in dict.fromkeys(IDENTIFIER_FIELDS.values()):
        groups = [found for (name, _), found in spellings.items() if name == namespace]
        varied = [found for found in groups if len(found) > 1]
        if not varied:
            continue
        refs = [ref for found in varied for ref in found.values() if ref is not None]
        issues.append(
            Issue(
                code="normalized_identifier_match",
                severity="warning",
                message=f"{len(varied)} {namespace} identifier(s) are written differently "
                "across the sources (case, spacing, leading zeros or a trailing .0) and "
                "were matched as one identifier; the item master's spelling is displayed.",
                evidence=refs[:20],
            )
        )
    return issues
