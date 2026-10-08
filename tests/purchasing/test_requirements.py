"""Needs first: requirements as the demand, net of stock, by material group.

The buyer usually brings the need (an MRP export, or a list typed in the chat).
The net need is `required + safety stock - free stock - on order`, never below
zero; units are never mixed silently; suppliers come from the purchase history
and from the item's material group, ranked with the buyer's own evaluation;
RFQs are grouped by material group.
"""

from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.agents.context import ToolContext
from bido_back.db import tenant_session
from bido_back.db.models import Requirement, Supplier
from bido_back.onboarding import service as onboarding
from bido_back.purchasing import analysis as calc
from bido_back.purchasing import service
from bido_back.purchasing.schemas import RequirementsAnalysisRequest, RfqFromAnalysisRequest
from bido_back.purchasing.workbook import import_workbook, map_workbook, read_workbook
from evals import needs
from tests.approvals.conftest import People
from tests.purchasing.conftest import FakeStorage
from tests.rfqs.test_service import buyer

TODAY = date(2026, 10, 8)
_CONTEXT = ToolContext(tenant_id=UUID(int=1), user_id=UUID(int=2), run_id=UUID(int=3))
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _item(part: str = "100000101", unit: str = "UN", category: str | None = None) -> calc.ItemInfo:
    return calc.ItemInfo(
        id=UUID(int=int(part)),
        part_number=part,
        description="AÇO SAE 1020 CHAPA 3MM",
        mpn=None,
        manufacturer=None,
        category=category,
        unit=unit,
    )


def _need(item: calc.ItemInfo, quantity: int, unit: str | None = "KG") -> calc.Need:
    return calc.Need(item=item, quantity=Decimal(quantity), unit=unit, need_by=date(2026, 10, 31))


def _purchase(item: calc.ItemInfo, supplier: UUID, name: str, days: int) -> calc.Purchase:
    return calc.Purchase(
        item_id=item.id,
        supplier_id=supplier,
        supplier_name=name,
        supplier_type="distributor",
        currency="BRL",
        ordered_at=TODAY - timedelta(days=days),
        quantity=Decimal(1000),
        unit_price=Decimal("78.50"),
    )


def test_net_need_counts_safety_stock_free_stock_and_open_orders() -> None:
    item = _item()
    stock = calc.Stock(
        on_hand=Decimal(8500),
        on_order=Decimal(1500),
        safety_stock=Decimal(6500),
        unit="UN",
        order_unit="KG",
    )
    result = calc.analyze_requirements(
        label="Necessidades",
        needs=[_need(item, 11000)],
        stock={item.id: stock},
        purchases=[],
        today=TODAY,
    )
    line = result.lines[0]
    # 11.000 + 6.500 - 8.500 - 1.500: the consolidated view's 7.500.
    assert (line.required, line.safety_stock, line.to_buy) == (11000, 6500, 7500)
    assert line.item.unit == "KG"
    # Stock counted in UN but priced per KG: read in KG, and said so.
    assert ("unit_assumed", "UN->KG") in [(f.code, f.supplier) for f in line.flags]


def test_units_of_the_same_family_need_no_flag_and_covered_needs_buy_nothing() -> None:
    item = _item("100000106")
    stock = calc.Stock(on_hand=Decimal(9000), safety_stock=Decimal(2500), unit="UN")
    result = calc.analyze_requirements(
        label="Necessidades",
        needs=[_need(item, 4000, "EA")],
        stock={item.id: stock},
        purchases=[],
        today=TODAY,
    )
    line = result.lines[0]
    assert line.to_buy == 0
    assert "unit_assumed" not in [f.code for f in line.flags]
    assert "covered_by_stock" in [f.code for f in line.flags]


def test_units_that_cannot_be_reconciled_leave_the_stock_out() -> None:
    item = _item()
    stock = calc.Stock(on_hand=Decimal(8500), safety_stock=Decimal(6500), unit="UN")
    result = calc.analyze_requirements(
        label="Necessidades",
        needs=[_need(item, 11000, "KG")],
        stock={item.id: stock},
        purchases=[],
        today=TODAY,
    )
    line = result.lines[0]
    # Never 11.000 KG minus 8.500 UN: the requirement as given, and a question.
    assert line.to_buy == 11000 and line.on_hand == 0
    assert "unit_mismatch" in [f.code for f in line.flags]


def test_without_stock_the_requirement_is_bought_as_given() -> None:
    item = _item()
    result = calc.analyze_requirements(
        label="Necessidades",
        needs=[_need(item, 7500)],
        stock={},
        purchases=[],
        today=TODAY,
    )
    assert result.lines[0].to_buy == 7500
    assert "no_supplier" in [f.code for f in result.lines[0].flags]


def test_material_group_candidates_and_the_buyers_evaluation() -> None:
    item = _item()
    steel, mro, other_steel, rival = uuid4(), uuid4(), uuid4(), uuid4()
    profiles = {
        steel: calc.SupplierProfile(steel, "Metalúrgica", "distributor", "Aço / Chapas",
                                    Decimal(95), Decimal(92), Decimal("4.2"), Decimal(7)),
        mro: calc.SupplierProfile(mro, "Distribuidora", "distributor", "MRO / Metais",
                                  Decimal(87), Decimal(89), Decimal("3.9"), Decimal(6)),
        other_steel: calc.SupplierProfile(other_steel, "Usina Norte", "manufacturer",
                                          "aço / chapas", Decimal(90), Decimal(97),
                                          Decimal("4.6"), Decimal(12)),
        rival: calc.SupplierProfile(rival, "Usina Sul", "manufacturer", "Aço / Chapas",
                                    None, None, Decimal("4.1"), None),
    }  # fmt: skip
    purchases = [
        _purchase(item, steel, "Metalúrgica", 90),
        _purchase(item, steel, "Metalúrgica", 60),
        _purchase(item, steel, "Metalúrgica", 30),
        _purchase(item, mro, "Distribuidora", 45),
    ]
    result = calc.analyze_requirements(
        label="Necessidades",
        needs=[_need(item, 1000)],
        stock={},
        purchases=purchases,
        today=TODAY,
        profiles=profiles,
    )
    line = result.lines[0]
    # The group the item's purchases mostly came from, whatever the case.
    assert line.group == "Aço / Chapas"
    # Suppliers of that group that never sold it, best rated first.
    assert [c.name for c in line.candidates] == ["Usina Norte", "Usina Sul"]
    by_name = {s.name: s for s in line.suppliers}
    assert by_name["Metalúrgica"].rating == Decimal("4.2")
    assert by_name["Metalúrgica"].category == "Aço / Chapas"
    # No dates in the history: the buyer's OTIF ranks the specialist first.
    assert line.suppliers[0].name == "Metalúrgica"


