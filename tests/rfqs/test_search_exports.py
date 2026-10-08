"""Search, exports and the proposal routes against Postgres, over HTTP where it matters."""

import csv
import io
from collections.abc import AsyncIterator
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.api.deps import ApiResources, get_db, get_principal
from bido_back.api.exports import exports_router
from bido_back.api.proposals import proposals_router
from bido_back.api.search import search_router
from bido_back.auth import Principal
from bido_back.db import create_sessionmaker, tenant_session
from bido_back.db.models import Rfq
from bido_back.search.service import search
from bido_back.settings import Settings
from tests.approvals.conftest import People

from .test_corrections import quoted_rfq
from .test_service import buyer

pytestmark = pytest.mark.db

UNUSED_DB = "postgresql+asyncpg://app@localhost:5432/unused"


async def test_search_finds_each_kind_in_the_tenant_only(
    app_engine: AsyncEngine, rfq_rows: People
) -> None:
    principal = buyer(rfq_rows)
    quoted = await quoted_rfq(app_engine, principal)
    mpn = quoted.rfq.lines[0].mpn
    assert mpn is not None

    by_mpn = await search(app_engine, principal, mpn.lower())
    assert {hit.kind for hit in by_mpn.items} == {"rfq_line", "quoted_part"}
    part = next(hit for hit in by_mpn.items if hit.kind == "quoted_part")
    assert part.rfq_id == quoted.rfq.id and part.matched == "offered_mpn"
    assert part.detail is not None and part.detail.startswith("Rhein")

    by_ref = await search(app_engine, principal, quoted.rfq.ref)
    assert by_ref.items[0].kind == "rfq" and by_ref.items[0].matched == "ref"

    suppliers = await search(app_engine, principal, "rhein")
    assert [hit.kind for hit in suppliers.items] == ["supplier"]

    # Wildcards are literal, and another tenant sees nothing.
    assert (await search(app_engine, principal, "%%")).total == 0
    outsider = Principal(tenant_id=rfq_rows.other_tenant, user_id=rfq_rows.other_approver)
    assert (await search(app_engine, outsider, mpn)).total == 0

    paged = await search(app_engine, principal, mpn, limit=1, offset=1)
    assert paged.total == 2 and len(paged.items) == 1 and paged.items[0].kind == "quoted_part"


def build_app() -> FastAPI:
    app = FastAPI()
    app.state.resources = ApiResources(
        Settings.model_validate({"environment": "test", "database_url": UNUSED_DB})
    )
    for router in (proposals_router(), search_router(), exports_router()):
        app.include_router(router)
    return app


async def test_routes_require_authentication() -> None:
    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        responses = [
            await http.get(f"/v1/proposals/{uuid4()}"),
            await http.patch(f"/v1/proposals/{uuid4()}", json={"currency": "EUR"}),
            await http.get("/v1/search", params={"q": "abc"}),
            await http.get("/v1/rfqs.csv"),
            await http.get(f"/v1/rfqs/{uuid4()}/comparison.xlsx"),
        ]
    assert [r.status_code for r in responses] == [401] * len(responses)


@pytest_asyncio.fixture
async def http(app_engine: AsyncEngine, rfq_rows: People) -> AsyncIterator[httpx.AsyncClient]:
    app = build_app()
    sessionmaker = create_sessionmaker(app_engine)

    def principal() -> Principal:
        return buyer(rfq_rows)

    def db() -> async_sessionmaker[AsyncSession]:
        return sessionmaker

    app.dependency_overrides[get_principal] = principal
    app.dependency_overrides[get_db] = db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_correct_then_export_over_http(
    http: httpx.AsyncClient, app_engine: AsyncEngine, rfq_rows: People
) -> None:
    quoted = await quoted_rfq(app_engine, buyer(rfq_rows))
    base = f"/v1/proposals/{quoted.proposal_id}"

    fixed = await http.patch(
        f"{base}/lines/{quoted.line_id}", json={"unit_price": "0.40", "reason": "typo"}
    )
    assert fixed.status_code == 200, fixed.text
    assert fixed.json()["corrected_fields"] == ["unit_price"]
    header = await http.patch(base, json={"currency": "EUR"})
    assert header.status_code == 200 and header.json()["needs_review"] is False

    assert (await http.patch(base, json={})).status_code == 422
    assert (await http.patch(base, json={"incoterm": "XYZ"})).status_code == 422
    assert (await http.patch(base, json={"currency": "eur"})).status_code == 422
    assert (await http.get(f"/v1/proposals/{uuid4()}")).status_code == 404

    history = (await http.get(f"{base}/corrections")).json()
    assert [(h["field"], h["action"]) for h in history] == [
        ("unit_price", "corrected"),
        ("currency", "corrected"),
    ]

    comparison = await http.get(f"/v1/rfqs/{quoted.rfq.id}/comparison.csv")
    assert comparison.status_code == 200
    assert comparison.headers["content-type"].startswith("text/csv")
    assert comparison.headers["content-disposition"] == (
        f'attachment; filename="{quoted.rfq.ref}-comparison.csv"'
    )
    rows = list(csv.DictReader(io.StringIO(comparison.content.decode("utf-8-sig"))))
    first = next(r for r in rows if r["Quantity"] == "4000")
    assert first["Unit price"] == "0.400000"
    assert first["Corrected fields"] == "unit_price"
    assert first["Line total (base)"] == "1600.00"

    async with tenant_session(app_engine, rfq_rows.tenant) as session:
        await session.execute(
            update(Rfq)
            .where(Rfq.id == quoted.rfq.id)
            .values(payment_terms="28 DDL", quote_validity_days=30)
        )
    listing = await http.get("/v1/rfqs.csv")
    [row] = [
        r
        for r in csv.DictReader(io.StringIO(listing.content.decode("utf-8-sig")))
        if r["RFQ"] == quoted.rfq.ref
    ]
    assert row["Responses"] == "0/1" and row["Currency"] == "EUR"
    assert row["Suppliers"].startswith("Rhein")
    # Line 2 of the RFQ is not quoted, so no supplier is complete: no landed cost.
    assert row["Landed cost"] == ""
    assert (row["Payment terms"], row["Quote validity (days)"]) == ("28 DDL", "30")

    xlsx = await http.get(f"/v1/rfqs/{quoted.rfq.id}/comparison.xlsx")
    assert xlsx.status_code == 200 and xlsx.content[:2] == b"PK"
    assert (await http.get(f"/v1/rfqs/{quoted.rfq.id}/comparison.pdf")).status_code == 422
    assert (await http.get(f"/v1/rfqs/{uuid4()}/comparison.csv")).status_code == 404

    found = await http.get("/v1/search", params={"q": quoted.rfq.ref, "limit": 5})
    assert found.status_code == 200 and found.json()["items"][0]["id"] == str(quoted.rfq.id)
    assert (await http.get("/v1/search", params={"q": "x"})).status_code == 422
    assert Decimal(first["Unit price (base)"]) == Decimal("0.400000")
