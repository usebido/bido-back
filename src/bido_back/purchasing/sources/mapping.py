"""Bounded parallel interpretation with a deterministic, locator-only materializer."""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from uuid import uuid4

from bido_back.agents.catalog.definitions import MAPPER_MODEL
from bido_back.agents.config import RunnerConfig
from bido_back.agents.context import ToolContext
from bido_back.agents.definition import AgentDefinition
from bido_back.purchasing.sources.contracts import (
    CellRef,
    Coverage,
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
}
Mapper = Callable[[MappingTask, ToolContext], Awaitable[MappingProposal]]
PAGE_ROWS = 60
PAGE_CHARS = 24_000
MAX_PAGES = 100
CONCURRENCY = 4
MAX_TABLES = 100
MANIFEST_CHARS = 20_000


def default_mapper(config: RunnerConfig | None = None) -> Mapper:
    async def call(task: MappingTask, context: ToolContext) -> MappingProposal:
        from bido_back.agents.runner import Completed, run_to_completion

        agent = AgentDefinition(
            name="source_mapper",
            prompt_file="source_mapper.v1.md",
            prompt_version=1,
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


def materialize(
    table: EvidenceTable,
    proposal: MappingProposal,
    first: int,
    last: int,
) -> tuple[list[Record], list[Issue], set[int]]:
    """Reject invented fields, references outside this page and overlapping semantic rows."""
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

    def add(kind: RecordKind, fields: dict[str, CellRef]) -> None:
        try:
            if not fields or set(fields) - set(CATALOG[kind]):
                raise ValueError("invalid_fields")
            if any(not first <= ref.row <= last for ref in fields.values()):
                raise ValueError("invalid_locator")
            refs = list(fields.values())
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
                        raise ValueError("overlapping_fields")
            values: dict[str, str] = {}
            evidence: dict[str, SourceRef] = {}
            for field, ref in fields.items():
                try:
                    values[field], evidence[field] = _copy(table, ref)
                except ValueError as exc:
                    if str(exc) != "empty_value":
                        raise
            if not values:
                return
            key = (kind, min(ref.row for ref in fields.values()))
            if key in seen:
                raise ValueError("overlapping_mapping")
            seen.add(key)
            records.append(Record(kind=kind, values=values, evidence=evidence))
            covered.update(ref.row for ref in evidence.values())
        except ValueError as exc:
            issues.append(
                Issue(
                    code=str(exc),
                    severity="blocker",
                    message=f"Source {table.source_id}, {table.name}: invalid proposed mapping.",
                )
            )

    for section in proposal.sections:
        columns = {assignment.field: assignment.column for assignment in section.columns}
        if (
            not (
                1 <= section.header_row < section.first_row <= section.last_row <= last
                and section.first_row >= first
            )
            or not columns
            or len(columns) != len(section.columns)
            or any(
                column < 1 or column > max((len(row) for row in table.rows), default=0)
                for column in columns.values()
            )
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
        for row in range(section.first_row, section.last_row + 1):
            if not any(table.rows[row - 1]):
                continue
            add(
                section.kind,
                {field: CellRef(row=row, column=column) for field, column in columns.items()},
            )
    for record in proposal.records:
        fields = {assignment.field: assignment.ref for assignment in record.fields}
        if len(fields) != len(record.fields):
            issues.append(
                Issue(
                    code="invalid_fields",
                    severity="blocker",
                    message="A record maps the same semantic field more than once.",
                )
            )
            continue
        add(record.kind, fields)
    return records, issues, covered


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
    sample_chars = max(1, MANIFEST_CHARS // max(len(tables), 1) // 160)
    manifest = [
        TableManifest(
            source_id=t.source_id,
            name=t.name,
            rows=len(t.rows),
            columns=max((len(r) for r in t.rows), default=0),
            sample=[
                [v[: min(120, sample_chars)] if v else v for v in row[:40]] for row in t.rows[:4]
            ],
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
            found, problems, rows = materialize(table, output, first + 1, last)
            records.extend(found)
            issues.extend(problems)
            covered.update(rows)
            previous = output.sections or previous
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
    return (
        [r for records, _, _ in results for r in records],
        [i for _, issues, _ in results for i in issues],
        [c for _, _, c in results],
    )
