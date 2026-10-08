"""add_requirements and analyze_requirements: a need typed in the chat, no BOM needed."""

from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.agents.tools import ToolError
from bido_back.db import tenant_session
from bido_back.db.models import Analysis, AnalysisLine, Item, Requirement
from tests.approvals.conftest import People

from .conftest import buyer_of, context_of, make_tools

pytestmark = pytest.mark.db


@pytest_asyncio.fixture
async def needs_cleanup(
    migrator_engine: AsyncEngine, copilot_rows: People
) -> AsyncIterator[People]:
    yield copilot_rows
    for tenant_id in (copilot_rows.tenant, copilot_rows.other_tenant):
        async with tenant_session(migrator_engine, tenant_id) as session:
            for model in (AnalysisLine, Analysis, Requirement, Item):
                await session.execute(delete(model).where(model.tenant_id == tenant_id))


async def test_a_typed_need_becomes_a_requirement_and_an_analysis(
    app_engine: AsyncEngine, needs_cleanup: People
) -> None:
    me = buyer_of(needs_cleanup)
    tools = make_tools(app_engine)
    ctx = context_of(me)
    part = f"CH-{uuid4().hex[:6]}"

    with pytest.raises(ToolError):
        await tools.call(ctx, "analyze_requirements")  # nothing open yet

    added = await tools.call(
        ctx,
        "add_requirements",
        lines=[{"part_number": part, "quantity": 7500, "unit": "KG", "need_by": "2026-10-31"}],
    )
    assert [(r.part_number, r.quantity, r.unit, r.need_by) for r in added.requirements] == [
        (part, Decimal(7500), "KG", date(2026, 10, 31))
    ]

    analysis = await tools.call(ctx, "analyze_requirements")
    assert analysis.source == "requirements"
    (line,) = analysis.lines
    # No stock and nobody who ever sold it: buy it as given, and say who is missing.
    assert (line.part_number, line.to_buy, line.unit) == (part, Decimal(7500), "KG")
    assert "no_supplier" in [f.code for f in line.flags]

    rfqs = await tools.call(ctx, "create_rfq_from_analysis", analysis_id=str(analysis.analysis_id))
    assert len(rfqs.rfqs) == 1 and rfqs.rfqs[0].lines == 1
    assert rfqs.invited_suppliers == []
