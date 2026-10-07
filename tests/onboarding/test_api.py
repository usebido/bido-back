"""`/v1/onboarding` over the ASGI transport, with fake queue and storage."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.api.deps import ApiResources, get_db, get_job_queue, get_principal, get_storage
from bido_back.api.onboarding import onboarding_router
from bido_back.auth import Principal
from bido_back.db import create_sessionmaker, tenant_session
from bido_back.db.models import TemplateScan
from bido_back.onboarding.candidates import StoredCandidate
from bido_back.settings import Settings
from bido_back.storage import BlobStorage
from bido_back.worker.job_types import TEMPLATE_BUILD, TEMPLATE_SCAN
from bido_back.worker.jobs import JobQueue
from tests.documents.fixtures import build_docx

from .conftest import FakeQueue, FakeStorage, Onboarding

DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
UNUSED_DB = "postgresql+asyncpg://app@localhost:5432/unused"


def build_app() -> FastAPI:
    app = FastAPI()
    app.state.resources = ApiResources(
        Settings.model_validate({"environment": "test", "database_url": UNUSED_DB})
    )
    app.include_router(onboarding_router())
    return app


async def test_routes_require_authentication() -> None:
    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        responses = [
            await http.get("/v1/onboarding"),
            await http.post("/v1/onboarding/scans"),
            await http.post("/v1/onboarding/uploads?filename=a.pdf", content=b"%PDF-"),
            await http.post("/v1/onboarding/templates", json={"source": "none"}),
            await http.post("/v1/onboarding/complete"),
        ]
    assert [r.status_code for r in responses] == [401] * len(responses)


@dataclass
class Api:
    http: httpx.AsyncClient
    queue: FakeQueue
    storage: FakeStorage
    principal: Principal


@pytest_asyncio.fixture
async def api(app_engine: AsyncEngine, onboarding_rows: Onboarding) -> AsyncIterator[Api]:
    app = build_app()
    sessionmaker = create_sessionmaker(app_engine)
    queue = FakeQueue()
    storage = FakeStorage()
    people = onboarding_rows.people
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        state = Api(
            http=http,
            queue=queue,
            storage=storage,
            principal=Principal(tenant_id=people.tenant, user_id=people.owner, role="owner"),
        )

        def principal() -> Principal:
            return state.principal

        def db() -> async_sessionmaker[AsyncSession]:
            return sessionmaker

        def jobs() -> JobQueue:
            return queue

        def blobs() -> BlobStorage:
            return storage

        app.dependency_overrides[get_principal] = principal
        app.dependency_overrides[get_db] = db
        app.dependency_overrides[get_job_queue] = jobs
        app.dependency_overrides[get_storage] = blobs
        yield state


@pytest.mark.db
async def test_standard_template_onboarding(api: Api, onboarding_rows: Onboarding) -> None:
    http = api.http
    state = (await http.get("/v1/onboarding")).json()
    assert state["onboarded_at"] is None and state["default_template"] is None
    assert state["mailbox"] == {"provider": "google", "email_address": "owner@acme.example"}

    created = await http.post("/v1/onboarding/templates", json={"source": "none"})
    assert created.status_code == 202, created.text
    template = created.json()
    assert template["status"] == "ready" and template["source"] == "none"
    assert template["columns"] == ["Item", "Qtd.", "Un.", "Prazo"]
    assert template["footer"] == "Validade da proposta · Condição de pagamento"
    assert api.queue.envelopes == []

    used = await http.post(f"/v1/onboarding/templates/{template['id']}/use")
    assert used.status_code == 200 and used.json()["is_default"] is True

    # A second template taken as default replaces the first one.
    second = (await http.post("/v1/onboarding/templates", json={"source": "none"})).json()
    await http.post(f"/v1/onboarding/templates/{second['id']}/use")
    assert (await http.get(f"/v1/onboarding/templates/{template['id']}")).json()[
        "is_default"
    ] is False

    done = await http.post("/v1/onboarding/complete")
    assert done.status_code == 200
    assert done.json()["onboarded_at"] is not None
    assert done.json()["default_template"]["id"] == second["id"]

    # Templates are the tenant's, invisible to another tenant.
    people = onboarding_rows.people
    api.principal = Principal(tenant_id=people.other_tenant, user_id=people.other_approver)
    assert (await http.get(f"/v1/onboarding/templates/{template['id']}")).status_code == 404


@pytest.mark.db
async def test_scan_needs_a_mailbox_and_is_reused_while_running(
    api: Api, onboarding_rows: Onboarding
) -> None:
    http = api.http
    started = await http.post("/v1/onboarding/scans", json={})
    assert started.status_code == 202, started.text
    scan = started.json()
    assert scan["status"] == "running" and scan["limit"] == 1500
    [envelope] = api.queue.envelopes
    assert envelope.type == TEMPLATE_SCAN and envelope.payload == {"scan_id": scan["id"]}
    assert envelope.idempotency_key == f"{TEMPLATE_SCAN}:{scan['id']}"

    again = await http.post("/v1/onboarding/scans")
    assert again.json()["id"] == scan["id"] and len(api.queue.envelopes) == 1

    # Another user of the tenant has no mailbox, and cannot see this scan.
    people = onboarding_rows.people
    api.principal = Principal(tenant_id=people.tenant, user_id=people.approver, role="approver")
    assert (await http.post("/v1/onboarding/scans")).status_code == 409
    assert (await http.get(f"/v1/onboarding/scans/{scan['id']}")).status_code == 404


@pytest.mark.db
async def test_template_from_scan_candidates(
    api: Api, app_engine: AsyncEngine, onboarding_rows: Onboarding
) -> None:
    http = api.http
    scan = (await http.post("/v1/onboarding/scans")).json()
    candidate = StoredCandidate(
        id="c1",
        provider_message_id="m1",
        subject="RFQ 2024-118 – Rolamentos 6205",
        recipients=["cotacao@rolemax.example", "b@rolemax.example"],
        sent_at="2026-08-15T12:00:00+00:00",
        attachments=[
            {"name": "itens.xlsx", "content_type": ""},
            {"name": "d.pdf", "content_type": ""},
        ],
        note=None,
        preselected=True,
    )

    early = await http.post(
        "/v1/onboarding/templates",
        json={"source": "search", "scan_id": scan["id"], "candidate_ids": ["c1"]},
    )
    assert early.status_code == 409  # the scan is still running

    async with tenant_session(app_engine, onboarding_rows.people.tenant) as session:
        await session.execute(
            update(TemplateScan)
            .where(TemplateScan.id == scan["id"])
            .values(status="done", checked=40, found=1, candidates=[candidate])
        )
    view = (await http.get(f"/v1/onboarding/scans/{scan['id']}")).json()
    assert view["candidates"] == [
        {
            "id": "c1",
            "subject": "RFQ 2024-118 – Rolamentos 6205",
            "to": "cotacao@rolemax.example",
            "more": 1,
            "date": "15 ago",
            "sent_at": "2026-08-15T12:00:00Z",
            "attachments": "2 anexos · xlsx, pdf",
            "attachment_count": 2,
            "note": None,
            "preselected": True,
        }
    ]
    assert "provider_message_id" not in view["candidates"][0]

    unknown = await http.post(
        "/v1/onboarding/templates",
        json={"source": "search", "scan_id": scan["id"], "candidate_ids": ["c1", "m1"]},
    )
    assert unknown.status_code == 422

    created = await http.post(
        "/v1/onboarding/templates",
        json={"source": "search", "scan_id": scan["id"], "candidate_ids": ["c1"]},
    )
    assert created.status_code == 202, created.text
    template = created.json()
    assert template["status"] == "building" and template["source_count"] == 1
    build = api.queue.envelopes[-1]
    assert build.type == TEMPLATE_BUILD and build.payload == {"template_id": template["id"]}

    not_ready = await http.post(f"/v1/onboarding/templates/{template['id']}/use")
    assert not_ready.status_code == 409


@pytest.mark.db
async def test_uploads(api: Api) -> None:
    http = api.http
    docx = build_docx(["Prezados, segue solicitação de cotação."])
    stored = await http.post(
        "/v1/onboarding/uploads",
        params={"filename": "../../pedido atual.docx"},
        content=docx,
        headers={"Content-Type": DOCX_TYPE},
    )
    assert stored.status_code == 201, stored.text
    upload = stored.json()
    assert upload["ext"] == "docx" and upload["size_bytes"] == len(docx)
    assert "/" not in upload["filename"]
    [key] = api.storage.blobs
    assert key.endswith(f"{upload['id']}.docx") and api.storage.blobs[key] == docx

    exe = await http.post("/v1/onboarding/uploads", params={"filename": "x.exe"}, content=b"MZ")
    assert exe.status_code == 415
    big = await http.post(
        "/v1/onboarding/uploads",
        params={"filename": "big.pdf"},
        content=b"%PDF-" + b"0" * (10 * 1024 * 1024),
    )
    assert big.status_code == 413
    empty = await http.post("/v1/onboarding/uploads", params={"filename": "e.csv"}, content=b"")
    assert empty.status_code == 422

    created = await http.post(
        "/v1/onboarding/templates", json={"source": "upload", "upload_ids": [upload["id"]]}
    )
    assert created.status_code == 202, created.text
    missing = await http.post(
        "/v1/onboarding/templates", json={"source": "upload", "upload_ids": [str(uuid4())]}
    )
    assert missing.status_code == 404
