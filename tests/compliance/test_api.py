"""Compliance routes over the ASGI transport, against Postgres.

The app is assembled from the routers; authentication and the database are
dependency overrides, as in tests/rfqs/test_api.py.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.api.compliance import compliance_router
from bido_back.api.deps import ApiResources, get_db, get_principal
from bido_back.api.rfqs import rfqs_router
from bido_back.auth import Principal
from bido_back.db import create_sessionmaker, tenant_session
from bido_back.db.models import Attachment, AuditEvent, Message
from bido_back.rfqs import service
from bido_back.rfqs.schemas import UpdateLineRequest
from bido_back.settings import Settings
from tests.approvals.conftest import People
from tests.rfqs.test_service import buyer, make_rfq, make_supplier

UNUSED_DB = "postgresql+asyncpg://app@localhost:5432/unused"


def build_app() -> FastAPI:
    app = FastAPI()
    app.state.resources = ApiResources(
        Settings.model_validate({"environment": "test", "database_url": UNUSED_DB})
    )
    app.include_router(compliance_router())
    app.include_router(rfqs_router())
    return app


async def test_routes_require_authentication() -> None:
    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        responses = [
            await http.get("/v1/compliance/requirements"),
            await http.put("/v1/compliance/requirements", json={"requirements": []}),
            await http.get(f"/v1/suppliers/{uuid4()}/compliance"),
        ]
    assert [r.status_code for r in responses] == [401] * len(responses)


@dataclass
class Api:
    http: httpx.AsyncClient
    principal: Principal


@pytest_asyncio.fixture
async def api(app_engine: AsyncEngine, compliance_rows: People) -> AsyncIterator[Api]:
    app = build_app()
    sessionmaker = create_sessionmaker(app_engine)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        state = Api(http=http, principal=buyer(compliance_rows))

        def principal() -> Principal:
            return state.principal

        def db() -> async_sessionmaker[AsyncSession]:
            return sessionmaker

        app.dependency_overrides[get_principal] = principal
        app.dependency_overrides[get_db] = db
        yield state


async def receive_attachment(
    engine: AsyncEngine, tenant_id: UUID, invite_id: UUID, filename: str
) -> UUID:
    async with tenant_session(engine, tenant_id) as session:
        message = Message(
            tenant_id=tenant_id,
            invite_id=invite_id,
            direction="in",
            message_id_header=f"<{uuid4()}@supplier.example.test>",
            occurred_at=datetime.now(UTC),
        )
        session.add(message)
        await session.flush()
        attachment = Attachment(
            tenant_id=tenant_id,
            message_id=message.id,
            filename=filename,
            content_type="application/pdf",
            size_bytes=1234,
            storage_key=f"test/{uuid4()}",
        )
        session.add(attachment)
        await session.flush()
        return attachment.id


@pytest.mark.db
async def test_requirements_default_then_edited_by_an_approver_only(
    api: Api, compliance_rows: People, app_engine: AsyncEngine
) -> None:
    http = api.http
    defaults = (await http.get("/v1/compliance/requirements")).json()
    assert defaults["customized"] is False
    assert [r["code"] for r in defaults["requirements"]][:2] == ["rohs", "reach_svhc"]

    new_set = {
        "requirements": [
            {"code": "rohs", "name": "RoHS", "scope": "supplier", "keywords": ["RoHS"]},
            {
                "code": "ppap",
                "name": "PPAP",
                "scope": "line",
                "line_keyword": "ppap",
                "blocking": True,
            },
        ]
    }
    api.principal = Principal(
        tenant_id=compliance_rows.tenant, user_id=compliance_rows.member, role="member"
    )
    assert (await http.put("/v1/compliance/requirements", json=new_set)).status_code == 403

    api.principal = buyer(compliance_rows)  # owner
    saved = await http.put("/v1/compliance/requirements", json=new_set)
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["customized"] is True
    assert [(r["code"], r["keywords"]) for r in body["requirements"]] == [
        ("rohs", ["rohs"]),
        ("ppap", []),
    ]

    bad = {"requirements": [{"code": "aec_q", "name": "AEC-Q", "scope": "line"}]}
    assert (await http.put("/v1/compliance/requirements", json=bad)).status_code == 422
    dup = {"requirements": [new_set["requirements"][0], new_set["requirements"][0]]}
    assert (await http.put("/v1/compliance/requirements", json=dup)).status_code == 422

    # The other tenant still sees the defaults.
    api.principal = Principal(
        tenant_id=compliance_rows.other_tenant,
        user_id=compliance_rows.other_approver,
        role="approver",
    )
    assert (await http.get("/v1/compliance/requirements")).json()["customized"] is False

    async with tenant_session(app_engine, compliance_rows.tenant) as session:
        actions = list(
            await session.scalars(
                select(AuditEvent.action).where(AuditEvent.tenant_id == compliance_rows.tenant)
            )
        )
    assert actions.count("compliance.requirements_updated") == 1


@pytest.mark.db
async def test_checklist_candidates_documents_and_comparison(
    api: Api, compliance_rows: People, app_engine: AsyncEngine
) -> None:
    http = api.http
    principal = buyer(compliance_rows)
    rfq = await make_rfq(app_engine, principal)
    line = rfq.lines[0]
    await service.update_line(
        app_engine, principal, rfq.id, line.id, UpdateLineRequest(qualification="AEC-Q200")
    )
    supplier = await make_supplier(app_engine, principal, "Rhein")
    other = await make_supplier(app_engine, principal, "Lyon")
    invite = await service.add_invite(
        app_engine, principal, rfq.id, supplier.id, supplier.contacts[0].id
    )
    other_invite = await service.add_invite(
        app_engine, principal, rfq.id, other.id, other.contacts[0].id
    )
    report = await receive_attachment(
        app_engine, compliance_rows.tenant, invite.id, "AEC-Q200_report_GRM188.pdf"
    )
    lyon_rohs = await receive_attachment(
        app_engine, compliance_rows.tenant, other_invite.id, "RoHS.pdf"
    )

    checklist = (await http.get(f"/v1/suppliers/{supplier.id}/compliance")).json()
    items = {i["code"]: i for i in checklist["items"]}
    assert items["aec_q"]["rfq_line_id"] == str(line.id)
    assert items["aec_q"]["status"] == "missing"
    assert [c["attachment_id"] for c in items["aec_q"]["candidates"]] == [str(report)]
    assert items["rohs"]["candidates"] == []  # Lyon's RoHS file is not Rhein's

    documents = f"/v1/suppliers/{supplier.id}/compliance/documents"
    # Line-scoped needs the line; another supplier's attachment is not found.
    assert (
        await http.post(documents, json={"requirement_code": "aec_q", "attachment_id": str(report)})
    ).status_code == 422
    assert (
        await http.post(
            documents,
            json={
                "requirement_code": "rohs",
                "attachment_id": str(lyon_rohs),
            },
        )
    ).status_code == 404
    assert (
        await http.post(documents, json={"requirement_code": "nope", "url": "https://x.test/a"})
    ).status_code == 422
    assert (
        await http.post(documents, json={"requirement_code": "rohs", "url": "http://x.test/a"})
    ).status_code == 422

    confirmed = await http.post(
        documents,
        json={
            "requirement_code": "aec_q",
            "rfq_line_id": str(line.id),
            "attachment_id": str(report),
        },
    )
    assert confirmed.status_code == 201, confirmed.text
    assert confirmed.json()["filename"] == "AEC-Q200_report_GRM188.pdf"
    rohs = await http.post(
        documents,
        json={
            "requirement_code": "rohs",
            "url": "https://rhein.example.test/rohs.pdf",
            "issued_on": "2024-01-10",
        },
    )
    assert rohs.status_code == 201, rohs.text
    assert rohs.json()["expires_on"] == "2025-01-10"  # 12-month validity

    after = (await http.get(f"/v1/suppliers/{supplier.id}/compliance?rfq_id={rfq.id}")).json()
    statuses = {i["code"]: i["status"] for i in after["items"]}
    assert statuses["aec_q"] == "received"
    assert statuses["rohs"] == "expired"
    assert after["counts"] == {"received": 1, "missing": 3, "expired": 1}

    comparison = (await http.get(f"/v1/rfqs/{rfq.id}/comparison")).json()
    summaries = {s["supplier_name"]: s for s in comparison["compliance"]}
    assert [b["code"] for b in summaries["Rhein"]["blocking"]] == ["rohs", "reach_svhc"]
    assert [b["code"] for b in summaries["Lyon"]["blocking"]] == ["rohs", "reach_svhc", "aec_q"]

    # Another tenant cannot see the supplier.
    api.principal = Principal(
        tenant_id=compliance_rows.other_tenant,
        user_id=compliance_rows.other_approver,
        role="approver",
    )
    assert (await http.get(f"/v1/suppliers/{supplier.id}/compliance")).status_code == 404
    assert (
        await http.post(documents, json={"requirement_code": "rohs", "url": "https://x.test/a"})
    ).status_code == 404
