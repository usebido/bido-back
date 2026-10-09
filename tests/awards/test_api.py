"""`/v1/rfqs/{id}/award` and `/v1/purchase-orders` over the ASGI transport."""

from collections.abc import AsyncIterator
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.api.awards import awards_router
from bido_back.api.deps import ApiResources, get_db, get_mailbox_senders, get_principal
from bido_back.auth import Principal
from bido_back.db import create_sessionmaker
from bido_back.rfqs.mailbox import MailboxSenderFactory
from bido_back.settings import Settings
from tests.approvals.conftest import People

from .test_awards_db import World, buyer
from .test_lifecycle import sent_po

UNUSED_DB = "postgresql+asyncpg://app@localhost:5432/unused"


def build_app() -> FastAPI:
    app = FastAPI()
    app.state.resources = ApiResources(
        Settings.model_validate({"environment": "test", "database_url": UNUSED_DB})
    )
    app.include_router(awards_router())
    return app


async def test_award_routes_require_authentication() -> None:
    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        responses = [
            await http.post(f"/v1/rfqs/{uuid4()}/award", json={}),
            await http.get(f"/v1/rfqs/{uuid4()}/award"),
            await http.get("/v1/purchase-orders"),
            await http.get(f"/v1/purchase-orders/{uuid4()}.xlsx"),
            await http.post(f"/v1/purchase-orders/{uuid4()}/send"),
            await http.post(f"/v1/purchase-orders/{uuid4()}/acknowledge", json={}),
            await http.post(f"/v1/purchase-orders/{uuid4()}/receipts", json={"lines": []}),
            await http.get(f"/v1/purchase-orders/{uuid4()}/receipts"),
            await http.post(f"/v1/purchase-orders/{uuid4()}/close", json={}),
            await http.post(f"/v1/purchase-orders/{uuid4()}/cancel", json={"reason": "x"}),
        ]
    assert [r.status_code for r in responses] == [401] * 10


@pytest_asyncio.fixture
async def world(app_engine: AsyncEngine, award_rows: People) -> World:
    return await World(app_engine, award_rows).setup()


def client_for(app_engine: AsyncEngine, world: World, principal: Principal) -> httpx.AsyncClient:
    app = build_app()
    sessionmaker = create_sessionmaker(app_engine)

    def db() -> async_sessionmaker[AsyncSession]:
        return sessionmaker

    def current() -> Principal:
        return principal

    def mailboxes() -> MailboxSenderFactory:
        return world.mailboxes

    app.dependency_overrides[get_db] = db
    app.dependency_overrides[get_principal] = current
    app.dependency_overrides[get_mailbox_senders] = mailboxes
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest_asyncio.fixture
async def http(app_engine: AsyncEngine, world: World) -> AsyncIterator[httpx.AsyncClient]:
    async with client_for(app_engine, world, buyer(world.people)) as client:
        yield client


@pytest.mark.db
async def test_award_and_purchase_order_routes(http: httpx.AsyncClient, world: World) -> None:
    rfq_id = world.rfq.id
    line1 = world.rfq.lines[0]
    differs = await http.post(
        f"/v1/rfqs/{rfq_id}/award",
        json={"lines": [{"rfq_line_id": str(line1.id), "invite_id": str(world.invites["Lyon"])}]},
    )
    assert differs.status_code == 422

    requested = await http.post(f"/v1/rfqs/{rfq_id}/award", json={})
    assert requested.status_code == 202
    body = requested.json()
    assert body["differs_from_recommendation"] is False and len(body["lines"]) == 2
    assert (await http.post(f"/v1/rfqs/{rfq_id}/award", json={})).status_code == 409
    assert (await http.get(f"/v1/rfqs/{rfq_id}/award")).json()["state"] == "pending_approval"

    await world.approve(body["approval_id"])

    award = (await http.get(f"/v1/rfqs/{rfq_id}/award")).json()
    assert award["state"] == "awarded"
    [po] = (await http.get("/v1/purchase-orders")).json()
    assert (po["ref"], po["status"], po["total"]) == ("PO-1", "draft", "1650.00")
    assert (await http.get("/v1/purchase-orders", params={"status": "sent"})).json() == []

    xlsx = await http.get(f"/v1/purchase-orders/{po['id']}.xlsx")
    assert xlsx.status_code == 200 and xlsx.content.startswith(b"PK")
    assert 'filename="PO-1.xlsx"' in xlsx.headers["content-disposition"]
    page = await http.get(f"/v1/purchase-orders/{po['id']}.html")
    assert page.status_code == 200 and "Purchase order PO-1" in page.text
    detail = (await http.get(f"/v1/purchase-orders/{po['id']}")).json()
    assert [line["position"] for line in detail["lines"]] == [1, 2]

    sent = await http.post(f"/v1/purchase-orders/{po['id']}/send")
    assert sent.status_code == 202
    detail = (await http.get(f"/v1/purchase-orders/{po['id']}")).json()
    assert detail["status"] == "pending_approval"
    assert detail["send_approval_id"] == sent.json()["approval_id"]
    assert (await http.get(f"/v1/purchase-orders/{uuid4()}")).status_code == 404


