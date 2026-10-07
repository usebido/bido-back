"""`POST /dev/inbound`: local only, raw body or multipart upload, result passed through."""

from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bido_back.api.dev import dev_router, get_dev_settings, get_inbound_service
from bido_back.intake.service import IngestResult
from bido_back.settings import Settings

RAW = b"From: a@supplier.test\r\nTo: b@quotes.test\r\nSubject: hi\r\n\r\nbody \xc3\xa9\r\n"


class FakeService:
    def __init__(self) -> None:
        self.raws: list[bytes] = []
        self.tenant_id = uuid4()

    async def ingest(self, raw: bytes) -> IngestResult:
        self.raws.append(raw)
        return IngestResult(
            outcome="stored",
            raw_key="inbound/x.eml",
            tenant_id=self.tenant_id,
            invite_id=uuid4(),
            message_id=uuid4(),
            matched_by="reply_token",
            extraction_enqueued=True,
        )


def _client(environment: str, service: FakeService) -> TestClient:
    settings = Settings.model_validate(
        {"environment": environment, "database_url": "postgresql://u@localhost:5432/db"}
    )
    app = FastAPI()
    app.include_router(dev_router)
    app.dependency_overrides[get_dev_settings] = lambda: settings
    app.dependency_overrides[get_inbound_service] = lambda: service
    return TestClient(app)


def test_refused_outside_local() -> None:
    service = FakeService()
    for environment in ("test", "dev", "prd"):
        response = _client(environment, service).post("/dev/inbound", content=RAW)
        assert response.status_code == 404
    assert service.raws == []


def test_raw_body_is_ingested() -> None:
    service = FakeService()
    response = _client("local", service).post(
        "/dev/inbound", content=RAW, headers={"Content-Type": "message/rfc822"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "stored"
    assert body["tenant_id"] == str(service.tenant_id)
    assert body["matched_by"] == "reply_token"
    assert body["extraction_enqueued"] is True
    assert service.raws == [RAW]


def test_multipart_upload_is_ingested() -> None:
    service = FakeService()
    response = _client("local", service).post(
        "/dev/inbound", files={"file": ("reply.eml", RAW, "application/octet-stream")}
    )
    assert response.status_code == 200
    assert service.raws == [RAW]


def test_empty_body_is_rejected() -> None:
    response = _client("local", FakeService()).post("/dev/inbound", content=b"  ")
    assert response.status_code == 422


def test_not_in_openapi() -> None:
    app = FastAPI()
    app.include_router(dev_router)
    assert "/dev/inbound" not in app.openapi()["paths"]
