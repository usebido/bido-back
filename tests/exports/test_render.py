"""CSV and .xlsx rendering: typed cells, formula neutralizing, encoding. Pure."""

import io
from datetime import date
from decimal import Decimal
from typing import Any, cast

from bido_back.exports.render import Table, safe_text, to_csv, to_xlsx
from bido_back.search.service import like_pattern

BOM = "\N{BYTE ORDER MARK}"

TABLE = Table(
    header=["RFQ", "Supplier", "Unit price", "Lead time", "Valid until"],
    rows=[
        ["RFQ-1", "Lyon Composants", Decimal("0.042000"), 42, date(2026, 11, 30)],
        ["RFQ-1", '=HYPERLINK("http://evil.example")', None, None, None],
    ],
)


def test_formula_like_text_is_neutralized() -> None:
    assert safe_text("=SUM(A1)") == "'=SUM(A1)"
    assert safe_text("-5") == "'-5"
    assert safe_text("@cmd") == "'@cmd"
    assert safe_text("GRM188R71H104") == "GRM188R71H104"


def test_csv_has_a_bom_plain_decimals_and_iso_dates() -> None:
    text = to_csv(TABLE).decode("utf-8")

    assert text.startswith(BOM + "RFQ,Supplier,Unit price,Lead time,Valid until\r\n")
    assert "RFQ-1,Lyon Composants,0.042000,42,2026-11-30\r\n" in text
    assert "'=HYPERLINK" in text


def test_xlsx_keeps_numbers_and_dates_typed() -> None:
    from openpyxl import load_workbook  # pyright: ignore[reportMissingTypeStubs]

    workbook = cast(
        Any, load_workbook(io.BytesIO(to_xlsx(TABLE, "A sheet name longer than 31 chars")))
    )
    sheet = workbook.active

    assert sheet.title == "A sheet name longer than 31 cha"
    assert [c.value for c in sheet[1]] == list(TABLE.header)
    assert sheet["C2"].value == Decimal("0.042") or sheet["C2"].value == 0.042
    assert sheet["D2"].value == 42
    assert sheet["E2"].value.date() == date(2026, 11, 30)
    assert sheet["B3"].value.startswith("'=")
    assert sheet.freeze_panes == "A2"


def test_like_pattern_matches_wildcards_literally() -> None:
    assert like_pattern("100%_x\\y") == "%100\\%\\_x\\\\y%"
