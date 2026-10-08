"""Direct uploads: signing the form (S3 and local), the local `/dev/uploads`
stand-in, and `POST /v1/onboarding/uploads/direct` + `/complete`."""

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.api.deps import (
    ApiResources,
    get_db,
    get_direct_uploads,
    get_job_queue,
    get_principal,
    get_storage,
)
from bido_back.api.dev import dev_router, get_dev_settings
from bido_back.api.onboarding import onboarding_router
from bido_back.auth import Principal
from bido_back.db import create_sessionmaker
from bido_back.settings import Settings
from bido_back.storage import BlobStorage, InvalidBlobKey
from bido_back.storage.direct import (
    InvalidUploadForm,
    LocalDirectUploads,
    PresignedPost,
    StoredObject,
)
from bido_back.storage.local import LocalFileStorage
from bido_back.storage.s3 import S3Storage

from .conftest import FakeQueue, FakeStorage, Onboarding

XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
UNUSED_DB = "postgresql+asyncpg://app@localhost:5432/unused"
TTL = timedelta(minutes=15)


# --- Local form ------------------------------------------------------------------------------


async def test_local_form_round_trip(tmp_path: Path) -> None:
    storage = LocalFileStorage(tmp_path)
    direct = LocalDirectUploads(storage, "http://localhost:8080/")
    form = await direct.presign_post(
        "onboarding/t/u.xlsx", content_type=XLSX_TYPE, max_bytes=5, expires_in=TTL
    )
    assert form.url == "http://localhost:8080/dev/uploads"
    assert await direct.stat("onboarding/t/u.xlsx") is None

    await direct.receive(form.fields, b"PK\x03\x04")
    assert await storage.get("onboarding/t/u.xlsx") == b"PK\x03\x04"
    assert await direct.stat("onboarding/t/u.xlsx") == StoredObject(size_bytes=4, content_type=None)


async def test_local_form_refuses_what_it_did_not_sign(tmp_path: Path) -> None:
    direct = LocalDirectUploads(LocalFileStorage(tmp_path), "http://localhost:8080")
    form = await direct.presign_post(
        "onboarding/t/u.xlsx", content_type=XLSX_TYPE, max_bytes=5, expires_in=TTL
    )
    fields = form.fields

    with pytest.raises(InvalidUploadForm):  # bigger than the form allows
        await direct.receive(fields, b"123456")
    with pytest.raises(InvalidUploadForm):  # empty
        await direct.receive(fields, b"")
    with pytest.raises(InvalidUploadForm):  # another key than the signed one
        await direct.receive({**fields, "key": "onboarding/other/u.xlsx"}, b"1")
    with pytest.raises(InvalidUploadForm):  # another content type
        await direct.receive({**fields, "Content-Type": "text/html"}, b"1")
    with pytest.raises(InvalidUploadForm):  # policy rewritten without re-signing
        terms = json.loads(base64.urlsafe_b64decode(fields["policy"]))
        terms["max_bytes"] = 10**9
        forged = base64.urlsafe_b64encode(json.dumps(terms).encode()).decode()
        await direct.receive({**fields, "policy": forged}, b"1")
    with pytest.raises(InvalidUploadForm):  # no signature at all
        await direct.receive({k: v for k, v in fields.items() if k != "signature"}, b"1")

    expired = await direct.presign_post(
        "onboarding/t/u.xlsx", content_type=XLSX_TYPE, max_bytes=5, expires_in=-TTL
    )
    with pytest.raises(InvalidUploadForm):
        await direct.receive(expired.fields, b"1")

    with pytest.raises(InvalidBlobKey):
        await direct.presign_post("../x", content_type=XLSX_TYPE, max_bytes=5, expires_in=TTL)
    assert not await asyncio.to_thread(lambda: list(tmp_path.rglob("*.xlsx")))


# --- S3 form ---------------------------------------------------------------------------------


class NotFoundError(Exception):
    def __init__(self) -> None:
        super().__init__("Not Found")
        self.response = {"Error": {"Code": "404"}}


