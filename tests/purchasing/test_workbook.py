"""Importing any workbook: several sheets, titles above the table, mixed content."""

import io
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.agents.catalog.contracts import (
    ColumnAssignment,
    SheetMappingInput,
    SheetMappingOutput,
    SheetSection,
)
from bido_back.agents.context import ToolContext
from bido_back.auth import Principal
from bido_back.db import tenant_session
from bido_back.db.models import BomLine, Contact, Item, PoLine, Supplier, SupplierContact
from bido_back.onboarding import service as onboarding
from bido_back.purchasing.workbook import (
    SheetGrid,
    import_workbook,
    map_workbook,
    parse_section,
    preview,
    profile,
    read_workbook,
)
from tests.approvals.conftest import People
from tests.purchasing.conftest import FakeStorage

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

HEADER: list[object] = [
    "Nº Pedido",
    "Fornecedor",
    "E-mail fornecedor",
    "Código",
    "Qtd",
    "Preço unitário",
    "Data do pedido",
    "Moeda",
]
RAME = ("Rame Italia S.r.l.", "vendite@rame.example.test")
CUSC = ("Cuscinetti Precisione", "ordini@cusc.example.test")
PURCHASES: list[list[object]] = [
    ["Relatório de compras 2026"],
    ["Gerado pelo ERP em 01/10/2026"],
    HEADER,
    ["PO-1", *RAME, "CU-01", 500, 11.4, date(2026, 6, 1), "EUR"],
    ["PO-2", *CUSC, "BR-6205", 400, 1.92, date(2026, 7, 1), "EUR"],
    ["PO-3", *RAME, "CU-01", 300, 11.9, date(2026, 8, 1), "EUR"],
]
STOCK: list[list[object]] = [
    ["Estoque em 30/09"],
    ["Código", "Saldo estoque", "Em pedido"],
    ["CU-01", 120, 0],
    ["BR-6205", 50, 100],
]
ODD: list[list[object]] = [
    ["Ref.", "Qtde. disp.", "Obs"],
    ["CU-01", 120, "ok"],
    ["BR-6205", 50, ""],
]


def workbook(sheets: Mapping[str, Sequence[Sequence[object]]]) -> bytes:
    from openpyxl import Workbook  # pyright: ignore[reportMissingTypeStubs]

    book = cast(Any, Workbook())
    book.remove(book.active)
    for title, rows in sheets.items():
        sheet = book.create_sheet(title)
        for row in rows:
            sheet.append(list(row))
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def ctx() -> ToolContext:
    return ToolContext(tenant_id=uuid4(), user_id=uuid4(), run_id=uuid4())


class StubMapper:
    def __init__(self, output: SheetMappingOutput) -> None:
        self.output = output
        self.calls: list[SheetMappingInput] = []

    async def __call__(self, task: SheetMappingInput, context: ToolContext) -> SheetMappingOutput:
        self.calls.append(task)
        return self.output


async def test_rules_find_tables_below_titles_and_two_sections_in_one_sheet() -> None:
    grids = read_workbook(workbook({"Compras": PURCHASES, "Estoque": STOCK}), "erp.xlsx")
    sections, problems = await map_workbook(grids, "erp.xlsx", mapper=None, context=ctx())

    assert problems == []
    found = {(s.sheet, s.import_type, s.header_row) for s in sections}
    assert found == {
        ("Compras", "purchase_history", 2),
        ("Compras", "supplier_contacts", 2),
        ("Estoque", "inventory", 1),
    }
    by_type = {s.import_type: s for s in sections}
    grid = {g.name: g for g in grids}
    history = parse_section(by_type["purchase_history"], grid["Compras"], None)
    contacts = parse_section(by_type["supplier_contacts"], grid["Compras"], None)
    assert (history.total_rows, len(history.rows), history.errors) == (3, 3, [])
    assert {r["email"] for _, r in contacts.rows} == {
        "vendite@rame.example.test",
        "ordini@cusc.example.test",
    }


