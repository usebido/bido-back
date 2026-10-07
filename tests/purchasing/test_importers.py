"""Reading spreadsheets: header synonyms, value parsing, row errors. No database."""

import io
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any, cast

import pytest

from bido_back.purchasing.importers import (
    ImportFileError,
    map_columns,
    parse_date,
    parse_rows,
    read_table,
)


def xlsx(rows: Sequence[Sequence[object]]) -> bytes:
    from openpyxl import Workbook  # pyright: ignore[reportMissingTypeStubs]

    workbook = cast(Any, Workbook())
    sheet = workbook.active
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def test_headers_in_italian_and_portuguese_map_to_fields() -> None:
    _, columns, missing, unknown = map_columns(
        "purchase_history",
        [
            "Numero Ordine", "Fornitore", "Codice Articolo", "Quantità", "Prezzo Unitario",
            "Data Ordine", "Valuta", "Data Consegna Prevista", "Data Ricevimento", "Scarti",
            "Note",
        ],
    )  # fmt: skip

    assert missing == []
    assert columns["po_number"] == "Numero Ordine"
    assert columns["unit_price"] == "Prezzo Unitario"
    assert columns["promised_at"] == "Data Consegna Prevista"
    assert columns["received_at"] == "Data Ricevimento"
    assert columns["rejected_qty"] == "Scarti"
    assert unknown == ["Note"]

    _, columns, missing, _ = map_columns("inventory", ["Código", "Estoque", "Pedido em aberto"])
    assert missing == []
    assert columns == {
        "part_number": "Código",
        "on_hand": "Estoque",
        "on_order": "Pedido em aberto",
    }


def test_missing_required_columns_import_nothing() -> None:
    table = parse_rows("bom", [["Product", "Part number"], ["Motor", "A-1"]])

    assert table.missing_required == ["qty_per_unit"]
    assert table.rows == []


def test_csv_with_semicolons_and_decimal_commas() -> None:
    data = "Código;Descrição;Unidade\nA-1;Rolamento 6205;pç\nB-2;Fio de cobre 1,2 mm;kg\n"
    rows = read_table(data.encode(), "itens.csv")
    table = parse_rows("items", rows)

    assert table.errors == []
    assert [values["part_number"] for _, values in table.rows] == ["A-1", "B-2"]

    plan = parse_rows(
        "production_plan",
        read_table(b"Produto;Quantidade;Data\nMotor EV-120;1.200,5;15/11/2026\n", "plan.csv"),
        decimal=",",
    )
    assert plan.rows[0][1]["quantity"] == Decimal("1200.5")
    assert plan.rows[0][1]["need_by"] == date(2026, 11, 15)


def test_bad_rows_are_reported_and_skipped() -> None:
    table = parse_rows(
        "purchase_history",
        [
            ["PO", "Supplier", "Part number", "Quantity", "Unit price", "Order date", "Currency"],
            ["PO-1", "Rame Italia", "CU-1", 100, 9.5, "2026-03-01", "EUR"],
            ["PO-2", "Rame Italia", "CU-1", -5, 9.5, "2026-03-02", "EUR"],
            ["PO-3", "Rame Italia", "CU-1", 10, "abc", "not a date", "EURO"],
            ["PO-4", "", "CU-1", 10, 9, "2026-03-04", "EUR"],
        ],
    )

    assert table.total_rows == 4
    assert [row for row, _ in table.rows] == [2]
    assert {(e.row, e.field) for e in table.errors} == {
        (3, "quantity"),
        (4, "unit_price"),
        (4, "ordered_at"),
        (4, "currency"),
        (5, "supplier"),
    }


def test_xlsx_cells_keep_their_types() -> None:
    rows = read_table(
        xlsx(
            [
                ["Part number", "On hand", "As of"],
                [1020.0, 12, datetime(2026, 10, 1)],
            ]
        ),
        "stock.xlsx",
    )
    table = parse_rows("inventory", rows)

    assert table.errors == []
    values = table.rows[0][1]
    assert values["part_number"] == "1020"  # a numeric code, not "1020.0"
    assert values["on_hand"] == Decimal(12)
    assert values["as_of"] == date(2026, 10, 1)


def test_dates() -> None:
    assert parse_date("2026-11-15") == date(2026, 11, 15)
    assert parse_date("15.11.2026") == date(2026, 11, 15)
    assert parse_date(46341) == date(2026, 11, 15)  # an Excel serial date
    assert parse_date("next week") is None


def test_unsupported_files_are_refused() -> None:
    with pytest.raises(ImportFileError):
        read_table(b"%PDF-1.4", "history.pdf")
    with pytest.raises(ImportFileError):
        read_table(b"not a zip", "history.xlsx")
