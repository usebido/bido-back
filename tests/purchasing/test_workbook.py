"""Importing any workbook: several sheets, titles above the table, mixed content."""

import io
from collections.abc import Mapping, Sequence
from datetime import date
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
from bido_back.db.models import Contact, PoLine, Supplier, SupplierContact
from bido_back.onboarding import service as onboarding
from bido_back.purchasing.workbook import (
    SheetGrid,
    import_workbook,
    map_workbook,
    parse_section,
    preview,
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
    assert text.count("\n") == 24
    assert "[30]" not in text and "…" in text


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
