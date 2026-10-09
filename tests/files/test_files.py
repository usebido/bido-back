"""The company file library: listing, filters, content search, reading, attaching,
the access policy, indexing (and its backfill), and files in `/v1/search`."""

import io
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.api.deps import (
    ApiResources,
    get_db,
    get_download_links,
    get_optional_job_queue,
    get_principal,
    get_storage,
)
from bido_back.api.files import files_router
from bido_back.api.search import search_router
from bido_back.auth import Principal, Role
from bido_back.db import create_sessionmaker, tenant_session
from bido_back.db.models import (
    Attachment,
    Contact,
    FileText,
    Invite,
    Message,
    Rfq,
    Supplier,
    TemplateUpload,
)
from bido_back.files import index
from bido_back.files.service import (
    file_type_of,
    make_snippet,
    query_words,
    tsquery_text,
)
from bido_back.settings import Settings
from bido_back.storage import BlobNotFound
from bido_back.storage.links import Disposition, FileLink
from bido_back.worker.envelope import Envelope
from bido_back.worker.job_types import FILES_INDEX
from tests.approvals.conftest import People, people
from tests.db.conftest import app_engine, migrator_engine, two_tenants

__all__ = ["app_engine", "migrator_engine", "people", "two_tenants"]

UNUSED_DB = "postgresql+asyncpg://app@localhost:5432/unused"


def _xlsx(rows: Sequence[Sequence[object]], *, title: str = "Sheet1") -> bytes:
    from openpyxl import Workbook  # pyright: ignore[reportMissingTypeStubs]

    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = title
    for row in rows:
        sheet.append(list(row))  # pyright: ignore[reportUnknownMemberType]
    out = io.BytesIO()
    workbook.save(out)
    return out.getvalue()


# --- Pure helpers -----------------------------------------------------------------------------


def test_query_words_are_alphanumeric_prefixes() -> None:
    assert query_words("Rolam 6204-2RS, açO!") == ["rolam", "6204", "2rs", "aço"]
    assert tsquery_text(["rolam", "6204"]) == "rolam:* & 6204:*"
    assert query_words("&|!:*()") == []


def test_file_type_from_the_name() -> None:
    assert file_type_of("Estoque.XLSX") == "spreadsheet"
    assert file_type_of("cotacao.pdf") == "pdf"
    assert file_type_of("rfq.docx") == "doc"
    assert file_type_of("logo.png") == "image"
    assert file_type_of("dados") == "other"


def test_snippet_shows_the_cell_or_page_it_comes_from() -> None:
    text = (
        "[estoque.xlsx MMBE!A1] Material [estoque.xlsx MMBE!B1] Descrição\n"
        "[estoque.xlsx MMBE!A2] 100000109 [estoque.xlsx MMBE!B2] ROLAMENTO 6204-2RS"
    )
    snippet = make_snippet(text, ["rolamento"])
    assert snippet is not None
    assert snippet.location == "MMBE!B2"
    assert "ROLAMENTO 6204-2RS" in snippet.text and "[" not in snippet.text
    pdf = make_snippet("[quote.pdf p.2] Prazo de entrega 30 dias", ["prazo"])
    assert pdf is not None and pdf.location == "p.2"
    assert make_snippet("nothing here", ["rolamento"]) is None


# --- Fakes and fixtures -----------------------------------------------------------------------


class FakeStorage:
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        self.blobs[key] = data

    async def get(self, key: str) -> bytes:
        if key not in self.blobs:
            raise BlobNotFound(key)
        return self.blobs[key]


@dataclass
class FakeLinks:
    calls: list[tuple[str, str, str, Disposition]] = field(
        default_factory=list[tuple[str, str, str, Disposition]]
    )

    async def presign_get(
        self,
        key: str,
        *,
        filename: str,
        content_type: str,
        disposition: Disposition,
        expires_in: timedelta,
    ) -> FileLink:
        self.calls.append((key, filename, content_type, disposition))
        return FileLink(
            url=f"https://files.test/{key}?d={disposition}",
            expires_at=datetime.now(UTC) + expires_in,
        )


@dataclass
class FakeJobs:
    enqueued: list[Envelope] = field(default_factory=list[Envelope])

    async def enqueue(self, envelope: Envelope) -> None:
        self.enqueued.append(envelope)


@dataclass
class Library:
    http: httpx.AsyncClient
    storage: FakeStorage
    links: FakeLinks
    jobs: FakeJobs
    people: People
    as_user: dict[str, Principal]
    rfq_id: UUID
    supplier_id: UUID
    message_id: UUID

    def act_as(self, principal: Principal) -> None:
        self.as_user["principal"] = principal


def _principal(tenant: UUID, user: UUID, role: Role = "member") -> Principal:
    return Principal(tenant_id=tenant, user_id=user, role=role)


