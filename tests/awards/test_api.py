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
        ]
    assert [r.status_code for r in responses] == [401] * 5


@pytest_asyncio.fixture
async def world(app_engine: AsyncEngine, award_rows: People) -> World:
    return await World(app_engine, award_rows).setup()


@pytest_asyncio.fixture
async def http(app_engine: AsyncEngine, world: World) -> AsyncIterator[httpx.AsyncClient]:
    app = build_app()
    sessionmaker = create_sessionmaker(app_engine)
    principal: Principal = buyer(world.people)

    def db() -> async_sessionmaker[AsyncSession]:
        return sessionmaker

    def current() -> Principal:
        return principal

    def mailboxes() -> MailboxSenderFactory:
        return world.mailboxes

    app.dependency_overrides[get_db] = db
    app.dependency_overrides[get_principal] = current
    app.dependency_overrides[get_mailbox_senders] = mailboxes
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
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
