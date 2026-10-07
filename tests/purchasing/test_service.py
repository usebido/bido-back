"""Imports, analyses and the RFQ draft, against Postgres (skipped without it)."""

from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.auth import Principal
from bido_back.db import tenant_session
from bido_back.db.models import AnalysisLine, Invite, Item, PoLine
from bido_back.onboarding import service as onboarding
from bido_back.purchasing import service
from bido_back.purchasing.importers import ImportType
from bido_back.purchasing.schemas import (
    AnalysisRequest,
    ImportRequest,
    RfqFromAnalysisRequest,
)
from bido_back.rfqs import suppliers
from bido_back.rfqs.schemas import ContactIn, CreateSupplierRequest, SupplierView
from tests.approvals.conftest import People
from tests.purchasing.conftest import FakeStorage, xlsx
from tests.rfqs.test_service import buyer

pytestmark = pytest.mark.db

TODAY = date(2026, 10, 7)

BOM = [
    ["Prodotto", "Codice Articolo", "Quantità per unità", "Descrizione", "Unità", "Categoria"],
    ["Motor EV-120", "BRG-6205", 2, "Cuscinetto 6205 2RS", "pz", "bearing"],
    ["Motor EV-120", "CU-WIRE", 1.25, "Filo di rame smaltato 1,2 mm", "kg", "material"],
    ["Motor EV-120", "HSG-AL", 1, "Carcassa in alluminio", "pz", "casting"],
]
STOCK = [
    ["Codice", "Giacenza", "In ordine"],
    ["BRG-6205", 50, 30],
    ["CU-WIRE", 30.5, 0],
    ["HSG-AL", 150, 0],
]
HISTORY = [
    ["Numero Ordine", "Fornitore", "Codice Articolo", "Quantità", "Prezzo Unitario",
     "Data Ordine", "Data Consegna Prevista", "Data Ricevimento", "Scarti"],
    ["PO-1", "Cuscinetti Precisione S.r.l.", "BRG-6205", 100, 2.0,
     date(2026, 1, 10), date(2026, 1, 20), date(2026, 1, 25), 0],
    ["PO-2", "Cuscinetti Precisione S.r.l.", "BRG-6205", 200, 2.2,
     date(2026, 8, 1), date(2026, 8, 15), date(2026, 8, 14), 10],
    ["PO-3", "Fonderia Veneta S.p.A.", "HSG-AL", 10, 30.0,
     date(2025, 12, 1), date(2025, 12, 20), date(2025, 12, 19), 0],
]  # fmt: skip
PLAN = [["Prodotto", "Quantità", "Data"], ["Motor EV-120", 100, date(2026, 11, 15)]]


async def make_supplier(engine: AsyncEngine, who: Principal, name: str) -> SupplierView:
    return await suppliers.create_supplier(
        engine,
        who,
        CreateSupplierRequest(
            name=name,
            language="it",
            contacts=[ContactIn(name="Giulia Rossi", email="vendite@cuscinetti.example.test")],
        ),
    )


async def load(
    engine: AsyncEngine,
    storage: FakeStorage,
    who: Principal,
    kind: ImportType,
    rows: Sequence[Sequence[object]],
) -> service.ImportReport:
    upload = await onboarding.store_upload(
        engine,
        storage,
        who,
        filename=f"{kind}.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        data=xlsx(rows),
    )
    return await service.run_import(
        engine, storage, who, ImportRequest(type=kind, upload_id=upload.id)
    )


async def seed(engine: AsyncEngine, who: Principal) -> None:
    storage = FakeStorage()
    files: list[tuple[ImportType, Sequence[Sequence[object]]]] = [
        ("bom", BOM),
        ("inventory", STOCK),
        ("purchase_history", HISTORY),
        ("production_plan", PLAN),
    ]
    for kind, rows in files:
        report = await load(engine, storage, who, kind, rows)
        assert report.errors == [] and report.missing_required == [], (kind, report)


async def test_imports_write_the_data(app_engine: AsyncEngine, purchasing_rows: People) -> None:
    who = buyer(purchasing_rows)
    supplier = await make_supplier(app_engine, who, "Cuscinetti Precisione S.r.l.")
    storage = FakeStorage()

    bom = await load(app_engine, storage, who, "bom", BOM)
    history = await load(app_engine, storage, who, "purchase_history", HISTORY)

    assert (bom.rows_imported, bom.created) == (3, {"items": 3, "bom_lines": 3})
    # The existing supplier is matched by name; the foundry is created.
    assert history.created == {"suppliers": 1, "purchase_orders": 3, "po_lines": 3}
    async with tenant_session(app_engine, who.tenant_id) as session:
        items = {i.part_number: i for i in await session.scalars(select(Item))}
        lines = list(await session.scalars(select(PoLine).order_by(PoLine.created_at)))
    assert items["CU-WIRE"].unit == "kg" and items["HSG-AL"].category == "casting"
    assert lines[1].rejected_qty == Decimal(10) and lines[1].received_qty == Decimal(200)
    assert supplier.id is not None

    again = await load(app_engine, storage, who, "purchase_history", HISTORY)
    assert again.created == {} and again.updated == {"po_lines": 3}


