"""The RFQ, supplier, approval-queue and /v1/me routes over the ASGI transport.

The app is assembled here from the routers (the production wiring lives in
api/app.py). Authentication, the database, the agents and the mailbox are
dependency overrides.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import cast
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.api.approvals import approvals_router
from bido_back.api.deps import (
    ApiResources,
    get_db,
    get_mailbox_senders,
    get_principal,
    get_rfq_agents,
)
from bido_back.api.me import me_router
from bido_back.api.rfqs import rfqs_router
from bido_back.api.suppliers import suppliers_router
from bido_back.api.templates import templates_router
from bido_back.auth import Principal
from bido_back.db import create_sessionmaker, tenant_session
from bido_back.db.models import Rfq
from bido_back.rfqs import service as rfq_service
from bido_back.rfqs.agents import RfqAgents
from bido_back.rfqs.mailbox import MailboxSenderFactory
from bido_back.rfqs.schemas import UpdateRfqRequest
from bido_back.settings import Settings
from tests.approvals.conftest import People

from .conftest import FakeMailboxes, fake_agents

UNUSED_DB = "postgresql+asyncpg://app@localhost:5432/unused"


def build_app() -> FastAPI:
    app = FastAPI()
    app.state.resources = ApiResources(
        Settings.model_validate({"environment": "test", "database_url": UNUSED_DB})
    )
    for router in (
        rfqs_router(),
        suppliers_router(),
        approvals_router(),
        me_router(),
        templates_router(),
    ):
        app.include_router(router)
    return app


async def test_routes_require_authentication() -> None:
    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        responses = [
            await http.get("/v1/rfqs"),
            await http.post("/v1/rfqs", json={"text": "x", "currency": "EUR"}),
            await http.get("/v1/suppliers"),
            await http.get("/v1/approvals"),
            await http.get(f"/v1/rfqs/{uuid4()}/comparison"),
            await http.get("/v1/me"),
            await http.patch(f"/v1/rfqs/{uuid4()}", json={"title": "x"}),
            await http.post(
                f"/v1/rfqs/{uuid4()}/lines", json={"description": "x", "quantities": [1]}
            ),
            await http.delete(f"/v1/rfqs/{uuid4()}/lines/{uuid4()}"),
            await http.delete(f"/v1/rfqs/{uuid4()}/invites/{uuid4()}"),
            await http.get(f"/v1/rfqs/{uuid4()}/invites/{uuid4()}/document.pdf"),
            await http.get(f"/v1/rfqs/{uuid4()}/document.pdf"),
        ]
    assert [r.status_code for r in responses] == [401] * len(responses)


@dataclass
class Api:
    http: httpx.AsyncClient
    mailboxes: FakeMailboxes
    principal: Principal


@pytest_asyncio.fixture
async def api(app_engine: AsyncEngine, rfq_rows: People) -> AsyncIterator[Api]:
    app = build_app()
    sessionmaker = create_sessionmaker(app_engine)
    agents = fake_agents()
    mailboxes = FakeMailboxes()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        state = Api(
            http=http,
            mailboxes=mailboxes,
            principal=Principal(tenant_id=rfq_rows.tenant, user_id=rfq_rows.owner, role="owner"),
        )

        def principal() -> Principal:
            return state.principal

        def db() -> async_sessionmaker[AsyncSession]:
            return sessionmaker

        def rfq_agents() -> RfqAgents:
            return agents

        def mailbox_senders() -> MailboxSenderFactory:
            return mailboxes

        app.dependency_overrides[get_principal] = principal
        app.dependency_overrides[get_db] = db
        app.dependency_overrides[get_rfq_agents] = rfq_agents
        app.dependency_overrides[get_mailbox_senders] = mailbox_senders
        yield state


@pytest.mark.db
async def test_rfq_flow_over_http(api: Api, rfq_rows: People) -> None:
    http = api.http

    created = await http.post(
        "/v1/rfqs", json={"text": "Need MLCC and connectors", "currency": "EUR"}
    )
    assert created.status_code == 201, created.text
    rfq = created.json()
    assert rfq["ref"] == "RFQ-1" and rfq["status"] == "draft"
    assert rfq["questions"] == ["Which delivery address?"]
    assert [line["quantities"] for line in rfq["lines"]] == [["4000", "10000"], ["500"]]

    listed = (await http.get("/v1/rfqs")).json()
    assert [r["id"] for r in listed] == [rfq["id"]]

    line_id = rfq["lines"][1]["id"]
    patched = await http.patch(
        f"/v1/rfqs/{rfq['id']}/lines/{line_id}", json={"quantities": [250, 1000], "unit": "pcs"}
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["quantities"] == ["250", "1000"]
    assert (
        await http.patch(f"/v1/rfqs/{rfq['id']}/lines/{line_id}", json={"price": 1})
    ).status_code == 422

    supplier = await http.post(
        "/v1/suppliers",
        json={
            "name": "Rhein Elektronik",
            "type": "authorized_distributor",
            "country": "DE",
            "language": "de",
            "contacts": [{"name": "Jonas Weber", "email": "jonas@rhein.example.test"}],
        },
    )
    assert supplier.status_code == 201, supplier.text
    supplier_body = supplier.json()
    assert [s["name"] for s in (await http.get("/v1/suppliers")).json()] == ["Rhein Elektronik"]
    bad_email = await http.post(
        "/v1/suppliers", json={"name": "X", "contacts": [{"email": "a@b.c\r\nBcc: x@y.z"}]}
    )
    assert bad_email.status_code == 422

    invite = await http.post(
        f"/v1/rfqs/{rfq['id']}/invites",
        json={
            "supplier_id": supplier_body["id"],
            "contact_id": supplier_body["contacts"][0]["id"],
        },
    )
    assert invite.status_code == 201, invite.text
    duplicate = await http.post(
        f"/v1/rfqs/{rfq['id']}/invites",
        json={
            "supplier_id": supplier_body["id"],
            "contact_id": supplier_body["contacts"][0]["id"],
        },
    )
    assert duplicate.status_code == 409

    sent = await http.post(f"/v1/rfqs/{rfq['id']}/send")
    assert sent.status_code == 200, sent.text
    [prepared] = sent.json()["approvals"]
    assert prepared["invite_id"] == invite.json()["id"]

    detail = (await http.get(f"/v1/rfqs/{rfq['id']}")).json()
    assert [i["status"] for i in detail["invites"]] == ["pending_approval"]
    assert "reply_token" not in detail["invites"][0]
    # The supplier's own language: what its email and PDF were rendered in.
    assert detail["invites"][0]["document_language"] == "de"
    # Nothing known about a supplier: Bido's standard template is Portuguese.
    assert detail["document_language"] == "pt"

    # Lines are frozen once an email was prepared from them.
    frozen = await http.patch(f"/v1/rfqs/{rfq['id']}/lines/{line_id}", json={"unit": "kg"})
    assert frozen.status_code == 409

    api.principal = Principal(tenant_id=rfq_rows.tenant, user_id=rfq_rows.approver, role="approver")
    queue = await http.get("/v1/approvals", params={"status": "pending"})
    assert queue.status_code == 200
    [item] = [i for i in queue.json() if i["id"] == prepared["approval_id"]]
    assert item["tool"] == "send_rfq_email" and item["requested_by_me"] is False
    email = item["rfq_email"]
    assert email["to"] == ["jonas@rhein.example.test"]
    assert email["subject"].startswith("[RFQ-1] ")
    assert email["sender_user_id"] == str(rfq_rows.owner)
    # The approver previews the cover note (HTML) and the attached RFQ document.
    assert "[RFQ-1]" in (email["html_body"] or "") and "<table" not in email["html_body"]
    attachment = email["attachment_name"]
    assert attachment.startswith("RFQ-1-") and attachment.endswith(".pdf")

    comparison = await http.get(f"/v1/rfqs/{rfq['id']}/comparison")
    assert comparison.status_code == 200, comparison.text
    body = comparison.json()
    assert body["suppliers"] == []
    assert [a["status"] for a in body["awaiting"]] == ["pending_approval"]

    me = await http.get("/v1/me")
    assert me.status_code == 200
    assert me.json()["user_id"] == str(rfq_rows.approver)
    assert me.json()["role"] == "approver"
    assert me.json()["allow_self_approval"] is True

    # Another tenant sees nothing.
    api.principal = Principal(tenant_id=rfq_rows.other_tenant, user_id=rfq_rows.other_approver)
    assert (await http.get(f"/v1/rfqs/{rfq['id']}")).status_code == 404
    assert (await http.get(f"/v1/rfqs/{rfq['id']}/comparison")).status_code == 404
    assert (await http.get("/v1/rfqs")).json() == []
    assert (await http.get("/v1/approvals")).json() == []


@pytest.mark.db
async def test_rfq_detail_and_list_show_payment_terms_and_quote_validity(
    api: Api, app_engine: AsyncEngine
) -> None:
    http = api.http
    created = (await http.post("/v1/rfqs", json={"text": "Need MLCC", "currency": "EUR"})).json()
    assert (created["payment_terms"], created["quote_validity_days"]) == (None, None)

    await rfq_service.update_rfq(
        app_engine,
        api.principal,
        UUID(created["id"]),
        UpdateRfqRequest(payment_terms="28 DDL", quote_validity_days=30),
    )

    detail = (await http.get(f"/v1/rfqs/{created['id']}")).json()
    [listed] = (await http.get("/v1/rfqs")).json()
    for body in (detail, listed):
        assert (body["payment_terms"], body["quote_validity_days"]) == ("28 DDL", 30)


@pytest.mark.db
async def test_invite_says_which_language_its_document_uses(api: Api) -> None:
    """The preview mirrors the PDF only when it knows the language the send will
    pick for each supplier (`bido_back.rfqs.language`): the supplier's own, else
    its country's, else its legal form's, else the buyer's (the RFQ default; this
    tenant uses Bido's Portuguese standard template)."""
    http = api.http
    created = (await http.post("/v1/rfqs", json={"text": "Need MLCC", "currency": "EUR"})).json()
    assert created["document_language"] == "pt"
    assert created["invites"] == []

    async def invite(supplier: dict[str, object]) -> dict[str, object]:
        body = (await http.post("/v1/suppliers", json=supplier)).json()
        contacts = cast(list[dict[str, object]], body["contacts"])
        response = await http.post(
            f"/v1/rfqs/{created['id']}/invites",
            json={"supplier_id": body["id"], "contact_id": contacts[0]["id"]},
        )
        assert response.status_code == 201, response.text
        return response.json()

    portuguese = await invite(
        {
            "name": "Condensadores do Minho",
            "language": "pt",
            "contacts": [{"name": "Rita Sousa", "email": "rita@minho.example.test"}],
        }
    )
    assert portuguese["document_language"] == "pt"
    # No language of its own: the country's when known (`suppliers.language` is
    # "en" by default, so the country decides for a supplier imported with one).
    brazilian = await invite(
        {
            "name": "Capacitores Paulista",
            "country": "BR",
            "contacts": [{"email": "vendas@paulista.example.test"}],
        }
    )
    assert brazilian["document_language"] == "pt"
    # Nothing known about the supplier (RFQ-6): the RFQ default, the buyer's language.
    unknown = await invite(
        {"name": "Nowhere Components", "contacts": [{"email": "sales@nowhere.example.test"}]}
    )
    assert unknown["document_language"] == "pt"
    english = await invite(
        {"name": "Pennine Components Ltd", "contacts": [{"email": "sales@pennine.example.test"}]}
    )
    assert english["document_language"] == "en"

    detail = (await http.get(f"/v1/rfqs/{created['id']}")).json()
    assert detail["document_language"] == "pt"
    assert [(i["supplier_name"], i["document_language"]) for i in detail["invites"]] == [
        ("Condensadores do Minho", "pt"),
        ("Capacitores Paulista", "pt"),
        ("Nowhere Components", "pt"),
        ("Pennine Components Ltd", "en"),
    ]
    [listed] = (await http.get("/v1/rfqs")).json()
    assert listed["document_language"] == "pt"


