"""Demand-window, inventory and saved-evidence contracts against isolated Postgres."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.auth import Principal
from bido_back.db import tenant_session
from bido_back.db.models import InventoryRow, PoLine, RfqLine
from bido_back.purchasing import service
from bido_back.purchasing.schemas import AnalysisRequest, RfqFromAnalysisRequest
from tests.approvals.conftest import People
from tests.purchasing.test_service import TODAY
from tests.rfqs.test_service import buyer

pytestmark = pytest.mark.db


async def _seed_plan(engine: AsyncEngine, who: Principal) -> None:
    bom = await service.import_rows(
        engine,
        who,
        "bom",
        [
            ["Product", "Part number", "Qty per unit"],
            ["Motor", "BEARING", 1],
        ],
    )
    plan = await service.import_rows(
        engine,
        who,
        "production_plan",
        [
            ["Product", "Quantity", "Need by"],
            ["Motor", 40, date(2026, 10, 9)],
            ["Motor", 400, date(2026, 11, 9)],
        ],
    )
    assert not bom.errors and not plan.errors


@pytest.mark.parametrize("explicit_window", [False, True])
async def test_weekly_horizon_excludes_next_month_and_persists_scope(
    app_engine: AsyncEngine,
    purchasing_rows: People,
    explicit_window: bool,
) -> None:
    who = buyer(purchasing_rows)
    await _seed_plan(app_engine, who)
    end = date(2026, 10, 11)
    request = (
        AnalysisRequest(product="Motor", planning_start=TODAY, planning_end=end)
        if explicit_window
        else AnalysisRequest(product="Motor", need_by=end)
    )
    view = await service.create_analysis(app_engine, who, request, today=TODAY)
    assert view.quantity == 40
    assert view.lines[0].to_buy == 40
    assert (view.planning_start, view.planning_end) == (TODAY, end)
    assert view.demand_source == "production_plan"
    restored = await service.get_analysis(app_engine, who, view.id)
    assert restored == view


async def test_plan_uses_dates_for_inbound_and_shortage_not_first_date_for_everything(
    app_engine: AsyncEngine,
    purchasing_rows: People,
) -> None:
    who = buyer(purchasing_rows)
    await _seed_plan(app_engine, who)
    report = await service.import_rows(
        app_engine,
        who,
        "inventory",
        [
            ["Part number", "On hand", "On order", "On order due", "As of"],
            ["BEARING", 10, 400, date(2026, 10, 20), TODAY],
        ],
    )
    assert not report.errors
    view = await service.create_analysis(
        app_engine, who, AnalysisRequest(product="Motor"), today=TODAY
    )
    [line] = view.lines
    assert (line.required, line.to_buy) == (440, 30)
    assert [
        (row.need_by, row.inbound_used, row.to_buy) for row in line.calculation.allocations
    ] == [
        (date(2026, 10, 9), 0, 30),
        (date(2026, 11, 9), 400, 0),
    ]


async def test_location_and_reservations_limit_stock_to_eligible_balance(
    app_engine: AsyncEngine,
    purchasing_rows: People,
) -> None:
    who = buyer(purchasing_rows)
    await _seed_plan(app_engine, who)
    report = await service.import_rows(
        app_engine,
        who,
        "inventory",
        [
            ["Part number", "On hand", "Location", "Reserved qty", "Blocked qty", "As of"],
            ["BEARING", 30, "Plant A", 10, 5, TODAY],
            ["BEARING", 1000, "Plant B", 0, 0, TODAY],
        ],
    )
    assert not report.errors
    view = await service.create_analysis(
        app_engine,
        who,
        AnalysisRequest(
            product="Motor",
            planning_end=date(2026, 10, 11),
            location="Plant A",
            stock_as_of=TODAY,
            max_stock_age_days=30,
        ),
        today=TODAY,
    )
    [line] = view.lines
    assert (line.on_hand, line.to_buy) == (15, 25)
    assert (
        line.calculation.gross_on_hand,
        line.calculation.reserved_qty,
        line.calculation.blocked_qty,
    ) == (30, 10, 5)
    assert line.calculation.stock_known


@pytest.mark.parametrize(
    "snapshot,issue",
    [
        (date(2026, 8, 1), "stock_stale"),
        (date(2026, 10, 8), "stock_after_cutoff"),
        (None, "stock_undated"),
    ],
)
async def test_unusable_snapshot_is_unknown_not_confirmed_zero(
    app_engine: AsyncEngine,
    purchasing_rows: People,
    snapshot: date | None,
    issue: str,
) -> None:
    who = buyer(purchasing_rows)
    await _seed_plan(app_engine, who)
    await service.import_rows(
        app_engine,
        who,
        "inventory",
        [
            ["Part number", "On hand", "As of"],
            ["BEARING", 100, snapshot],
        ],
    )
    view = await service.create_analysis(
        app_engine,
        who,
        AnalysisRequest(
            product="Motor",
            quantity=Decimal(40),
            max_stock_age_days=30,
        ),
        today=TODAY,
    )
    [line] = view.lines
    assert line.on_hand == 0 and line.to_buy == 40
    assert line.calculation.stock_known is False
    assert issue in line.calculation.stock_issues


async def test_older_or_undated_import_cannot_replace_newer_inventory(
    app_engine: AsyncEngine,
    purchasing_rows: People,
) -> None:
    who = buyer(purchasing_rows)
    await _seed_plan(app_engine, who)
    header = ["Part number", "On hand", "As of"]
    await service.import_rows(app_engine, who, "inventory", [header, ["BEARING", 20, TODAY]])
    for stale in (date(2026, 9, 1), None):
        report = await service.import_rows(
            app_engine, who, "inventory", [header, ["BEARING", 100, stale]]
        )
        assert report.skipped == {"inventory_older_snapshot": 1}
    async with tenant_session(app_engine, who.tenant_id) as session:
        row = await session.scalar(select(InventoryRow))
        assert row is not None and row.on_hand == 20 and row.as_of == TODAY


async def test_rfq_preserves_planning_calculation_evidence(
    app_engine: AsyncEngine,
    purchasing_rows: People,
) -> None:
    who = buyer(purchasing_rows)
    await _seed_plan(app_engine, who)
    analysis = await service.create_analysis(
        app_engine,
        who,
        AnalysisRequest(
            product="Motor",
            planning_end=date(2026, 10, 11),
        ),
        today=TODAY,
    )
    with pytest.raises(service.InvalidRequest, match="confirm purchase quantities"):
        await service.create_rfq_from_analysis(
            app_engine, who, analysis.id, RfqFromAnalysisRequest()
        )
    analysis = await service.adjust_analysis(
        app_engine,
        who,
        analysis.id,
        quantities={"BEARING": Decimal(40)},
    )
    rfq = await service.create_rfq_from_analysis(
        app_engine,
        who,
        analysis.id,
        RfqFromAnalysisRequest(),
    )
    async with tenant_session(app_engine, who.tenant_id) as session:
        line = await session.scalar(select(RfqLine).where(RfqLine.rfq_id == rfq.rfq.id))
        assert line is not None
        evidence = line.source_evidence
        assert evidence["analysis_id"] == str(analysis.id)
        assert evidence["planning_end"] == "2026-10-11"
        assert evidence["calculation"] == analysis.lines[0].calculation.model_dump(mode="json")


async def test_shared_components_warn_about_competing_production_demands(
    app_engine: AsyncEngine,
    purchasing_rows: People,
) -> None:
    who = buyer(purchasing_rows)
    await _seed_plan(app_engine, who)
    await service.import_rows(
        app_engine,
        who,
        "bom",
        [
            ["Product", "Part number", "Qty per unit"],
            ["Pump", "BEARING", 2],
        ],
    )
    await service.import_rows(
        app_engine,
        who,
        "production_plan",
        [
            ["Product", "Quantity", "Need by"],
            ["Pump", 15, TODAY],
        ],
    )
    view = await service.create_analysis(
        app_engine,
        who,
        AnalysisRequest(
            product="Motor",
            planning_end=date(2026, 10, 11),
        ),
        today=TODAY,
    )
    assert "shared_stock_competing_demand" in view.lines[0].calculation.stock_issues


async def test_receipt_date_does_not_establish_received_quantity(
    app_engine: AsyncEngine,
    purchasing_rows: People,
) -> None:
    who = buyer(purchasing_rows)
    await _seed_plan(app_engine, who)
    report = await service.import_rows(
        app_engine,
        who,
        "purchase_history",
        [
            [
                "PO number",
                "Supplier",
                "Part number",
                "Quantity",
                "Unit price",
                "Order date",
                "Received date",
            ],
            ["PO-1", "Supplier A", "BEARING", 100, 2, TODAY, TODAY],
        ],
    )
    assert not report.errors
    async with tenant_session(app_engine, who.tenant_id) as session:
        line = await session.scalar(select(PoLine))
        assert line is not None and line.received_at == TODAY and line.received_qty is None


async def test_unknown_inbound_destination_requires_purchase_quantity_confirmation(
    app_engine: AsyncEngine,
    purchasing_rows: People,
) -> None:
    who = buyer(purchasing_rows)
    await _seed_plan(app_engine, who)
    await service.import_rows(
        app_engine,
        who,
        "inventory",
        [
            ["Part number", "On hand", "Location", "As of"],
            ["BEARING", 0, "Plant A", TODAY],
        ],
    )
    await service.import_rows(
        app_engine,
        who,
        "purchase_history",
        [
            [
                "PO number",
                "Supplier",
                "Part number",
                "Quantity",
                "Unit price",
                "Order date",
                "Promised date",
            ],
            ["PO-1", "Supplier A", "BEARING", 50, 2, TODAY, TODAY],
        ],
    )
    view = await service.create_analysis(
        app_engine,
        who,
        AnalysisRequest(
            product="Motor",
            quantity=Decimal(40),
            location="Plant A",
            need_by=TODAY,
        ),
        today=TODAY,
    )
    assert view.lines[0].calculation.stock_known
    assert "inbound_location_unknown" in view.lines[0].calculation.stock_issues
    with pytest.raises(service.InvalidRequest, match="confirm purchase quantities"):
        await service.create_rfq_from_analysis(app_engine, who, view.id, RfqFromAnalysisRequest())
