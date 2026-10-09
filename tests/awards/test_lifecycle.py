"""The purchase order after the send: acknowledged, received per line, closed, cancelled.

Against Postgres, through the lifecycle service; the RFQ completes when its
orders are done, and every step shows in the activity feed.
"""

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.auth import Principal
from bido_back.awards import lifecycle, service
from bido_back.awards.orders import po_lines
from bido_back.awards.schemas import (
    AcknowledgeRequest,
    AwardRequest,
    AwardSelection,
    CancelRequest,
    CloseRequest,
    ReceiptRequest,
)
from bido_back.db import tenant_session
from bido_back.db.models import AuditEvent, IssuedPurchaseOrder, Rfq
from bido_back.rfqs.activity import rfq_activity
from bido_back.rfqs.errors import InvalidState, NotFound
from bido_back.rfqs.overview import list_rfq_overviews
from tests.approvals.conftest import People

from .test_awards_db import TODAY, World, buyer

pytestmark = pytest.mark.db


@pytest.fixture
async def world(app_engine: AsyncEngine, award_rows: People) -> World:
    return await World(app_engine, award_rows).setup()


async def send(world: World, po: IssuedPurchaseOrder) -> None:
    """Request and approve the PO's send, so the worker marks it `sent`."""
    requested = await service.request_po_send(
        world.engine, buyer(world.people), po.id, world.mailboxes
    )
    await world.approve(requested.approval_id)


async def sent_po(world: World) -> IssuedPurchaseOrder:
    """The recommendation awarded (one PO to Rhein: 4000 + 500 pcs) and sent."""
    await world.award()
    [po] = await world.orders()
    await send(world, po)
    [po] = await world.orders()
    assert (po.status, po.fulfilment_state) == ("sent", "sent")
    return po


async def two_sent_pos(world: World) -> list[IssuedPurchaseOrder]:
    """Line 1 to Lyon, line 2 to Rhein: two POs, both sent."""
    line1, line2 = world.rfq.lines
    await world.award(
        AwardRequest(
            lines=[
                AwardSelection(rfq_line_id=line1.id, invite_id=world.invites["Lyon"]),
                AwardSelection(rfq_line_id=line2.id),
            ],
            reason="Lyon is qualified for the MLCC",
        )
    )
    orders = await world.orders()
    assert len(orders) == 2
    for po in orders:
        await send(world, po)
    return await world.orders()


def receipt(lines: dict[UUID, str], **fields: object) -> ReceiptRequest:
    return ReceiptRequest.model_validate(
        {
            "lines": [{"line_id": str(line_id), "quantity": qty} for line_id, qty in lines.items()],
            **fields,
        }
    )


async def events(world: World, action: str) -> list[AuditEvent]:
    async with tenant_session(world.engine, world.people.tenant) as session:
        return list(
            await session.scalars(
                select(AuditEvent)
                .where(
                    AuditEvent.action == action,
                    AuditEvent.payload["rfq_id"].astext == str(world.rfq.id),
                )
                .order_by(AuditEvent.created_at)
            )
        )


async def stage(world: World) -> tuple[str, str | None, bool]:
    page = await list_rfq_overviews(world.engine, buyer(world.people), today=TODAY, q=world.rfq.ref)
    [row] = [item for item in page.items if item.id == world.rfq.id]
    return row.stage, row.next_step, row.completed_at is not None


