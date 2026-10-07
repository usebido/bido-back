"""Importing whatever spreadsheet the buyer has: every sheet, any layout.

Buyers send what their ERP or their own Excel produces. There is no template:

1. Read every sheet (`read_workbook`).
2. Find the sections (`map_workbook`). Rules first: in the first rows of each
   sheet, a row whose headers name every required field of an import type is
   a header row (Portuguese, English and Italian synonyms). A sheet the rules
   cannot read goes to the sheet mapper (Luna), which only proposes WHERE the
   table is and WHAT each column means; one sheet may hold two sections (e.g.
   purchase history plus the supplier's email). Its proposal is checked:
   known sheet, known type, fields of that type, columns inside the sheet, no
   column used twice.
3. Validate every value with the importers (`parse_mapped`), as for any import.
4. Ask before writing when unsure: a section with low confidence, a missing
   required field, a question from the mapper, or more than a fifth of its rows
   failing makes the import `needs_confirmation`. Nothing is written until
   the buyer confirms; then the valid rows are imported, in dependency order.

The model never sees more than a preview (first rows, cells cut short) and
never produces a value that is written.
"""

import io
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Literal, Protocol, cast
from uuid import UUID, uuid4

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.agents.catalog.contracts import (
    ImportFieldCatalog,
    SheetMappingInput,
    SheetMappingOutput,
    SheetPreview,
)
from bido_back.agents.config import RunnerConfig
from bido_back.agents.context import ToolContext
from bido_back.auth import Principal
from bido_back.db import tenant_session
from bido_back.db.models import TemplateUpload
from bido_back.purchasing.importers import (
    MAX_ROWS,
    SPECS,
    ImportFileError,
    ImportType,
    ParsedTable,
    map_columns,
    parse_mapped,
    read_table,
)
from bido_back.quotes.parsing import DecimalSeparator
from bido_back.storage import BlobStorage

logger = logging.getLogger(__name__)

Db = AsyncEngine | async_sessionmaker[AsyncSession]

SheetMapper = Callable[[SheetMappingInput, ToolContext], Awaitable[SheetMappingOutput]]

HEADER_SCAN_ROWS: Final = 30
PREVIEW_ROWS: Final = 25
PREVIEW_COLUMNS: Final = 30
PREVIEW_CELL_CHARS: Final = 40
MAX_SHEETS: Final = 20
CONFIDENT: Final = 0.7
MAX_FAILED_SHARE: Final = 0.2
# Write order: items and BOMs before stock and history that reference them.
_ORDER: Final[tuple[ImportType, ...]] = (
    "items",
    "bom",
    "supplier_contacts",
    "purchase_history",
    "inventory",
    "production_plan",
)
_DETAIL_TYPES: Final[tuple[ImportType, ...]] = (
    "bom",
    "purchase_history",
    "inventory",
    "production_plan",
)


@dataclass(frozen=True, slots=True)
class SheetGrid:
    name: str
    rows: list[list[object]]

    @property
    def width(self) -> int:
        return max((len(r) for r in self.rows), default=0)


@dataclass(slots=True)
class Section:
    sheet: str
    import_type: ImportType
    header_row: int  # 0-based index into the sheet's rows
    first_row: int  # 0-based, inclusive
    last_row: int | None  # 0-based, inclusive; None = to the end
    index: dict[str, int]
    confidence: float
    source: Literal["rules", "model"]
    question: str | None = None


class WorkbookFileError(Exception):
    pass


# --- Reading -------------------------------------------------------------------------------