async def test_the_mapper_reads_what_the_rules_cannot_and_is_checked() -> None:
    grids = read_workbook(workbook({"Disp": ODD}), "stock.xlsx")
    mapper = StubMapper(
        SheetMappingOutput(
            sections=[
                SheetSection(
                    sheet="Disp",
                    import_type="inventory",
                    header_row=1,
                    first_data_row=2,
                    last_data_row=None,
                    columns=[
                        ColumnAssignment(field="part_number", column=0),
                        ColumnAssignment(field="on_hand", column=1),
                    ],
                    confidence=0.85,
                    question=None,
                ),
                # Not a field of this type: dropped, never imported.
                SheetSection(
                    sheet="Disp",
                    import_type="bom",
                    header_row=1,
                    first_data_row=2,
                    last_data_row=None,
                    columns=[ColumnAssignment(field="password", column=2)],
                    confidence=0.9,
                    question=None,
                ),
            ]
        )
    )
    sections, problems = await map_workbook(grids, "stock.xlsx", mapper=mapper, context=ctx())

    [section] = sections
    assert (section.import_type, section.source, section.index) == (
        "inventory",
        "model",
        {"part_number": 0, "on_hand": 1},
    )
    assert problems == ["Disp: invalid column mapping for bom"]
    # The model saw a preview with column indexes, not the whole file.
    assert "row 1: [0] Ref. | [1] Qtde. disp. | [2] Obs" in mapper.calls[0].sheets[0].preview


def test_the_preview_is_short() -> None:
    grid = SheetGrid(name="big", rows=[[f"c{i}-{j}" * 30 for j in range(50)] for i in range(100)])
    text = preview(grid)
    # 50 columns: every column, fewer rows (750 cells), each cell cut short.
    assert text.count("\n") == 14
    assert "[49]" in text and "…" in text


@pytest.mark.db
async def test_import_asks_when_unsure_then_imports_after_confirmation(
    app_engine: AsyncEngine, purchasing_rows: People
) -> None:
    who = Principal(tenant_id=purchasing_rows.tenant, user_id=purchasing_rows.owner, role="owner")
    storage = FakeStorage()
    upload = await onboarding.store_upload(
        app_engine,
        storage,
        who,
        filename="erp.xlsx",
        content_type=XLSX,
        data=workbook({"Compras": PURCHASES, "Disp": ODD}),
    )
    mapper = StubMapper(
        SheetMappingOutput(
            sections=[
                SheetSection(
                    sheet="Disp",
                    import_type="inventory",
                    header_row=1,
                    first_data_row=2,
                    last_data_row=None,
                    columns=[
                        ColumnAssignment(field="part_number", column=0),
                        ColumnAssignment(field="on_hand", column=1),
                    ],
                    confidence=0.5,
                    question="Na aba 'Disp', 'Qtde. disp.' é o estoque em mãos?",
                )
            ]
        )
    )

    first = await import_workbook(app_engine, storage, who, upload.id, mapper=mapper)

    assert first.status == "needs_confirmation"
    assert first.questions == ["Na aba 'Disp', 'Qtde. disp.' é o estoque em mãos?"]
    async with tenant_session(app_engine, who.tenant_id) as session:
        assert await session.scalar(select(func.count()).select_from(PoLine)) == 0

    done = await import_workbook(app_engine, storage, who, upload.id, mapper=mapper, confirmed=True)

    assert done.status == "imported" and done.questions == []
    reports = {(r.sheet, r.type): r for r in done.sections}
    assert reports[("Compras", "purchase_history")].rows_ok == 3
    assert reports[("Disp", "inventory")].rows_ok == 2
    assert reports[("Compras", "supplier_contacts")].created.get("contacts") == 2
    async with tenant_session(app_engine, who.tenant_id) as session:
        emails = set(
            await session.scalars(
                select(Contact.email)
                .join(SupplierContact, SupplierContact.contact_id == Contact.id)
                .join(Supplier, Supplier.id == SupplierContact.supplier_id)
                .where(Supplier.name == "Rame Italia S.r.l.")
            )
        )
    assert emails == {"vendite@rame.example.test"}

    # Importing again adds no duplicate contacts.
    again = await import_workbook(
        app_engine, storage, who, upload.id, mapper=mapper, confirmed=True
    )
    assert {(r.sheet, r.type): r for r in again.sections}[
        ("Compras", "supplier_contacts")
    ].created.get("contacts") is None