@pytest.mark.db
async def test_send_without_a_connected_mailbox_is_409(api: Api, rfq_rows: People) -> None:
    rfq = (await api.http.post("/v1/rfqs", json={"text": "Need parts", "currency": "EUR"})).json()
    api.mailboxes.disconnected.add(rfq_rows.owner)

    response = await api.http.post(f"/v1/rfqs/{rfq['id']}/send")

    assert response.status_code == 409
    assert "mailbox" in response.json()["detail"]


@pytest.mark.db
async def test_send_without_a_reply_by_date_is_409_and_the_rfq_says_so(
    api: Api, app_engine: AsyncEngine, rfq_rows: People
) -> None:
    http = api.http
    rfq = (await http.post("/v1/rfqs", json={"text": "Need parts", "currency": "EUR"})).json()
    assert rfq["send_blockers"] == []
    async with tenant_session(app_engine, rfq_rows.tenant) as session:
        await session.execute(update(Rfq).where(Rfq.id == rfq["id"]).values(response_due=None))
    detail = (await http.get(f"/v1/rfqs/{rfq['id']}")).json()
    assert detail["send_blockers"] == ["reply_by_missing"]
    listed = (await http.get("/v1/rfqs")).json()
    assert [r["send_blockers"] for r in listed] == [["reply_by_missing"]]
    supplier = (
        await http.post(
            "/v1/suppliers",
            json={"name": "Rhein", "contacts": [{"email": "jonas@rhein.example.test"}]},
        )
    ).json()
    invite = await http.post(
        f"/v1/rfqs/{rfq['id']}/invites",
        json={"supplier_id": supplier["id"], "contact_id": supplier["contacts"][0]["id"]},
    )
    assert invite.status_code == 201, invite.text

    response = await http.post(f"/v1/rfqs/{rfq['id']}/send")

    assert response.status_code == 409
    assert response.json()["detail"].startswith(f"{rfq['ref']} has no reply-by date")
    assert (await http.get("/v1/approvals")).json() == []
    assert api.mailboxes.calls == []


