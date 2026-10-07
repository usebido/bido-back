"""`/v1/rfqs/{rfq_id}/followups` over the ASGI transport."""

from collections.abc import AsyncIterator
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.api.deps import ApiResources, get_db, get_principal
from bido_back.api.followups import followups_router
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
    app.include_router(followups_router())
    return app


async def test_followup_routes_require_authentication() -> None:
    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        responses = [
            await http.get(f"/v1/rfqs/{uuid4()}/followups"),
            await http.patch(f"/v1/rfqs/{uuid4()}/followups", json={"enabled": False}),
        ]
    assert [r.status_code for r in responses] == [401, 401]


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
async def test_get_and_patch_followup_settings(
    http: httpx.AsyncClient, app_engine: AsyncEngine, rfq_rows: People
) -> None:
    principal = Principal(tenant_id=rfq_rows.tenant, user_id=rfq_rows.owner, role="owner")
    rfq = await rfq_service.create_rfq_from_text(app_engine, principal, "Need caps", FakeIntake())

    got = await http.get(f"/v1/rfqs/{rfq.id}/followups")
    assert got.status_code == 200
    assert got.json()["settings"] == {
        "enabled": True,
        "first_after_business_days": 2,
        "every_business_days": 2,
        "max_chases": 3,
        "response_due": "2026-11-01",
    }

    patched = await http.patch(
        f"/v1/rfqs/{rfq.id}/followups",
        json={"max_chases": 1, "every_business_days": 3, "response_due": None},
    )
    assert patched.status_code == 200
    settings = patched.json()["settings"]
    assert (settings["max_chases"], settings["every_business_days"]) == (1, 3)
    assert settings["response_due"] is None and settings["first_after_business_days"] == 2

    assert (
        await http.patch(f"/v1/rfqs/{rfq.id}/followups", json={"max_chases": 99})
    ).status_code == 422
    assert (
        await http.patch(f"/v1/rfqs/{rfq.id}/followups", json={"surprise": 1})
    ).status_code == 422
    assert (await http.get(f"/v1/rfqs/{uuid4()}/followups")).status_code == 404
