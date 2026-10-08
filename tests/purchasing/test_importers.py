"""Reading spreadsheets: header synonyms, value parsing, row errors. No database."""

import io
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any, cast

import pytest

from bido_back.purchasing.importers import (
    DroppedColumn,
    ImportFileError,
    decimal_evidence,
    map_columns,
    parse_date,
    parse_mapped,
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
        "price_quantity_basis": "Por",
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


def test_totals_are_skipped_and_prices_follow_the_price_quantity_basis() -> None:
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
    assert all("price_quantity_basis" not in v for _, v in table.rows)


# An ERP export's purchase lines (46 columns in the real one): units of measure,
# a local and a converted price, statuses and cancelled quantities.
ERP_HEADER: list[object] = [
    "Company_Code", "PO_Number", "PO_Line", "PO_Date", "Supplier_ID", "Supplier_Name",
    "Material_ID", "Material_Description", "Plant", "Currency", "FX_to_USD", "Ordered_Qty",
    "UoM", "Unit_Price_Local", "Unit_Price_USD", "Cancelled_Qty", "Requested_Delivery_Date",
    "Confirmed_Delivery_Date", "Last_Receipt_Date", "Received_Qty_Gross", "Rejected_Qty",
    "Net_Received_Qty", "Outstanding_Qty", "PO_Status", "Payment_Status",
]  # fmt: skip


def erp_line(po: str, qty: int, unit: str, cancelled: int, status: str) -> list[object]:
    return [
        "RVN", po, 10, date(2026, 3, 1), "SUP-1", "Arvella LLC", "EL-1", "Harness", "EV01",
        "EUR", 1.1, qty, unit, 14.5, 15.95, cancelled, date(2026, 4, 1), date(2026, 4, 3),
        date(2026, 4, 2), qty - cancelled, 0, qty - cancelled, 0, status, "PAID",
    ]  # fmt: skip


def test_an_erp_export_maps_by_rules_with_the_local_price() -> None:
    _, columns, missing, _ = map_columns("purchase_history", ERP_HEADER)
    assert missing == []
    assert columns["part_number"] == "Material_ID"
    assert columns["unit_price"] == "Unit_Price_Local"  # not the converted USD price
    assert columns["promised_at"] == "Confirmed_Delivery_Date"
    assert columns["received_at"] == "Last_Receipt_Date"
    assert columns["received_qty"] == "Received_Qty_Gross"
    assert (columns["status"], columns["cancelled_qty"]) == ("PO_Status", "Cancelled_Qty")
    assert "price_quantity_basis" not in columns  # "UoM" is a unit of measure

    # Requested dates count as promised only when nothing confirms a date.
    _, columns, _, _ = map_columns(
        "purchase_history",
        ["PO", "Supplier", "Part", "Qty", "Price", "PO date", "Requested_Delivery_Date"],
    )
    assert columns["promised_at"] == "Requested_Delivery_Date"
    _, columns, missing, _ = map_columns(
        "inventory", ["Material", "Estoque disponível", "Quantidade em pedido"]
    )
    assert missing == [] and columns["on_order"] == "Quantidade em pedido"


def test_cancelled_lines_are_not_purchases() -> None:
    table = parse_rows(
        "purchase_history",
        [
            ERP_HEADER,
            erp_line("PO-1", 90, "EA", 0, "CLOSED"),
            erp_line("PO-2", 100, "EA", 100, "CANCELLED"),
            erp_line("PO-3", 100, "EA", 40, "CLOSED"),  # partly cancelled
            erp_line("PO-4", 50, "EA", 0, "Cancelado"),
        ],
    )
    assert table.errors == []
    assert (table.total_rows, table.cancelled_rows, table.skipped_rows) == (2, 2, 2)
    assert [(v["po_number"], v["quantity"]) for _, v in table.rows] == [
        ("PO-1", Decimal(90)),
        ("PO-3", Decimal(60)),
    ]
    assert all("status" not in v and "cancelled_qty" not in v for _, v in table.rows)


def test_an_optional_column_that_does_not_fit_its_field_is_dropped_not_every_row() -> None:
    # The mapper read "UoM" (EA, M) as the price's quantity basis: every row failed.
    header = ["PO", "Supplier", "Part", "Qty", "UoM", "Price", "PO date"]
    rows: list[tuple[int, Sequence[object]]] = [
        (2, ["PO-1", "Arvella", "EL-1", 90, "EA", 14.09, date(2026, 3, 1)]),
        (3, ["PO-2", "Arvella", "EL-2", 120, "M", 2.5, date(2026, 3, 2)]),
        (4, ["PO-3", "Bramvolt", "EL-3", 10, "EA", 7.0, date(2026, 3, 3)]),
    ]
    index = {
        "po_number": 0, "supplier": 1, "part_number": 2, "quantity": 3,
        "price_quantity_basis": 4, "unit_price": 5, "ordered_at": 6,
    }  # fmt: skip

    table = parse_mapped("purchase_history", header, index, rows)

    assert table.errors == [] and len(table.rows) == 3
    assert table.dropped == [DroppedColumn("price_quantity_basis", "UoM")]
    assert "price_quantity_basis" not in table.columns
    assert table.rows[0][1]["unit_price"] == Decimal("14.09")  # not divided by anything

    # Without the check every row fails on that column (what the buyer saw).
    unchecked = parse_mapped("purchase_history", header, index, rows, check_types=False)
    assert unchecked.rows == [] and unchecked.field_failures == {"price_quantity_basis": 3}


def test_a_mostly_valid_column_is_kept_and_a_required_one_is_never_dropped() -> None:
    header = ["PO", "Supplier", "Part", "Qty", "Price", "PO date", "Received qty"]
    rows: list[tuple[int, Sequence[object]]] = [
        (2, ["PO-1", "A", "P-1", 10, 1.0, date(2026, 3, 1), 10]),
        (3, ["PO-2", "A", "P-2", 10, 1.0, date(2026, 3, 1), "n/a"]),
        (4, ["PO-3", "A", "P-3", 10, "EA", date(2026, 3, 1), 10]),
    ]
    index = {
        "po_number": 0, "supplier": 1, "part_number": 2, "quantity": 3, "unit_price": 4,
        "ordered_at": 5, "received_qty": 6,
    }  # fmt: skip

    table = parse_mapped("purchase_history", header, index, rows)

    # Two of three received quantities read: the column stays and its bad row fails.
    assert table.dropped == []
    assert [o for o, _ in table.rows] == [2]
    assert table.field_failures == {"received_qty": 1, "unit_price": 1}

    # A required column that holds no numbers at all still fails its rows.
    rows = [(2, ["PO-1", "A", "P-1", 10, "EA", date(2026, 3, 1), 10])]
    table = parse_mapped("purchase_history", header, index, rows)
    assert table.dropped == [] and table.rows == []
    assert table.field_failures == {"unit_price": 1}


def test_supplier_types() -> None:
    assert supplier_type("Fabricante") == "manufacturer"
    assert supplier_type("Distribuidor autorizado") == "authorized_distributor"
    assert supplier_type("Revendedor") == "reseller"
    assert supplier_type("Broker") == "reseller"
    assert supplier_type("Parceiro estratégico") is None
