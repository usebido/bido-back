"""Bounded parallel interpretation with a deterministic, locator-only materializer.

The mapper proposes locators: table sections (columns, report-level constants), text
records, the meaning of a categorical column's distinct spellings (`value_maps`) and,
when the source's language establishes it, how numeric dates are written. Code checks
every locator and interpretation, copies source values, and derives canonical readings
with their provenance (`Record.canonical`): vocabulary terms, numbers whose decimal
separator other values of the column, sheet or source prove, and dates whose day/month
order they prove. A value that stays ambiguous is never guessed.
"""

import asyncio
import re
import unicodedata
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from bido_back.agents.catalog.definitions import MAPPER_MODEL
from bido_back.agents.config import RunnerConfig
from bido_back.agents.context import ToolContext
from bido_back.agents.definition import AgentDefinition
from bido_back.purchasing.importers import norm
from bido_back.purchasing.sources.contracts import (
    CanonicalBasis,
    CanonicalValue,
    CellRef,
    ConstantMapping,
    Coverage,
    DateOrder,
    EvidenceTable,
    Issue,
    MappingProposal,
    MappingTask,
    Record,
    RecordKind,
    SourceRef,
    TableManifest,
    TableProposal,
)
from bido_back.purchasing.sources.evidence import (
    DATE_FIELDS,
    NUMBER_FIELDS,
    identifier_variants,
)
from bido_back.purchasing.sources.formats import (
    SourceFormats,
    Votes,
    column_votes,
    number_needs_separator,
    read_date,
    read_number,
    resolve_date,
    scan_formats,
)
from bido_back.purchasing.sources.vocabulary import (
    UNIT_VOCABULARY,
    VOCABULARY,
    allowed,
    built_in,
    vocabulary_key,
)

CATALOG: dict[str, list[str]] = {
    "inventory": [
        "part_number",
        "description",
        "unit",
        "price_unit",
        "location",
        "warehouse",
        "available",
        "on_order",
        "safety_stock",
        "blocked",
        "reserved",
        "total",
        "unit_price",
        "currency",
        "as_of",
        "receipt_date",
        "balance_basis",
    ],
    "material_requirement": [
        "part_number",
        "description",
        "unit",
        "location",
        "quantity",
        "need_by",
        "reference",
        "project",
        "demand_basis",
        "area",
        "product_code",
    ],
    "supplier": ["supplier_code", "name", "email", "country"],
    "purchase": [
        "part_number",
        "supplier_code",
        "supplier_name",
        "quantity",
        "unit_price",
        "currency",
        "ordered_at",
        "unit",
        "po_number",
        "po_line",
        "location",
        "promised_at",
        "status",
        # The order line's own receipt and open-balance facts, when the register has them.
        "received_at",
        "received_quantity",
        "rejected_quantity",
        "open_quantity",
        "expected_at",
    ],
    "supplier_performance": [
        "supplier_code",
        "name",
        "otif_pct",
        "quality_pct",
        "rating",
        "lead_time_days",
    ],
    "purchase_summary": [
        "part_number",
        "description",
        "unit",
        "location",
        "warehouse",
        "available",
        "on_order",
        "safety_stock",
        "demand",
        "projected",
        "reported_to_buy",
        "unit_price",
        "reported_estimated_cost",
        "currency",
    ],
    "item": ["part_number", "description", "unit", "area"],
    "bom": [
        "product_code",
        "part_number",
        "quantity_per",
        "unit",
        "product_unit",
        "base_quantity",
        "location",
        "revision",
        "valid_from",
        "valid_to",
    ],
    "production_plan": [
        "reference",
        "product_code",
        "quantity",
        "unit",
        "location",
        "area",
        "need_by",
        "status",
    ],
    "receipt": [
        "po_number",
        "po_line",
        "location",
        "received_at",
        "quantity",
        "rejected",
        "unit",
        "receipt_id",
        "movement",
    ],
    "delivery_schedule": [
        "po_number",
        "po_line",
        "location",
        "part_number",
        "quantity",
        "unit",
        "expected_at",
        "status",
    ],
    "supplier_item": ["part_number", "supplier_code", "status", "valid_from"],
}
Mapper = Callable[[MappingTask, ToolContext], Awaitable[MappingProposal]]
PAGE_ROWS = 60
PAGE_CHARS = 24_000
MAX_PAGES = 100
CONCURRENCY = 4
MAX_TABLES = 100
MANIFEST_CHARS = 20_000
# Columns whose blank cells may repeat the value above them (a grouping key).
FILL_DOWN_FIELDS = frozenset(
    {
        "po_number", "po_line", "reference", "product_code", "part_number", "supplier_code",
        "supplier_name", "location", "warehouse",
    }
)  # fmt: skip
# Key columns that a transaction row names; a row without any is not a transaction.
_KEY_FIELDS = frozenset({"part_number", "product_code", "po_number", "reference", "supplier_code"})
# First words of rows an ERP or a person adds under a table: never transactions.
_TOTAL_WORDS = frozenset(
    {
        "total", "totals", "totais", "totale", "totali", "totaux", "subtotal", "subtotais",
        "subtotale", "sum", "soma", "somatorio", "suma", "summe", "zwischensumme", "gesamt",
        "gesamtsumme", "grand",
    }
)  # fmt: skip
_TOTAL_MARKS = frozenset({"*", "**", "***"})
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
# Share of a mapped column's values that must read as its field's type.
ADMISSIBLE_SHARE = 0.8
PROMPT_FILE = "source_mapper.v3.md"
PROMPT_VERSION = 3


