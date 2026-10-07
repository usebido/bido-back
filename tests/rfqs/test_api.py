"""The RFQ, supplier, approval-queue and /v1/me routes over the ASGI transport.

The app is assembled here from the routers (the production wiring lives in
api/app.py). Authentication, the database, the agents and the mailbox are
dependency overrides.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.api.approvals import approvals_router
from bido_back.api.deps import (
    ApiResources,
    get_db,
    get_mailbox_senders,
    get_principal,
    get_rfq_agents,
)
from bido_back.api.me import me_router
from bido_back.api.rfqs import rfqs_router
from bido_back.api.suppliers import suppliers_router
from bido_back.api.templates import templates_router
from bido_back.auth import Principal
from bido_back.db import create_sessionmaker
from bido_back.rfqs.agents import RfqAgents
from bido_back.rfqs.mailbox import MailboxSenderFactory
from bido_back.settings import Settings
from tests.approvals.conftest import People

from .conftest import FakeMailboxes, fake_agents

UNUSED_DB = "postgresql+asyncpg://app@localhost:5432/unused"


def build_app() -> FastAPI:
    app = FastAPI()
    app.state.resources = ApiResources(
        Settings.model_validate({"environment": "test", "database_url": UNUSED_DB})
    )
    for router in (
        rfqs_router(),
        suppliers_router(),
        approvals_router(),
        me_router(),
        templates_router(),
    ):
        app.include_router(router)
    return app


async def test_routes_require_authentication() -> None:
    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        responses = [
            await http.get("/v1/rfqs"),
            await http.post("/v1/rfqs", json={"text": "x"}),
            await http.get("/v1/suppliers"),
            await http.get("/v1/approvals"),
            await http.get(f"/v1/rfqs/{uuid4()}/comparison"),
            await http.get("/v1/me"),
        ]
    assert [r.status_code for r in responses] == [401] * len(responses)


@dataclass
class Api:
    http: httpx.AsyncClient
    mailboxes: FakeMailboxes
    principal: Principal


@pytest_asyncio.fixture
async def api(app_engine: AsyncEngine, rfq_rows: People) -> AsyncIterator[Api]:
    app = build_app()
    sessionmaker = create_sessionmaker(app_engine)
    agents = fake_agents()
    mailboxes = FakeMailboxes()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        state = Api(
            http=http,
            mailboxes=mailboxes,
            principal=Principal(tenant_id=rfq_rows.tenant, user_id=rfq_rows.owner, role="owner"),
        )

        def principal() -> Principal:
            return state.principal

        def db() -> async_sessionmaker[AsyncSession]:
            return sessionmaker

        def rfq_agents() -> RfqAgents:
            return agents

        def mailbox_senders() -> MailboxSenderFactory:
            return mailboxes

        app.dependency_overrides[get_principal] = principal
        app.dependency_overrides[get_db] = db
        app.dependency_overrides[get_rfq_agents] = rfq_agents
        app.dependency_overrides[get_mailbox_senders] = mailbox_senders
        yield state


@pytest.mark.db
async def test_rfq_flow_over_http(api: Api, rfq_rows: People) -> None:
    http = api.http

    created = await http.post("/v1/rfqs", json={"text": "Need MLCC and connectors"})
    assert created.status_code == 201, created.text
    rfq = created.json()
    assert rfq["ref"] == "RFQ-1" and rfq["status"] == "draft"
    assert rfq["questions"] == ["Which delivery address?"]
    assert [line["quantities"] for line in rfq["lines"]] == [["4000", "10000"], ["500"]]

    listed = (await http.get("/v1/rfqs")).json()
    assert [r["id"] for r in listed] == [rfq["id"]]

    line_id = rfq["lines"][1]["id"]
    patched = await http.patch(
        f"/v1/rfqs/{rfq['id']}/lines/{line_id}", json={"quantities": [250, 1000], "unit": "pcs"}
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["quantities"] == ["250", "1000"]
    assert (
        await http.patch(f"/v1/rfqs/{rfq['id']}/lines/{line_id}", json={"price": 1})
    ).status_code == 422

    supplier = await http.post(
        "/v1/suppliers",
        json={
            "name": "Rhein Elektronik",
            "type": "authorized_distributor",
            "country": "DE",
            "language": "de",
            "contacts": [{"name": "Jonas Weber", "email": "jonas@rhein.example.test"}],
        },
    )
    assert supplier.status_code == 201, supplier.text
    supplier_body = supplier.json()
    assert [s["name"] for s in (await http.get("/v1/suppliers")).json()] == ["Rhein Elektronik"]
    bad_email = await http.post(
        "/v1/suppliers", json={"name": "X", "contacts": [{"email": "a@b.c\r\nBcc: x@y.z"}]}
    )
    assert bad_email.status_code == 422

    invite = await http.post(
        f"/v1/rfqs/{rfq['id']}/invites",
        json={
            "supplier_id": supplier_body["id"],
            "contact_id": supplier_body["contacts"][0]["id"],
        },
    )
    assert invite.status_code == 201, invite.text
    duplicate = await http.post(
        f"/v1/rfqs/{rfq['id']}/invites",
        json={
            "supplier_id": supplier_body["id"],
            "contact_id": supplier_body["contacts"][0]["id"],
        },
    )
    assert duplicate.status_code == 409

    sent = await http.post(f"/v1/rfqs/{rfq['id']}/send")
    assert sent.status_code == 200, sent.text
    [prepared] = sent.json()["approvals"]
    assert prepared["invite_id"] == invite.json()["id"]

    detail = (await http.get(f"/v1/rfqs/{rfq['id']}")).json()
    assert [i["status"] for i in detail["invites"]] == ["pending_approval"]
    assert "reply_token" not in detail["invites"][0]

    # Lines are frozen once an email was prepared from them.
    frozen = await http.patch(f"/v1/rfqs/{rfq['id']}/lines/{line_id}", json={"unit": "kg"})
    assert frozen.status_code == 409

    api.principal = Principal(tenant_id=rfq_rows.tenant, user_id=rfq_rows.approver, role="approver")
    queue = await http.get("/v1/approvals", params={"status": "pending"})
    assert queue.status_code == 200
    [item] = [i for i in queue.json() if i["id"] == prepared["approval_id"]]
    assert item["tool"] == "send_rfq_email" and item["requested_by_me"] is False
    email = item["rfq_email"]
    assert email["to"] == ["jonas@rhein.example.test"]
    assert email["subject"].startswith("[RFQ-1] ")
    assert email["sender_user_id"] == str(rfq_rows.owner)

    comparison = await http.get(f"/v1/rfqs/{rfq['id']}/comparison")
    assert comparison.status_code == 200, comparison.text
    body = comparison.json()
    assert body["suppliers"] == []
    assert [a["status"] for a in body["awaiting"]] == ["pending_approval"]

    me = await http.get("/v1/me")
    assert me.status_code == 200
    assert me.json()["user_id"] == str(rfq_rows.approver)
    assert me.json()["role"] == "approver"
    assert me.json()["allow_self_approval"] is True

    # Another tenant sees nothing.
    api.principal = Principal(tenant_id=rfq_rows.other_tenant, user_id=rfq_rows.other_approver)
    assert (await http.get(f"/v1/rfqs/{rfq['id']}")).status_code == 404
    assert (await http.get(f"/v1/rfqs/{rfq['id']}/comparison")).status_code == 404
    assert (await http.get("/v1/rfqs")).json() == []
    assert (await http.get("/v1/approvals")).json() == []


@pytest.mark.db
async def test_send_without_a_connected_mailbox_is_409(api: Api, rfq_rows: People) -> None:
    rfq = (await api.http.post("/v1/rfqs", json={"text": "Need parts"})).json()
    api.mailboxes.disconnected.add(rfq_rows.owner)

    response = await api.http.post(f"/v1/rfqs/{rfq['id']}/send")

    assert response.status_code == 409
    assert "mailbox" in response.json()["detail"]
