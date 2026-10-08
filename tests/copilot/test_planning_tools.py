"""Tool refinements preserve dated supply coverage instead of flattening the build plan."""

from datetime import date

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.purchasing import service
from tests.approvals.conftest import People
from tests.purchasing.conftest import purchasing_rows
from tests.purchasing.test_service import TODAY

from .conftest import buyer_of, context_of, make_tools

__all__ = ["purchasing_rows"]
pytestmark = pytest.mark.db


async def test_refining_safety_stock_preserves_the_plan_and_dated_receipts(
    app_engine: AsyncEngine, purchasing_rows: People
) -> None:
    principal = buyer_of(purchasing_rows)
    await service.import_rows(
        app_engine,
        principal,
        "bom",
        [["Product", "Part number", "Qty per unit"], ["Motor", "BEARING", 1]],
    )
    await service.import_rows(
        app_engine,
        principal,
        "production_plan",
        [
            ["Product", "Quantity", "Need by"],
            ["Motor", 40, date(2026, 10, 9)],
            ["Motor", 400, date(2026, 11, 9)],
        ],
    )
    report = await service.import_rows(
        app_engine,
        principal,
        "inventory",
        [
            ["Part number", "On hand", "On order", "On order due", "Location", "As of"],
            ["BEARING", 10, 400, date(2026, 10, 20), "Plant A", TODAY],
        ],
    )
    assert not report.errors
    tools = make_tools(app_engine)
    ctx = context_of(principal)
    first = await tools.call(
        ctx,
        "run_purchase_analysis",
        product="Motor",
        planning_start="2026-10-08",
        planning_end="2026-11-10",
        location="Plant A",
        stock_as_of=TODAY.isoformat(),
        max_stock_age_days=30,
    )
    changed = await tools.call(
        ctx,
        "refine_analysis",
        analysis_id=str(first.analysis_id),
        safety_stock_pct=10,
    )
    restored = await service.get_analysis(app_engine, principal, changed.analysis_id)
    assert first.demand_source == restored.demand_source == "production_plan"
    assert restored.quantity == 440
    assert (restored.planning_start, restored.planning_end) == (
        date(2026, 10, 8),
        date(2026, 11, 10),
    )
    assert restored.location == "Plant A" and restored.max_stock_age_days == 30
    assert [
        (row.need_by, row.inbound_used, row.to_buy)
        for row in restored.lines[0].calculation.allocations
    ] == [(date(2026, 10, 9), 0, 34), (date(2026, 11, 9), 400, 40)]
    assert not tools.mailboxes.sender.sent