def _anchors(table: EvidenceTable) -> list[int]:
    """Include late headers and separate blocks instead of only the report's title rows."""
    boundaries = [
        i for i, row in enumerate(table.rows) if any(row) and (i == 0 or not any(table.rows[i - 1]))
    ]
    indices = sorted(
        {
            i
            for boundary in boundaries
            for i in (boundary, boundary + 1)
            if i < len(table.rows) and any(table.rows[i])
        }
    )
    return indices[:16]


def default_mapper(config: RunnerConfig | None = None) -> Mapper:
    async def call(task: MappingTask, context: ToolContext) -> MappingProposal:
        from bido_back.agents.runner import Completed, run_to_completion

        agent = AgentDefinition(
            name="source_mapper",
            prompt_file=PROMPT_FILE,
            prompt_version=PROMPT_VERSION,
            model=MAPPER_MODEL,
            output_model=MappingProposal,
            max_turns=1,
            effort="medium",
        )
        result = await run_to_completion(agent, task, context, config=config)
        if isinstance(result, Completed):
            return result.output
        return MappingProposal(
            issues=[
                Issue(
                    code="mapping_failed",
                    severity="blocker",
                    message="Source interpretation failed.",
                )
            ]
        )

    return call


def _pages(table: EvidenceTable) -> list[tuple[int, int]]:
    result: list[tuple[int, int]] = []
    first, size = 0, 0
    for i, row in enumerate(table.rows):
        length = sum(len(v or "") + 8 for v in row)
        if length > PAGE_CHARS:
            raise ValueError("source_row_limit")
        if i > first and (i - first >= PAGE_ROWS or size + length > PAGE_CHARS):
            result.append((first, i))
            first, size = i, 0
        size += length
    if first < len(table.rows):
        result.append((first, len(table.rows)))
    return result


def _copy(table: EvidenceTable, ref: CellRef) -> tuple[str, SourceRef]:
    if ref.row > len(table.rows) or ref.column > len(table.rows[ref.row - 1]):
        raise ValueError("invalid_locator")
    value = table.rows[ref.row - 1][ref.column - 1]
    if value is None or not value.strip():
        raise ValueError("empty_value")
    if (ref.start is None) != (ref.end is None):
        raise ValueError("invalid_slice")
    if ref.start is not None and ref.end is not None:
        if not 0 <= ref.start < ref.end <= len(value):
            raise ValueError("invalid_slice")
        value = value[ref.start : ref.end]
    return value.strip(), SourceRef(
        source_id=table.source_id,
        table=table.name,
        **ref.model_dump(),
    )


