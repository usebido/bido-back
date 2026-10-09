"""What a report pack is: deterministic readers of an ERP's standard list reports.

A pack recognizes a table by its header row (the report's own column names, in the
languages the ERP exports) and reads it into the same `Record`s the source mapper's
locators produce: every value is a copied cell (or an exact slice of one) with its
`SourceRef`, and what the report's conventions decide (an MRP requirement's minus
sign, a price per price unit, the status a list implies) is a `CanonicalValue` with
basis `report_convention`. No model is called for a recognized table.

`reconcile` then sees every record of the call, so a pack can settle what one report
leaves to another (a stock list without a date dated by the MRP list's stock line,
duplicated purchase orders) and say so as `Assumption`s.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Protocol

from bido_back.purchasing.importers import norm
from bido_back.purchasing.sources.contracts import (
    Assumption,
    CanonicalValue,
    EvidenceTable,
    Issue,
    Record,
    RecordKind,
    SourceRef,
)
from bido_back.purchasing.sources.evidence import NUMBER_FIELDS
from bido_back.purchasing.sources.formats import (
    decimal_vote,
    number_needs_separator,
    read_number,
)

# Rows searched for a report's header: ERP list exports put a title and the selection
# above it.
HEADER_ROWS = 15
# ERP subtotal marks in any cell: never a transaction.
_TOTAL_MARKS = frozenset({"*", "**", "***"})


@dataclass(frozen=True, slots=True)
class Column:
    """A report column: the field it is read as and its header in each language."""

    name: str
    headers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReportSpec:
    code: str
    title: str
    columns: tuple[Column, ...]
    # Column names that identify the report; all must be present.
    required: frozenset[str]
    # At least one of these must be present too (empty: no such condition).
    any_of: frozenset[str] = frozenset()

    def match(self, cells: Sequence[str | None]) -> dict[str, int] | None:
        """The report's columns found in one header row (1-based), when it is this one."""
        headers = {norm(cell): i for i, cell in enumerate(cells, start=1) if cell and cell.strip()}
        found: dict[str, int] = {}
        for column in self.columns:
            for header in column.headers:
                position = headers.get(norm(header))
                if position is not None and position not in found.values():
                    found[column.name] = position
                    break
        if not self.required <= set(found) or (self.any_of and not self.any_of & set(found)):
            return None
        return found


@dataclass(frozen=True, slots=True)
class Recognition:
    spec: ReportSpec
    header_row: int
    columns: dict[str, int]


@dataclass
class Reading:
    """What a pack read from one table."""

    table: EvidenceTable
    recognition: Recognition
    records: list[Record] = field(default_factory=list[Record])
    issues: list[Issue] = field(default_factory=list[Issue])
    assumptions: list[Assumption] = field(default_factory=list[Assumption])
    covered: set[int] = field(default_factory=set[int])

    @property
    def report(self) -> str:
        return self.recognition.spec.code


@dataclass(frozen=True, slots=True)
class PackContext:
    # Source id -> file name: a report's date is often only in its file name.
    filenames: Mapping[str, str]
    # Disclosed fallback for a stock position nothing dates.
    today: date


class ReportPack(Protocol):
    name: str

    def recognize(self, table: EvidenceTable) -> Recognition | None: ...

    def read(self, table: EvidenceTable, recognition: Recognition, context: PackContext) -> Reading:
        """Records of one recognized table."""
        ...

    def reconcile(
        self, readings: Sequence[Reading], context: PackContext
    ) -> tuple[list[Issue], list[Assumption]]:
        """Settle what reports leave to each other, in place, across the call's readings."""
        ...


def recognize(specs: Sequence[ReportSpec], table: EvidenceTable) -> Recognition | None:
    """The most specific report whose header row is among the table's first rows."""
    if table.layout != "table":
        return None
    best: Recognition | None = None
    for index, cells in enumerate(table.rows[:HEADER_ROWS], start=1):
        for spec in specs:
            columns = spec.match(cells)
            if columns is not None and (best is None or len(columns) > len(best.columns)):
                best = Recognition(spec=spec, header_row=index, columns=columns)
        if best is not None:
            return best
    return None