class FakeS3:
    def __init__(self) -> None:
        self.presigned: dict[str, object] = {}
        self.heads: dict[str, dict[str, object]] = {}

    def put_object(self, **kwargs: object) -> object:
        return {}

    def get_object(self, **kwargs: object) -> dict[str, object]:
        raise NotFoundError

    def head_object(self, **kwargs: object) -> dict[str, object]:
        key = str(kwargs["Key"])
        if key not in self.heads:
            raise NotFoundError
        return self.heads[key]

    def generate_presigned_url(self, *args: object, **kwargs: object) -> str:
        raise AssertionError("not used here")

    def generate_presigned_post(self, **kwargs: object) -> dict[str, object]:
        self.presigned = kwargs
        fields = kwargs["Fields"]
        assert isinstance(fields, dict)
        return {
            "url": "https://bucket.s3.us-east-2.amazonaws.com/",
            "fields": {**fields, "key": kwargs["Key"], "policy": "p", "x-amz-signature": "s"},
        }


async def test_s3_form_pins_type_encryption_and_size() -> None:
    s3 = FakeS3()
    storage = S3Storage("bucket", region="us-east-2", kms_key_id="alias/bido", s3=s3)
    form = await storage.presign_post(
        "onboarding/t/u.xlsx", content_type=XLSX_TYPE, max_bytes=1234, expires_in=TTL
    )
    assert form.url == "https://bucket.s3.us-east-2.amazonaws.com/"
    assert form.fields["key"] == "onboarding/t/u.xlsx"
    assert s3.presigned["Bucket"] == "bucket"
    assert s3.presigned["ExpiresIn"] == 900
    conditions = s3.presigned["Conditions"]
    assert isinstance(conditions, list)
    assert {"Content-Type": XLSX_TYPE} in conditions
    assert {"x-amz-server-side-encryption": "aws:kms"} in conditions
    assert {"x-amz-server-side-encryption-aws-kms-key-id": "alias/bido"} in conditions
    assert ["content-length-range", 1, 1234] in conditions

    assert await storage.stat("onboarding/t/u.xlsx") is None
    s3.heads["onboarding/t/u.xlsx"] = {"ContentLength": 42, "ContentType": XLSX_TYPE}
    assert await storage.stat("onboarding/t/u.xlsx") == StoredObject(42, XLSX_TYPE)
    with pytest.raises(InvalidBlobKey):
        await storage.presign_post("../x", content_type=XLSX_TYPE, max_bytes=1, expires_in=TTL)


# --- POST /dev/uploads -----------------------------------------------------------------------


def _dev_client(environment: str, storage: BlobStorage) -> TestClient:
    settings = Settings.model_validate({"environment": environment, "database_url": UNUSED_DB})
    app = FastAPI()
    app.include_router(dev_router)
    app.dependency_overrides[get_dev_settings] = lambda: settings
    app.dependency_overrides[get_storage] = lambda: storage
    return TestClient(app)


async def test_dev_uploads_takes_the_signed_form(tmp_path: Path) -> None:
    storage = LocalFileStorage(tmp_path)
    form = await LocalDirectUploads(storage, "http://testserver").presign_post(
        "onboarding/t/u.csv", content_type="text/csv", max_bytes=100, expires_in=TTL
    )
    client = _dev_client("local", storage)

    stored = client.post("/dev/uploads", data=form.fields, files={"file": ("u.csv", b"a,b\n1,2\n")})
    assert stored.status_code == 204, stored.text
    assert await storage.get("onboarding/t/u.csv") == b"a,b\n1,2\n"

    too_big = client.post("/dev/uploads", data=form.fields, files={"file": ("u.csv", b"x" * 101)})
    assert too_big.status_code == 403
    no_file = client.post("/dev/uploads", data=form.fields, files={"other": ("u.csv", b"x")})
    assert no_file.status_code == 422
    for environment in ("test", "dev", "prd"):
        refused = _dev_client(environment, storage).post(
            "/dev/uploads", data=form.fields, files={"file": ("u.csv", b"x")}
        )
        assert refused.status_code == 404


# --- /v1/onboarding/uploads/direct + /complete -----------------------------------------------


class FakeDirectUploads:
    """Signs nothing; the test puts objects in `objects` as if the browser uploaded them."""

    def __init__(self) -> None:
        self.objects: dict[str, StoredObject] = {}
        self.forms: list[tuple[str, str, int]] = []

    async def presign_post(
        self, key: str, *, content_type: str, max_bytes: int, expires_in: timedelta
    ) -> PresignedPost:
        self.forms.append((key, content_type, max_bytes))
        return PresignedPost(
            url="https://uploads.test/",
            fields={"key": key, "Content-Type": content_type},
            expires_at=datetime.now(UTC) + expires_in,
        )

    async def stat(self, key: str) -> StoredObject | None:
        return self.objects.get(key)


