"""Validated source values and evidence utilities shared by assessment paths."""

import re
import unicodedata
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Final

from bido_back.purchasing.importers import norm
from bido_back.purchasing.sources.contracts import (
    EvidenceTable,
    Issue,
    IssueColumn,
    IssueLocation,
    Record,
    SourceRef,
)
from bido_back.purchasing.sources.formats import read_date, read_number
from bido_back.purchasing.sources.vocabulary import UNIT_VOCABULARY, built_in, vocabulary_key

NUMBER_FIELDS: Final = frozenset(
    {
        "available", "on_order", "safety_stock", "blocked", "reserved", "total", "unit_price",
        "quantity", "quantity_per", "base_quantity", "rejected", "otif_pct", "quality_pct",
        "rating", "lead_time_days", "demand", "projected", "reported_to_buy",
        "reported_estimated_cost", "received_quantity", "rejected_quantity", "open_quantity",
        "quality_inspection", "in_transit", "price_basis_quantity", "score_overall",
        "score_price", "score_quality", "score_delivery", "score_service",
        "planned_delivery_days", "min_lot_size", "rounding", "min_order_quantity",
        "alt_unit_factor",
    }
)  # fmt: skip
DATE_FIELDS: Final = frozenset(
    {
        "as_of", "receipt_date", "need_by", "ordered_at", "promised_at", "received_at",
        "expected_at", "valid_from", "valid_to", "reschedule_to",
    }
)  # fmt: skip
# Business identifiers matched across reports with `join_key`, by namespace.
IDENTIFIER_FIELDS: Final[dict[str, str]] = {
    "part_number": "item",
    "product_code": "item",
    "supplier_code": "supplier",
    "po_number": "order",
    "po_line": "order line",
    "schedule_line": "schedule line",
    "reference": "reference",
    "receipt_id": "receipt",
    "location": "location",
    "warehouse": "warehouse",
}
_NUMBER_BASES: Final = frozenset(
    {"native_number", "decimal_separator", "report_convention", "unit_conversion"}
)
_DATE_BASES: Final = frozenset({"date_order", "declared_date_order", "report_convention"})
# Interpretations of a categorical spelling: the mapper's checked one, or a report pack's.
_TERM_BASES: Final = frozenset({"value_map", "report_convention", "unit_conversion"})


def source_text(record: Record | None, field: str) -> str | None:
    if record is None:
        return None
    return record.values.get(field, "").strip() or None


def source_refs(records: Sequence[Record], *fields: str) -> list[SourceRef]:
    result: list[SourceRef] = []
    seen: set[tuple[str, str, int, int, int | None, int | None]] = set()
    for record in records:
        for field, ref in record.evidence.items():
            if fields and field not in fields:
                continue
            key = (ref.source_id, ref.table, ref.row, ref.column, ref.start, ref.end)
            if key not in seen:
                seen.add(key)
                result.append(ref)
    return result


def add_issue(
    issues: list[Issue],
    code: str,
    message: str,
    records: Sequence[Record],
    *fields: str,
    warning: bool = False,
    locations: Sequence[IssueLocation] = (),
) -> None:
    issues.append(
        Issue(
            code=code,
            severity="warning" if warning else "blocker",
            message=message,
            evidence=source_refs(records, *fields),
            locations=list(locations),
        )
    )


# Example rows an issue's location names per table; the total is always given.
LOCATION_ROWS: Final = 5


def _first_ref(record: Record) -> SourceRef | None:
    return record.evidence.get("part_number") or next(iter(record.evidence.values()), None)


def table_of(record: Record) -> tuple[str, str] | None:
    """The source and table a record was read from, when it cites one."""
    ref = _first_ref(record)
    return (ref.source_id, ref.table) if ref is not None else None


def row_of(record: Record) -> tuple[str, str, int] | int:
    """The source row a record was read from; a record citing none is only itself."""
    ref = _first_ref(record)
    return (ref.source_id, ref.table, ref.row) if ref is not None else id(record)