def read_workbook(data: bytes, filename: str) -> list[SheetGrid]:
    lower = filename.lower()
    if lower.endswith(".csv"):
        return [SheetGrid(name=filename, rows=read_table(data, filename))]
    if lower.endswith(".xls"):
        raise WorkbookFileError("old .xls files are not supported; save it as .xlsx or .csv")
    if not lower.endswith(".xlsx"):
        raise WorkbookFileError("spreadsheets must be .xlsx or .csv")
    from openpyxl import load_workbook  # pyright: ignore[reportMissingTypeStubs]

    try:
        workbook = cast(_Workbook, load_workbook(io.BytesIO(data), read_only=True, data_only=True))
    except Exception as exc:  # openpyxl raises several unrelated types on bad input
        raise WorkbookFileError("the file is not a readable .xlsx workbook") from exc
    sheets: list[SheetGrid] = []
    try:
        for sheet in workbook.worksheets[:MAX_SHEETS]:
            rows: list[list[object]] = []
            for raw in sheet.iter_rows(values_only=True):
                rows.append(list(raw))
                if len(rows) > MAX_ROWS + HEADER_SCAN_ROWS:
                    raise WorkbookFileError(f"sheets are limited to {MAX_ROWS} rows")
            sheets.append(SheetGrid(name=sheet.title, rows=rows))
    finally:
        workbook.close()
    return sheets


class _Sheet(Protocol):
    @property
    def title(self) -> str: ...

    def iter_rows(self, *, values_only: bool) -> Iterable[tuple[object, ...]]: ...


class _Workbook(Protocol):
    @property
    def worksheets(self) -> list[_Sheet]: ...

    def close(self) -> None: ...