async def test_the_full_cycle_acknowledged_received_closed_completes_the_rfq(
    world: World,
) -> None:
    po = await sent_po(world)
    principal = buyer(world.people)
    assert await stage(world) == ("awarded", None, False)

    await lifecycle.acknowledge_purchase_order(
        world.engine,
        principal,
        po.id,
        AcknowledgeRequest(
            acknowledged_on=date(2026, 10, 8), supplier_order_ref="AB-77", note="by phone"
        ),
    )
    view = await service.get_purchase_order(world.engine, principal, po.id)
    assert view.status == "sent" and view.fulfilment.state == "acknowledged"
    assert view.fulfilment.open and view.fulfilment.supplier_order_ref == "AB-77"
    assert view.fulfilment.acknowledged_by == world.people.owner
    assert view.fulfilment.acknowledged_at is not None
    assert view.fulfilment.acknowledged_at.date() == date(2026, 10, 8)
    with pytest.raises(InvalidState):  # once
        await lifecycle.acknowledge_purchase_order(
            world.engine, principal, po.id, AcknowledgeRequest()
        )

    async with tenant_session(world.engine, world.people.tenant) as session:
        mlcc, connectors = await po_lines(session, po.id)
    assert (mlcc.quantity, connectors.quantity) == (Decimal(4000), Decimal(500))

    # A first, partial delivery with its invoice.
    first = await lifecycle.receive_purchase_order(
        world.engine,
        principal,
        po.id,
        receipt(
            {mlcc.id: "1500", connectors.id: "500"},
            received_on="2026-10-20",
            invoice_number="INV-1",
            invoice_date="2026-10-19",
        ),
    )
    assert first.state == "partially_received"
    view = await service.get_purchase_order(world.engine, principal, po.id)
    assert view.fulfilment.state == "partially_received"
    assert (view.fulfilment.ordered_quantity, view.fulfilment.received_quantity) == (
        Decimal(4500),
        Decimal(2000),
    )
    assert [(line.received_quantity, line.remaining_quantity) for line in view.lines] == [
        (Decimal(1500), Decimal(2500)),
        (Decimal(500), Decimal(0)),
    ]
    with pytest.raises(InvalidState):  # not everything arrived: no plain close
        await lifecycle.close_purchase_order(world.engine, principal, po.id, CloseRequest())

    # Too much is refused, with the numbers, and nothing is recorded.
    with pytest.raises(lifecycle.OverReceipt, match="ordered 4000.0000 pcs, received 1500"):
        await lifecycle.receive_purchase_order(
            world.engine, principal, po.id, receipt({mlcc.id: "2501"})
        )
    with pytest.raises(lifecycle.OverReceipt):
        await lifecycle.receive_purchase_order(
            world.engine, principal, po.id, receipt({connectors.id: "1"})
        )
    with pytest.raises(lifecycle.UnknownLine):
        await lifecycle.receive_purchase_order(
            world.engine, principal, po.id, receipt({uuid4(): "1"})
        )
    receipts = await lifecycle.list_receipts(world.engine, principal, po.id)
    assert len(receipts) == 1 and receipts[0].invoice_number == "INV-1"
    assert [(line.position, line.quantity) for line in receipts[0].lines] == [
        (1, Decimal(1500)),
        (2, Decimal(500)),
    ]

    # The rest arrives: received; close; the RFQ is completed.
    second = await lifecycle.receive_purchase_order(
        world.engine, principal, po.id, receipt({mlcc.id: "2500"}, received_on="2026-10-28")
    )
    assert second.state == "received"
    with pytest.raises(InvalidState):  # fully received: close it, do not cancel it
        await lifecycle.cancel_purchase_order(
            world.engine, principal, po.id, CancelRequest(reason="changed my mind")
        )
    await lifecycle.close_purchase_order(world.engine, principal, po.id, CloseRequest())
    view = await service.get_purchase_order(world.engine, principal, po.id)
    assert view.fulfilment.state == "closed" and not view.fulfilment.open
    assert not view.fulfilment.closed_short and view.fulfilment.closed_by == world.people.owner
    assert view.fulfilment.receipts_count == 2
    with pytest.raises(InvalidState):
        await lifecycle.close_purchase_order(world.engine, principal, po.id, CloseRequest())
    with pytest.raises(InvalidState):
        await lifecycle.receive_purchase_order(
            world.engine, principal, po.id, receipt({mlcc.id: "1"})
        )

    assert await stage(world) == ("completed", None, True)
    async with tenant_session(world.engine, world.people.tenant) as session:
        rfq = await session.get(Rfq, world.rfq.id)
    assert rfq is not None and rfq.status == "closed" and rfq.completed_at is not None
    [completed] = await events(world, "rfq.completed")
    assert completed.payload == {
        "rfq_id": str(world.rfq.id),
        "purchase_orders": 1,
        "closed": 1,
        "cancelled": 0,
    }
    [received_event, _] = await events(world, "purchase_order.received")
    assert received_event.payload["lines"] == [
        {"position": 1, "quantity": 1500, "received": 1500, "ordered": 4000},
        {"position": 2, "quantity": 500, "received": 500, "ordered": 500},
    ]
    assert received_event.payload["invoice_number"] == "INV-1"
    assert received_event.actor_user_id == world.people.owner

    feed = await rfq_activity(world.engine, principal, world.rfq.id)
    summaries = [item.summary for item in feed]
    # The close and the completion happen in one transaction: same instant, any order.
    assert sorted(summaries[:2]) == sorted(
        ["RFQ completed: 1 purchase order closed", "Purchase order PO-1 closed"]
    )
    assert summaries[2:5] == [
        "Purchase order PO-1 fully received: line 1: 4000 of 4000",
        "Purchase order PO-1 partially received: line 1: 1500 of 4000, line 2: 500 of 500 "
        "(invoice INV-1)",
        "Purchase order PO-1 acknowledged by Rhein (their ref AB-77)",
    ]
    by_action = {item.action: item for item in feed}
    assert by_action["purchase_order.acknowledged"].supplier_name == "Rhein"
    assert by_action["purchase_order.acknowledged"].kind == "user"
    assert by_action["rfq.completed"].kind == "system"