def test_unit_families() -> None:
    assert calc.unit_family("UN") == calc.unit_family("ea") == "each"
    assert calc.unit_family("Kg") == "kg"
    assert calc.unit_family(" ") is None
    assert calc.unit_family("rolo") == "rolo"


async def test_rules_read_an_mrp_export_without_the_model() -> None:
    grids = read_workbook(needs.needs_export(TODAY), needs.FILENAME)
    sections, problems = await map_workbook(grids, needs.FILENAME, mapper=None, context=_CONTEXT)
    assert problems == []
    assert {(s.sheet, s.import_type) for s in sections} == needs.EXPECTED_SECTIONS
    requirement = next(s for s in sections if s.import_type == "requirements")
    assert {"part_number", "quantity", "need_by", "unit", "reference", "project"} <= set(
        requirement.index
    )
    stock = next(s for s in sections if s.sheet == "MMBE_Estoque")
    # Free stock, never the total with blocked stock; the buffer and both units.
    header = grids[0].rows[0]
    assert header[stock.index["on_hand"]] == "Estoque disponível"
    assert header[stock.index["safety_stock"]] == "Estoque de segurança"
    assert header[stock.index["order_unit"]] == "Unidade de preço"


async def test_a_supplier_list_without_ratings_is_not_an_evaluation() -> None:
    grids = [g for g in read_workbook(needs.needs_export(TODAY), needs.FILENAME)
             if g.name == "Fornecedores"]  # fmt: skip
    sections, _ = await map_workbook(grids, needs.FILENAME, mapper=None, context=_CONTEXT)
    assert [s.import_type for s in sections] == ["supplier_contacts"]


@pytest.mark.db
async def test_requirements_import_analysis_and_rfqs_per_group(
    app_engine: AsyncEngine, purchasing_rows: People
) -> None:
    who = buyer(purchasing_rows)
    storage = FakeStorage()
    upload = await onboarding.store_upload(
        app_engine,
        storage,
        who,
        filename=needs.FILENAME,
        content_type=XLSX,
        data=needs.needs_export(TODAY),
    )
    report = await import_workbook(app_engine, storage, who, upload.id, mapper=None)
    assert report.status == "imported", report

    view = await service.create_requirements_analysis(
        app_engine, who, RequirementsAnalysisRequest(), today=TODAY
    )
    golden = needs.golden()
    assert view.source == "requirements"
    assert {line.part_number: line.to_buy for line in view.lines} == {
        code: g["to_buy"] for code, g in golden.items()
    }
    async with tenant_session(app_engine, who.tenant_id) as session:
        evaluated = await session.scalar(
            select(Supplier).where(Supplier.name == needs.GROUPS[0].supplier)
        )
    assert evaluated is not None
    assert (evaluated.category, evaluated.otif_pct, evaluated.lead_time_days) == (
        "Chapas de Aço",
        Decimal(94),
        Decimal(8),
    )

    created = await service.create_rfqs_from_analysis(
        app_engine, who, view.id, RfqFromAnalysisRequest()
    )
    to_buy_groups = {g["group"] for g in golden.values() if g["to_buy"] > 0}
    assert len(created) == len(to_buy_groups)
    for rfq in created:
        parts = {line.part_number for line in rfq.rfq.lines}
        groups = {golden[p]["group"] for p in parts if p}
        assert len(groups) == 1
        assert needs.group(groups.pop()).supplier in {s.name for s in rfq.invited}
    # Quoted requirements are not analysed again; the covered one stays open.
    left = await service.list_requirements(app_engine, who)
    assert [r.part_number for r in left] == [c for c, g in golden.items() if g["to_buy"] == 0]


@pytest.mark.db
async def test_requirements_typed_in_the_chat_are_upserted(
    app_engine: AsyncEngine, purchasing_rows: People
) -> None:
    who = buyer(purchasing_rows)
    line = service.NewRequirement(
        part_number="NEW-77", quantity=Decimal(120), need_by=date(2026, 11, 3), unit="m"
    )
    first = await service.add_requirements(app_engine, who, [line])
    again = await service.add_requirements(
        app_engine,
        who,
        [
            service.NewRequirement(
                part_number="NEW-77", quantity=Decimal(150), need_by=date(2026, 11, 3), unit="m"
            )
        ],
    )
    assert first[0].id == again[0].id and again[0].quantity == 150
    assert again[0].source == "chat" and again[0].description == "NEW-77"
    async with tenant_session(app_engine, purchasing_rows.other_tenant) as session:
        assert list(await session.scalars(select(Requirement))) == []
    with pytest.raises(service.InvalidRequest):
        await service.add_requirements(
            app_engine,
            who,
            [service.NewRequirement(part_number="X", quantity=Decimal(0), need_by=TODAY)],
        )