async def test_dry_run_writes_nothing(app_engine: AsyncEngine, purchasing_rows: People) -> None:
    who = buyer(purchasing_rows)
    storage = FakeStorage()
    upload = await onboarding.store_upload(
        app_engine, storage, who, filename="bom.xlsx", content_type="x", data=xlsx(BOM)
    )

    report = await service.run_import(
        app_engine, storage, who, ImportRequest(type="bom", upload_id=upload.id, dry_run=True)
    )

    assert (report.rows_total, report.rows_imported, report.created) == (3, 0, {})
    async with tenant_session(app_engine, who.tenant_id) as session:
        assert list(await session.scalars(select(Item))) == []


async def test_analysis_from_the_production_plan(
    app_engine: AsyncEngine, purchasing_rows: People
) -> None:
    who = buyer(purchasing_rows)
    await seed(app_engine, who)

    view = await service.create_analysis(
        app_engine, who, AnalysisRequest(product="motor"), today=TODAY
    )

    assert (view.product, view.quantity, view.need_by) == (
        "Motor EV-120",
        Decimal(100),
        date(2026, 11, 15),
    )
    lines = {line.part_number: line for line in view.lines}
    assert lines["BRG-6205"].to_buy == Decimal(120)
    assert lines["BRG-6205"].target_price == Decimal("2.1333")  # min(2.20, 2.1333)
    assert lines["BRG-6205"].suppliers[0].name == "Cuscinetti Precisione S.r.l."
    assert lines["CU-WIRE"].to_buy == Decimal("94.500")
    assert [f.code for f in lines["CU-WIRE"].flags] == ["no_history"]
    assert lines["HSG-AL"].to_buy == Decimal(0)
    assert view.summary.items_to_buy == 2

    again = await service.get_analysis(app_engine, who, view.id)
    assert again.lines == view.lines


async def test_rfq_from_analysis_drafts_lines_and_invites(
    app_engine: AsyncEngine, purchasing_rows: People
) -> None:
    who = buyer(purchasing_rows)
    await make_supplier(app_engine, who, "Cuscinetti Precisione S.r.l.")
    await seed(app_engine, who)
    view = await service.create_analysis(
        app_engine, who, AnalysisRequest(product="Motor EV-120", quantity=Decimal(100)), today=TODAY
    )

    created = await service.create_rfq_from_analysis(
        app_engine, who, view.id, RfqFromAnalysisRequest()
    )

    rfq = created.rfq
    assert rfq.status == "draft"
    assert [
        (line.description, line.quantities, line.unit, line.category) for line in rfq.lines
    ] == [
        ("Cuscinetto 6205 2RS", [Decimal(120)], "pz", "catalog"),
        ("Filo di rame smaltato 1,2 mm", [Decimal("94.5")], "kg", "material"),
    ]
    assert [s.name for s in created.invited] == ["Cuscinetti Precisione S.r.l."]
    assert created.skipped == []
    async with tenant_session(app_engine, who.tenant_id) as session:
        invites = list(await session.scalars(select(Invite).where(Invite.rfq_id == rfq.id)))
        linked = list(
            await session.scalars(select(AnalysisLine).where(AnalysisLine.rfq_line_id.is_not(None)))
        )
    assert [i.status for i in invites] == ["draft"]
    assert {line.target_price for line in linked} == {Decimal("2.1333"), None}

    with pytest.raises(service.Conflict):
        await service.create_rfq_from_analysis(app_engine, who, view.id, RfqFromAnalysisRequest())


async def test_suppliers_without_contacts_are_skipped(
    app_engine: AsyncEngine, purchasing_rows: People
) -> None:
    who = buyer(purchasing_rows)
    await seed(app_engine, who)  # the import creates suppliers without contacts
    view = await service.create_analysis(
        app_engine, who, AnalysisRequest(product="Motor EV-120", quantity=Decimal(100)), today=TODAY
    )

    created = await service.create_rfq_from_analysis(
        app_engine, who, view.id, RfqFromAnalysisRequest()
    )

    assert created.invited == []
    assert [(s.name, s.reason) for s in created.skipped] == [
        ("Cuscinetti Precisione S.r.l.", "no contact email")
    ]


async def test_other_tenants_see_nothing(app_engine: AsyncEngine, purchasing_rows: People) -> None:
    who = buyer(purchasing_rows)
    await seed(app_engine, who)
    view = await service.create_analysis(
        app_engine, who, AnalysisRequest(product="motor"), today=TODAY
    )
    stranger = Principal(
        tenant_id=purchasing_rows.other_tenant, user_id=purchasing_rows.other_approver
    )

    with pytest.raises(service.NotFound):
        await service.get_analysis(app_engine, stranger, view.id)
    with pytest.raises(service.NotFound):  # no BOM in that tenant
        await service.create_analysis(app_engine, stranger, AnalysisRequest(product="motor"))
    assert await service.list_products(app_engine, stranger) == []
    products = await service.list_products(app_engine, who, today=TODAY)
    assert [(p.product, p.bom_items, p.planned_quantity) for p in products] == [
        ("Motor EV-120", 3, Decimal(100))
    ]


async def test_uploads_of_other_users_cannot_be_imported(
    app_engine: AsyncEngine, purchasing_rows: People
) -> None:
    who = buyer(purchasing_rows)
    storage = FakeStorage()
    upload = await onboarding.store_upload(
        app_engine, storage, who, filename="bom.xlsx", content_type="x", data=xlsx(BOM)
    )
    colleague = Principal(tenant_id=purchasing_rows.tenant, user_id=purchasing_rows.member)

    with pytest.raises(service.NotFound):
        await service.run_import(
            app_engine, storage, colleague, ImportRequest(type="bom", upload_id=upload.id)
        )
    assert isinstance(upload.id, UUID)