async def test_close_short_records_the_shortfall_and_needs_a_reason(world: World) -> None:
    po = await sent_po(world)
    principal = buyer(world.people)
    async with tenant_session(world.engine, world.people.tenant) as session:
        mlcc, _ = await po_lines(session, po.id)
    # Goods can arrive before the buyer records an acknowledgement.
    await lifecycle.receive_purchase_order(
        world.engine, principal, po.id, receipt({mlcc.id: "4000"})
    )
    with pytest.raises(lifecycle.ReasonRequired):
        await lifecycle.close_purchase_order(
            world.engine, principal, po.id, CloseRequest(short=True)
        )
    await lifecycle.close_purchase_order(
        world.engine, principal, po.id, CloseRequest(short=True, reason="connectors discontinued")
    )
    view = await service.get_purchase_order(world.engine, principal, po.id)
    assert view.fulfilment.state == "closed" and view.fulfilment.closed_short
    assert view.fulfilment.close_reason == "connectors discontinued"
    [closed] = await events(world, "purchase_order.closed")
    assert closed.payload["short"] is True
    assert closed.payload["shortfall"] == [{"position": 2, "ordered": 500, "received": 0}]
    feed = await rfq_activity(world.engine, principal, world.rfq.id)
    assert "Purchase order PO-1 closed short: 1 line outstanding" in [i.summary for i in feed[:2]]
    assert await stage(world) == ("completed", None, True)


async def test_cancel_before_receipt_and_the_rfq_completes_only_with_a_closed_order(
    world: World,
) -> None:
    lyon, rhein = await two_sent_pos(world)
    principal = buyer(world.people)
    assert (lyon.ref, rhein.ref) == ("PO-1", "PO-2")

    await lifecycle.cancel_purchase_order(
        world.engine, principal, lyon.id, CancelRequest(reason="Lyon cannot deliver in time")
    )
    view = await service.get_purchase_order(world.engine, principal, lyon.id)
    assert view.fulfilment.state == "cancelled" and view.fulfilment.cancel_reason
    assert view.fulfilment.cancelled_by == world.people.owner
    with pytest.raises(InvalidState):
        await lifecycle.cancel_purchase_order(
            world.engine, principal, lyon.id, CancelRequest(reason="again")
        )
    with pytest.raises(InvalidState):
        await lifecycle.acknowledge_purchase_order(
            world.engine, principal, lyon.id, AcknowledgeRequest()
        )
    # One cancelled, one still open: not completed.
    assert await stage(world) == ("awarded", None, False)
    assert await events(world, "rfq.completed") == []

    async with tenant_session(world.engine, world.people.tenant) as session:
        [connectors] = await po_lines(session, rhein.id)
    await lifecycle.receive_purchase_order(
        world.engine, principal, rhein.id, receipt({connectors.id: "500"})
    )
    await lifecycle.close_purchase_order(world.engine, principal, rhein.id, CloseRequest())
    assert await stage(world) == ("completed", None, True)
    [completed] = await events(world, "rfq.completed")
    assert (completed.payload["closed"], completed.payload["cancelled"]) == (1, 1)
    feed = await rfq_activity(world.engine, principal, world.rfq.id)
    assert "RFQ completed: 1 purchase order closed, 1 cancelled" in [i.summary for i in feed[:2]]
    [cancelled] = [item for item in feed if item.action == "purchase_order.cancelled"]
    assert (
        cancelled.summary == "Purchase order PO-1 cancelled" and cancelled.supplier_name == "Lyon"
    )