def column_letter(column: int) -> str:
    """A spreadsheet column's letters: 1 -> A, 27 -> AA."""
    letters = ""
    while column > 0:
        column, rest = divmod(column - 1, 26)
        letters = chr(ord("A") + rest) + letters
    return letters


def locate(
    records: Sequence[Record], fields: Sequence[str], missing: Sequence[str] = ()
) -> list[IssueLocation]:
    """Per table, the columns read as `fields` and the rows of `records`, capped.

    `missing` are the fields these rows have no value for: their column is the one the
    table's other records give (see `describe_locations`), else unknown.
    """
    tables: dict[tuple[str, str], list[Record]] = defaultdict(list)
    for record in records:
        if (where := table_of(record)) is not None:
            tables[where].append(record)
    result: list[IssueLocation] = []
    for (source_id, table), rows in tables.items():
        columns: list[IssueColumn] = []
        for field in fields:
            column = next(
                (
                    ref.column
                    for r in rows
                    if (ref := r.evidence.get(field)) is not None and ref.table == table
                ),
                None,
            )
            columns.append(
                IssueColumn(
                    field=field,
                    column=column,
                    letter=column_letter(column) if column is not None else None,
                    missing=field in missing,
                )
            )
        lines = sorted({ref.row for r in rows if (ref := _first_ref(r)) is not None})
        result.append(
            IssueLocation(
                source_id=source_id,
                table=table,
                columns=columns,
                rows=lines[:LOCATION_ROWS],
                rows_total=len(lines),
            )
        )
    return result


def _header(table: EvidenceTable, column: int, first_row: int) -> str | None:
    """The column's header: the nearest text above the table's first record row."""
    for number in range(first_row - 1, 0, -1):
        cells = table.rows[number - 1] if number <= len(table.rows) else []
        cell = cells[column - 1] if column <= len(cells) else None
        if cell is not None and cell.strip():
            return cell.strip()
    return None


def describe_locations(
    issues: Sequence[Issue],
    records: Sequence[Record],
    tables: Sequence[EvidenceTable],
    filenames: Mapping[str, str],
) -> None:
    """Name each located issue's file, column letters and headers, in place.

    A column the located rows leave empty is the one the same table's other records
    read as that field, when the table has one.
    """
    by_name = {(t.source_id, t.name): t for t in tables}
    first: dict[tuple[str, str], int] = {}
    columns: dict[tuple[str, str, str], int] = {}
    for record in records:
        for field, ref in record.evidence.items():
            columns.setdefault((ref.source_id, ref.table, field), ref.column)
        # Data rows, not a title cell a section's constant was copied from.
        if (ref := _first_ref(record)) is not None:
            where = (ref.source_id, ref.table)
            first[where] = min(first.get(where, ref.row), ref.row)
    for issue in issues:
        for location in issue.locations:
            where = (location.source_id, location.table)
            location.filename = filenames.get(location.source_id, location.filename)
            table = by_name.get(where)
            for column in location.columns:
                if column.column is None:
                    column.column = columns.get((*where, column.field))
                if column.column is None:
                    continue
                column.letter = column_letter(column.column)
                if table is not None and where in first:
                    column.header = _header(table, column.column, first[where])


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


def unit_price(
    record: Record | None, issues: list[Issue], *, warning: bool = False
) -> Decimal | None:
    """A price per ONE unit: the source price divided by the quantity it is quoted for
    ("Por"/"Unidade de preço" 1000 = per thousand), when the source states one."""
    price = source_number(record, "unit_price", issues, warning=warning)
    if price is None:
        return None
    basis = source_number(record, "price_basis_quantity", issues, warning=warning)
    if basis is None or basis <= 0:
        return price
    return price / basis


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
    if canonical is not None and canonical.basis in _TERM_BASES and canonical.source == raw:
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
    if canonical is not None and canonical.basis in _TERM_BASES and canonical.source == raw:
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
    return _join_key(value)


@lru_cache(maxsize=65_536)
def _join_key(value: str) -> str | None:
    # Cached: large ERP exports compare the same few thousand codes millions of times.
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
