"""The chat must use its persisted review and wait for buyer confirmation."""

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.agents.tools import ToolError
from bido_back.db import tenant_session
from bido_back.db.models import InventoryRow
from tests.approvals.conftest import People
from tests.purchasing.conftest import FakeStorage, purchasing_rows
from tests.purchasing.test_planning import Specialists, create

from .conftest import buyer_of, context_of, make_tools

__all__ = ["purchasing_rows"]
pytestmark = pytest.mark.db


async def test_planner_tools_resume_and_import_the_frozen_review_only(
    app_engine: AsyncEngine,
    purchasing_rows: People,
) -> None:
    who, storage, fake = buyer_of(purchasing_rows), FakeStorage(), Specialists()
    ctx = context_of(who)
    plan = await create(app_engine, storage, who)
    tools = make_tools(
        app_engine, attachment_ids=(plan.upload_id,), storage=storage, workbook_agents=fake.agents()
    )
    resumed = await tools.call(
        ctx, "plan_spreadsheet", upload_id=plan.upload_id, objective=plan.objective
    )
    assert resumed.plan_id == plan.plan_id
    unfinished = await tools.call(
        ctx, "execute_spreadsheet_plan", plan_id=plan.plan_id, max_steps=3
    )
    assert unfinished.status == "running"
    with pytest.raises(ToolError, match="has a plan"):
        await tools.call(ctx, "import_spreadsheet", upload_id=plan.upload_id)
    with pytest.raises(ToolError, match="not ready"):
        await tools.call(
            ctx,
            "import_spreadsheet",
            upload_id=plan.upload_id,
            plan_id=plan.plan_id,
            plan_revision=1,
        )
    ready = await tools.call(ctx, "execute_spreadsheet_plan", plan_id=plan.plan_id)
    assert ready.status == "ready"
    args = {"upload_id": plan.upload_id, "plan_id": plan.plan_id, "plan_revision": 1}
    with pytest.raises(ToolError, match="stored mapping"):
        await tools.call(ctx, "import_spreadsheet", **args, mapping={"sections": []})
    with pytest.raises(ToolError, match="plan_revision"):
        await tools.call(ctx, "import_spreadsheet", upload_id=plan.upload_id, plan_id=plan.plan_id)
    preview = await tools.call(ctx, "import_spreadsheet", **args, confirmed=True)
    repeated = await tools.call(ctx, "import_spreadsheet", **args, confirmed=True)
    assert preview.status == repeated.status == "needs_confirmation"
    async with tenant_session(app_engine, who.tenant_id) as session:
        assert await session.scalar(select(func.count()).select_from(InventoryRow)) == 0

    later = make_tools(
        app_engine,
        earlier_attachment_ids=(plan.upload_id,),
        storage=storage,
        workbook_agents=fake.agents(),
    )
    saved = await later.call(ctx, "get_spreadsheet_plan", plan_id=plan.plan_id)
    assert saved.confirmation_requested
    imported = await later.call(ctx, "import_spreadsheet", **args, confirmed=True)
    assert imported.status == "imported"
    assert (await later.call(ctx, "import_spreadsheet", **args, confirmed=True)) == imported


async def test_plan_ids_do_not_authorize_a_different_conversation(
    app_engine: AsyncEngine,
    purchasing_rows: People,
) -> None:
    who, storage = buyer_of(purchasing_rows), FakeStorage()
    plan = await create(app_engine, storage, who)
    unrelated = make_tools(app_engine, storage=storage)
    for name in ("get_spreadsheet_plan", "execute_spreadsheet_plan", "revise_spreadsheet_plan"):
        args: dict[str, object] = {"plan_id": plan.plan_id}
        if name == "revise_spreadsheet_plan":
            args["clarification"] = "Some clarification"
        with pytest.raises(ToolError, match="not attached in this conversation"):
            await unrelated.call(context_of(who), name, **args)
