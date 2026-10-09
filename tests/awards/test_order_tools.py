"""The copilot's purchase-order tools: a preview records nothing; the confirmation does."""

from dataclasses import replace
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.agents.context import ToolContext
from bido_back.agents.tools import AnyTool, ToolError
from bido_back.copilot.tools import CopilotToolDeps, copilot_tools
from bido_back.db import tenant_session
from bido_back.db.models import PurchaseOrderReceipt
from tests.approvals.conftest import People
from tests.rfqs.conftest import fake_agents

from .test_awards_db import TODAY, World
from .test_lifecycle import sent_po, stage, two_sent_pos

pytestmark = pytest.mark.db


@pytest.fixture
async def world(app_engine: AsyncEngine, award_rows: People) -> World:
    return await World(app_engine, award_rows).setup()


class Tools:
    def __init__(self, world: World, conversation_id: UUID | None = None) -> None:
        self.world = world
        self.conversation_id = conversation_id or uuid4()
        deps = CopilotToolDeps(
            db=world.engine,
            agents=fake_agents(),
            mailboxes=world.mailboxes,
            today=lambda: TODAY,
            conversation_id=self.conversation_id,
        )
        self.tools: dict[str, AnyTool] = {tool.name: tool for tool in copilot_tools(deps)}

    def context(self) -> ToolContext:
        return ToolContext(
            tenant_id=self.world.people.tenant, user_id=self.world.people.owner, run_id=uuid4()
        )

    async def call(self, ctx: ToolContext, name: str, **args: Any) -> Any:
        tool = self.tools[name]
        return await tool.invoke(ctx, tool.parse_input(args))

    async def confirm(self, ctx: ToolContext, name: str, proposed: Any, **args: Any) -> Any:
        """The buyer's next message confirms: a later run, the same conversation."""
        later = replace(ctx, run_id=uuid4())
        return await self.call(later, name, proposal_id=str(proposed.proposal_id), **args)


async def receipts_count(world: World) -> int:
    async with tenant_session(world.engine, world.people.tenant) as session:
        return await session.scalar(select(func.count()).select_from(PurchaseOrderReceipt)) or 0


async def test_list_purchase_orders_counts_every_state(world: World) -> None:
    tools = Tools(world)
    ctx = tools.context()
    empty = await tools.call(ctx, "list_purchase_orders")
    assert empty.purchase_orders == [] and empty.by_state == {}

    await two_sent_pos(world)
    listed = await tools.call(ctx, "list_purchase_orders", open=True)
    assert [(po.ref, po.supplier_name, po.state, po.open) for po in listed.purchase_orders] == [
        ("PO-2", "Rhein", "sent", True),
        ("PO-1", "Lyon", "sent", True),
    ]
    assert listed.by_state == {"sent": 2}
    by_rfq = await tools.call(ctx, "list_purchase_orders", rfq=world.rfq.ref)
    assert len(by_rfq.purchase_orders) == 2
    with pytest.raises(ToolError, match="no RFQ"):
        await tools.call(ctx, "list_purchase_orders", rfq="RFQ-999")
    assert (await tools.call(ctx, "list_purchase_orders", open=False)).purchase_orders == []


async def test_receive_previews_ordered_received_and_this_delivery_then_records_it(
    world: World,
) -> None:
    po = await sent_po(world)
    tools = Tools(world)
    ctx = tools.context()

    # Too much is refused before anything is proposed.
    with pytest.raises(ToolError, match="would exceed the order by 1"):
        await tools.call(
            ctx,
            "receive_purchase_order",
            po="PO-1",
            lines=[{"position": 1, "quantity": "4001"}],
        )
    with pytest.raises(ToolError, match="has no line 9"):
        await tools.call(
            ctx, "receive_purchase_order", po="po 1", lines=[{"position": 9, "quantity": "1"}]
        )

    proposed = await tools.call(
        ctx,
        "receive_purchase_order",
        po="PO-1",
        lines=[{"position": 1, "quantity": "1500"}],
        invoice_number="INV-9",
    )
    assert proposed.awaiting_confirmation and proposed.ref == "PO-1"
    order = proposed.preview.purchase_order
    assert (order.state, order.state_after, order.supplier_name) == (
        "sent",
        "partially_received",
        "Rhein",
    )
    assert order.received_on == TODAY and order.invoice_number == "INV-9"
    assert [
        (line.position, line.ordered, line.received_so_far, line.this_receipt, line.remaining_after)
        for line in order.lines
    ] == [
        (1, Decimal(4000), Decimal(0), Decimal(1500), Decimal(2500)),
        (2, Decimal(500), Decimal(0), None, Decimal(500)),
    ]
    assert await receipts_count(world) == 0  # a preview records nothing
    with pytest.raises(ToolError, match="has not confirmed yet"):
        await tools.call(ctx, "receive_purchase_order", proposal_id=str(proposed.proposal_id))

    # The stored receipt runs, whatever the model passes now.
    done = await tools.confirm(
        ctx,
        "receive_purchase_order",
        proposed,
        po="PO-1",
        lines=[{"position": 1, "quantity": "4000"}],
    )
    assert not done.awaiting_confirmation and done.state == "partially_received"
    assert done.receipt_id is not None and not done.rfq_completed
    assert await receipts_count(world) == 1
    [po] = await world.orders()
    assert po.fulfilment_state == "partially_received"
    with pytest.raises(ToolError, match="already confirmed"):
        await tools.confirm(ctx, "receive_purchase_order", proposed)

    # Empty lines: everything still outstanding, named by the RFQ; the order is received.
    rest = await tools.call(ctx, "receive_purchase_order", rfq=world.rfq.ref)
    assert rest.preview.purchase_order.state_after == "received"
    assert [(line.position, line.this_receipt) for line in rest.preview.purchase_order.lines] == [
        (1, Decimal(2500)),
        (2, Decimal(500)),
    ]
    received = await tools.confirm(ctx, "receive_purchase_order", rest)
    assert received.state == "received"
    with pytest.raises(ToolError, match="not expecting goods"):
        await tools.call(ctx, "receive_purchase_order", po="PO-1")