def _copy_constant(table: EvidenceTable, constant: ConstantMapping) -> tuple[str, SourceRef]:
    """A constant's value: its cell, its slice, or the exact `text` found inside the cell."""
    ref = constant.ref
    if constant.text is None:
        return _copy(table, ref)
    whole = CellRef(row=ref.row, column=ref.column)
    _copy(table, whole)  # Bounds and an empty cell are checked before searching it.
    cell = table.rows[ref.row - 1][ref.column - 1] or ""
    wanted = constant.text.strip()
    start = cell.find(wanted) if wanted else -1
    if start < 0:
        raise ValueError("invalid_constant_text")
    return _copy(
        table, CellRef(row=ref.row, column=ref.column, start=start, end=start + len(wanted))
    )


def _overlap(refs: Sequence[CellRef | SourceRef]) -> bool:
    for i, ref in enumerate(refs):
        for other in refs[i + 1 :]:
            if ref.row != other.row or ref.column != other.column:
                continue
            if (
                ref.start is None
                or other.start is None
                or ref.end is None
                or other.end is None
                or max(ref.start, other.start) < min(ref.end, other.end)
            ):
                return True
    return False


def _spelling(value: str) -> str:
    """Spellings are matched ignoring case and spacing, never accents ("fermé" != "ferme")."""
    return " ".join(unicodedata.normalize("NFC", value).casefold().split())


def _column_values(table: EvidenceTable, column: int) -> set[str]:
    """Every spelling written in one column of the table, in any of its rows."""
    values = (row[column - 1] if len(row) >= column else None for row in table.rows)
    return {_spelling(value) for value in values if value is not None and value.strip()}


def _cell(table: EvidenceTable, row: int, column: int) -> str | None:
    cells = table.rows[row - 1] if 0 < row <= len(table.rows) else []
    value = cells[column - 1] if 0 < column <= len(cells) else None
    return value if value is not None and value.strip() else None


def _header_matches(table: EvidenceTable, header_row: int, column: int, text: str) -> bool:
    """The echoed header is the column's header cell (or the merged cell above it)."""
    wanted = _spelling(text)
    return any(
        (cell := _cell(table, row, column)) is not None and _spelling(cell) == wanted
        for row in (header_row, header_row - 1)
    )


def _derived_row(
    table: EvidenceTable, row: int, columns: dict[str, int], earlier: list[int]
) -> bool:
    """Totals and subtotals: a total keyword, an ERP subtotal mark, or a key-less sum."""
    for value in table.rows[row - 1]:
        if value is None or not value.strip():
            continue
        if value.strip() in _TOTAL_MARKS:
            return True
        words = norm(value).split()
        if words and words[0] in _TOTAL_WORDS and read_number(value) is None:
            return True
    keys = [columns[name] for name in _KEY_FIELDS if name in columns]
    if not keys or any(_cell(table, row, column) for column in keys) or len(earlier) < 2:
        return False
    for name, column in columns.items():
        value = _cell(table, row, column)
        if name not in NUMBER_FIELDS or value is None:
            continue
        total = read_number(value)
        above = [read_number(v) for r in earlier if (v := _cell(table, r, column)) is not None]
        if total is not None and above and None not in above:
            if sum((n for n in above if n is not None), Decimal(0)) == total:
                return True
    return False


def _admissible(
    table: EvidenceTable,
    kind: RecordKind,
    name: str,
    cells: list[tuple[int, int]],
    mapping: dict[str, str | None],
) -> bool:
    """Most of a mapped column's values read as its field's type, unit or address."""
    unit = vocabulary_key(kind, name) == UNIT_VOCABULARY
    if name not in NUMBER_FIELDS and name not in DATE_FIELDS and not unit and name != "email":
        return True
    values = [(cell, value) for cell in cells if (value := _cell(table, *cell)) is not None]
    if not values:
        return True

    def reads(cell: tuple[int, int], value: str) -> bool:
        if name in NUMBER_FIELDS:
            return (
                cell in table.numbers
                or read_number(value) is not None
                or number_needs_separator(value)
            )
        if name in DATE_FIELDS:
            reading = read_date(value)
            return reading.value is not None or reading.ambiguous
        if unit:
            return (
                built_in(kind, name, value) is not None or mapping.get(_spelling(value)) is not None
            )
        return _EMAIL.fullmatch(value.strip()) is not None

    good = sum(1 for cell, value in values if reads(cell, value))
    return good >= ADMISSIBLE_SHARE * len(values)


