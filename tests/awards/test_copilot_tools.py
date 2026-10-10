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


CONVERSATION = uuid4()


async def call(world: World, name: str, **args: Any) -> Any:
    deps = CopilotToolDeps(
        db=world.engine,
        agents=fake_agents(),
        mailboxes=world.mailboxes,
        today=lambda: TODAY,
        conversation_id=CONVERSATION,
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
    # The first call only previews: nothing is asked for yet.
    preview = await call(
        world,
        "prepare_award",
        rfq=ref,
        choices=[{"line_position": 1, "supplier": "lyon"}],
        reason="Lyon is qualified",
    )
    assert preview.awaiting_confirmation is True and preview.approval_id is None
    assert [(a.position, a.supplier_name) for a in preview.preview.award] == [(1, "Lyon")]
    assert (await call(world, "get_award_status", rfq=ref)).state == "none"
    # Confirmed in a later run: the stored request is what runs.
    prepared = await call(
        world, "prepare_award", rfq="RFQ-999", proposal_id=str(preview.proposal_id)
    )
    assert prepared.approval_id is not None
    assert prepared.differs_from_recommendation is True
    assert [(line.position, line.supplier_name) for line in prepared.lines] == [(1, "Lyon")]
    assert (await call(world, "get_award_status", rfq=ref)).state == "pending_approval"
    assert (await call(world, "list_open_pos")).purchase_orders == []

    await world.approve(prepared.approval_id)

    status = await call(world, "get_award_status", rfq=ref)
    assert status.state == "awarded" and [p.ref for p in status.purchase_orders] == ["PO-1"]
    [po] = (await call(world, "list_open_pos")).purchase_orders
    assert (po.ref, po.supplier_name, po.status) == ("PO-1", "Lyon", "draft")

    # The buyer asks to send the draft order: a preview first, then the send approval.
    send = await call(world, "send_purchase_order", po="PO-1")
    assert send.awaiting_confirmation is True and send.send_approval_id is None
    shown = send.preview.purchase_order
    assert (shown.ref, shown.email_to) == ("PO-1", "sales@lyon.example.test")
    sent = await call(world, "send_purchase_order", proposal_id=str(send.proposal_id))
    assert sent.send_approval_id is not None and sent.ref == "PO-1"
    [po] = (await call(world, "list_open_pos", include_sent=True)).purchase_orders
    assert po.status == "pending_approval"


async def test_an_award_names_a_supplier_by_part_of_its_name(world: World) -> None:
    """ "fecha a linha 1 com a Lyon": the comparison's name may be longer."""
    preview = await call(
        world,
        "prepare_award",
        rfq=world.rfq.ref,
        choices=[{"line_position": 1, "supplier": "  LYO "}],
        reason="Lyon is qualified",
    )
    assert [a.supplier_name for a in preview.preview.award] == ["Lyon"]