@pytest_asyncio.fixture
async def library(
    app_engine: AsyncEngine, migrator_engine: AsyncEngine, people: People
) -> AsyncIterator[Library]:
    rfq_id, supplier_id, contact_id, invite_id, message_id = (uuid4() for _ in range(5))
    async with tenant_session(app_engine, people.tenant) as session:
        session.add(Supplier(id=supplier_id, tenant_id=people.tenant, name="Rolatec Ltda."))
        session.add(
            Contact(id=contact_id, tenant_id=people.tenant, kind="supplier", email="v@x.test")
        )
        session.add(
            Rfq(
                id=rfq_id,
                tenant_id=people.tenant,
                ref=f"RFQ-{uuid4().hex[:8]}",
                title="Rolamentos",
                status="open",
                created_by=people.owner,
            )
        )
        await session.flush()
        session.add(
            Invite(
                id=invite_id,
                tenant_id=people.tenant,
                rfq_id=rfq_id,
                supplier_id=supplier_id,
                contact_id=contact_id,
                reply_token=uuid4().hex,
                status="sent",
            )
        )
        await session.flush()
        session.add(
            Message(
                id=message_id,
                tenant_id=people.tenant,
                invite_id=invite_id,
                direction="in",
                subject="Cotação",
                occurred_at=datetime.now(UTC),
            )
        )
    app = FastAPI()
    app.state.resources = ApiResources(
        Settings.model_validate({"environment": "test", "database_url": UNUSED_DB})
    )
    app.include_router(files_router())
    app.include_router(search_router())
    sessionmaker = create_sessionmaker(app_engine)
    storage, links, jobs = FakeStorage(), FakeLinks(), FakeJobs()
    current = {"principal": _principal(people.tenant, people.owner, "owner")}

    def db() -> async_sessionmaker[AsyncSession]:
        return sessionmaker

    app.dependency_overrides[get_principal] = lambda: current["principal"]
    app.dependency_overrides[get_db] = db
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_download_links] = lambda: links
    app.dependency_overrides[get_optional_job_queue] = lambda: jobs
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield Library(
                http, storage, links, jobs, people, current, rfq_id, supplier_id, message_id
            )
    finally:
        for tenant_id in (people.tenant, people.other_tenant):
            async with tenant_session(migrator_engine, tenant_id) as session:
                for model in (FileText, Attachment, Message, Invite, Rfq, Supplier, Contact):
                    await session.execute(delete(model).where(model.tenant_id == tenant_id))
                await session.execute(
                    delete(TemplateUpload).where(TemplateUpload.tenant_id == tenant_id)
                )


async def _upload(
    engine: AsyncEngine,
    lib: Library,
    filename: str,
    data: bytes,
    *,
    tenant: UUID | None = None,
    user: UUID | None = None,
    created_at: datetime | None = None,
) -> UUID:
    tenant = tenant or lib.people.tenant
    upload_id = uuid4()
    key = f"onboarding/{tenant}/{upload_id}.{filename.rsplit('.', 1)[-1].lower()}"
    await lib.storage.put(key, data, "application/octet-stream")
    async with tenant_session(engine, tenant) as session:
        row = TemplateUpload(
            id=upload_id,
            tenant_id=tenant,
            user_id=user or lib.people.owner,
            filename=filename,
            content_type="application/octet-stream",
            size_bytes=len(data),
            storage_key=key,
        )
        if created_at is not None:
            row.created_at = created_at
        session.add(row)
    return upload_id


async def _attachment(engine: AsyncEngine, lib: Library, filename: str, data: bytes) -> UUID:
    attachment_id = uuid4()
    key = f"attachments/{lib.people.tenant}/{lib.message_id}/1-{filename}"
    await lib.storage.put(key, data, "application/octet-stream")
    async with tenant_session(engine, lib.people.tenant) as session:
        session.add(
            Attachment(
                id=attachment_id,
                tenant_id=lib.people.tenant,
                message_id=lib.message_id,
                filename=filename,
                content_type="application/octet-stream",
                size_bytes=len(data),
                storage_key=key,
            )
        )
    return attachment_id


# --- API --------------------------------------------------------------------------------------


async def test_files_routes_require_authentication() -> None:
    app = FastAPI()
    app.state.resources = ApiResources(
        Settings.model_validate({"environment": "test", "database_url": UNUSED_DB})
    )
    app.include_router(files_router())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        codes = [
            (await http.get("/v1/files")).status_code,
            (await http.get(f"/v1/files/{uuid4()}/preview")).status_code,
            (await http.get(f"/v1/files/{uuid4()}/download")).status_code,
            (await http.post(f"/v1/files/{uuid4()}/attach")).status_code,
        ]
    assert codes == [401, 401, 401, 401]