@pytest.mark.db
async def test_purchase_order_lifecycle_routes(
    http: httpx.AsyncClient, app_engine: AsyncEngine, world: World
) -> None:
    po = await sent_po(world)
    detail = (await http.get(f"/v1/purchase-orders/{po.id}")).json()
    assert detail["status"] == "sent" and detail["fulfilment"]["state"] == "sent"
    assert detail["fulfilment"]["open"] is True
    mlcc, connectors = detail["lines"]
    assert (mlcc["quantity"], mlcc["received_quantity"], mlcc["remaining_quantity"]) == (
        "4000.0000",
        "0",
        "4000.0000",
    )
    listed = (await http.get("/v1/purchase-orders", params={"open": "true"})).json()
    assert [(item["ref"], item["fulfilment"]["state"]) for item in listed] == [("PO-1", "sent")]
    assert (await http.get("/v1/purchase-orders", params={"open": "false"})).json() == []
    assert (await http.get("/v1/purchase-orders", params={"state": "nope"})).status_code == 422

    acknowledged = await http.post(
        f"/v1/purchase-orders/{po.id}/acknowledge",
        json={"acknowledged_on": "2026-10-08", "supplier_order_ref": "AB-77"},
    )
    assert acknowledged.status_code == 200, acknowledged.text
    assert acknowledged.json()["fulfilment"]["state"] == "acknowledged"
    assert acknowledged.json()["fulfilment"]["supplier_order_ref"] == "AB-77"
    again = await http.post(f"/v1/purchase-orders/{po.id}/acknowledge", json={})
    assert again.status_code == 409

    too_much = await http.post(
        f"/v1/purchase-orders/{po.id}/receipts",
        json={"lines": [{"line_id": mlcc["id"], "quantity": "4001"}]},
    )
    assert too_much.status_code == 422 and "exceed" in too_much.json()["detail"]
    assert (await http.get(f"/v1/purchase-orders/{po.id}/receipts")).json() == []

    partial = await http.post(
        f"/v1/purchase-orders/{po.id}/receipts",
        json={
            "received_on": "2026-10-20",
            "invoice_number": "INV-1",
            "lines": [{"line_id": mlcc["id"], "quantity": "1500"}],
        },
    )
    assert partial.status_code == 201, partial.text
    assert partial.json()["fulfilment"]["state"] == "partially_received"
    assert partial.json()["fulfilment"]["received_quantity"] == "1500.0000"
    close_early = await http.post(f"/v1/purchase-orders/{po.id}/close", json={})
    assert close_early.status_code == 409
    no_reason = await http.post(f"/v1/purchase-orders/{po.id}/close", json={"short": True})
    assert no_reason.status_code == 422

    rest = await http.post(
        f"/v1/purchase-orders/{po.id}/receipts",
        json={
            "received_on": "2026-10-28",
            "lines": [
                {"line_id": mlcc["id"], "quantity": "2500"},
                {"line_id": connectors["id"], "quantity": "500"},
            ],
        },
    )
    assert rest.status_code == 201 and rest.json()["fulfilment"]["state"] == "received"
    [first, second] = (await http.get(f"/v1/purchase-orders/{po.id}/receipts")).json()
    assert first["invoice_number"] == "INV-1" and len(second["lines"]) == 2
    assert [line["position"] for line in second["lines"]] == [1, 2]

    cancel_late = await http.post(
        f"/v1/purchase-orders/{po.id}/cancel", json={"reason": "too late"}
    )
    assert cancel_late.status_code == 409
    closed = await http.post(f"/v1/purchase-orders/{po.id}/close", json={})
    assert closed.status_code == 200 and closed.json()["fulfilment"]["state"] == "closed"
    assert closed.json()["fulfilment"]["closed_short"] is False
    assert (await http.get("/v1/purchase-orders", params={"open": "true"})).json() == []
    assert (await http.post(f"/v1/purchase-orders/{uuid4()}/close", json={})).status_code == 404

    # Another tenant sees nothing and changes nothing, even with the id.
    outsider = Principal(
        tenant_id=world.people.other_tenant, user_id=world.people.other_approver, role="approver"
    )
    async with client_for(app_engine, world, outsider) as other:
        assert (await other.get("/v1/purchase-orders")).json() == []
        assert (await other.get(f"/v1/purchase-orders/{po.id}/receipts")).status_code == 404
        assert (
            await other.post(f"/v1/purchase-orders/{po.id}/cancel", json={"reason": "mine"})
        ).status_code == 404
        assert (
            await other.post(
                f"/v1/purchase-orders/{po.id}/receipts",
                json={"lines": [{"line_id": mlcc["id"], "quantity": "1"}]},
            )
        ).status_code == 404
