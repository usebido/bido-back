"""Reading uploads back: file links (S3 and local), spreadsheet previews, and
`GET /v1/uploads/{id}/download` + `/preview` with their access rules."""

import io
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.api.deps import (
    ApiResources,
    get_db,
    get_download_links,
    get_principal,
    get_storage,
)
from bido_back.api.dev import dev_router, get_dev_settings
from bido_back.api.uploads import uploads_router
from bido_back.auth import Principal
from bido_back.db import create_sessionmaker, tenant_session
from bido_back.db.models import TemplateUpload
from bido_back.settings import Settings
from bido_back.storage import BlobStorage, InvalidBlobKey
from bido_back.storage.links import (
    Disposition,
    FileLink,
    InvalidFileLink,
    LocalDownloadLinks,
    content_disposition,
)
from bido_back.storage.local import LocalFileStorage
from bido_back.storage.s3 import S3Storage
from bido_back.uploads import files
from bido_back.uploads.files import (
    PREVIEW_COLUMNS,
    PREVIEW_ROWS,
    SheetNotFound,
    read_page,
    read_preview,
)

from .conftest import FakeStorage, Onboarding

UNUSED_DB = "postgresql+asyncpg://app@localhost:5432/unused"
TTL = timedelta(minutes=5)


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


# --- Content-Disposition ---------------------------------------------------------------------


def test_content_disposition_keeps_the_name_and_cannot_be_broken_out_of() -> None:
    assert content_disposition("attachment", "Estoque março.xlsx") == (
        "attachment; filename=\"Estoque mar_o.xlsx\"; filename*=UTF-8''Estoque%20mar%C3%A7o.xlsx"
    )
    tricky = content_disposition("inline", 'a"; x=1\\.pdf')
    assert tricky.startswith('inline; filename="a_; x=1_.pdf"; ')
    assert "%22" in tricky and "%5C" in tricky


# --- Local links -----------------------------------------------------------------------------


async def test_local_link_round_trip_and_refusals(tmp_path: Path) -> None:
    storage = LocalFileStorage(tmp_path)
    links = LocalDownloadLinks(storage, "http://localhost:8080/")
    link = await links.presign_get(
        "onboarding/t/u.pdf",
        filename="Pedido.pdf",
        content_type="application/pdf",
        disposition="inline",
        expires_in=TTL,
    )
    url = urlsplit(link.url)
    assert f"{url.scheme}://{url.netloc}{url.path}" == "http://localhost:8080/dev/files"
    query = {k: v[0] for k, v in parse_qs(url.query).items()}
    key, content_type, disposition = LocalDownloadLinks.verify(query["token"], query["signature"])
    assert (key, content_type) == ("onboarding/t/u.pdf", "application/pdf")
    assert disposition.startswith("inline; ")

    with pytest.raises(InvalidFileLink):
        LocalDownloadLinks.verify(query["token"], "0" * 64)
    with pytest.raises(InvalidFileLink):
        LocalDownloadLinks.verify("", "")
    expired = await links.presign_get(
        "onboarding/t/u.pdf",
        filename="x.pdf",
        content_type="application/pdf",
        disposition="inline",
        expires_in=-TTL,
    )
    expired_query = {k: v[0] for k, v in parse_qs(urlsplit(expired.url).query).items()}
    with pytest.raises(InvalidFileLink):
        LocalDownloadLinks.verify(expired_query["token"], expired_query["signature"])
    with pytest.raises(InvalidBlobKey):
        await links.presign_get(
            "../x", filename="x", content_type="a/b", disposition="attachment", expires_in=TTL
        )


def _dev_client(environment: str, storage: BlobStorage) -> TestClient:
    settings = Settings.model_validate({"environment": environment, "database_url": UNUSED_DB})
    app = FastAPI()
    app.include_router(dev_router)
    app.dependency_overrides[get_dev_settings] = lambda: settings
    app.dependency_overrides[get_storage] = lambda: storage
    return TestClient(app)