def _constants(
    table: EvidenceTable, section: TableProposal, issues: list[Issue]
) -> dict[str, tuple[str, SourceRef]]:
    """Validated report-level values for the section, copied from their own cells."""
    found: dict[str, tuple[str, SourceRef]] = {}
    for constant in section.constants:
        try:
            if constant.field not in CATALOG[section.kind] or constant.field in found:
                raise ValueError("invalid_constant")
            if section.first_row <= constant.ref.row <= section.last_row:
                # One data row's value can never stand for every row of the section.
                raise ValueError("invalid_constant")
            found[constant.field] = _copy_constant(table, constant)
            if _overlap([ref for _, ref in found.values()]):
                del found[constant.field]
                raise ValueError("overlapping_fields")
        except ValueError as exc:
            issues.append(
                Issue(
                    code="invalid_constant" if str(exc) == "empty_value" else str(exc),
                    severity="blocker",
                    message=f"Source {table.source_id}, {table.name}: a report-level value "
                    "for a section is not usable evidence.",
                )
            )
    return found


def _value_maps(
    table: EvidenceTable,
    section: TableProposal,
    columns: dict[str, int],
    constants: dict[str, tuple[str, SourceRef]],
    issues: list[Issue],
) -> dict[str, dict[str, str | None]]:
    """Checked spelling -> canonical term per field; None marks disagreeing meanings."""
    result: dict[str, dict[str, str | None]] = {}
    invalid = conflicted = 0
    for entry in section.value_maps:
        name = entry.field
        observed = _column_values(table, columns[name]) if name in columns else set[str]()
        if name in constants:
            observed.add(_spelling(constants[name][0]))
        key = _spelling(entry.source)
        if entry.canonical not in allowed(section.kind, name) or key not in observed:
            invalid += 1
            continue
        mapping = result.setdefault(name, {})
        known = built_in(section.kind, name, entry.source)
        if (key in mapping and mapping[key] != entry.canonical) or (
            known is not None and known != entry.canonical
        ):
            mapping[key] = None
            conflicted += 1
        else:
            mapping.setdefault(key, entry.canonical)
    if invalid:
        issues.append(
            Issue(
                code="invalid_value_map",
                severity="warning",
                message=f"{table.name}: {invalid} proposed interpretation(s) of categorical "
                "values were ignored: the spelling does not occur in the section's column "
                "or the term is not in the field's vocabulary.",
            )
        )
    if conflicted:
        issues.append(
            Issue(
                code="conflicting_value_map",
                severity="warning",
                message=f"{table.name}: {conflicted} categorical spelling(s) received "
                "contradicting interpretations and stay unresolved.",
            )
        )
    return result