@pytest.mark.db
async def test_library_lists_every_members_uploads_and_supplier_attachments(
    app_engine: AsyncEngine, library: Library
) -> None:
    people = library.people
    mine = await _upload(app_engine, library, "Estoque.xlsx", _xlsx([["a"]]))
    colleague = await _upload(app_engine, library, "MRP.csv", b"a;b\n", user=people.member)
    supplier = await _attachment(app_engine, library, "cotacao.pdf", b"%PDF-1.4")
    await _upload(
        app_engine,
        library,
        "outra.csv",
        b"x\n",
        tenant=people.other_tenant,
        user=people.other_approver,
    )

    body = (await library.http.get("/v1/files")).json()
    by_id = {item["id"]: item for item in body["items"]}
    assert set(by_id) == {str(mine), str(colleague), str(supplier)}
    assert body["total"] == 3
    assert by_id[str(colleague)]["uploaded_by"] == str(people.member)
    assert by_id[str(supplier)]["kind"] == "supplier_attachment"
    assert by_id[str(supplier)]["supplier_name"] == "Rolatec Ltda."
    assert by_id[str(supplier)]["rfq_id"] == str(library.rfq_id)
    assert by_id[str(supplier)]["content_type"] == "application/pdf"
    assert by_id[str(mine)]["file_type"] == "spreadsheet"
    # Nothing was read yet, so the listing asks for the reading job (the backfill).
    assert [e.type for e in library.jobs.enqueued] == [FILES_INDEX]

    # The other company sees only its own file.
    library.act_as(_principal(people.other_tenant, people.other_approver))
    other = (await library.http.get("/v1/files")).json()
    assert [item["filename"] for item in other["items"]] == ["outra.csv"]


@pytest.mark.db
async def test_filters(app_engine: AsyncEngine, library: Library) -> None:
    old = datetime(2026, 1, 10, tzinfo=UTC)
    await _upload(app_engine, library, "Estoque.xlsx", b"PK", created_at=old)
    await _upload(app_engine, library, "Plano.csv", b"a\n")
    await _attachment(app_engine, library, "cotacao.pdf", b"%PDF")

    async def names(**params: str) -> list[str]:
        body = (await library.http.get("/v1/files", params=params)).json()
        return sorted(item["filename"] for item in body["items"])

    assert await names(kind="upload") == ["Estoque.xlsx", "Plano.csv"]
    assert await names(kind="supplier_attachment") == ["cotacao.pdf"]
    assert await names(type="pdf") == ["cotacao.pdf"]
    assert await names(type="spreadsheet") == ["Estoque.xlsx", "Plano.csv"]
    assert await names(rfq_id=str(library.rfq_id)) == ["cotacao.pdf"]
    assert await names(supplier_id=str(uuid4())) == []
    assert await names(**{"to": str(date(2026, 1, 31))}) == ["Estoque.xlsx"]
    assert await names(**{"from": str(date(2026, 2, 1))}) == ["Plano.csv", "cotacao.pdf"]
    assert await names(q="plan") == ["Plano.csv"]


@pytest.mark.db
async def test_content_search_after_indexing_with_snippets(
    app_engine: AsyncEngine, library: Library
) -> None:
    sheet = _xlsx([["Material", "Descrição"], ["100000109", "ROLAMENTO 6204-2RS"]], title="MMBE")
    upload = await _upload(app_engine, library, "Estoque.xlsx", sheet)
    await _upload(app_engine, library, "logo.png", b"\x89PNG\r\n\x1a\nnot really")
    csv_attachment = await _attachment(
        app_engine, library, "precos.csv", "item;preço\nparafuso M8;0,19\n".encode()
    )

    read, more = await index.index_pending(app_engine, library.storage, library.people.tenant)
    assert (read, more) == (3, False)
    # Idempotent: a second run reads nothing and writes nothing new.
    assert await index.index_pending(app_engine, library.storage, library.people.tenant) == (
        0,
        False,
    )
    async with tenant_session(app_engine, library.people.tenant) as session:
        rows = (await session.execute(select(FileText.file_id, FileText.status))).all()
    statuses = {file_id: status for file_id, status in rows}
    assert len(statuses) == 3
    assert statuses[upload] == "indexed" and statuses[csv_attachment] == "indexed"
    assert sorted(statuses.values()).count("skipped") == 1  # the image needs vision

    hits = (await library.http.get("/v1/files", params={"q": "rolam 6204"})).json()["items"]
    assert [h["id"] for h in hits] == [str(upload)]
    assert hits[0]["matched"] == "content" and hits[0]["indexed"] is True
    assert hits[0]["snippet"]["location"] == "MMBE!B2"
    assert "ROLAMENTO" in hits[0]["snippet"]["text"]

    parafuso = (await library.http.get("/v1/files", params={"q": "parafuso"})).json()["items"]
    assert [h["id"] for h in parafuso] == [str(csv_attachment)]
    # The name still matches on its own, without a snippet.
    by_name = (await library.http.get("/v1/files", params={"q": "estoque"})).json()["items"]
    assert by_name[0]["matched"] == "filename" and by_name[0]["snippet"] is None
    # Every file is read now: listing asks for no more reading.
    assert library.jobs.enqueued == []


