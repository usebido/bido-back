"""The copilot's award tools: status, open purchase orders, and asking for an award."""

from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.agents.context import ToolContext
from bido_back.agents.tools import AnyTool, ToolError
from bido_back.copilot.tools import CopilotToolDeps, copilot_tools
from tests.approvals.conftest import People
from tests.rfqs.conftest import fake_agents

from .test_awards_db import TODAY, World

pytestmark = pytest.mark.db


@pytest.fixture
async def world(app_engine: AsyncEngine, award_rows: People) -> World:
    return await World(app_engine, award_rows).setup()


async def call(world: World, name: str, **args: Any) -> Any:
    deps = CopilotToolDeps(
        db=world.engine, agents=fake_agents(), mailboxes=world.mailboxes, today=lambda: TODAY
    )
    tools: dict[str, AnyTool] = {tool.name: tool for tool in copilot_tools(deps)}
    tool = tools[name]
    ctx = ToolContext(tenant_id=world.people.tenant, user_id=world.people.owner, run_id=uuid4())
    return await tool.invoke(ctx, tool.parse_input(args))


async def test_prepare_award_asks_for_approval_and_awards_nothing(world: World) -> None:
    ref = world.rfq.ref
    assert (await call(world, "get_award_status", rfq=ref)).state == "none"

    with pytest.raises(ToolError, match="say why"):
        await call(
            world, "prepare_award", rfq=ref, choices=[{"line_position": 1, "supplier": "lyon"}]
        )
    prepared = await call(
        world,
        "prepare_award",
        rfq=ref,
        choices=[{"line_position": 1, "supplier": "lyon"}],
        reason="Lyon is qualified",
    )
    assert prepared.differs_from_recommendation is True
    assert [(line.position, line.supplier_name) for line in prepared.lines] == [(1, "Lyon")]
    assert (await call(world, "get_award_status", rfq=ref)).state == "pending_approval"
    assert (await call(world, "list_open_pos")).purchase_orders == []

    await world.approve(prepared.approval_id)

    status = await call(world, "get_award_status", rfq=ref)
    assert status.state == "awarded" and [p.ref for p in status.purchase_orders] == ["PO-1"]
    [po] = (await call(world, "list_open_pos")).purchase_orders
    assert (po.ref, po.supplier_name, po.status) == ("PO-1", "Lyon", "draft")