async def test_dev_files_serves_what_the_link_fixed(tmp_path: Path) -> None:
    storage = LocalFileStorage(tmp_path)
    await storage.put("onboarding/t/u.csv", b"<html>not a page</html>", "text/html")
    link = await LocalDownloadLinks(storage, "http://testserver").presign_get(
        "onboarding/t/u.csv",
        filename="dados.csv",
        content_type="text/csv; charset=utf-8",
        disposition="attachment",
        expires_in=TTL,
    )
    client = _dev_client("local", storage)
    served = client.get(link.url)
    assert served.status_code == 200
    assert served.content == b"<html>not a page</html>"
    assert served.headers["content-type"].startswith("text/csv")
    assert served.headers["content-disposition"].startswith('attachment; filename="dados.csv"')
    assert served.headers["x-content-type-options"] == "nosniff"

    forged = client.get(link.url.replace("signature=", "signature=0"))
    assert forged.status_code == 403
    assert _dev_client("dev", storage).get(link.url).status_code == 404


# --- S3 links --------------------------------------------------------------------------------


class FakeS3:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def put_object(self, **kwargs: object) -> object:
        return {}

    def get_object(self, **kwargs: object) -> dict[str, object]:
        raise AssertionError("not read")

    def head_object(self, **kwargs: object) -> dict[str, object]:
        raise AssertionError("not read")

    def generate_presigned_post(self, **kwargs: object) -> dict[str, object]:
        raise AssertionError("not signed")

    def generate_presigned_url(self, *args: object, **kwargs: object) -> str:
        self.calls.append((args, kwargs))
        return "https://bucket.s3.us-east-2.amazonaws.com/k?X-Amz-Signature=s"


async def test_s3_link_overrides_type_and_disposition() -> None:
    s3 = FakeS3()
    storage = S3Storage("bucket", region="us-east-2", s3=s3)
    link = await storage.presign_get(
        "onboarding/t/u.xlsx",
        filename="Histórico.xlsx",
        content_type="application/vnd.ms-excel",
        disposition="attachment",
        expires_in=TTL,
    )
    assert link.url.startswith("https://bucket.s3.us-east-2.amazonaws.com/")
    (args, kwargs) = s3.calls[0]
    assert args == ("get_object",)
    assert kwargs["ExpiresIn"] == 300
    params = cast(dict[str, object], kwargs["Params"])
    assert params["Bucket"] == "bucket" and params["Key"] == "onboarding/t/u.xlsx"
    assert params["ResponseContentType"] == "application/vnd.ms-excel"
    assert str(params["ResponseContentDisposition"]).startswith("attachment; ")
    with pytest.raises(InvalidBlobKey):
        await storage.presign_get(
            "/abs", filename="x", content_type="a/b", disposition="inline", expires_in=TTL
        )


# --- Spreadsheet previews --------------------------------------------------------------------


def test_preview_shows_every_column_and_pages_the_rows() -> None:
    wide = [[f"r{r}c{c}" for c in range(46)] for r in range(450)]
    wide[0][0] = "x" * 500
    data = _xlsx(wide)
    (first,) = read_preview(data, "xlsx", "big.xlsx")
    assert first.total_rows == 450 and first.total_columns == 46
    assert len(first.rows) == PREVIEW_ROWS
    assert all(len(row) == 46 for row in first.rows)  # every column, not a window
    assert first.rows[0][0].endswith("…") and len(first.rows[0][0]) == 200
    assert first.rows[1][45] == "r1c45"
    assert (first.offset, first.has_more, first.truncated_columns) == (0, True, False)
    assert first.truncated

    (middle,) = read_page(data, "xlsx", "big.xlsx", "Sheet1", 200, 200)
    assert middle.offset == 200 and middle.rows[0][0] == "r200c0" and middle.has_more
    (last,) = read_page(data, "xlsx", "big.xlsx", "Sheet1", 400, 200)
    assert len(last.rows) == 50 and last.rows[-1][0] == "r449c0" and not last.has_more
    (past,) = read_page(data, "xlsx", "big.xlsx", "Sheet1", 1000, 200)
    assert past.rows == [] and not past.has_more and past.total_rows == 450