CS12: list[list[object]] = [
    ["Explosão de lista técnica multinível"],
    ["Material: MT-120-ASM   Centro: 1100   Qtd.base: 2 UN"],
    [],
    ["Nível", "Componente", "Texto do objeto", "Quantidade", "UM"],
    ["0", "MT-120-ASM", "MOTOR EV-120", "2,000", "UN"],
    [".1", "RT-120-ASM", "ROTOR", "2,000", "UN"],
    ["..2", "PK-MAG-08", "PACOTE IMAS", "8,000", "UN"],
    ["...3", "IM-N48H-40", "IMA NDFEB", "8,000", "UN"],
    ["..2", "EX-120-42", "EIXO", "1,000", "UN"],
    [".1", "RL-6206", "ROLAMENTO", "2,000", "UN"],
    [".1", "ST-120-ASM", "ESTATOR", "x", "UN"],
    ["..2", "FI-CU-180", "FIO COBRE", "6,400", "KG"],
]


async def test_a_multi_level_bom_is_exploded_to_the_parts_bought() -> None:
    grids = read_workbook(workbook({"CS12": CS12}), "cs12.xlsx")
    sections, problems = await map_workbook(grids, "cs12.xlsx", mapper=None, context=ctx())

    assert problems == []
    [section] = sections
    assert section.import_type == "bom" and section.header_row == 3
    table = parse_section(section, grids[0], ",")
    # Per ONE motor (base quantity 2): magnets 2 rotors x 8 packs x 8 / 2 = 64.
    assert {v["part_number"]: v["qty_per_unit"] for _, v in table.rows} == {
        "IM-N48H-40": Decimal(64),
        "EX-120-42": Decimal(1),
        "RL-6206": Decimal(1),
    }
    assert {v["product"] for _, v in table.rows} == {"MT-120-ASM"}
    # The motor row and the two assemblies are skipped; the stator's bad quantity
    # fails its row and the wire under it.
    assert table.skipped_rows == 3
    assert sorted((e.row, e.field) for e in table.errors) == [(11, "qty_per_unit"), (12, None)]
    assert table.total_rows - len(table.rows) == 2


async def test_the_product_of_a_bom_can_come_from_its_title() -> None:
    rows: list[list[object]] = [
        ["Lista técnica — Material: MT-120-ASM"],
        ["Componente", "Quantidade"],
        ["RL-6206", 2],
    ]
    grids = read_workbook(workbook({"BOM": rows}), "bom.xlsx")
    sections, _ = await map_workbook(grids, "bom.xlsx", mapper=None, context=ctx())

    [section] = sections
    table = parse_section(section, grids[0], None)
    assert [v["product"] for _, v in table.rows] == ["MT-120-ASM"]


ME2M: list[list[object]] = [
    ["Documentos de compras por material"],
    [],
    ["Doc.compras", "Item", "Fornecedor", "Nome do fornecedor", "Material", "Qtd.do pedido",
     "Preço líquido", "Moeda", "Por", "Data doc."],
    ["4500000001", "00010", "300902", "Global Parts Brokers Ltd", "RL-6206", "400,000", "8,16",
     "EUR", "1", "01.06.2026"],
    ["4500000002", "00010", "100377", "Rolamentos Precisão", "RL-6206", "150,000", "57,99",
     "BRL", "1", "01.07.2026"],
]  # fmt: skip
XK03: list[list[object]] = [
    ["Mestre de fornecedores"],
    [],
    ["Fornecedor", "Pessoa de contato", "E-mail", "Classificação"],
    ["300902", "Tom Hughes", "quotes@gpbrokers.example.test", "Revendedor"],
    ["100377", "Carla Nunes", "cotacao@rolprecisao.example.test", "Distribuidor autorizado"],
]
MB52: list[list[object]] = [
    ["Lista de estoque em depósito"],
    [],
    ["Material", "Utilização livre", "Em contr.qualidade", "Estoque bloqueado"],
    ["RL-6206", "1.200,000", "60,000", "0,000"],
    ["*", "Total", "", ""],
]


