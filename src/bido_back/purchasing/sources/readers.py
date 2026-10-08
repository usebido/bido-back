"""File adapters into the same located evidence used by API/connector table adapters."""

import io
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol, cast

from bido_back.documents.extract import Limits, extract_text
from bido_back.purchasing.importers import ImportFileError, read_table
from bido_back.purchasing.sources.contracts import EvidenceTable, Issue

MAX_SOURCE_CELLS = 100_000
MAX_SOURCE_CHARS = 1_000_000
MAX_SOURCE_ROWS = 20_000
MAX_SOURCE_BYTES = 25 * 1024 * 1024


@dataclass(frozen=True)
class SourceDocument:
    source_id: str
    filename: str
    content_type: str
    data: bytes


class _Cell(Protocol):
    @property
    def value(self) -> object: ...

    @property
    def number_format(self) -> str: ...

    @property
    def data_type(self) -> str: ...


class _Sheet(Protocol):
    @property
    def title(self) -> str: ...

    def iter_rows(self) -> Iterable[tuple[_Cell, ...]]: ...


class _Workbook(Protocol):
    @property
    def worksheets(self) -> list[_Sheet]: ...

    def close(self) -> None: ...


def _text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _xlsx(source: SourceDocument) -> tuple[list[EvidenceTable], list[Issue]]:
    from openpyxl import load_workbook  # pyright: ignore[reportMissingTypeStubs]

    values = cast(_Workbook, load_workbook(io.BytesIO(source.data), read_only=True, data_only=True))
    formulas = cast(
        _Workbook, load_workbook(io.BytesIO(source.data), read_only=True, data_only=False)
    )
    tables: list[EvidenceTable] = []
    issues: list[Issue] = []
    cells, chars = 0, 0
    try:
        for sheet, raw_sheet in zip(values.worksheets, formulas.worksheets, strict=True):
            rows: list[list[str | None]] = []
            formula_found = False
            for number, (row, raw_row) in enumerate(
                zip(sheet.iter_rows(), raw_sheet.iter_rows(), strict=True), start=1
            ):
                cells += len(row)
                if number > MAX_SOURCE_ROWS or cells > MAX_SOURCE_CELLS:
                    raise ValueError("source_limit")
                rendered: list[str | None] = []
                for cell, raw in zip(row, raw_row, strict=True):
                    value = cell.value
                    text = _text(value)
                    if isinstance(value, int) and re.fullmatch(r"0+", cell.number_format):
                        text = str(value).zfill(len(cell.number_format))
                    if raw.data_type == "f":
                        formula_found = True
                        if value is None:
                            text = None
                    rendered.append(text)
                    chars += len(text or "")
                    if chars > MAX_SOURCE_CHARS:
                        raise ValueError("source_limit")
                rows.append(rendered)
            tables.append(EvidenceTable(source_id=source.source_id, name=sheet.title, rows=rows))
            if formula_found:
                issues.append(
                    Issue(
                        code="formula_cache",
                        severity="blocker",
                        message=f"{sheet.title}: formula results may be missing or stale; "
                        "formulas were not executed.",
                    )
                )
    finally:
        values.close()
        formulas.close()
    return tables, issues


def read_source(source: SourceDocument) -> tuple[list[EvidenceTable], list[Issue]]:
    """Read every sheet or the text adapter's disclosed coverage; never run source code."""
    if len(source.data) > MAX_SOURCE_BYTES:
        return [], [_unread(source, "source_limit")]
    try:
        suffix = source.filename.lower().rsplit(".", 1)[-1]
        if suffix == "xlsx":
            return _xlsx(source)
        if suffix == "csv":
            rows = [[_text(v) for v in row] for row in read_table(source.data, source.filename)]
            if (
                len(rows) > MAX_SOURCE_ROWS
                or sum(len(row) for row in rows) > MAX_SOURCE_CELLS
                or sum(len(value or "") for row in rows for value in row) > MAX_SOURCE_CHARS
            ):
                return [], [_unread(source, "source_limit")]
            return [EvidenceTable(source_id=source.source_id, name=source.filename, rows=rows)], []
        document = extract_text(
            source.filename,
            source.content_type,
            source.data,
            limits=Limits(
                max_chars=MAX_SOURCE_CHARS,
                max_cells=MAX_SOURCE_CELLS,
                max_rows=MAX_SOURCE_ROWS,
            ),
        )
        if not document.readable:
            return [], [_unread(source, "needs_vision" if document.needs_vision else "unreadable")]
        text_rows: list[list[str | None]] = [[line] for line in document.text.splitlines()]
        if len(text_rows) > MAX_SOURCE_ROWS:
            return [], [_unread(source, "source_limit")]
        issues = [_unread(source, "partial_coverage")] if document.truncated else []
        return [
            EvidenceTable(
                source_id=source.source_id,
                name=source.filename,
                layout="text",
                rows=text_rows,
            )
        ], issues
    except (ImportFileError, ValueError):
        return [], [_unread(source, "unreadable_or_limit")]
    except Exception:
        # Parser exceptions may contain customer content; expose a stable code only.
        return [], [_unread(source, "unreadable")]


def _unread(source: SourceDocument, code: str) -> Issue:
    return Issue(code=code, severity="blocker", message=f"Source {source.source_id}: {code}.")