def test_preview_caps_the_columns_and_says_so() -> None:
    data = _xlsx([[f"c{c}" for c in range(PREVIEW_COLUMNS + 10)], ["a"]])
    (sheet,) = read_preview(data, "xlsx", "wide.xlsx")
    assert sheet.total_columns == PREVIEW_COLUMNS + 10
    assert all(len(row) == PREVIEW_COLUMNS for row in sheet.rows)
    assert sheet.rows[0][-1] == f"c{PREVIEW_COLUMNS - 1}" and sheet.rows[1][0] == "a"
    assert sheet.truncated_columns and sheet.truncated


def test_preview_pages_every_sheet_or_one_and_refuses_an_unknown_sheet() -> None:
    from openpyxl import Workbook  # pyright: ignore[reportMissingTypeStubs]

    workbook = Workbook()
    first = workbook.active
    assert first is not None
    first.title = "Estoque"
    first.append(["Material", "Qtd"])  # pyright: ignore[reportUnknownMemberType]
    second = workbook.create_sheet("Fornecedores")
    for n in range(5):
        second.append([f"S{n}"])  # pyright: ignore[reportUnknownMemberType]
    out = io.BytesIO()
    workbook.save(out)
    data = out.getvalue()
    assert [s.name for s in read_preview(data, "xlsx", "w.xlsx")] == ["Estoque", "Fornecedores"]
    (page,) = read_page(data, "xlsx", "w.xlsx", "Fornecedores", 3, 10)
    assert page.rows == [["S3"], ["S4"]] and page.total_rows == 5 and not page.has_more
    with pytest.raises(SheetNotFound):
        read_page(data, "xlsx", "w.xlsx", "Compras", 0, 10)


def test_csv_preview_pages_and_skips_empty_lines() -> None:
    data = ("h\n\n" + "".join(f"v{n}\n" for n in range(300))).encode()
    (sheet,) = read_page(data, "csv", "a.csv", "a.csv", 250, 100)
    assert sheet.total_rows == 301 and sheet.offset == 250 and len(sheet.rows) == 51
    assert sheet.rows[0] == ["v249"] and not sheet.has_more


def test_preview_never_evaluates_formulas_and_formats_values() -> None:
    data = _xlsx(
        [
            ["Material", "Qtd", "Total", "Data", "Ativo"],
            ["100000101", 4500.0, "=B2*2", datetime(2026, 10, 31), True],
            [None, None, None, None, None],
            ["100000102", 12.5, '=WEBSERVICE("http://x")', None, None],
        ],
        title="Compras",
    )
    (sheet,) = read_preview(data, "xlsx", "f.xlsx")
    assert sheet.name == "Compras"
    # A formula never saved by Excel has no cached value: it shows nothing, never its text.
    # The empty row the file has shows as an empty row, so offsets match the file.
    assert sheet.rows == [
        ["Material", "Qtd", "Total", "Data", "Ativo"],
        ["100000101", "4500", "", "2026-10-31", "TRUE"],
        ["", "", "", "", ""],
        ["100000102", "12.5", "", "", ""],
    ]
    assert sheet.total_rows == 4 and not sheet.truncated


def test_preview_reads_semicolon_csv_in_any_common_encoding() -> None:
    data = "Material;Descrição;Qtd\n100000101;AÇO;4500\n".encode("cp1252")
    (sheet,) = read_preview(data, "csv", "mrp.csv")
    assert sheet.name == "mrp.csv"
    assert sheet.rows == [["Material", "Descrição", "Qtd"], ["100000101", "AÇO", "4500"]]


# --- The api ---------------------------------------------------------------------------------


@dataclass
class FakeLinks:
    calls: list[tuple[str, str, str, Disposition]]

    async def presign_get(
        self,
        key: str,
        *,
        filename: str,
        content_type: str,
        disposition: Disposition,
        expires_in: timedelta,
    ) -> FileLink:
        assert expires_in <= timedelta(minutes=5)
        self.calls.append((key, filename, content_type, disposition))
        return FileLink(
            url=f"https://files.test/{key}?d={disposition}",
            expires_at=datetime.now(UTC) + expires_in,
        )