async def test_create_needs_a_currency_it_can_tell(api: Api, rfq_rows: People) -> None:
    http = api.http

    # No purchase history and no currency in the request: refused, nothing created.
    unknown = await http.post("/v1/rfqs", json={"text": "Need parts"})
    assert unknown.status_code == 400, unknown.text
    assert "ask the buyer which currency this RFQ is in" in unknown.json()["detail"]
    assert (await http.get("/v1/rfqs")).json() == []

    invalid = await http.post("/v1/rfqs", json={"text": "Need parts", "currency": "XYZ"})
    assert invalid.status_code == 422

    created = await http.post("/v1/rfqs", json={"text": "Need parts", "currency": "brl"})
    assert created.status_code == 201, created.text
    assert (created.json()["base_currency"], created.json()["currency_source"]) == (
        "BRL",
        "stated",
    )


async def _invite(http: httpx.AsyncClient, rfq_id: str, name: str, email: str) -> dict[str, object]:
    supplier = (
        await http.post("/v1/suppliers", json={"name": name, "contacts": [{"email": email}]})
    ).json()
    invite = await http.post(
        f"/v1/rfqs/{rfq_id}/invites",
        json={"supplier_id": supplier["id"], "contact_id": supplier["contacts"][0]["id"]},
    )
    assert invite.status_code == 201, invite.text
    return invite.json()