def _blank(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _cell(value: object) -> str:
    text = " ".join(str(value).split())
    return text[:PREVIEW_CELL_CHARS] + ("…" if len(text) > PREVIEW_CELL_CHARS else "")


def preview(grid: SheetGrid) -> str:
    lines: list[str] = []
    for number, row in enumerate(grid.rows[:PREVIEW_ROWS], start=1):
        cells = [f"[{i}] {_cell(v)}" for i, v in enumerate(row[:PREVIEW_COLUMNS]) if not _blank(v)]
        if cells:
            lines.append(f"row {number}: " + " | ".join(cells))
    return "\n".join(lines)


# --- Finding the sections ----------------------------------------------------------------


def _rule_candidates(grid: SheetGrid) -> list[Section]:
    best: dict[ImportType, tuple[int, int, dict[str, int]]] = {}
    for at, row in enumerate(grid.rows[:HEADER_SCAN_ROWS]):
        if sum(1 for c in row if not _blank(c)) < 2:
            continue
        for import_type in SPECS:
            index, _, missing, _ = map_columns(import_type, row)
            if missing:
                continue
            score = len(index)
            if import_type not in best or score > best[import_type][1]:
                best[import_type] = (at, score, index)
    sections = [
        Section(
            sheet=grid.name,
            import_type=t,
            header_row=at,
            first_row=at + 1,
            last_row=None,
            index=index,
            confidence=0.9,
            source="rules",
        )
        for t, (at, _, index) in best.items()
    ]
    if any(s.import_type in _DETAIL_TYPES for s in sections):
        # Purchase lines and BOMs carry descriptions too: not a separate catalog.
        sections = [s for s in sections if s.import_type != "items"]
    if any(s.import_type == "bom" for s in sections):
        # A BOM's "quantity" also reads as a production plan; the BOM wins.
        sections = [s for s in sections if s.import_type != "production_plan"]
    return sections


def _catalog() -> list[ImportFieldCatalog]:
    return [
        ImportFieldCatalog(
            import_type=t,
            fields=[s.name for s in specs],
            required=[s.name for s in specs if s.required],
        )
        for t, specs in SPECS.items()
    ]


def _from_model(
    output: SheetMappingOutput, grids: Mapping[str, SheetGrid]
) -> tuple[list[Section], list[str]]:
    """Checked sections from the mapper's proposal, and the problems found."""
    sections: list[Section] = []
    problems: list[str] = []
    for proposed in output.sections:
        grid = grids.get(proposed.sheet)
        if grid is None or proposed.import_type == "ignore":
            continue
        import_type = proposed.import_type
        names = {s.name for s in SPECS[import_type]}
        header = proposed.header_row - 1
        first = proposed.first_data_row - 1
        last = proposed.last_data_row - 1 if proposed.last_data_row is not None else None
        if (
            not 0 <= header < len(grid.rows)
            or first <= header
            or (last is not None and last < first)
        ):
            problems.append(f"{proposed.sheet}: rows out of range")
            continue
        index: dict[str, int] = {}
        used: set[int] = set()
        bad = False
        for assignment in proposed.columns:
            if (
                assignment.field not in names
                or assignment.field in index
                or not 0 <= assignment.column < grid.width
                or assignment.column in used
            ):
                bad = True
                break
            index[assignment.field] = assignment.column
            used.add(assignment.column)
        if bad:
            problems.append(f"{proposed.sheet}: invalid column mapping for {import_type}")
            continue
        sections.append(
            Section(
                sheet=grid.name,
                import_type=import_type,
                header_row=header,
                first_row=first,
                last_row=last,
                index=index,
                confidence=proposed.confidence,
                source="model",
                question=proposed.question,
            )
        )
    return sections, problems


async def map_workbook(
    grids: Sequence[SheetGrid],
    filename: str,
    *,
    mapper: SheetMapper | None,
    context: ToolContext,
) -> tuple[list[Section], list[str]]:
    """The sections to import: rules where they suffice, the mapper for the rest."""
    sections: list[Section] = []
    unresolved: list[SheetGrid] = []
    for grid in grids:
        found = _rule_candidates(grid)
        if found:
            sections.extend(found)
        elif any(not _blank(c) for row in grid.rows[:PREVIEW_ROWS] for c in row):
            unresolved.append(grid)
    problems: list[str] = []
    if unresolved:
        if mapper is None:
            problems.extend(f"{g.name}: could not find the table" for g in unresolved)
        else:
            output = await mapper(
                SheetMappingInput(
                    filename=filename,
                    sheets=[
                        SheetPreview(
                            sheet=g.name, rows=len(g.rows), columns=g.width, preview=preview(g)
                        )
                        for g in unresolved
                    ],
                    catalog=_catalog(),
                ),
                context,
            )
            mapped, problems = _from_model(output, {g.name: g for g in unresolved})
            sections.extend(mapped)
    return sections, problems


# --- Validating and importing ---------------------------------------------------------------


class SectionReport(BaseModel):
    sheet: str
    type: ImportType
    source: Literal["rules", "model"]
    header_row: int  # 1-based, as in the spreadsheet
    columns: dict[str, str]
    missing_required: list[str]
    rows_total: int
    rows_ok: int
    rows_failed: int
    errors: list[str]
    confidence: float
    question: str | None
    created: dict[str, int] = {}
    updated: dict[str, int] = {}


class WorkbookImportReport(BaseModel):
    upload_id: UUID
    filename: str
    status: Literal["imported", "needs_confirmation", "nothing_found"]
    sections: list[SectionReport]
    # What to ask the buyer before importing; empty when imported.
    questions: list[str]
    problems: list[str]


@dataclass(slots=True)
class _Parsed:
    section: Section
    table: ParsedTable
    report: SectionReport = field(init=False)

    def __post_init__(self) -> None:
        t = self.table
        failed = t.total_rows - len(t.rows)
        self.report = SectionReport(
            sheet=self.section.sheet,
            type=self.section.import_type,
            source=self.section.source,
            header_row=self.section.header_row + 1,
            columns=t.columns,
            missing_required=t.missing_required,
            rows_total=t.total_rows,
            rows_ok=len(t.rows),
            rows_failed=failed,
            errors=[
                f"row {e.row}: {e.field + ': ' if e.field else ''}{e.message}"
                for e in t.errors[:10]
            ],
            confidence=round(self.section.confidence, 2),
            question=self.section.question,
        )

    def doubts(self) -> list[str]:
        r = self.report
        found: list[str] = []
        if r.question:
            found.append(r.question)
        elif r.confidence < CONFIDENT:
            found.append(f"Sheet '{r.sheet}': is this {r.type.replace('_', ' ')}?")
        if r.missing_required:
            found.append(
                f"Sheet '{r.sheet}' ({r.type}): which columns hold {', '.join(r.missing_required)}?"
            )
        if r.rows_total and r.rows_failed / r.rows_total > MAX_FAILED_SHARE:
            found.append(
                f"Sheet '{r.sheet}': {r.rows_failed} of {r.rows_total} rows have invalid "
                "values; import only the valid rows?"
            )
        return found


def parse_section(
    section: Section, grid: SheetGrid, decimal: DecimalSeparator | None
) -> ParsedTable:
    header = grid.rows[section.header_row]
    end = len(grid.rows) if section.last_row is None else section.last_row + 1
    rows = [
        (number + 1, grid.rows[number])
        for number in range(section.first_row, min(end, len(grid.rows)))
    ]
    return parse_mapped(section.import_type, header, section.index, rows, decimal=decimal)


async def import_workbook(
    db: Db,
    storage: BlobStorage,
    principal: Principal,
    upload_id: UUID,
    *,
    mapper: SheetMapper | None,
    confirmed: bool = False,
    decimal: DecimalSeparator | None = None,
) -> WorkbookImportReport:
    """Read every sheet, find the sections, and import them (or ask first)."""
    from bido_back.purchasing.service import apply_parsed

    async with tenant_session(db, principal.tenant_id) as session:
        upload = await session.scalar(
            select(TemplateUpload).where(
                TemplateUpload.id == upload_id, TemplateUpload.user_id == principal.user_id
            )
        )
        if upload is None:
            raise WorkbookFileError(f"upload {upload_id} not found")
        key, filename = upload.storage_key, upload.filename
    data = await storage.get(key)
    try:
        grids = read_workbook(data, filename)
    except ImportFileError as exc:
        raise WorkbookFileError(str(exc)) from exc
    context = ToolContext(tenant_id=principal.tenant_id, user_id=principal.user_id, run_id=uuid4())
    sections, problems = await map_workbook(grids, filename, mapper=mapper, context=context)
    by_name = {g.name: g for g in grids}
    parsed: list[_Parsed] = []
    for section in sections:
        try:
            table = parse_section(section, by_name[section.sheet], decimal)
        except ImportFileError as exc:
            problems.append(f"{section.sheet}: {exc}")
            continue
        parsed.append(_Parsed(section, table))
    if not parsed:
        return WorkbookImportReport(
            upload_id=upload_id,
            filename=filename,
            status="nothing_found",
            sections=[],
            questions=["I could not find a table to import. Which sheet and columns hold it?"],
            problems=problems,
        )
    questions = [q for p in parsed for q in p.doubts()]
    if questions and not confirmed:
        logger.info(
            "workbook import needs confirmation tenant=%s upload=%s sections=%d",
            principal.tenant_id,
            upload_id,
            len(parsed),
        )
        return WorkbookImportReport(
            upload_id=upload_id,
            filename=filename,
            status="needs_confirmation",
            sections=[p.report for p in parsed],
            questions=questions,
            problems=problems,
        )
    order = {t: i for i, t in enumerate(_ORDER)}
    async with tenant_session(db, principal.tenant_id) as session:
        for item in sorted(parsed, key=lambda p: order[p.section.import_type]):
            created, updated = await apply_parsed(session, principal.tenant_id, item.table)
            item.report.created, item.report.updated = created, updated
    logger.info(
        "workbook imported tenant=%s upload=%s sections=%d rows=%d",
        principal.tenant_id,
        upload_id,
        len(parsed),
        sum(p.report.rows_ok for p in parsed),
    )
    return WorkbookImportReport(
        upload_id=upload_id,
        filename=filename,
        status="imported",
        sections=[p.report for p in parsed],
        questions=[],
        problems=problems,
    )


def default_sheet_mapper(config: RunnerConfig | None = None) -> SheetMapper:
    async def run_mapper(task: SheetMappingInput, context: ToolContext) -> SheetMappingOutput:
        from bido_back.agents.catalog.definitions import SHEET_MAPPER
        from bido_back.agents.runner import Completed, Failed, run_to_completion

        result = await run_to_completion(SHEET_MAPPER, task, context, config=config)
        if isinstance(result, Completed):
            return result.output
        code = result.failure.code if isinstance(result, Failed) else "unexpected_approval"
        logger.error("agent sheet_mapper run %s failed: %s", context.run_id, code)
        return SheetMappingOutput(sections=[])

    return run_mapper