def _app() -> FastAPI:
    app = FastAPI()
    app.state.resources = ApiResources(
        Settings.model_validate({"environment": "test", "database_url": UNUSED_DB})
    )
    app.include_router(uploads_router())
    return app


async def test_upload_routes_require_authentication() -> None:
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        responses = [
            await http.get(f"/v1/uploads/{uuid4()}/download"),
            await http.get(f"/v1/uploads/{uuid4()}/preview"),
        ]
    assert [r.status_code for r in responses] == [401, 401]


@dataclass
class UploadsApi:
    http: httpx.AsyncClient
    storage: FakeStorage
    links: FakeLinks
    principal: Principal
    as_user: dict[str, Principal]


@pytest_asyncio.fixture
async def uploads_api(
    app_engine: AsyncEngine, onboarding_rows: Onboarding
) -> AsyncIterator[UploadsApi]:
    app = _app()
    sessionmaker = create_sessionmaker(app_engine)
    storage = FakeStorage()
    links = FakeLinks(calls=[])
    people = onboarding_rows.people
    owner = Principal(tenant_id=people.tenant, user_id=people.owner, role="owner")
    current = {"principal": owner}

    def db() -> async_sessionmaker[AsyncSession]:
        return sessionmaker

    app.dependency_overrides[get_principal] = lambda: current["principal"]
    app.dependency_overrides[get_db] = db
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_download_links] = lambda: links
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield UploadsApi(http, storage, links, owner, current)


async def _upload(
    app_engine: AsyncEngine,
    api: UploadsApi,
    filename: str,
    data: bytes,
    *,
    declared_type: str = "application/octet-stream",
    size_bytes: int | None = None,
) -> UUID:
    upload_id = uuid4()
    ext = filename.rsplit(".", 1)[-1].lower()
    key = f"onboarding/{api.principal.tenant_id}/{upload_id}.{ext}"
    await api.storage.put(key, data, declared_type)
    async with tenant_session(app_engine, api.principal.tenant_id) as session:
        session.add(
            TemplateUpload(
                id=upload_id,
                tenant_id=api.principal.tenant_id,
                user_id=api.principal.user_id,
                filename=filename,
                content_type=declared_type,
                size_bytes=size_bytes if size_bytes is not None else len(data),
                storage_key=key,
            )
        )
    return upload_id


@pytest.mark.db
async def test_download_link_is_an_attachment_with_a_type_from_the_extension(
    app_engine: AsyncEngine, uploads_api: UploadsApi
) -> None:
    upload_id = await _upload(
        app_engine, uploads_api, "Histórico Compras.xlsx", b"PK", declared_type="text/html"
    )
    response = await uploads_api.http.get(f"/v1/uploads/{upload_id}/download")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["filename"] == "Histórico Compras.xlsx"
    xlsx = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert body["content_type"] == xlsx  # never the declared text/html
    assert body["url"].endswith("?d=attachment")
    key = f"onboarding/{uploads_api.principal.tenant_id}/{upload_id}.xlsx"
    assert uploads_api.links.calls == [(key, "Histórico Compras.xlsx", xlsx, "attachment")]


@pytest.mark.db
async def test_only_the_uploader_can_read_an_upload(
    app_engine: AsyncEngine, uploads_api: UploadsApi, onboarding_rows: Onboarding
) -> None:
    upload_id = await _upload(app_engine, uploads_api, "a.csv", b"a;b\n1;2\n")
    people = onboarding_rows.people
    strangers = [
        Principal(tenant_id=people.tenant, user_id=people.member, role="member"),
        Principal(tenant_id=people.other_tenant, user_id=people.other_approver, role="owner"),
        # Right user id, wrong tenant: the row is invisible under the other tenant's RLS.
        Principal(tenant_id=people.other_tenant, user_id=people.owner, role="owner"),
    ]
    for stranger in strangers:
        uploads_api.as_user["principal"] = stranger
        for path in ("download", "preview"):
            response = await uploads_api.http.get(f"/v1/uploads/{upload_id}/{path}")
            assert response.status_code == 404, (stranger, path)
    uploads_api.as_user["principal"] = uploads_api.principal
    missing = await uploads_api.http.get(f"/v1/uploads/{uuid4()}/preview")
    assert missing.status_code == 404
    assert uploads_api.links.calls == []