async def test_acknowledge_close_and_cancel_ask_first(world: World) -> None:
    lyon, rhein = await two_sent_pos(world)
    tools = Tools(world)
    ctx = tools.context()
    with pytest.raises(ToolError, match="several purchase orders"):
        await tools.call(ctx, "acknowledge_purchase_order", rfq=world.rfq.ref)

    ack = await tools.call(
        ctx, "acknowledge_purchase_order", po=str(rhein.id), supplier_order_ref="R-1"
    )
    assert ack.awaiting_confirmation and ack.preview.purchase_order.supplier_order_ref == "R-1"
    assert ack.preview.purchase_order.acknowledged_on == TODAY
    [_, rhein] = await world.orders()
    assert rhein.fulfilment_state == "sent"
    confirmed = await tools.confirm(ctx, "acknowledge_purchase_order", ack)
    assert confirmed.state == "acknowledged"
    with pytest.raises(ToolError, match="only a sent order"):
        await tools.call(ctx, "acknowledge_purchase_order", po="PO-2")

    # Closing short needs the buyer's reason; the preview shows what is outstanding.
    with pytest.raises(ToolError, match="not everything arrived"):
        await tools.call(ctx, "close_purchase_order", po="PO-2")
    with pytest.raises(ToolError, match="reason is required"):
        await tools.call(ctx, "close_purchase_order", po="PO-2", short=True)
    closing = await tools.call(
        ctx, "close_purchase_order", po="PO-2", short=True, reason="no longer needed"
    )
    assert closing.preview.purchase_order.short and closing.preview.purchase_order.state_after
    assert [
        (line.position, line.remaining_after) for line in closing.preview.purchase_order.lines
    ] == [(2, Decimal(500))]
    assert await stage(world) == ("awarded", None, False)
    closed = await tools.confirm(ctx, "close_purchase_order", closing)
    assert closed.state == "closed" and not closed.rfq_completed

    with pytest.raises(ToolError, match="reason is required"):
        await tools.call(ctx, "cancel_purchase_order", po="PO-1")
    cancelling = await tools.call(ctx, "cancel_purchase_order", po="PO-1", reason="Lyon is late")
    assert cancelling.preview.purchase_order.state_after == "cancelled"
    [lyon, _] = await world.orders()
    assert lyon.fulfilment_state == "sent"
    cancelled = await tools.confirm(ctx, "cancel_purchase_order", cancelling)
    assert cancelled.state == "cancelled" and cancelled.rfq_completed
    # PO-1 had been sent: the supplier's cancellation email now waits for approval.
    assert cancelled.cancellation_approval_id is not None
    assert await stage(world) == ("completed", "approve", True)
    with pytest.raises(ToolError, match="already cancelled"):
        await tools.call(ctx, "cancel_purchase_order", po="PO-1", reason="again")

    # Another conversation cannot confirm this one's proposal.
    other = Tools(world)
    again = await tools.call(ctx, "list_purchase_orders")
    assert again.by_state == {"closed": 1, "cancelled": 1}
    with pytest.raises(ToolError, match="no cancel_purchase_order proposal"):
        await other.confirm(ctx, "cancel_purchase_order", cancelling)
