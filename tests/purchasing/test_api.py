"""Purchasing routes and copilot tools over Postgres (skipped without it)."""

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.agents.context import ToolContext
from bido_back.agents.tools import AnyTool, ToolError
from bido_back.api.deps import ApiResources, get_db, get_principal, get_storage
from bido_back.api.onboarding import onboarding_router
from bido_back.api.purchasing import purchasing_router
from bido_back.auth import Principal
from bido_back.copilot.tools import (
    CopilotToolDeps,
    PurchaseAnalysisIn,
    PurchaseAnalysisOut,
    RfqFromAnalysisIn,
    RfqFromAnalysisOut,
    copilot_tools,
)
from bido_back.db import create_sessionmaker
from bido_back.settings import Settings
from bido_back.storage import BlobStorage
from tests.approvals.conftest import People
from tests.purchasing.conftest import FakeStorage, xlsx
from tests.purchasing.test_service import BOM, TODAY, make_supplier, seed
from tests.rfqs.conftest import FakeMailboxes, fake_agents
from tests.rfqs.test_service import buyer

UNUSED_DB = "postgresql+asyncpg://app@localhost:5432/unused"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def build_app() -> FastAPI:
    app = FastAPI()
    app.state.resources = ApiResources(
        Settings.model_validate({"environment": "test", "database_url": UNUSED_DB})
    )
    app.include_router(onboarding_router())
    app.include_router(purchasing_router())
    return app


async def test_routes_require_authentication() -> None:
    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        responses = [
            await http.post("/v1/data/imports", json={"type": "bom", "upload_id": str(uuid4())}),
            await http.get("/v1/data/products"),
            await http.post("/v1/analyses", json={"product": "motor"}),
            await http.get(f"/v1/analyses/{uuid4()}"),
            await http.post(f"/v1/analyses/{uuid4()}/rfq"),
        ]
    assert [r.status_code for r in responses] == [401] * len(responses)


@pytest_asyncio.fixture
async def http(
    app_engine: AsyncEngine, purchasing_rows: People
) -> AsyncIterator[httpx.AsyncClient]:
    app = build_app()
    sessionmaker = create_sessionmaker(app_engine)
    storage = FakeStorage()

    def principal() -> Principal:
        return buyer(purchasing_rows)

    def db() -> async_sessionmaker[AsyncSession]:
        return sessionmaker

    def blobs() -> BlobStorage:
        return storage

    app.dependency_overrides[get_principal] = principal
    app.dependency_overrides[get_db] = db
    app.dependency_overrides[get_storage] = blobs
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.mark.db
async def test_import_analyse_and_draft_over_http(http: httpx.AsyncClient) -> None:
    upload = await http.post(
        "/v1/onboarding/uploads",
        params={"filename": "distinta.xlsx"},
        content=xlsx(BOM),
        headers={"content-type": XLSX},
    )
    assert upload.status_code == 201, upload.text
    upload_id = upload.json()["id"]

    dry = await http.post(
        "/v1/data/imports", json={"type": "bom", "upload_id": upload_id, "dry_run": True}
    )
    done = await http.post("/v1/data/imports", json={"type": "bom", "upload_id": upload_id})
    assert dry.json()["rows_imported"] == 0 and dry.json()["rows_total"] == 3
    assert done.json()["created"] == {"items": 3, "bom_lines": 3}
    assert done.json()["columns"]["qty_per_unit"] == "Quantità per unità"

    # Explicit zero inventory establishes a balance; absent stock is unknown.
    stock_upload = await http.post(
        "/v1/onboarding/uploads",
        params={"filename": "stock.xlsx"},
        content=xlsx([["Part number", "On hand"], ["BRG-6205", 0], ["CU-WIRE", 0], ["HSG-AL", 0]]),
        headers={"content-type": XLSX},
    )
    stock_import = await http.post(
        "/v1/data/imports",
        json={"type": "inventory", "upload_id": stock_upload.json()["id"]},
    )
    assert stock_import.status_code == 200

    no_plan = await http.post("/v1/analyses", json={"product": "motor"})
    assert no_plan.status_code == 422
    unknown = await http.post("/v1/analyses", json={"product": "turbine", "quantity": 5})
    assert unknown.status_code == 404

    created = await http.post("/v1/analyses", json={"product": "motor", "quantity": 10})
    assert created.status_code == 201, created.text
    analysis = created.json()
    assert [(line["part_number"], line["to_buy"]) for line in analysis["lines"]] == [
        ("BRG-6205", "20"),
        ("CU-WIRE", "12.5"),
        ("HSG-AL", "10"),
    ]
    fetched = await http.get(f"/v1/analyses/{analysis['id']}")
    assert fetched.json() == analysis

    rfq = await http.post(f"/v1/analyses/{analysis['id']}/rfq")
    assert rfq.status_code == 201, rfq.text
    assert len(rfq.json()["rfq"]["lines"]) == 3
    again = await http.post(f"/v1/analyses/{analysis['id']}/rfq", json={})
    assert again.status_code == 409