@pytest.mark.db
async def test_preview_by_file_type(app_engine: AsyncEngine, uploads_api: UploadsApi) -> None:
    http = uploads_api.http
    sheet = await _upload(
        app_engine, uploads_api, "estoque.xlsx", _xlsx([["Material", "Qtd"], ["100000101", 8500]])
    )
    table = (await http.get(f"/v1/uploads/{sheet}/preview")).json()
    assert table["kind"] == "table" and table["url"] is None
    assert table["sheet_names"] == ["Sheet1"]
    assert table["sheets"] == [
        {
            "name": "Sheet1",
            "rows": [["Material", "Qtd"], ["100000101", "8500"]],
            "total_rows": 2,
            "total_columns": 2,
            "truncated": False,
            "offset": 0,
            "has_more": False,
            "truncated_columns": False,
        }
    ]

    pdf = await _upload(app_engine, uploads_api, "Pedido 4471.pdf", b"%PDF-1.7")
    inline = (await http.get(f"/v1/uploads/{pdf}/preview")).json()
    assert inline["kind"] == "inline" and inline["content_type"] == "application/pdf"
    assert inline["url"].endswith("?d=inline") and inline["sheets"] == []
    assert uploads_api.links.calls[-1][2:] == ("application/pdf", "inline")

    for name in ("modelo.docx", "antigo.xls"):
        other = await _upload(app_engine, uploads_api, name, b"PK")
        none = (await http.get(f"/v1/uploads/{other}/preview")).json()
        assert (none["kind"], none["reason"], none["url"]) == ("none", "unsupported", None)

    big = await _upload(app_engine, uploads_api, "big.csv", b"a\n", size_bytes=30 * 1024 * 1024)
    too_large = (await http.get(f"/v1/uploads/{big}/preview")).json()
    assert (too_large["kind"], too_large["reason"]) == ("none", "too_large")

    broken = await _upload(app_engine, uploads_api, "broken.xlsx", b"not a zip")
    unreadable = (await http.get(f"/v1/uploads/{broken}/preview")).json()
    assert (unreadable["kind"], unreadable["reason"]) == ("none", "unreadable")


@pytest.mark.db
async def test_preview_pages_one_sheet_through_the_api(
    app_engine: AsyncEngine, uploads_api: UploadsApi
) -> None:
    files.clear_caches()
    rows = [["Material", *[f"c{c}" for c in range(45)]]] + [
        [f"M{n}", *range(45)] for n in range(450)
    ]
    upload = await _upload(app_engine, uploads_api, "historico.xlsx", _xlsx(rows, title="Compras"))
    http = uploads_api.http
    page = (
        await http.get(f"/v1/uploads/{upload}/preview?sheet=Compras&offset=400&limit=100")
    ).json()
    assert page["sheet_names"] == ["Compras"]
    (sheet,) = page["sheets"]
    assert sheet["offset"] == 400 and sheet["total_rows"] == 451 and sheet["total_columns"] == 46
    assert len(sheet["rows"]) == 51 and sheet["rows"][0][0] == "M399" and not sheet["has_more"]
    assert all(len(row) == 46 for row in sheet["rows"])
    # The same page again comes from what this process kept.
    again = (
        await http.get(f"/v1/uploads/{upload}/preview?sheet=Compras&offset=400&limit=100")
    ).json()
    assert again == page

    missing = await http.get(f"/v1/uploads/{upload}/preview?sheet=Estoque")
    assert missing.status_code == 404 and missing.json()["detail"] == "sheet not found"
    for bad in ("limit=0", "limit=1001", "offset=-1"):
        response = await http.get(f"/v1/uploads/{upload}/preview?{bad}")
        assert response.status_code == 422, bad
    files.clear_caches()