@dataclass
class _Derivations:
    """Canonical readings derived for one section or text record, and their disclosure."""

    table: EvidenceTable
    formats: SourceFormats
    declared: DateOrder | None
    affected: dict[tuple[str, CanonicalBasis], list[SourceRef]] = field(
        default_factory=dict[tuple[str, CanonicalBasis], list[SourceRef]]
    )
    proofs: dict[tuple[str, CanonicalBasis], SourceRef] = field(
        default_factory=dict[tuple[str, CanonicalBasis], SourceRef]
    )
    contradicted: set[str] = field(default_factory=set[str])

    def decide(
        self, name: str, column: Votes, *, dates: bool
    ) -> tuple[str | None, SourceRef | None]:
        """The column's proof, else the sheet's, else the source's; a conflict stops."""
        for votes in (column, *self.formats.levels(self.table, dates=dates)):
            if votes.conflict:
                return None, None
            proof = votes.proof()
            if proof is not None:
                if dates and self.declared is not None and proof[0] != self.declared:
                    self.contradicted.add(name)
                return proof
        return None, None

    def note(
        self, name: str, basis: CanonicalBasis, ref: SourceRef, proof: SourceRef | None
    ) -> None:
        self.affected.setdefault((name, basis), []).append(ref)
        if proof is not None:
            self.proofs.setdefault((name, basis), proof)

    def derive(self, record: Record, column: dict[str, Votes]) -> None:
        for name, raw in record.values.items():
            ref = record.evidence.get(name)
            if ref is None:
                continue
            if name in NUMBER_FIELDS:
                self._number(record, name, raw, ref, column)
            elif name in DATE_FIELDS:
                self._date(record, name, raw, ref, column)

    def _number(
        self, record: Record, name: str, raw: str, ref: SourceRef, column: dict[str, Votes]
    ) -> None:
        if read_number(raw) is not None:
            return
        if ref.start is None and (ref.row, ref.column) in self.table.numbers:
            try:
                value = Decimal(raw)
            except InvalidOperation:
                return
            record.canonical[name] = CanonicalValue(
                value=format(value, "f"), source=raw, basis="native_number"
            )
            return
        if not number_needs_separator(raw):
            return
        separator, proof = self.decide(name, column[name], dates=False)
        if separator not in (",", "."):
            return
        number = read_number(raw, "," if separator == "," else ".")
        if number is None or proof is None:
            return
        record.canonical[name] = CanonicalValue(
            value=format(number, "f"), source=raw, basis="decimal_separator", evidence=[proof]
        )
        self.note(name, "decimal_separator", ref, proof)

    def _date(
        self, record: Record, name: str, raw: str, ref: SourceRef, column: dict[str, Votes]
    ) -> None:
        if not read_date(raw).ambiguous:
            return
        order, proof = self.decide(name, column[name], dates=True)
        basis: CanonicalBasis = "date_order"
        if order is None and proof is None and not self._conflicting(name, column[name]):
            order, basis = self.declared, "declared_date_order"
        if order not in ("day_first", "month_first"):
            return
        day = resolve_date(raw, "day_first" if order == "day_first" else "month_first")
        if day is None:
            return
        record.canonical[name] = CanonicalValue(
            value=day.isoformat(),
            source=raw,
            basis=basis,
            evidence=[proof] if proof is not None else [],
        )
        self.note(name, basis, ref, proof)

    def _conflicting(self, name: str, column: Votes) -> bool:
        return any(v.conflict for v in (column, *self.formats.levels(self.table, dates=True)))

    def disclose(self, issues: list[Issue]) -> None:
        for (name, basis), refs in self.affected.items():
            proof = self.proofs.get((name, basis))
            evidence = [*([proof] if proof is not None else []), *refs[:10]]
            if basis == "decimal_separator":
                code = "inferred_decimal_separator"
                reason = "other values of the same column, sheet or source prove the separator"
            elif basis == "date_order":
                code = "inferred_date_order"
                reason = "other dates of the same column, sheet or source prove the day/month order"
            else:
                code = "declared_date_order"
                reason = (
                    "the mapper declared the day/month order from the source's language; no "
                    "value establishes or contradicts it"
                )
            issues.append(
                Issue(
                    code=code,
                    severity="warning",
                    message=f"{self.table.name}: {len(refs)} ambiguous {name} value(s) were "
                    f"read because {reason}.",
                    evidence=evidence,
                )
            )
        for name in sorted(self.contradicted):
            issues.append(
                Issue(
                    code="declared_date_order_contradicted",
                    severity="warning",
                    message=f"{self.table.name}: the {name} values prove a day/month order "
                    "other than the one the mapper declared; the values' order was used.",
                )
            )