@pytest.mark.db
async def test_index_job_hands_over_when_files_are_left(
    app_engine: AsyncEngine, library: Library
) -> None:
    for n in range(3):
        await _upload(app_engine, library, f"f{n}.csv", b"a;b\n1;2\n")
    handler = index.FilesIndexHandler(app_engine, library.storage, library.jobs)
    read, more = await index.index_pending(
        app_engine, library.storage, library.people.tenant, max_files=2
    )
    assert (read, more) == (2, True)
    await handler(index.index_envelope(library.people.tenant, "test"))
    assert library.jobs.enqueued == []  # the handler finished the last one
    async with tenant_session(app_engine, library.people.tenant) as session:
        assert await session.scalar(select(func.count()).select_from(FileText)) == 3


@pytest.mark.db
async def test_preview_and_download_of_a_supplier_attachment(
    app_engine: AsyncEngine, library: Library
) -> None:
    attachment = await _attachment(app_engine, library, "precos.csv", b"item;preco\nM8;0,19\n")
    preview = await library.http.get(f"/v1/files/{attachment}/preview")
    assert preview.status_code == 200, preview.text
    assert preview.json()["kind"] == "table"
    assert preview.json()["sheets"][0]["rows"][1] == ["M8", "0,19"]
    download = await library.http.get(f"/v1/files/{attachment}/download")
    assert download.status_code == 200
    assert library.links.calls[-1][3] == "attachment"

    library.act_as(_principal(library.people.other_tenant, library.people.other_approver))
    for path in ("preview", "download"):
        assert (await library.http.get(f"/v1/files/{attachment}/{path}")).status_code == 404
    assert (await library.http.post(f"/v1/files/{attachment}/attach")).status_code == 404


@pytest.mark.db
async def test_attach_gives_an_upload_the_caller_owns(
    app_engine: AsyncEngine, library: Library
) -> None:
    people = library.people
    mine = await _upload(app_engine, library, "Estoque.xlsx", b"PK")
    colleague = await _upload(app_engine, library, "MRP.csv", b"a\n", user=people.member)
    supplier = await _attachment(app_engine, library, "cotacao.pdf", b"%PDF")

    own = (await library.http.post(f"/v1/files/{mine}/attach")).json()
    assert own["upload_id"] == str(mine)

    first = (await library.http.post(f"/v1/files/{colleague}/attach")).json()
    again = (await library.http.post(f"/v1/files/{colleague}/attach")).json()
    assert first["upload_id"] == again["upload_id"] != str(colleague)
    from_supplier = (await library.http.post(f"/v1/files/{supplier}/attach")).json()
    assert from_supplier["filename"] == "cotacao.pdf"

    async with tenant_session(app_engine, people.tenant) as session:
        reference = await session.get(TemplateUpload, UUID(first["upload_id"]))
        original = await session.get(TemplateUpload, colleague)
    assert reference is not None and original is not None
    assert reference.user_id == people.owner
    assert reference.storage_key == original.storage_key  # no copy of the bytes
    assert (reference.source_kind, reference.source_id) == ("upload", colleague)

    # References never show in the library.
    listed = (await library.http.get("/v1/files")).json()
    assert {item["id"] for item in listed["items"]} == {str(mine), str(colleague), str(supplier)}


@pytest.mark.db
async def test_files_are_in_the_tenant_search(app_engine: AsyncEngine, library: Library) -> None:
    upload = await _upload(
        app_engine, library, "Estoque.xlsx", _xlsx([["Material"], ["ARRUELA LISA M8"]])
    )
    attachment = await _attachment(app_engine, library, "cotacao-arruela.pdf", b"%PDF")
    await index.index_pending(app_engine, library.storage, library.people.tenant)

    body = (await library.http.get("/v1/search", params={"q": "arruela"})).json()
    files = {item["id"]: item for item in body["files"]}
    assert files[str(upload)]["matched"] == "content"
    assert files[str(upload)]["snippet"]["location"] == "Sheet1!A2"
    assert files[str(attachment)]["matched"] == "filename"
    assert files[str(attachment)]["rfq_id"] == str(library.rfq_id)
    assert files[str(attachment)]["supplier_name"] == "Rolatec Ltda."
    # Only the first page carries files.
    later = (await library.http.get("/v1/search", params={"q": "arruela", "offset": 20})).json()
    assert later["files"] == []