def tool(tools: tuple[AnyTool, ...], name: str) -> AnyTool:
    return next(t for t in tools if t.name == name)


@pytest.mark.db
async def test_copilot_tools(app_engine: AsyncEngine, purchasing_rows: People) -> None:
    who = buyer(purchasing_rows)
    await make_supplier(app_engine, who, "Cuscinetti Precisione S.r.l.")
    await seed(app_engine, who)
    tools = copilot_tools(
        CopilotToolDeps(
            db=app_engine, agents=fake_agents(), mailboxes=FakeMailboxes(), today=lambda: TODAY
        )
    )
    ctx = ToolContext(tenant_id=who.tenant_id, user_id=who.user_id, run_id=uuid4())
    analyse = tool(tools, "run_purchase_analysis")
    draft = tool(tools, "create_rfq_from_analysis")

    result = await analyse.invoke(ctx, analyse.parse_input({"product": "motor"}))
    assert isinstance(result, PurchaseAnalysisOut)
    assert (result.product, result.items_to_buy) == ("Motor EV-120", 2)
    bearing = result.lines[0]
    assert (bearing.part_number, bearing.suppliers[0].name) == (
        "BRG-6205",
        "Cuscinetti Precisione S.r.l.",
    )
    assert analyse.kind == "draft" and draft.kind == "draft"
    assert isinstance(analyse.parse_input({"product": "x"}), PurchaseAnalysisIn)

    out = await draft.invoke(ctx, draft.parse_input({"analysis_id": str(result.analysis_id)}))
    assert isinstance(out, RfqFromAnalysisOut)
    assert out.status == "draft" and out.invited_suppliers == ["Cuscinetti Precisione S.r.l."]
    assert isinstance(RfqFromAnalysisIn(analysis_id=UUID(int=1)), RfqFromAnalysisIn)


@pytest.mark.db
async def test_a_miscopied_analysis_id_uses_the_latest_open_analysis(
    app_engine: AsyncEngine, purchasing_rows: People
) -> None:
    who = buyer(purchasing_rows)
    await make_supplier(app_engine, who, "Cuscinetti Precisione S.r.l.")
    await seed(app_engine, who)
    tools = copilot_tools(
        CopilotToolDeps(
            db=app_engine, agents=fake_agents(), mailboxes=FakeMailboxes(), today=lambda: TODAY
        )
    )
    ctx = ToolContext(tenant_id=who.tenant_id, user_id=who.user_id, run_id=uuid4())
    analyse = tool(tools, "run_purchase_analysis")
    draft = tool(tools, "create_rfq_from_analysis")
    await analyse.invoke(ctx, analyse.parse_input({"product": "motor"}))

    out = await draft.invoke(ctx, draft.parse_input({"analysis_id": str(uuid4())}))

    assert isinstance(out, RfqFromAnalysisOut) and out.status == "draft"
    # The fallback is used up: that analysis now has its RFQ.
    with pytest.raises(ToolError, match="not found"):
        await draft.invoke(ctx, draft.parse_input({"analysis_id": str(uuid4())}))