def _app() -> FastAPI:
    app = FastAPI()
    app.state.resources = ApiResources(
        Settings.model_validate({"environment": "test", "database_url": UNUSED_DB})
    )
    app.include_router(onboarding_router())
    return app


async def test_direct_routes_require_authentication() -> None:
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        responses = [
            await http.post(
                "/v1/onboarding/uploads/direct", json={"filename": "a.csv", "size_bytes": 1}
            ),
            await http.post(f"/v1/onboarding/uploads/{uuid4()}/complete", json={"filename": "a"}),
        ]
    assert [r.status_code for r in responses] == [401, 401]


@pytest_asyncio.fixture
async def direct_api(
    app_engine: AsyncEngine, onboarding_rows: Onboarding
) -> AsyncIterator[tuple[httpx.AsyncClient, FakeDirectUploads, Principal]]:
    app = _app()
    sessionmaker = create_sessionmaker(app_engine)
    direct = FakeDirectUploads()
    people = onboarding_rows.people
    principal = Principal(tenant_id=people.tenant, user_id=people.owner, role="owner")

    def db() -> async_sessionmaker[AsyncSession]:
        return sessionmaker

    app.dependency_overrides[get_principal] = lambda: principal
    app.dependency_overrides[get_db] = db
    app.dependency_overrides[get_direct_uploads] = lambda: direct
    app.dependency_overrides[get_storage] = FakeStorage
    app.dependency_overrides[get_job_queue] = FakeQueue
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http, direct, principal


@pytest.mark.db
async def test_direct_upload_flow(
    direct_api: tuple[httpx.AsyncClient, FakeDirectUploads, Principal],
) -> None:
    http, direct, principal = direct_api
    size = 60 * 1024 * 1024  # over the 10 MB of the raw-body upload
    started = await http.post(
        "/v1/onboarding/uploads/direct",
        json={"filename": "ERP Estoque.xlsx", "size_bytes": size, "content_type": XLSX_TYPE},
    )
    assert started.status_code == 201, started.text
    form = started.json()
    upload_id = form["upload_id"]
    key = f"onboarding/{principal.tenant_id}/{upload_id}.xlsx"
    assert direct.forms == [(key, XLSX_TYPE, size)]
    assert form["fields"]["key"] == key and form["max_bytes"] == size

    complete = f"/v1/onboarding/uploads/{upload_id}/complete"
    not_yet = await http.post(complete, json={"filename": "ERP Estoque.xlsx"})
    assert not_yet.status_code == 404

    direct.objects[key] = StoredObject(size_bytes=size, content_type=XLSX_TYPE)
    done = await http.post(complete, json={"filename": "ERP Estoque.xlsx"})
    assert done.status_code == 201, done.text
    assert done.json() == {
        "id": upload_id,
        "filename": "ERP Estoque.xlsx",
        "size_bytes": size,
        "ext": "xlsx",
    }
    again = await http.post(complete, json={"filename": "ERP Estoque.xlsx"})
    assert again.status_code == 201 and again.json() == done.json()

    template = await http.post(
        "/v1/onboarding/templates", json={"source": "upload", "upload_ids": [upload_id]}
    )
    assert template.status_code == 202, template.text


@pytest.mark.db
async def test_direct_upload_refusals(
    direct_api: tuple[httpx.AsyncClient, FakeDirectUploads, Principal],
) -> None:
    http, direct, principal = direct_api
    start = "/v1/onboarding/uploads/direct"
    exe = await http.post(start, json={"filename": "x.exe", "size_bytes": 2})
    assert exe.status_code == 415
    huge = await http.post(start, json={"filename": "x.csv", "size_bytes": 100 * 1024 * 1024 + 1})
    assert huge.status_code == 413
    zero = await http.post(start, json={"filename": "x.csv", "size_bytes": 0})
    assert zero.status_code == 422
    assert direct.forms == []

    # A completed object that is empty or too big is not recorded.
    upload_id = uuid4()
    key = f"onboarding/{principal.tenant_id}/{upload_id}.csv"
    direct.objects[key] = StoredObject(size_bytes=0, content_type="text/csv")
    empty = await http.post(
        f"/v1/onboarding/uploads/{upload_id}/complete", json={"filename": "x.csv"}
    )
    assert empty.status_code == 422
    direct.objects[key] = StoredObject(size_bytes=100 * 1024 * 1024 + 1, content_type="text/csv")
    over = await http.post(
        f"/v1/onboarding/uploads/{upload_id}/complete", json={"filename": "x.csv"}
    )
    assert over.status_code == 413