@pytest.mark.db
async def test_draft_rfq_is_editable_over_http_until_an_email_is_prepared(
    api: Api, rfq_rows: People
) -> None:
    http = api.http
    rfq = (
        await http.post("/v1/rfqs", json={"text": "Need MLCC and connectors", "currency": "EUR"})
    ).json()
    rfq_id = rfq["id"]

    # The RFQ itself: only the fields present change.
    patched = await http.patch(
        f"/v1/rfqs/{rfq_id}",
        json={
            "title": "Connectors Q4",
            "response_due": "2099-01-15",
            "payment_terms": "30 days net",
            "quote_validity_days": 45,
            "incoterm": "DAP",
            "delivery_location": "Vila Solana 732",
        },
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert (body["title"], body["response_due"]) == ("Connectors Q4", "2099-01-15")
    assert (body["payment_terms"], body["quote_validity_days"]) == ("30 days net", 45)
    assert {(line["incoterm"], line["delivery_location"]) for line in body["lines"]} == {
        ("DAP", "Vila Solana 732")
    }
    assert (await http.patch(f"/v1/rfqs/{rfq_id}", json={"title": None})).status_code == 409
    assert (await http.patch(f"/v1/rfqs/{rfq_id}", json={"price": 1})).status_code == 422

    # Lines: add at the end, remove, never the last one.
    added = await http.post(
        f"/v1/rfqs/{rfq_id}/lines",
        json={
            "description": "Steel sheet 3mm",
            "part_number": "100000101",
            "quantities": [7500],
            "unit": "kg",
            "need_by": "2099-01-31",
        },
    )
    assert added.status_code == 201, added.text
    lines = added.json()["lines"]
    assert [line["position"] for line in lines] == [1, 2, 3]
    assert (lines[-1]["part_number"], lines[-1]["unit"], lines[-1]["quantities"]) == (
        "100000101",
        "kg",
        ["7500"],
    )
    removed = await http.delete(f"/v1/rfqs/{rfq_id}/lines/{lines[0]['id']}")
    assert removed.status_code == 200, removed.text
    assert [line["position"] for line in removed.json()["lines"]] == [1, 2]
    assert (await http.delete(f"/v1/rfqs/{rfq_id}/lines/{uuid4()}")).status_code == 404
    remaining = removed.json()["lines"]
    assert (await http.delete(f"/v1/rfqs/{rfq_id}/lines/{remaining[0]['id']}")).status_code == 200
    last = await http.delete(f"/v1/rfqs/{rfq_id}/lines/{remaining[1]['id']}")
    assert last.status_code == 409

    # Invites: a draft invite can be taken off.
    dropped = await _invite(http, rfq_id, "Rhein", "jonas@rhein.example.test")
    kept = await _invite(http, rfq_id, "Main", "ana@main.example.test")
    after = await http.delete(f"/v1/rfqs/{rfq_id}/invites/{dropped['id']}")
    assert after.status_code == 200, after.text
    assert [i["id"] for i in after.json()["invites"]] == [kept["id"]]
    assert (await http.delete(f"/v1/rfqs/{rfq_id}/invites/{dropped['id']}")).status_code == 404

    # Once an email is prepared, its approval's content can never go stale: every edit is 409.
    sent = await http.post(f"/v1/rfqs/{rfq_id}/send")
    assert sent.status_code == 200, sent.text
    [prepared] = sent.json()["approvals"]
    frozen = [
        await http.patch(f"/v1/rfqs/{rfq_id}", json={"title": "Changed"}),
        await http.post(f"/v1/rfqs/{rfq_id}/lines", json={"description": "x", "quantities": [1]}),
        await http.delete(f"/v1/rfqs/{rfq_id}/lines/{remaining[1]['id']}"),
        await http.delete(f"/v1/rfqs/{rfq_id}/invites/{kept['id']}"),
    ]
    assert [r.status_code for r in frozen] == [409, 409, 409, 409]
    detail = (await http.get(f"/v1/rfqs/{rfq_id}")).json()
    assert detail["title"] == "Connectors Q4"
    assert [i["status"] for i in detail["invites"]] == ["pending_approval"]
    queue = (await http.get("/v1/approvals", params={"status": "pending"})).json()
    assert [i["id"] for i in queue] == [prepared["approval_id"]]

    # Another tenant can neither see nor edit it.
    api.principal = Principal(tenant_id=rfq_rows.other_tenant, user_id=rfq_rows.other_approver)
    assert (await http.patch(f"/v1/rfqs/{rfq_id}", json={"title": "x"})).status_code == 404
    assert (
        await http.post(f"/v1/rfqs/{rfq_id}/lines", json={"description": "x", "quantities": [1]})
    ).status_code == 404
    assert (await http.delete(f"/v1/rfqs/{rfq_id}/invites/{kept['id']}")).status_code == 404


@pytest.mark.db
async def test_invite_document_pdf_over_http(api: Api, rfq_rows: People) -> None:
    http = api.http
    rfq = (
        await http.post("/v1/rfqs", json={"text": "Need MLCC and connectors", "currency": "EUR"})
    ).json()
    assert rfq["status"] == "draft"
    invite = await _invite(http, rfq["id"], "Elbe", "sales@elbe.example.test")
    # A draft invite (nothing proposed yet) renders from the RFQ as it is now.
    assert invite["status"] == "draft"
    response = await http.get(f"/v1/rfqs/{rfq['id']}/invites/{invite['id']}/document.pdf")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"] == f'inline; filename="{rfq["ref"]}-elbe.pdf"'
    assert response.headers["cache-control"] == "no-store"
    assert response.content.startswith(b"%PDF")

    missing = await http.get(f"/v1/rfqs/{rfq['id']}/invites/{uuid4()}/document.pdf")
    assert missing.status_code == 404
    api.principal = Principal(tenant_id=rfq_rows.other_tenant, user_id=rfq_rows.other_approver)
    other = await http.get(f"/v1/rfqs/{rfq['id']}/invites/{invite['id']}/document.pdf")
    assert other.status_code == 404


@pytest.mark.db
async def test_rfq_document_pdf_over_http(api: Api, rfq_rows: People) -> None:
    http = api.http
    rfq = (
        await http.post("/v1/rfqs", json={"text": "Need MLCC and connectors", "currency": "EUR"})
    ).json()
    # A draft with no supplier yet has a document: the buyer's download from the RFQ page.
    assert rfq["status"] == "draft" and rfq["invites"] == []
    response = await http.get(f"/v1/rfqs/{rfq['id']}/document.pdf")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"] == f'attachment; filename="{rfq["ref"]}.pdf"'
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.content.startswith(b"%PDF")

    # Still there once suppliers are invited and a send is proposed; the file name
    # never carries a supplier.
    await _invite(http, rfq["id"], "Elbe", "sales@elbe.example.test")
    sent = await http.post(f"/v1/rfqs/{rfq['id']}/send")
    assert sent.status_code == 200, sent.text
    again = await http.get(f"/v1/rfqs/{rfq['id']}/document.pdf")
    assert again.status_code == 200, again.text
    assert again.headers["content-disposition"] == f'attachment; filename="{rfq["ref"]}.pdf"'

    assert (await http.get(f"/v1/rfqs/{uuid4()}/document.pdf")).status_code == 404
    api.principal = Principal(tenant_id=rfq_rows.other_tenant, user_id=rfq_rows.other_approver)
    other = await http.get(f"/v1/rfqs/{rfq['id']}/document.pdf")
    assert other.status_code == 404
