"""An invite's email thread over HTTP: order, senders, attachments, quarantine and 404s."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.api.deps import ApiResources, get_db, get_principal
from bido_back.api.rfqs import rfqs_router
from bido_back.auth import Principal
from bido_back.db import create_sessionmaker, tenant_session
from bido_back.db.models import Attachment, Contact, Message
from bido_back.rfqs import service
from bido_back.settings import Settings
from tests.approvals.conftest import People

from .test_service import buyer, make_rfq, make_supplier

pytestmark = pytest.mark.db

UNUSED_DB = "postgresql+asyncpg://app@localhost:5432/unused"
SENT_AT = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


@dataclass
class Client:
    http: httpx.AsyncClient
    principal: Principal


@pytest_asyncio.fixture
async def client(app_engine: AsyncEngine, rfq_rows: People) -> AsyncIterator[Client]:
    app = FastAPI()
    app.state.resources = ApiResources(
        Settings.model_validate({"environment": "test", "database_url": UNUSED_DB})
    )
    app.include_router(rfqs_router())
    sessionmaker = create_sessionmaker(app_engine)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        state = Client(http=http, principal=buyer(rfq_rows))

        def principal() -> Principal:
            return state.principal

        def db() -> async_sessionmaker[AsyncSession]:
            return sessionmaker

        app.dependency_overrides[get_principal] = principal
        app.dependency_overrides[get_db] = db
        yield state


@dataclass
class Thread:
    rfq_id: UUID
    invite_id: UUID
    outbound_id: UUID
    reply_id: UUID
    quarantined_id: UUID
    attachment_id: UUID


async def thread(engine: AsyncEngine, principal: Principal) -> Thread:
    """An invite with the buyer's RFQ email, the supplier's reply with a PDF, and an
    out-of-office set aside. Rows are inserted out of order: the API sorts them."""
    rfq = await make_rfq(engine, principal)
    supplier = await make_supplier(engine, principal, f"Rhein{uuid4().hex[:6]}")
    contact = supplier.contacts[0]
    invite = await service.add_invite(engine, principal, rfq.id, supplier.id, contact.id)
    async with tenant_session(engine, principal.tenant_id) as session:
        buyer_contact = Contact(
            tenant_id=principal.tenant_id, kind="buyer", name="Ana Buyer", email="ana@buyer.test"
        )
        session.add(buyer_contact)
        await session.flush()
        reply = Message(
            tenant_id=principal.tenant_id,
            invite_id=invite.id,
            direction="in",
            message_id_header=f"<{uuid4()}@supplier.example.test>",
            from_contact_id=contact.id,
            subject="Re: [RFQ-1] Capacitors",
            body_text="Please find our quote attached.",
            status="extracted",
            occurred_at=SENT_AT + timedelta(days=2),
        )
        quarantined = Message(
            tenant_id=principal.tenant_id,
            invite_id=invite.id,
            direction="in",
            message_id_header=f"<{uuid4()}@supplier.example.test>",
            subject="Out of office",
            body_text="I am away until Monday.",
            status="quarantined",
            occurred_at=SENT_AT + timedelta(days=1),
        )
        outbound = Message(
            tenant_id=principal.tenant_id,
            invite_id=invite.id,
            direction="out",
            message_id_header=f"<{uuid4()}@buyer.test>",
            from_contact_id=buyer_contact.id,
            subject="[RFQ-1] Capacitors",
            body_text="Dear supplier, please quote.",
            status="stored",
            occurred_at=SENT_AT,
        )
        session.add_all([reply, quarantined, outbound])
        await session.flush()
        attachment = Attachment(
            tenant_id=principal.tenant_id,
            message_id=reply.id,
            filename="quote.pdf",
            content_type="application/octet-stream",
            size_bytes=1234,
            storage_key=f"attachments/{principal.tenant_id}/{reply.id}/1-quote.pdf",
        )
        session.add(attachment)
        await session.flush()
        return Thread(
            rfq_id=rfq.id,
            invite_id=invite.id,
            outbound_id=outbound.id,
            reply_id=reply.id,
            quarantined_id=quarantined.id,
            attachment_id=attachment.id,
        )


def url(rfq_id: UUID, invite_id: UUID) -> str:
    return f"/v1/rfqs/{rfq_id}/invites/{invite_id}/messages"


async def test_thread_is_oldest_first_with_senders_and_attachments(
    client: Client, app_engine: AsyncEngine
) -> None:
    t = await thread(app_engine, client.principal)

    response = await client.http.get(url(t.rfq_id, t.invite_id))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["invite_id"] == str(t.invite_id)
    assert body["supplier_name"].startswith("Rhein")
    messages = body["messages"]
    assert [m["id"] for m in messages] == [
        str(t.outbound_id),
        str(t.quarantined_id),
        str(t.reply_id),
    ]
    assert [m["direction"] for m in messages] == ["out", "in", "in"]

    outbound, _, reply = messages
    assert (outbound["from_name"], outbound["from_email"]) == ("Ana Buyer", "ana@buyer.test")
    assert outbound["body_text"] == "Dear supplier, please quote."
    assert outbound["origin"] == "email" and outbound["attachments"] == []

    assert reply["from_name"] == "Jonas Weber"
    assert reply["from_email"].startswith("sales@rhein")
    assert reply["subject"] == "Re: [RFQ-1] Capacitors"
    assert reply["status"] == "extracted" and reply["quarantined"] is False
    # Served as its extension says, like the file library's download.
    assert reply["attachments"] == [
        {
            "id": str(t.attachment_id),
            "filename": "quote.pdf",
            "content_type": "application/pdf",
            "size_bytes": 1234,
        }
    ]


async def test_quarantined_message_is_listed_and_flagged(
    client: Client, app_engine: AsyncEngine
) -> None:
    t = await thread(app_engine, client.principal)

    messages = (await client.http.get(url(t.rfq_id, t.invite_id))).json()["messages"]
    [held] = [m for m in messages if m["quarantined"]]
    assert held["id"] == str(t.quarantined_id)
    assert held["status"] == "quarantined"
    assert held["body_text"] == "I am away until Monday."
    assert held["from_name"] is None and held["from_email"] is None


async def test_invite_without_messages_has_an_empty_thread(
    client: Client, app_engine: AsyncEngine
) -> None:
    rfq = await make_rfq(app_engine, client.principal)
    supplier = await make_supplier(app_engine, client.principal, f"Quiet{uuid4().hex[:6]}")
    invite = await service.add_invite(
        app_engine, client.principal, rfq.id, supplier.id, supplier.contacts[0].id
    )

    response = await client.http.get(url(rfq.id, invite.id))
    assert response.status_code == 200, response.text
    assert response.json()["messages"] == []


async def test_invite_of_another_rfq_is_404(client: Client, app_engine: AsyncEngine) -> None:
    t = await thread(app_engine, client.principal)
    other_rfq = await make_rfq(app_engine, client.principal)

    assert (await client.http.get(url(other_rfq.id, t.invite_id))).status_code == 404
    assert (await client.http.get(url(t.rfq_id, uuid4()))).status_code == 404
    assert (await client.http.get(url(uuid4(), t.invite_id))).status_code == 404


async def test_another_tenant_sees_no_thread(
    client: Client, app_engine: AsyncEngine, rfq_rows: People
) -> None:
    t = await thread(app_engine, client.principal)

    client.principal = Principal(tenant_id=rfq_rows.other_tenant, user_id=rfq_rows.other_approver)
    response = await client.http.get(url(t.rfq_id, t.invite_id))
    assert response.status_code == 404
    assert "Please find" not in response.text