def materialize(
    table: EvidenceTable,
    proposal: MappingProposal,
    first: int,
    last: int,
    *,
    formats: SourceFormats | None = None,
) -> tuple[list[Record], list[Issue], set[int]]:
    """Reject invented fields, references outside this page and overlapping semantic rows."""
    formats = formats or scan_formats([table])
    records: list[Record] = []
    issues: list[Issue] = []
    for issue in proposal.issues:
        try:
            for ref in issue.evidence:
                if (
                    ref.source_id != table.source_id
                    or ref.table != table.name
                    or not first <= ref.row <= last
                ):
                    raise ValueError("invalid_issue_locator")
                _copy(table, CellRef(row=ref.row, column=ref.column, start=ref.start, end=ref.end))
            issues.append(issue)
        except ValueError:
            issues.append(
                Issue(
                    code="invalid_issue_locator",
                    severity="blocker",
                    message="A proposed issue cites unavailable evidence.",
                )
            )
    covered: set[int] = set()
    seen: set[tuple[RecordKind, int]] = set()

    def add(
        kind: RecordKind,
        fields: dict[str, CellRef],
        constants: dict[str, tuple[str, SourceRef]] | None = None,
    ) -> Record | None:
        try:
            constants = constants or {}
            if not fields or (set(fields) | set(constants)) - set(CATALOG[kind]):
                raise ValueError("invalid_fields")
            if any(not first <= ref.row <= last for ref in fields.values()):
                raise ValueError("invalid_locator")
            if _overlap(list(fields.values())):
                raise ValueError("overlapping_fields")
            values: dict[str, str] = {}
            evidence: dict[str, SourceRef] = {}
            for name, ref in fields.items():
                try:
                    values[name], evidence[name] = _copy(table, ref)
                except ValueError as exc:
                    if str(exc) != "empty_value":
                        raise
            if not values:
                return None
            # A report-level value applies unless the row has its own value for the field.
            for name, (value, ref) in constants.items():
                if name not in values:
                    values[name], evidence[name] = value, ref
            key = (kind, min(ref.row for ref in fields.values()))
            if key in seen:
                raise ValueError("overlapping_mapping")
            seen.add(key)
            record = Record(kind=kind, values=values, evidence=evidence)
            records.append(record)
            covered.update(ref.row for ref in evidence.values())
            return record
        except ValueError as exc:
            issues.append(
                Issue(
                    code=str(exc),
                    severity="blocker",
                    message=f"Source {table.source_id}, {table.name}: invalid proposed mapping.",
                )
            )
            return None

    width = max((len(row) for row in table.rows), default=0)
    for section in proposal.sections:
        columns = {assignment.field: assignment.column for assignment in section.columns}
        if (
            not (
                1 <= section.header_row < section.first_row <= section.last_row <= last
                and section.first_row >= first
            )
            or not columns
            or len(columns) != len(section.columns)
            or any(column < 1 or column > width for column in columns.values())
            or len(set(columns.values())) != len(columns)
        ):
            issues.append(
                Issue(
                    code="invalid_mapping",
                    severity="blocker",
                    message="Table mapping has invalid rows or columns.",
                )
            )
            continue
        mismatched = [
            a.field
            for a in section.columns
            if a.header_text is not None
            and not _header_matches(table, section.header_row, a.column, a.header_text)
        ]
        if mismatched:
            issues.append(
                Issue(
                    code="header_mismatch",
                    severity="blocker",
                    message=f"{table.name}: the header echoed for {', '.join(sorted(mismatched))} "
                    "is not the header cell of the mapped column; the section was not read.",
                )
            )
            continue
        fills = {a.field for a in section.columns if a.fill_down}
        if fills - FILL_DOWN_FIELDS:
            issues.append(
                Issue(
                    code="invalid_fill_down",
                    severity="blocker",
                    message=f"{table.name}: only identifier columns may repeat the value above.",
                )
            )
            continue
        constants = _constants(table, section, issues)
        maps = _value_maps(table, section, columns, constants, issues)
        transactions: list[int] = []
        derived: list[int] = []
        for row in range(section.first_row, section.last_row + 1):
            if not any(table.rows[row - 1]):
                continue
            if _derived_row(table, row, columns, transactions):
                derived.append(row)
            else:
                transactions.append(row)
        inadmissible = [
            name
            for name, column in columns.items()
            if not _admissible(
                table, section.kind, name, [(r, column) for r in transactions], maps.get(name, {})
            )
        ] + [
            name
            for name, (_, ref) in constants.items()
            if ref.start is None
            and not _admissible(
                table, section.kind, name, [(ref.row, ref.column)], maps.get(name, {})
            )
        ]
        if inadmissible:
            issues.append(
                Issue(
                    code="inadmissible_mapping",
                    severity="blocker",
                    message=f"{table.name}: the values mapped as {', '.join(sorted(inadmissible))} "
                    "do not read as that field (number, date, unit or e-mail); the section was "
                    "not read.",
                )
            )
            continue
        found: list[Record] = []
        filled = 0
        above: dict[str, tuple[str, SourceRef]] = {}
        for row in transactions:
            row_fills: dict[str, tuple[str, SourceRef]] = {}
            for name in fills:
                if _cell(table, row, columns[name]) is not None:
                    above[name] = _copy(table, CellRef(row=row, column=columns[name]))
                elif name in above:
                    row_fills[name] = above[name]
            record = add(
                section.kind,
                {name: CellRef(row=row, column=column) for name, column in columns.items()},
                {**constants, **row_fills},
            )
            if record is not None:
                found.append(record)
                filled += bool(row_fills)
        if derived:
            issues.append(
                Issue(
                    code="derived_rows_skipped",
                    severity="warning",
                    message=f"{table.name}: {len(derived)} total or subtotal row(s) inside a "
                    "section were not read as transactions.",
                    evidence=[
                        SourceRef(source_id=table.source_id, table=table.name, row=r, column=1)
                        for r in derived[:10]
                    ],
                )
            )
        if filled:
            issues.append(
                Issue(
                    code="filled_down",
                    severity="warning",
                    message=f"{table.name}: {filled} row(s) left a grouping key blank and use "
                    "the value of the row above; the evidence points at that cell.",
                )
            )
        rows = range(section.first_row, section.last_row + 1)
        votes = {
            name: column_votes(
                table,
                [(row, columns[name]) for row in rows]
                if name in columns
                else [(constants[name][1].row, constants[name][1].column)],
                dates=name in DATE_FIELDS,
            )
            for name in (set(columns) | set(constants)) & (NUMBER_FIELDS | DATE_FIELDS)
        }
        derivations = _Derivations(table, formats, section.date_order)
        for record in found:
            for name, mapping in maps.items():
                raw = record.values.get(name)
                if raw is not None and _spelling(raw) in mapping:
                    record.canonical[name] = CanonicalValue(
                        value=mapping[_spelling(raw)], source=raw, basis="value_map"
                    )
            derivations.derive(record, votes)
        derivations.disclose(issues)
    for proposed in proposal.records:
        fields = {assignment.field: assignment.ref for assignment in proposed.fields}
        if len(fields) != len(proposed.fields):
            issues.append(
                Issue(
                    code="invalid_fields",
                    severity="blocker",
                    message="A record maps the same semantic field more than once.",
                )
            )
            continue
        record = add(proposed.kind, fields)
        if record is not None:
            derivations = _Derivations(table, formats, proposed.date_order)
            derivations.derive(
                record,
                {
                    name: column_votes(table, [(ref.row, ref.column)], dates=name in DATE_FIELDS)
                    for name, ref in fields.items()
                },
            )
            derivations.disclose(issues)
    return records, issues, covered