async def test_a_draft_order_can_be_cancelled_but_not_one_waiting_for_approval(
    world: World,
) -> None:
    await world.award()
    [po] = await world.orders()
    principal = buyer(world.people)
    assert po.fulfilment_state == "not_sent"
    with pytest.raises(InvalidState):  # never sent: nothing to acknowledge, receive or close
        await lifecycle.acknowledge_purchase_order(
            world.engine, principal, po.id, AcknowledgeRequest()
        )
    with pytest.raises(InvalidState):
        await lifecycle.close_purchase_order(
            world.engine, principal, po.id, CloseRequest(short=True, reason="x")
        )

    requested = await service.request_po_send(world.engine, principal, po.id, world.mailboxes)
    with pytest.raises(InvalidState, match="waiting for approval"):
        await lifecycle.cancel_purchase_order(
            world.engine, principal, po.id, CancelRequest(reason="no longer needed")
        )
    await world.reject(requested.approval_id)

    await lifecycle.cancel_purchase_order(
        world.engine, principal, po.id, CancelRequest(reason="no longer needed")
    )
    [po] = await world.orders()
    assert (po.status, po.fulfilment_state) == ("draft", "cancelled")
    with pytest.raises(InvalidState):  # a cancelled order is not sent
        await service.request_po_send(world.engine, principal, po.id, world.mailboxes)
    # All cancelled, none closed: the RFQ is not completed.
    assert await stage(world) == ("awarded", None, False)


async def test_listing_filters_by_open_state_and_rfq(world: World) -> None:
    lyon, _ = await two_sent_pos(world)
    principal = buyer(world.people)
    await lifecycle.cancel_purchase_order(
        world.engine, principal, lyon.id, CancelRequest(reason="late")
    )
    all_orders = await service.list_purchase_orders(world.engine, principal)
    assert [(po.ref, po.fulfilment.state) for po in all_orders] == [
        ("PO-1", "cancelled"),
        ("PO-2", "sent"),
    ]
    open_orders = await service.list_purchase_orders(world.engine, principal, open=True)
    assert [po.ref for po in open_orders] == ["PO-2"]
    finished = await service.list_purchase_orders(world.engine, principal, open=False)
    assert [po.ref for po in finished] == ["PO-1"]
    by_state = await service.list_purchase_orders(world.engine, principal, state="cancelled")
    assert [po.ref for po in by_state] == ["PO-1"]
    assert await service.list_purchase_orders(world.engine, principal, rfq_id=uuid4()) == []
    assert len(await service.list_purchase_orders(world.engine, principal, rfq_id=lyon.rfq_id)) == 2


async def test_another_tenant_cannot_touch_the_order(world: World) -> None:
    po = await sent_po(world)
    other = Principal(
        tenant_id=world.people.other_tenant, user_id=world.people.other_approver, role="approver"
    )
    with pytest.raises(NotFound):
        await lifecycle.acknowledge_purchase_order(world.engine, other, po.id, AcknowledgeRequest())
    with pytest.raises(NotFound):
        await lifecycle.receive_purchase_order(world.engine, other, po.id, receipt({uuid4(): "1"}))
    with pytest.raises(NotFound):
        await lifecycle.close_purchase_order(world.engine, other, po.id, CloseRequest())
    with pytest.raises(NotFound):
        await lifecycle.cancel_purchase_order(
            world.engine, other, po.id, CancelRequest(reason="not mine")
        )
    with pytest.raises(NotFound):
        await lifecycle.list_receipts(world.engine, other, po.id)
    assert await service.list_purchase_orders(world.engine, other, open=True) == []
    view = await service.get_purchase_order(world.engine, buyer(world.people), po.id)
    assert view.fulfilment.state == "sent"
