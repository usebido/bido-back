"""`POST /dev/inbound`: local only, raw body or multipart upload, result passed through."""

from decimal import Decimal
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bido_back.api.deps import get_db, get_principal
from bido_back.api.dev import (
    debug_router,
    dev_router,
    get_dev_settings,
    get_inbound_service,
    get_payment_demo_creator,
)
from bido_back.auth import Principal
from bido_back.intake.service import IngestResult
from bido_back.payments.demo import DebugPaymentPurchaseOrder
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


class FakePaymentDemoCreator:
    def __init__(self) -> None:
        self.amounts: list[Decimal] = []
        self.email_domains: list[str] = []

    async def __call__(
        self,
        db: async_sessionmaker[AsyncSession],
        principal: Principal,
        *,
        amount_usd: Decimal,
        email_domain: str,
    ) -> DebugPaymentPurchaseOrder:
        self.amounts.append(amount_usd)
        self.email_domains.append(email_domain)
        return DebugPaymentPurchaseOrder(
            id=uuid4(),
            ref="PO-1",
            rfq_id=uuid4(),
            rfq_ref="RFQ-1",
            supplier_id=uuid4(),
            supplier_name="Bido Debug Payments Supplier",
            currency="USD",
            total=amount_usd,
            status="sent",
            fulfilment_state="sent",
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


def _payment_client(environment: str, creator: FakePaymentDemoCreator) -> TestClient:
    settings = Settings.model_validate(
        {
            "environment": environment,
            "database_url": "postgresql://u@localhost:5432/db",
            "email_domain": "mail.example.test",
        }
    )
    app = FastAPI()
    app.include_router(debug_router)
    app.dependency_overrides[get_dev_settings] = lambda: settings
    app.dependency_overrides[get_principal] = lambda: Principal(
        tenant_id=uuid4(), user_id=uuid4(), role="owner"
    )
    app.dependency_overrides[get_db] = lambda: async_sessionmaker[AsyncSession]()
    app.dependency_overrides[get_payment_demo_creator] = lambda: creator
    return TestClient(app)


def test_refused_outside_local() -> None:
    service = FakeService()
    for environment in ("test", "dev", "prd"):
        response = _client(environment, service).post("/dev/inbound", content=RAW)
        assert response.status_code == 404
    assert service.raws == []


def test_payment_demo_refused_outside_debug_environments() -> None:
    creator = FakePaymentDemoCreator()
    for environment in ("test", "prd"):
        response = _payment_client(environment, creator).post("/dev/payments/purchase-order")
        assert response.status_code == 404
    assert creator.amounts == []


def test_payment_demo_creates_default_one_dollar_order_in_dev() -> None:
    creator = FakePaymentDemoCreator()
    response = _payment_client("dev", creator).post("/dev/payments/purchase-order")
    assert response.status_code == 200
    body = response.json()
    assert body["ref"] == "PO-1"
    assert body["currency"] == "USD"
    assert body["total"] == "1.00"
    assert body["status"] == "sent"
    assert body["fulfilment_state"] == "sent"
    assert creator.amounts == [Decimal("1.00")]
    assert creator.email_domains == ["mail.example.test"]


def test_payment_demo_accepts_custom_amount() -> None:
    creator = FakePaymentDemoCreator()
    response = _payment_client("local", creator).post(
        "/dev/payments/purchase-order", json={"amount_usd": "2.50"}
    )
    assert response.status_code == 200
    assert response.json()["total"] == "2.50"
    assert creator.amounts == [Decimal("2.50")]


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
    app.include_router(debug_router)
    assert "/dev/inbound" not in app.openapi()["paths"]
    assert "/dev/payments/purchase-order" not in app.openapi()["paths"]
