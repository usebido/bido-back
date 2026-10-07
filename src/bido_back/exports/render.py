"""Render a table as CSV or a single-sheet .xlsx.

Text cells that a spreadsheet would read as a formula (starting with `=`,
`+`, `-`, `@`, tab or carriage return) are prefixed with `'`: part numbers
and descriptions come from suppliers and are not trusted. Numbers and dates
stay typed in the .xlsx; in CSV, decimals are written in plain notation.
"""

import csv
import io
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Final, Literal, cast

ExportFormat = Literal["xlsx", "csv"]
Value = str | int | Decimal | date | None

MEDIA_TYPES: Final[dict[ExportFormat, str]] = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "csv": "text/csv; charset=utf-8",
}
_FORMULA_START: Final = ("=", "+", "-", "@", "\t", "\r")
_BOM: Final = "\N{BYTE ORDER MARK}"
# Excel's own sheet-name limit.
_SHEET_NAME_MAX: Final = 31


@dataclass(frozen=True, slots=True)
class Table:
    header: Sequence[str]
    rows: Sequence[Sequence[Value]]


def safe_text(value: str) -> str:
    return f"'{value}" if value.startswith(_FORMULA_START) else value


def _csv_value(value: Value) -> str:
    match value:
        case None:
            return ""
        case Decimal():
            return format(value, "f")
        case date():
            return value.isoformat()
        case int():
            return str(value)
        case str():
            return safe_text(value)


def to_csv(table: Table) -> bytes:
    """UTF-8 with a byte-order mark, so Excel opens accented names correctly."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow([safe_text(h) for h in table.header])
    writer.writerows([_csv_value(v) for v in row] for row in table.rows)
    return (_BOM + buffer.getvalue()).encode("utf-8")


def to_xlsx(table: Table, sheet_name: str) -> bytes:
    # openpyxl ships no type information; contain it here.
    from openpyxl import Workbook  # pyright: ignore[reportMissingTypeStubs]
    from openpyxl.styles import Font  # pyright: ignore[reportMissingTypeStubs]

    workbook = cast(Any, Workbook())
    sheet = workbook.active
    sheet.title = sheet_name[:_SHEET_NAME_MAX]
    sheet.append([safe_text(h) for h in table.header])
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    sheet.freeze_panes = "A2"
    for row in table.rows:
        sheet.append([safe_text(v) if isinstance(v, str) else v for v in row])
    for index, row in enumerate(sheet.iter_rows(min_row=2), start=0):
        for cell, value in zip(row, table.rows[index], strict=True):
            if isinstance(value, Decimal):
                cell.number_format = "#,##0.00####"
            elif isinstance(value, date):
                cell.number_format = "yyyy-mm-dd"
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def render(table: Table, fmt: ExportFormat, sheet_name: str) -> bytes:
    return to_csv(table) if fmt == "csv" else to_xlsx(table, sheet_name)
