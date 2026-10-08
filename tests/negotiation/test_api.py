"""`/v1/rfqs/{rfq_id}/negotiation` over the ASGI transport."""

from collections.abc import AsyncIterator
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.api.deps import ApiResources, get_db, get_principal
from bido_back.api.negotiation import negotiation_router
from bido_back.auth import Principal
from bido_back.db import create_sessionmaker
from bido_back.rfqs import service as rfq_service
from bido_back.settings import Settings
from tests.approvals.conftest import People
from tests.rfqs.conftest import FakeIntake

UNUSED_DB = "postgresql+asyncpg://app@localhost:5432/unused"


def build_app() -> FastAPI:
    app = FastAPI()
    app.state.resources = ApiResources(
        Settings.model_validate({"environment": "test", "database_url": UNUSED_DB})
    )
    app.include_router(negotiation_router())
    return app


async def test_negotiation_routes_require_authentication() -> None:
    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        responses = [
            await http.get(f"/v1/rfqs/{uuid4()}/negotiation"),
            await http.put(f"/v1/rfqs/{uuid4()}/negotiation", json={}),
            await http.post(f"/v1/rfqs/{uuid4()}/negotiation/close"),
        ]
    assert [r.status_code for r in responses] == [401, 401, 401]


@pytest_asyncio.fixture
async def http(app_engine: AsyncEngine, rfq_rows: People) -> AsyncIterator[httpx.AsyncClient]:
    app = build_app()
    sessionmaker = create_sessionmaker(app_engine)
    principal = Principal(tenant_id=rfq_rows.tenant, user_id=rfq_rows.owner, role="owner")

    def db() -> async_sessionmaker[AsyncSession]:
        return sessionmaker

    def current() -> Principal:
        return principal

    app.dependency_overrides[get_db] = db
    app.dependency_overrides[get_principal] = current
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.mark.db
async def test_set_up_read_and_close(
    http: httpx.AsyncClient, app_engine: AsyncEngine, rfq_rows: People
) -> None:
    principal = Principal(tenant_id=rfq_rows.tenant, user_id=rfq_rows.owner, role="owner")
    rfq = await rfq_service.create_rfq_from_text(
        app_engine, principal, "MLCC", FakeIntake(), currency="EUR"
    )
    url = f"/v1/rfqs/{rfq.id}/negotiation"

    initial = await http.get(url)
    assert initial.status_code == 200 and initial.json()["status"] == "not_started"
    line_id = initial.json()["settings"]["lines"][0]["rfq_line_id"]

    bad = await http.put(
        url,
        json={"lines": [{"rfq_line_id": line_id, "target_unit_price": 2, "max_unit_price": 1}]},
    )
    assert bad.status_code == 422

    saved = await http.put(
        url,
        json={
            "deadline": "2026-12-01T12:00:00Z",
            "max_rounds": 2,
            "lines": [{"rfq_line_id": line_id, "target_unit_price": "0.04"}],
        },
    )
    assert saved.status_code == 200
    body = saved.json()
    assert (body["status"], body["settings"]["max_rounds"]) == ("open", 2)
    assert body["settings"]["lines"][0]["target_unit_price"] == "0.040000"

    closed = await http.post(f"{url}/close")
    assert closed.status_code == 200
    assert closed.json()["status"] == "closed" and closed.json()["outcome"] is not None
    assert (await http.put(url, json={})).status_code == 409
    assert (await http.get(f"/v1/rfqs/{uuid4()}/negotiation")).status_code == 404
