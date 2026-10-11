"""The buyer's milestone routes over the ASGI transport, with fake storage and file links.

`MilestoneApi` (shared with the factory's route tests) builds the app with both routers and
the database, storage, file links and job queue overridden.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.api.deps import (
    ApiResources,
    get_db,
    get_download_links,
    get_job_queue,
    get_principal,
    get_storage,
)
from bido_back.api.factory import factory_router
from bido_back.api.milestones import milestones_router
from bido_back.auth import Principal
from bido_back.db import create_sessionmaker
from bido_back.settings import Settings
from bido_back.storage import BlobStorage
from bido_back.storage.links import DownloadLinks
from bido_back.worker.jobs import JobQueue
from tests.approvals.conftest import FakeJobQueue
from tests.awards.test_awards_db import buyer
from tests.payments.flow.test_service_db import member
from tests.payments.milestones.conftest import Funded
from tests.payments.milestones.test_documents_db import (
    SETTINGS,
    FakeLinks,
    FakeStorage,
    definition_at,
    jpeg,
    outsider,
    pdf,
    release_settings,
    sample,
)

PO = "/v1/purchase-orders/{po}/payment"


def build_app(settings: Settings = SETTINGS) -> FastAPI:
    app = FastAPI()
    app.state.resources = ApiResources(settings)
    app.include_router(milestones_router())
    app.include_router(factory_router())
    return app


@dataclass
class MilestoneApi:
    """Clients of the milestone routes against the test database and shared fakes."""

    engine: AsyncEngine
    storage: FakeStorage = field(default_factory=FakeStorage)
    links: FakeLinks = field(default_factory=FakeLinks)
    jobs: FakeJobQueue = field(default_factory=FakeJobQueue)
    opened: list[httpx.AsyncClient] = field(default_factory=list[httpx.AsyncClient])

    def client(
        self, principal: Principal | None = None, *, settings: Settings = SETTINGS
    ) -> httpx.AsyncClient:
        """A client as `principal` (none: the factory's routes, or unauthenticated)."""
        app = build_app(settings)
        sessionmaker = create_sessionmaker(self.engine)
        storage, links, jobs = self.storage, self.links, self.jobs

        def db() -> async_sessionmaker[AsyncSession]:
            return sessionmaker

        def blob_storage() -> BlobStorage:
            return storage

        def download_links() -> DownloadLinks:
            return links

        def job_queue() -> JobQueue:
            return jobs

        app.dependency_overrides[get_db] = db
        app.dependency_overrides[get_storage] = blob_storage
        app.dependency_overrides[get_download_links] = download_links
        app.dependency_overrides[get_job_queue] = job_queue
        if principal is not None:

            def current() -> Principal:
                return principal

            app.dependency_overrides[get_principal] = current
        transport = httpx.ASGITransport(app=app)
        self.opened.append(httpx.AsyncClient(transport=transport, base_url="http://test"))
        return self.opened[-1]


@pytest_asyncio.fixture
async def api(funded: Funded) -> AsyncIterator[MilestoneApi]:
    clients = MilestoneApi(funded.engine)
    yield clients
    for client in clients.opened:
        await client.aclose()


def document(doc_type: str, filename: str) -> dict[str, str]:
    return {"doc_type": doc_type, "filename": filename}


async def test_milestone_routes_require_authentication() -> None:
    transport = httpx.ASGITransport(app=build_app())
    base = PO.format(po=uuid4())
    some = uuid4()
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        responses = [
            await http.get(f"{base}/milestones"),
            await http.post(f"{base}/factory-link"),
            await http.delete(f"{base}/factory-link"),
            await http.post(
                f"{base}/milestones/0/documents",
                params=document("tool_start_photo", "a.jpg"),
                content=jpeg(),
            ),
            await http.delete(f"{base}/milestones/0/documents/{some}"),
            await http.get(f"{base}/milestones/0/documents/{some}/link"),
            await http.post(f"{base}/milestones/0/submit", json={}),
            await http.post(f"{base}/milestones/0/request-changes", json={"reason": "No."}),
        ]
    assert [r.status_code for r in responses] == [401] * 8


@pytest.mark.db
async def test_the_buyer_runs_a_milestone_over_http(funded: Funded, api: MilestoneApi) -> None:
    http = api.client(buyer(funded.people))
    base = PO.format(po=funded.po_id)
    definition = await definition_at(funded, 0)
    photos = next(d for d in definition.documents if d.required and "jpg" in d.accepts)

    listed = await http.get(f"{base}/milestones")
    assert listed.status_code == 200
    assert [m["status"] for m in listed.json()] == ["awaiting_documents", "locked", "locked"]

    issued = await http.post(f"{base}/factory-link")
    assert issued.status_code == 201
    assert issued.json()["url"] == f"{SETTINGS.factory_page_url}#{issued.json()['token']}"
    assert (await http.delete(f"{base}/factory-link")).status_code == 204

    early = await http.post(f"{base}/milestones/0/submit", json={})
    assert early.status_code == 422
    assert all(code in early.json()["detail"] for code in definition.required_codes)

    data = jpeg()
    added = await http.post(
        f"{base}/milestones/0/documents",
        params=document(photos.code, "steel.jpg"),
        content=data,
        headers={"Content-Type": "image/jpeg"},
    )
    assert added.status_code == 201
    body = added.json()
    assert (body["doc_type"], body["filename"], body["source"]) == (
        photos.code,
        "steel.jpg",
        "buyer",
    )
    assert (body["content_type"], body["size_bytes"]) == ("image/jpeg", len(data))
    again = await http.post(
        f"{base}/milestones/0/documents", params=document(photos.code, "steel.jpg"), content=data
    )
    assert (again.status_code, again.json()["id"]) == (201, body["id"])

    link = await http.get(
        f"{base}/milestones/0/documents/{body['id']}/link", params={"disposition": "inline"}
    )
    assert link.status_code == 200
    assert (link.json()["filename"], link.json()["content_type"]) == ("steel.jpg", "image/jpeg")
    assert api.links.calls[-1][3] == "inline"

    removed = await http.delete(f"{base}/milestones/0/documents/{body['id']}")
    assert removed.status_code == 204
    gone = await http.delete(f"{base}/milestones/0/documents/{body['id']}")
    assert gone.status_code == 404

    for code in sorted(definition.required_codes):
        document_type = definition.document(code)
        assert document_type is not None
        name, content = sample(document_type)
        response = await http.post(
            f"{base}/milestones/0/documents", params=document(code, name), content=content
        )
        assert response.status_code == 201, response.text

    submitted = await http.post(f"{base}/milestones/0/submit", json={"note": "All there."})
    assert submitted.status_code == 202
    sent = submitted.json()
    assert sent["milestone"]["status"] == "in_review"
    assert sent["milestone"]["approval_id"] == sent["approval_id"]
    assert (await http.post(f"{base}/milestones/0/submit", json={})).status_code == 409
    late = await http.post(
        f"{base}/milestones/0/documents", params=document(photos.code, "late.jpg"), content=jpeg()
    )
    assert late.status_code == 409

    refused = await http.post(
        f"{base}/milestones/0/request-changes",
        json={"reason_code": "dimensional_nonconformance", "reason": "Cavity 2 is short."},
    )
    assert refused.status_code == 200
    milestone = refused.json()
    assert milestone["status"] == "changes_requested"
    assert milestone["submissions"][0]["rejection_code"] == "dimensional_nonconformance"
    assert milestone["submissions"][0]["rejection_reason"] == "Cavity 2 is short."
    again_refused = await http.post(
        f"{base}/milestones/0/request-changes", json={"reason": "Again."}
    )
    assert again_refused.status_code == 409
    unknown_code = await http.post(
        f"{base}/milestones/0/request-changes", json={"reason_code": "nope", "reason": "Again."}
    )
    assert unknown_code.status_code == 422


@pytest.mark.db
async def test_uploads_are_refused_by_size_kind_and_type(funded: Funded, api: MilestoneApi) -> None:
    small = release_settings(milestone_document_max_bytes=1024 * 1024)
    http = api.client(buyer(funded.people), settings=small)
    url = f"{PO.format(po=funded.po_id)}/milestones/0/documents"
    definition = await definition_at(funded, 0)
    photos = next(d for d in definition.documents if d.required and "pdf" not in d.accepts)

    too_big = await http.post(
        url, params=document(photos.code, "big.jpg"), content=b"\xff\xd8\xff" + b"0" * (1024 * 1024)
    )
    a_pdf = await http.post(url, params=document(photos.code, "a.pdf"), content=pdf())
    misnamed = await http.post(url, params=document(photos.code, "a.png"), content=jpeg())
    unknown = await http.post(url, params=document("not_a_document", "a.jpg"), content=jpeg())
    bad_code = await http.post(url, params=document("Not A Code", "a.jpg"), content=jpeg())
    empty = await http.post(url, params=document(photos.code, "a.jpg"), content=b"")
    no_name = await http.post(url, params={"doc_type": photos.code}, content=jpeg())

    assert too_big.status_code == 413
    assert (a_pdf.status_code, misnamed.status_code) == (415, 415)
    assert [r.status_code for r in (unknown, bad_code, empty, no_name)] == [422] * 4
    assert api.storage.puts == []


@pytest.mark.db
async def test_without_the_attester_key_a_milestone_is_not_sent(
    funded: Funded, api: MilestoneApi
) -> None:
    http = api.client(
        buyer(funded.people), settings=release_settings(solana_gate_attester_key=None)
    )
    response = await http.post(f"{PO.format(po=funded.po_id)}/milestones/0/submit", json={})
    assert response.status_code == 503


@pytest.mark.db
async def test_any_member_works_on_milestones(funded: Funded, api: MilestoneApi) -> None:
    http = api.client(member(funded.people))
    base = PO.format(po=funded.po_id)
    assert (await http.get(f"{base}/milestones")).status_code == 200
    assert (await http.post(f"{base}/factory-link")).status_code == 201


@pytest.mark.db
async def test_another_company_finds_nothing(funded: Funded, api: MilestoneApi) -> None:
    owner = api.client(buyer(funded.people))
    base = PO.format(po=funded.po_id)
    definition = await definition_at(funded, 0)
    photos = next(d for d in definition.documents if "jpg" in d.accepts)
    added = await owner.post(
        f"{base}/milestones/0/documents", params=document(photos.code, "a.jpg"), content=jpeg()
    )
    assert added.status_code == 201
    document_id = added.json()["id"]

    http = api.client(outsider(funded))
    responses = [
        await http.get(f"{base}/milestones"),
        await http.post(f"{base}/factory-link"),
        await http.delete(f"{base}/factory-link"),
        await http.post(
            f"{base}/milestones/0/documents", params=document(photos.code, "b.jpg"), content=jpeg()
        ),
        await http.delete(f"{base}/milestones/0/documents/{document_id}"),
        await http.get(f"{base}/milestones/0/documents/{document_id}/link"),
        await http.post(f"{base}/milestones/0/submit", json={}),
        await http.post(f"{base}/milestones/0/request-changes", json={"reason": "No."}),
    ]
    assert [r.status_code for r in responses] == [404] * 8
    assert len(api.storage.puts) == 1
