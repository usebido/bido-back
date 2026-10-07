"""`/v1/sandbox` over the ASGI transport."""

from collections.abc import AsyncIterator, Callable
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.api.deps import ApiResources, get_db, get_principal, get_reply_schedulers
from bido_back.api.sandbox import sandbox_router
from bido_back.auth import Principal
from bido_back.db import create_sessionmaker, tenant_session
from bido_back.email.types import OutboundEmail
from bido_back.rfqs import suppliers
from bido_back.rfqs.schemas import ContactIn, CreateSupplierRequest
from bido_back.settings import Settings
from bido_back.simulator.sender import ReplyScheduler, SandboxEmailSender
from tests.approvals.conftest import People
from tests.simulator.test_simulator_db import FakeScheduler, make_sandbox

UNUSED_DB = "postgresql+asyncpg://app@localhost:5432/unused"


def build_app() -> FastAPI:
    app = FastAPI()
    app.state.resources = ApiResources(
        Settings.model_validate({"environment": "test", "database_url": UNUSED_DB})
    )
    app.include_router(sandbox_router())
    return app


async def test_sandbox_routes_require_authentication() -> None:
    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.get("/v1/sandbox/personas")
    assert response.status_code == 401


class Client:
    def __init__(self, http: httpx.AsyncClient, scheduler: FakeScheduler) -> None:
        self.http = http
        self.scheduler = scheduler


@pytest_asyncio.fixture
async def client(app_engine: AsyncEngine, sandbox_rows: People) -> AsyncIterator[Client]:
    app = build_app()
    sessionmaker = create_sessionmaker(app_engine)
    principal = Principal(tenant_id=sandbox_rows.tenant, user_id=sandbox_rows.owner, role="owner")
    scheduler = FakeScheduler()

    def db() -> async_sessionmaker[AsyncSession]:
        return sessionmaker

    def current() -> Principal:
        return principal

    def schedulers() -> Callable[[], ReplyScheduler]:
        return lambda: scheduler

    app.dependency_overrides[get_db] = db
    app.dependency_overrides[get_principal] = current
    app.dependency_overrides[get_reply_schedulers] = schedulers
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield Client(http, scheduler)


@pytest.mark.db
async def test_a_real_tenant_gets_404_everywhere(client: Client) -> None:
    responses = [
        await client.http.get("/v1/sandbox/personas"),
        await client.http.put(f"/v1/sandbox/personas/{uuid4()}", json={"language": "it"}),
        await client.http.get("/v1/sandbox/outbound"),
        await client.http.post(f"/v1/sandbox/outbound/{uuid4()}/reply-now"),
        await client.http.post("/v1/sandbox/reset"),
    ]
    assert [r.status_code for r in responses] == [404] * 5


@pytest.mark.db
async def test_personas_outbox_reply_now_and_reset(
    client: Client, app_engine: AsyncEngine, migrator_engine: AsyncEngine, sandbox_rows: People
) -> None:
    await make_sandbox(migrator_engine, sandbox_rows.tenant)
    principal = Principal(tenant_id=sandbox_rows.tenant, user_id=sandbox_rows.owner, role="owner")
    supplier = await suppliers.create_supplier(
        app_engine,
        principal,
        CreateSupplierRequest(
            name="Rame Italia S.r.l.",
            language="it",
            contacts=[ContactIn(name="Giulia", email="giulia@rame.example.test")],
        ),
    )

    put = await client.http.put(
        f"/v1/sandbox/personas/{supplier.id}",
        json={"language": "it", "behaviors": ["tough_negotiator"], "quote_format": "pdf"},
    )
    assert put.status_code == 200
    listed = (await client.http.get("/v1/sandbox/personas")).json()
    assert [(p["supplier_name"], p["persona"]["behaviors"]) for p in listed] == [
        ("Rame Italia S.r.l.", ["tough_negotiator"])
    ]
    bad = await client.http.put(f"/v1/sandbox/personas/{supplier.id}", json={"language": "xx"})
    assert bad.status_code == 422

    async with tenant_session(app_engine, sandbox_rows.tenant) as session:
        sent = await SandboxEmailSender(
            session, sandbox_rows.tenant, lambda: client.scheduler
        ).send(
            OutboundEmail(
                from_name="Buyer",
                from_address="buyer@example.test",
                to=("giulia@rame.example.test",),
                subject="[RFQ-9] Rame",
                text_body="Quote please",
            )
        )
    [outbound] = (await client.http.get("/v1/sandbox/outbound")).json()
    assert outbound["status"] == "pending" and outbound["subject"] == "[RFQ-9] Rame"
    assert sent.message_id.startswith("<")

    now = await client.http.post(f"/v1/sandbox/outbound/{outbound['id']}/reply-now")
    assert now.status_code == 200
    assert client.scheduler.calls[-1] == (sandbox_rows.tenant, UUID(outbound["id"]), 0)

    reset = (await client.http.post("/v1/sandbox/reset")).json()
    # "Rame Italia S.r.l." is a demo supplier: its demo persona is restored.
    assert reset == {"cancelled": 1, "personas_restored": 1}
    restored = (await client.http.get("/v1/sandbox/personas")).json()
    assert restored[0]["persona"]["behaviors"] == ["cheap_but_late"]
    again = await client.http.post(f"/v1/sandbox/outbound/{outbound['id']}/reply-now")
    assert again.status_code == 409