def _continue(sections: list[TableProposal], previous: list[TableProposal]) -> list[TableProposal]:
    """A section continued from an earlier page keeps that page's report-level context."""
    result: list[TableProposal] = []
    for section in sections:
        earlier = next(
            (p for p in previous if p.kind == section.kind and p.header_row == section.header_row),
            None,
        )
        if earlier is not None:
            update: dict[str, object] = {}
            if not section.constants and earlier.constants:
                update["constants"] = earlier.constants
            if not section.value_maps and earlier.value_maps:
                update["value_maps"] = earlier.value_maps
            if section.date_order is None and earlier.date_order is not None:
                update["date_order"] = earlier.date_order
            if update:
                section = section.model_copy(update=update)
        result.append(section)
    return result


async def map_sources(
    tables: Sequence[EvidenceTable],
    objective: str,
    context: ToolContext,
    mapper: Mapper,
) -> tuple[list[Record], list[Issue], list[Coverage]]:
    if len(tables) > MAX_TABLES:
        return (
            [],
            [
                Issue(
                    code="source_limit",
                    severity="blocker",
                    message="Too many source tables for one assessment.",
                )
            ],
            [
                Coverage(
                    source_id=t.source_id, filename=t.source_id, table=t.name, rows=len(t.rows)
                )
                for t in tables
            ],
        )
    sample_cells = sum(sum(bool(v) for v in t.rows[i][:40]) for t in tables for i in _anchors(t))
    sample_chars = max(1, MANIFEST_CHARS // max(sample_cells, 1))
    manifest = [
        TableManifest(
            source_id=t.source_id,
            name=t.name,
            rows=len(t.rows),
            columns=max((len(r) for r in t.rows), default=0),
            sample=[
                [v[: min(120, sample_chars)] if v else v for v in t.rows[i][:40]]
                for i in _anchors(t)
            ],
            sample_rows=[i + 1 for i in _anchors(t)],
        )
        for t in tables
    ]
    semaphore = asyncio.Semaphore(CONCURRENCY)
    try:
        pages = [_pages(t) for t in tables]
        if sum(map(len, pages)) > MAX_PAGES:
            raise ValueError("source_page_limit")
    except ValueError:
        return (
            [],
            [
                Issue(
                    code="source_limit",
                    severity="blocker",
                    message="Sources exceed the bounded interpretation budget.",
                )
            ],
            [
                Coverage(
                    source_id=t.source_id, filename=t.source_id, table=t.name, rows=len(t.rows)
                )
                for t in tables
            ],
        )
    formats = scan_formats(tables)

    async def interpret(
        table: EvidenceTable,
        spans: list[tuple[int, int]],
    ) -> tuple[list[Record], list[Issue], Coverage]:
        records: list[Record] = []
        issues: list[Issue] = []
        previous: list[TableProposal] = []
        covered: set[int] = set()
        complete = True
        for first, last in spans:
            task = MappingTask(
                objective=objective,
                source_id=table.source_id,
                table=table.name,
                layout=table.layout,
                first_row=first + 1,
                last_row=last,
                rows=table.rows[first:last],
                previous_sections=previous,
                manifest=manifest,
                catalog=CATALOG,
                vocabulary=VOCABULARY,
            )
            try:
                async with semaphore:
                    output = await mapper(
                        task,
                        ToolContext(
                            tenant_id=context.tenant_id, user_id=context.user_id, run_id=uuid4()
                        ),
                    )
            except Exception:
                complete = False
                issues.append(
                    Issue(
                        code="mapping_failed",
                        severity="blocker",
                        message="Source interpretation failed for a page.",
                    )
                )
                continue
            if any(i.code == "mapping_failed" for i in output.issues):
                complete = False
            sections = _continue(output.sections, previous)
            output = output.model_copy(update={"sections": sections})
            found, problems, rows = materialize(table, output, first + 1, last, formats=formats)
            records.extend(found)
            issues.extend(problems)
            covered.update(rows)
            previous = sections or previous
        if len(covered) < sum(bool(any(row)) for row in table.rows):
            issues.append(
                Issue(
                    code="unmapped_rows",
                    severity="warning",
                    message=f"{table.name}: {len(covered)} of {len(table.rows)} rows mapped; "
                    "headers, narrative and unsupported content are not canonical records.",
                )
            )
        return (
            records,
            issues,
            Coverage(
                source_id=table.source_id,
                filename=table.source_id,
                table=table.name,
                rows=len(table.rows),
                mapped_rows=len(covered),
                inspected=complete,
            ),
        )

    results = await asyncio.gather(*(interpret(t, p) for t, p in zip(tables, pages, strict=True)))
    records = [r for found, _, _ in results for r in found]
    return (
        records,
        [*(i for _, issues, _ in results for i in issues), *identifier_variants(records)],
        [c for _, _, c in results],
    )