@pytest.mark.db
async def test_an_erp_export_imports_with_codes_types_and_notes(
    app_engine: AsyncEngine, purchasing_rows: People
) -> None:
    who = Principal(tenant_id=purchasing_rows.tenant, user_id=purchasing_rows.owner, role="owner")
    storage = FakeStorage()
    upload = await onboarding.store_upload(
        app_engine,
        storage,
        who,
        filename="sap.xlsx",
        content_type=XLSX,
        data=workbook({"ME2M": ME2M, "XK03": XK03, "MB52": MB52, "CS12": CS12[:10]}),
    )

    report = await import_workbook(app_engine, storage, who, upload.id, mapper=None)

    assert report.status == "imported", report.questions
    sections = {r.sheet: r for r in report.sections}
    assert {s: (r.type, r.rows_ok, r.rows_failed) for s, r in sections.items()} == {
        "ME2M": ("purchase_history", 2, 0),
        "XK03": ("supplier_contacts", 2, 0),
        "MB52": ("inventory", 1, 0),
        "CS12": ("bom", 3, 0),
    }
    assert sections["MB52"].rows_skipped == 1
    assert any("Em contr.qualidade, Estoque bloqueado" in n for n in sections["MB52"].notes)
    assert any("Multi-level BOM" in n for n in sections["CS12"].notes)
    assert any("by code" in n for n in sections["XK03"].notes)
    async with tenant_session(app_engine, who.tenant_id) as session:
        suppliers = {
            name: kind
            for name, kind in (await session.execute(select(Supplier.name, Supplier.type))).all()
        }
        bom = {
            number: qty
            for number, qty in (
                await session.execute(
                    select(Item.part_number, BomLine.qty_per_unit)
                    .join(Item, Item.id == BomLine.item_id)
                    .where(BomLine.product == "MT-120-ASM")
                )
            ).all()
        }
    # The supplier master's codes became the names the purchase orders give them.
    assert suppliers == {
        "Global Parts Brokers Ltd": "reseller",
        "Rolamentos Precisão": "authorized_distributor",
    }
    assert bom == {"IM-N48H-40": Decimal(64), "EX-120-42": Decimal(1), "RL-6206": Decimal(1)}


async def test_a_sheet_the_rules_read_only_in_part_also_goes_to_the_mapper() -> None:
    # "Item Code" + "Item Description" read as a parts list; the rest is unknown to the rules.
    rows: list[list[object]] = [
        ["PO #", "Date of Order", "Vendor Name", "Item Code", "Item Description", "Qty Ordered",
         "Unit Cost (EUR)"],
        ["PO-1", date(2026, 6, 1), "Rame Italia", "CU-01", "Copper wire", 500, 11.4],
    ]  # fmt: skip
    grids = read_workbook(workbook({"ERP": rows}), "erp.xlsx")

    alone, problems = await map_workbook(grids, "erp.xlsx", mapper=None, context=ctx())
    assert {s.import_type for s in alone} == {"items"} and problems == []

    mapper = StubMapper(
        SheetMappingOutput(
            sections=[
                SheetSection(
                    sheet="ERP",
                    import_type="purchase_history",
                    header_row=1,
                    first_data_row=2,
                    last_data_row=None,
                    columns=[
                        ColumnAssignment(field=f, column=c)
                        for f, c in (
                            ("po_number", 0),
                            ("ordered_at", 1),
                            ("supplier", 2),
                            ("part_number", 3),
                            ("quantity", 5),
                            ("unit_price", 6),
                        )
                    ],  # fmt: skip
                    confidence=0.9,
                    question=None,
                )
            ]
        )
    )
    sections, _ = await map_workbook(grids, "erp.xlsx", mapper=mapper, context=ctx())
    assert [(s.import_type, s.source) for s in sections] == [("purchase_history", "model")]


class SequenceMapper:
    """Answers each call with the next output (the last one repeats)."""

    def __init__(self, *outputs: SheetMappingOutput) -> None:
        self.outputs = list(outputs)
        self.calls: list[SheetMappingInput] = []

    async def __call__(self, task: SheetMappingInput, context: ToolContext) -> SheetMappingOutput:
        self.calls.append(task)
        return self.outputs[min(len(self.calls), len(self.outputs)) - 1]


# Headers the rules do not read, so the sheet goes to the mapper.
ORDINI: list[list[object]] = [
    ["Rif. ordine", "Ditta", "Art.", "Descr.", "UdM", "Pz", "Costo", "Emesso il"],
    ["OA-1", "Rame Italia", "CU-01", "Filo di rame", "KG", 500, 11.4, date(2026, 6, 1)],
    ["OA-2", "Rame Italia", "CU-02", "Barra di rame", "M", 80, 9.9, date(2026, 6, 3)],
    ["OA-3", "Cuscinetti P.", "BR-6205", "Cuscinetto", "EA", 400, 1.92, date(2026, 7, 1)],
]


def ordini(*mapped: tuple[str, int]) -> SheetMappingOutput:
    return SheetMappingOutput(
        sections=[
            SheetSection(
                sheet="Ordini",
                import_type="purchase_history",
                header_row=1,
                first_data_row=2,
                last_data_row=None,
                columns=[ColumnAssignment(field=f, column=c) for f, c in mapped],
                confidence=0.9,
                question=None,
            )
        ]
    )