def table_separator(table: EvidenceTable) -> str | None:
    """The decimal separator the table's text numbers prove, when they all agree."""
    votes = {
        vote
        for row in table.rows
        for cell in row
        if cell is not None and (vote := decimal_vote(cell, pure=True)) is not None
    }
    return next(iter(votes)) if len(votes) == 1 else None


@dataclass(frozen=True, slots=True)
class Row:
    """One data row of a recognized report, read by column name."""

    table: EvidenceTable
    number: int
    columns: Mapping[str, int]
    # The table's proven decimal separator, for text numbers such as "1.200,000".
    separator: str | None = None

    def reading(self, name: str) -> CanonicalValue | None:
        """How a number cell reads when its spelling alone does not say."""
        value, column = self.text(name), self.columns.get(name)
        if value is None or column is None or read_number(value) is not None:
            return None
        if (self.number, column) in self.table.numbers:
            try:
                return CanonicalValue(
                    value=format(Decimal(value), "f"), source=value, basis="native_number"
                )
            except InvalidOperation:
                return None
        if self.separator in (",", ".") and number_needs_separator(value):
            number = read_number(value, "," if self.separator == "," else ".")
            if number is not None:
                return CanonicalValue(
                    value=format(number, "f"), source=value, basis="decimal_separator"
                )
        return None

    def text(self, name: str) -> str | None:
        column = self.columns.get(name)
        cells = self.table.rows[self.number - 1]
        if column is None or column > len(cells):
            return None
        value = cells[column - 1]
        return value.strip() if value is not None and value.strip() else None

    def ref(self, name: str, start: int | None = None, end: int | None = None) -> SourceRef:
        return SourceRef(
            source_id=self.table.source_id,
            table=self.table.name,
            row=self.number,
            column=self.columns[name],
            start=start,
            end=end,
        )

    def number_of(self, name: str) -> Decimal | None:
        value = self.text(name)
        if value is None:
            return None
        reading = self.reading(name)
        if reading is not None and reading.value is not None:
            return Decimal(reading.value)
        return read_number(value)


def data_rows(table: EvidenceTable, recognition: Recognition, key: str) -> tuple[list[Row], int]:
    """Rows under the header up to the first blank row; rows without `key` or with a
    subtotal mark are skipped (their count is returned)."""
    rows: list[Row] = []
    skipped = 0
    separator = table_separator(table)
    for number in range(recognition.header_row + 1, len(table.rows) + 1):
        cells = table.rows[number - 1]
        if not any(cell is not None and cell.strip() for cell in cells):
            if rows:
                break
            continue
        row = Row(table, number, recognition.columns, separator)
        if row.text(key) is None or any(
            cell is not None and cell.strip() in _TOTAL_MARKS for cell in cells
        ):
            skipped += 1
            continue
        rows.append(row)
    return rows, skipped


def record(kind: RecordKind, row: Row, fields: Mapping[str, str]) -> Record:
    """A record of `kind` from the row's cells: target field -> report column name."""
    values: dict[str, str] = {}
    evidence: dict[str, SourceRef] = {}
    canonical: dict[str, CanonicalValue] = {}
    for target, column in fields.items():
        value = row.text(column)
        if value is not None:
            values[target] = value
            evidence[target] = row.ref(column)
            if target in NUMBER_FIELDS and (reading := row.reading(column)) is not None:
                canonical[target] = reading
    return Record(kind=kind, values=values, evidence=evidence, canonical=canonical)


def put(
    target: Record,
    name: str,
    value: str,
    ref: SourceRef | None,
    canonical: str | None = None,
    *,
    basis: str = "report_convention",
) -> None:
    """Set one value (a copied spelling) and, when a convention decides it, its reading."""
    target.values[name] = value
    if ref is not None:
        target.evidence[name] = ref
    if canonical is not None:
        target.canonical[name] = CanonicalValue.model_validate(
            {"value": canonical, "source": value, "basis": basis}
        )


def plain(number: Decimal) -> str:
    """A decimal as canonical text, without exponent or trailing zeros."""
    text = format(number.normalize(), "f")
    return "0" if text in ("-0", "") else text
