"""Reading spreadsheets: header synonyms, value parsing, row errors. No database."""

import io
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any, cast

import pytest

from bido_back.purchasing.importers import (
    ImportFileError,
    decimal_evidence,
    map_columns,
    parse_date,
    parse_number,
    parse_rows,
    read_table,
    supplier_type,
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


def test_sap_headers_map_to_fields() -> None:
    # ME2M: "Item" is the line next to "Material"; the supplier's name wins over its code.
    _, columns, missing, unknown = map_columns(
        "purchase_history",
        [
            "Doc.compras", "Item", "Fornecedor", "Nome do fornecedor", "Material", "Texto breve",
            "Qtd.do pedido", "UMP", "Preço líquido", "Moeda", "Por", "Data doc.", "Data remessa",
            "Data EM", "Qtd.fornecida", "Qtd.devolvida", "Centro",
        ],
    )  # fmt: skip
    assert missing == []
    assert columns == {
        "po_number": "Doc.compras",
        "supplier": "Nome do fornecedor",
        "supplier_code": "Fornecedor",
        "part_number": "Material",
        "quantity": "Qtd.do pedido",
        "unit_price": "Preço líquido",
        "price_unit": "Por",
        "ordered_at": "Data doc.",
        "currency": "Moeda",
        "promised_at": "Data remessa",
        "received_at": "Data EM",
        "received_qty": "Qtd.fornecida",
        "rejected_qty": "Qtd.devolvida",
        "line_no": "Item",
        "description": "Texto breve",
    }
    assert unknown == ["UMP", "Centro"]

    # Without a "Material" column, "Item" is the part number.
    _, columns, _, _ = map_columns("inventory", ["Item", "Estoque"])
    assert columns["part_number"] == "Item"

    # XK03: a bare "Nome" next to a contact column is the supplier's name.
    _, columns, missing, _ = map_columns(
        "supplier_contacts",
        ["Fornecedor", "Nome", "País", "Pessoa de contato", "E-mail", "Classificação"],
    )
    assert missing == []
    assert (columns["supplier"], columns["supplier_code"]) == ("Nome", "Fornecedor")
    assert (columns["contact_name"], columns["type"]) == ("Pessoa de contato", "Classificação")
    # Without a contact column, a "Nome" is not taken for the supplier.
    _, columns, _, _ = map_columns("supplier_contacts", ["Fornecedor", "Nome", "E-mail"])
    assert columns["supplier"] == "Fornecedor" and "supplier_code" not in columns

    # MB52 and MD04.
    _, columns, missing, _ = map_columns(
        "inventory", ["Material", "Utilização livre", "Em contr.qualidade", "Depósito"]
    )
    assert missing == [] and columns["on_hand"] == "Utilização livre"
    _, columns, missing, _ = map_columns(
        "production_plan", ["Material", "Texto breve", "Qtd.planejada", "Data necessidade"]
    )
    assert missing == [] and columns["product"] == "Material"


def test_text_numbers_resolve_with_their_column() -> None:
    # "888,000" reads as 888 or 888000: the column's other numbers decide.
    table = parse_rows(
        "inventory",
        [["Material", "Utilização livre"], ["A", "888,000"], ["B", "1.850,000"], ["C", "12"]],
    )
    assert table.errors == []
    assert [v["on_hand"] for _, v in table.rows] == [Decimal(888), Decimal(1850), Decimal(12)]

    # No evidence in the column: the caller's separator (the workbook's evidence), else an error.
    rows: list[list[object]] = [["Material", "Utilização livre"], ["A", "888,000"]]
    assert parse_rows("inventory", rows, decimal=",").rows[0][1]["on_hand"] == Decimal(888)
    assert [e.field for e in parse_rows("inventory", rows).errors] == ["on_hand"]

    assert decimal_evidence(["1.234,56", "1,000", 3.5]) == ","
    assert decimal_evidence(["1,234.56", "12"]) == "."
    assert decimal_evidence(["888,000", "1.000"]) is None  # ambiguous only
    assert decimal_evidence(["1.234,56", "1,234.56"]) is None  # conflicting
    assert parse_number("1.250,00-", ",") == Decimal("-1250.00")  # SAP's trailing minus


def test_totals_are_skipped_and_prices_follow_the_price_unit() -> None:
    table = parse_rows(
        "purchase_history",
        [
            ["PO", "Supplier", "Part number", "Quantity", "Unit price", "Por", "Order date"],
            ["PO-1", "Conectores", "NTC-10K", 3000, "195,40", 100, "01.03.2026"],
            ["PO-2", "Conectores", "HV-3P", 10, "185,00", 1, "02.03.26"],
            ["", "", "", "", "", "", ""],
            ["*", "Total", "", 3010, "", "", ""],
        ],
    )
    assert table.errors == []
    assert (table.total_rows, table.skipped_rows) == (2, 1)
    assert [v["unit_price"] for _, v in table.rows] == [Decimal("1.954"), Decimal("185.00")]
    assert table.rows[1][1]["ordered_at"] == date(2026, 3, 2)
    assert all("price_unit" not in v for _, v in table.rows)


def test_supplier_types() -> None:
    assert supplier_type("Fabricante") == "manufacturer"
    assert supplier_type("Distribuidor autorizado") == "authorized_distributor"
    assert supplier_type("Revendedor") == "reseller"
    assert supplier_type("Broker") == "reseller"
    assert supplier_type("Parceiro estratégico") is None