BASE: tuple[tuple[str, int], ...] = (
    ("po_number", 0), ("supplier", 1), ("part_number", 2), ("quantity", 5), ("ordered_at", 7),
)  # fmt: skip


def section_logs(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [
        r.__dict__ for r in caplog.records if r.getMessage() == "workbook section"
    ]  # fmt: skip


async def stored(app_engine: AsyncEngine, who: Principal, rows: list[list[object]]) -> Any:
    storage = FakeStorage()
    upload = await onboarding.store_upload(
        app_engine,
        storage,
        who,
        filename="ordini.xlsx",
        content_type=XLSX,
        data=workbook({"Ordini": rows}),
    )
    return storage, upload.id


async def test_a_failing_mapping_is_retried_once_with_what_was_wrong(
    app_engine: AsyncEngine, purchasing_rows: People, caplog: pytest.LogCaptureFixture
) -> None:
    who = Principal(tenant_id=purchasing_rows.tenant, user_id=purchasing_rows.owner, role="owner")
    storage, upload_id = await stored(app_engine, who, ORDINI)
    # First the price is read from the description (every row fails), then right.
    mapper = SequenceMapper(ordini(*BASE, ("unit_price", 3)), ordini(*BASE, ("unit_price", 6)))
    caplog.set_level("INFO", logger="bido_back.purchasing.workbook")

    report = await import_workbook(app_engine, storage, who, upload_id, mapper=mapper)

    assert report.status == "imported", report.questions
    assert [(r.rows_ok, r.rows_failed) for r in report.sections] == [(3, 0)]
    assert len(mapper.calls) == 2
    assert mapper.calls[0].feedback == []
    feedback = " ".join(mapper.calls[1].feedback)
    assert "'unit_price'" in feedback and "[3] 'Descr.'" in feedback and "3 of 3" in feedback
    [line] = section_logs(caplog)
    assert (line["degraded"], line["degraded_reason"], line["retried"]) == (
        True,
        "retry_mapping",
        True,
    )
    # Codes and field names only: no header or cell of the buyer's sheet.
    logged = repr({k: line[k] for k in ("degraded_reasons", "dropped_fields", "error_reasons")})
    assert "Descr" not in logged and "Rame" not in logged


async def test_a_retry_that_does_not_help_keeps_the_first_reading_and_asks_plainly(
    app_engine: AsyncEngine, purchasing_rows: People, caplog: pytest.LogCaptureFixture
) -> None:
    who = Principal(tenant_id=purchasing_rows.tenant, user_id=purchasing_rows.owner, role="owner")
    storage, upload_id = await stored(app_engine, who, ORDINI)
    mapper = SequenceMapper(ordini(*BASE, ("unit_price", 3)), SheetMappingOutput(sections=[]))
    caplog.set_level("INFO", logger="bido_back.purchasing.workbook")

    report = await import_workbook(app_engine, storage, who, upload_id, mapper=mapper)

    assert report.status == "needs_confirmation"
    assert len(mapper.calls) == 2  # one retry, never more
    [question] = report.questions
    assert "none of its 3 rows could be read" in question
    assert "unit_price (column 'Descr.')" in question
    assert "only the valid rows" not in question
    [line] = section_logs(caplog)
    assert line["degraded_reasons"] == ["retry_mapping", "high_failure_rate"]


async def test_a_unit_of_measure_read_as_a_number_is_dropped_without_a_retry(
    app_engine: AsyncEngine, purchasing_rows: People, caplog: pytest.LogCaptureFixture
) -> None:
    who = Principal(tenant_id=purchasing_rows.tenant, user_id=purchasing_rows.owner, role="owner")
    storage, upload_id = await stored(app_engine, who, ORDINI)
    mapper = SequenceMapper(
        ordini(*BASE, ("unit_price", 6), ("price_quantity_basis", 4)),
    )
    caplog.set_level("INFO", logger="bido_back.purchasing.workbook")

    report = await import_workbook(app_engine, storage, who, upload_id, mapper=mapper)

    assert report.status == "imported"
    [section] = report.sections
    assert (section.rows_ok, section.rows_failed) == (3, 0)
    assert "price_quantity_basis" not in section.columns
    assert any("'UdM' does not hold price quantity basis values" in n for n in section.notes)
    assert len(mapper.calls) == 1
    [line] = section_logs(caplog)
    assert (line["degraded"], line["degraded_reason"]) == (True, "optional_field_dropped")
    assert line["dropped_fields"] == ["price_quantity_basis"]
    async with tenant_session(app_engine, who.tenant_id) as session:
        prices = set(await session.scalars(select(PoLine.unit_price)))
    assert prices == {Decimal("11.4"), Decimal("9.9"), Decimal("1.92")}


async def test_the_mapper_is_told_field_types_and_what_confusable_fields_mean() -> None:
    grids = read_workbook(workbook({"Ordini": ORDINI}), "ordini.xlsx")
    mapper = SequenceMapper(ordini(*BASE, ("unit_price", 6)))
    await map_workbook(grids, "ordini.xlsx", mapper=mapper, context=ctx())
    [history] = [c for c in mapper.calls[0].catalog if c.import_type == "purchase_history"]
    assert history.types["price_quantity_basis"] == "number"
    assert "never the unit of measure" in history.hints["price_quantity_basis"]


async def test_the_mapper_sees_what_each_column_holds() -> None:
    grids = read_workbook(workbook({"Ordini": ORDINI}), "ordini.xlsx")
    mapper = SequenceMapper(ordini(*BASE, ("unit_price", 6)))
    await map_workbook(grids, "ordini.xlsx", mapper=mapper, context=ctx())
    lines = mapper.calls[0].sheets[0].profile.splitlines()
    assert lines[4] == "[4] UdM: unit-like, 3 distinct (KG, M, EA), 0% empty"
    assert lines[6] == "[6] Costo: number, 3 distinct, 1.92..11.4, 0% empty"
    assert lines[7].startswith("[7] Emesso il: date, 3 distinct, 2026-06-01..2026-07-01")


# A wide ERP export: the PO status sits past column 30.
WIDE_TITLES: list[object] = [
    "PO_Ref", "Vendor_Label", "Mat_Ref", "Ord_Q", "Net_Price_LC", "Doc_Date",
    *[f"Extra_{i}" for i in range(6, 40)], "Line_State", *[f"Tail_{i}" for i in range(41, 50)],
]  # fmt: skip


def wide_rows() -> list[list[object]]:
    rows: list[list[object]] = [list(WIDE_TITLES)]
    for n, state in enumerate(("CLOSED", "CANCELLED", "OPEN")):
        row: list[object] = [f"PO-{n}", "Rame Italia", f"CU-0{n}", 100 + n, 2.5,
                             date(2026, 6, 1 + n)]  # fmt: skip
        row += [f"x{i}" for i in range(6, 40)] + [state] + [i for i in range(41, 50)]
        rows.append(row)
    return rows


async def test_a_column_past_the_thirtieth_reaches_the_mapper_and_is_used() -> None:
    grids = read_workbook(workbook({"Wide": wide_rows()}), "wide.xlsx")
    assert grids[0].width == 50
    mapped = (("po_number", 0), ("supplier", 1), ("part_number", 2), ("quantity", 3),
              ("unit_price", 4), ("ordered_at", 5), ("status", 40))  # fmt: skip
    mapper = SequenceMapper(
        SheetMappingOutput(
            sections=[
                SheetSection(
                    sheet="Wide",
                    import_type="purchase_history",
                    header_row=1,
                    first_data_row=2,
                    last_data_row=None,
                    columns=[ColumnAssignment(field=f, column=c) for f, c in mapped],
                    confidence=0.9,
                    question=None,
                )
            ]
        )
    )
    [section], problems = await map_workbook(grids, "wide.xlsx", mapper=mapper, context=ctx())
    sent = mapper.calls[0].sheets[0]
    assert "[40] Line_State: text, 3 distinct (CLOSED, CANCELLED, OPEN)" in sent.profile
    assert "[49] Tail_49" in sent.preview
    assert len(sent.profile.splitlines()) == 50
    assert problems == []
    table = parse_section(section, grids[0], None)
    assert [r[1]["po_number"] for r in table.rows] == ["PO-0", "PO-2"]
    assert table.cancelled_rows == 1


def test_a_wide_sheet_previews_fewer_rows_but_profiles_every_row() -> None:
    rows = wide_rows() * 1
    rows += [list(r) for r in rows[1:] for _ in range(20)]
    grid = SheetGrid(name="Wide", rows=rows)
    assert len([ln for ln in preview(grid).splitlines() if ln]) == 15
    assert "[3] Ord_Q: number, 3 distinct, 100..102, 0% empty" in profile(grid)
