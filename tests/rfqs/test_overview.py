"""The RFQ list row (stage, next step, suppliers, totals), payment terms and the activity feed."""

from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.api.deps import ApiResources, get_db, get_principal
from bido_back.api.rfqs import rfqs_router
from bido_back.auth import Principal
from bido_back.comparison.loader import load_comparison
from bido_back.db import create_sessionmaker, tenant_session
from bido_back.db.models import AuditEvent, Message, Proposal
from bido_back.rfqs import service
from bido_back.rfqs.activity import rfq_activity
from bido_back.rfqs.audit import changes
from bido_back.rfqs.errors import NotFound
from bido_back.rfqs.overview import RfqFacts, derive_next_step, derive_stage, list_rfq_overviews
from bido_back.rfqs.schemas import AddLineRequest, UpdateLineRequest, UpdateRfqRequest
from bido_back.settings import Settings
from tests.approvals.conftest import People

from .conftest import FakeIntake, FakeMailboxes, user_email
from .test_corrections import quoted_rfq
from .test_service import buyer, make_rfq, make_supplier, prepared

UNUSED_DB = "postgresql+asyncpg://app@localhost:5432/unused"
TODAY = date(2026, 10, 7)


# --- Derivation (no database) ---------------------------------------------------------------


def facts(
    *statuses: str, pending: int = 0, awarded: bool = False, pos: tuple[str, ...] = ()
) -> RfqFacts:
    """`pos`: each PO's fulfilment state (not_sent = still to send)."""
    return RfqFacts(
        suppliers=[f"S{i}" for i in range(len(statuses))],
        invite_statuses=list(statuses),
        pending_approvals=pending,
        awarded=awarded,
        po_states=list(pos),
    )


@pytest.mark.parametrize(
    ("status", "negotiation", "fx", "stage", "step"),
    [
        ("draft", "not_started", facts(), "draft", "send RFQ"),
        ("draft", "not_started", facts("pending_approval", pending=1), "draft", "approve"),
        ("open", "not_started", facts("sent", "sent"), "bidding", "waiting for suppliers"),
        ("open", "not_started", facts("responded", "sent"), "bidding", "waiting for suppliers"),
        ("open", "not_started", facts("responded", "declined"), "review", "review quotes"),
        ("open", "open", facts("responded", "sent", pending=2), "bidding", "approve"),
        ("open", "ready_to_close", facts("responded", "responded"), "review", "close negotiation"),
        ("open", "closed", facts("responded"), "review", "award"),
        (
            "closed",
            "closed",
            facts("responded", awarded=True, pos=("sent", "not_sent")),
            "awarded",
            "send PO",
        ),
        # Sent is not done: goods are still expected.
        (
            "closed",
            "closed",
            facts("responded", awarded=True, pos=("sent", "sent")),
            "awarded",
            None,
        ),
        (
            "closed",
            "closed",
            facts("responded", awarded=True, pos=("closed", "partially_received")),
            "awarded",
            None,
        ),
        (
            "closed",
            "closed",
            facts("responded", awarded=True, pos=("closed", "closed")),
            "completed",
            None,
        ),
        (
            "closed",
            "closed",
            facts("responded", awarded=True, pos=("cancelled", "closed")),
            "completed",
            None,
        ),
        # Everything cancelled, nothing closed: not completed.
        ("closed", "closed", facts("responded", awarded=True, pos=("cancelled",)), "awarded", None),
        ("closed", "not_started", facts("responded"), "completed", None),
        ("cancelled", "not_started", facts("sent", pending=1), "cancelled", None),
    ],
)
def test_stage_and_next_step(
    status: str, negotiation: str, fx: RfqFacts, stage: str, step: str | None
) -> None:
    assert derive_stage(status, negotiation, fx) == stage
    assert derive_next_step(status, negotiation, fx) == step


# --- Against Postgres -----------------------------------------------------------------------


@pytest.mark.db
async def test_list_row_has_suppliers_need_by_owner_and_filters(
    app_engine: AsyncEngine, rfq_rows: People
) -> None:
    principal = buyer(rfq_rows)
    quoted = await quoted_rfq(app_engine, principal)

    page = await list_rfq_overviews(app_engine, principal, today=TODAY, q=quoted.rfq.ref)
    [row] = [item for item in page.items if item.id == quoted.rfq.id]
    assert row.supplier_names and row.supplier_names[0].startswith("Rhein")
    assert row.invited_count == 1
    assert row.base_currency == "EUR"
    # Nothing known about a supplier: Bido's (Portuguese) standard template decides.
    assert row.document_language == "pt"
    assert row.owner is not None
    expected_need_by = min(
        (line.need_by for line in quoted.rfq.lines if line.need_by is not None), default=None
    )
    assert row.need_by == expected_need_by
    assert row.stage == "draft" and row.next_step == "send RFQ"
    assert page.total >= 1

    # Filters: status, stage and a search that matches nothing.
    drafts = await list_rfq_overviews(app_engine, principal, today=TODAY, status="draft")
    assert all(item.status == "draft" for item in drafts.items)
    assert any(item.id == quoted.rfq.id for item in drafts.items)
    bidding = await list_rfq_overviews(app_engine, principal, today=TODAY, stage="bidding")
    assert all(item.stage == "bidding" for item in bidding.items)
    nothing = await list_rfq_overviews(app_engine, principal, today=TODAY, q=f"zz{uuid4().hex}")
    assert nothing.items == [] and nothing.total == 0
    # Wildcards are literal.
    assert (await list_rfq_overviews(app_engine, principal, today=TODAY, q="%")).total == 0

    paged = await list_rfq_overviews(app_engine, principal, today=TODAY, limit=1)
    assert len(paged.items) == 1 and paged.total >= 1

    # Another tenant sees none of it.
    outsider = Principal(tenant_id=rfq_rows.other_tenant, user_id=rfq_rows.other_approver)
    other = await list_rfq_overviews(app_engine, outsider, today=TODAY, q=quoted.rfq.ref)
    assert other.items == []


@pytest.mark.db
async def test_pending_send_approval_makes_the_next_step_approve(
    app_engine: AsyncEngine, rfq_rows: People
) -> None:
    rfq, _, _, _ = await prepared(app_engine, rfq_rows, FakeMailboxes())
    page = await list_rfq_overviews(app_engine, buyer(rfq_rows), today=TODAY, q=rfq.ref)
    [row] = [item for item in page.items if item.id == rfq.id]
    assert row.next_step == "approve"


@pytest.mark.db
async def test_comparison_carries_payment_terms(app_engine: AsyncEngine, rfq_rows: People) -> None:
    principal = buyer(rfq_rows)
    quoted = await quoted_rfq(app_engine, principal)
    async with tenant_session(app_engine, principal.tenant_id) as session:
        await session.execute(
            update(Proposal)
            .where(Proposal.id == quoted.proposal_id)
            .values(payment_terms="30 days net")
        )
    comparison = await load_comparison(app_engine, principal, quoted.rfq.id, TODAY)
    [column] = comparison.suppliers
    assert column.payment_terms == "30 days net"


def test_audit_changes_are_json_safe_and_only_what_changed() -> None:
    before = {
        "quantities": [500],
        "need_by": None,
        "description": "x" * 300,
        "price": Decimal("1.50"),
        "ref": UUID(int=1),
    }
    after = {
        "quantities": [Decimal(1500)],
        "need_by": date(2026, 1, 2),
        "description": "x" * 300,
        "price": Decimal("1.50"),
        "ref": UUID(int=2),
    }
    assert changes(before, after) == {
        "quantities": {"from": [500], "to": [1500]},
        "need_by": {"from": None, "to": "2026-01-02"},
        "ref": {"from": str(UUID(int=1)), "to": str(UUID(int=2))},
    }


@pytest.mark.db
async def test_activity_shows_the_rfq_lifecycle_with_its_actor(
    app_engine: AsyncEngine, rfq_rows: People
) -> None:
    principal = buyer(rfq_rows)
    me = await user_email(app_engine, rfq_rows.owner)
    rfq = await make_rfq(app_engine, principal)
    drafted = await service.draft_rfq_from_text(
        app_engine, principal, "via the copilot", FakeIntake(), currency="EUR"
    )
    from_copilot = await service.store_text_draft(app_engine, principal, drafted, origin="copilot")
    supplier = await make_supplier(app_engine, principal, "Rhein")

    connectors = rfq.lines[1]
    await service.update_line(
        app_engine,
        principal,
        rfq.id,
        connectors.id,
        UpdateLineRequest(quantities=[Decimal(1500)], part_number="CON-40"),
    )
    await service.update_rfq(
        app_engine,
        principal,
        rfq.id,
        UpdateRfqRequest(response_due=date(2099, 12, 1), incoterm="FCA"),
    )
    invite = await service.add_invite(
        app_engine, principal, rfq.id, supplier.id, supplier.contacts[0].id
    )
    added = await service.add_line(
        app_engine,
        principal,
        rfq.id,
        AddLineRequest(description="Cable", quantities=[Decimal(50)], unit="m"),
    )
    await service.remove_line(app_engine, principal, rfq.id, added.lines[-1].id)
    await service.remove_invite(app_engine, principal, rfq.id, invite.id)

    feed = await rfq_activity(app_engine, principal, rfq.id)
    assert [(item.action, item.summary) for item in feed] == [
        ("rfq.invite_removed", "Supplier removed: Rhein"),
        ("rfq.line_removed", "Line 3 removed: Cable"),
        ("rfq.line_added", "Line 3 added: Cable"),
        ("rfq.invite_added", "Supplier added: Rhein"),
        (
            "rfq.terms_set",
            "Terms updated: response deadline 2099-11-01 -> 2099-12-01, "
            "Incoterm set to FCA on 2 lines",
        ),
        ("rfq.line_updated", "Line 2 updated: quantity 500 -> 1500, part number set to CON-40"),
        ("rfq.created", "RFQ created with 2 lines"),
    ]
    assert {item.kind for item in feed} == {"user"}
    assert {item.actor for item in feed} == {me}
    assert [item.supplier_name for item in feed if item.action.startswith("rfq.invite")] == [
        "Rhein",
        "Rhein",
    ]

    [created] = [i for i in await rfq_activity(app_engine, principal, from_copilot.id)]
    assert created.summary == "RFQ created from the Copilot with 2 lines" and created.actor == me

    async with tenant_session(app_engine, principal.tenant_id) as session:
        events = {
            event.action: event
            for event in await session.scalars(
                select(AuditEvent).where(AuditEvent.payload["rfq_id"].astext == str(rfq.id))
            )
        }
    assert all(event.actor_user_id == rfq_rows.owner for event in events.values())
    assert events["rfq.created"].payload == {
        "rfq_id": str(rfq.id),
        "ref": rfq.ref,
        "origin": "api",
        "lines": 2,
        "currency": "EUR",
    }
    assert events["rfq.line_updated"].payload["changes"] == {
        "part_number": {"from": None, "to": "CON-40"},
        "quantities": {"from": [500], "to": [1500]},
    }
    assert events["rfq.terms_set"].payload["line_fields"] == {"incoterm": "FCA"}
    assert events["rfq.invite_added"].payload["supplier_id"] == str(supplier.id)


@pytest.mark.db
async def test_activity_lists_replies_and_approvals_without_content(
    app_engine: AsyncEngine, rfq_rows: People
) -> None:
    principal = buyer(rfq_rows)
    rfq, _, _, _ = await prepared(app_engine, rfq_rows, FakeMailboxes())
    quoted = await quoted_rfq(app_engine, principal)
    async with tenant_session(app_engine, principal.tenant_id) as session:
        await session.execute(
            update(Message)
            .where(Message.direction == "in")
            .values(subject="SECRET subject", body_text="SECRET body")
        )

    sent = await rfq_activity(app_engine, principal, rfq.id)
    assert any(item.action == "approval.requested" for item in sent)
    assert any("RFQ email" in item.summary for item in sent)

    replies = await rfq_activity(app_engine, principal, quoted.rfq.id)
    [reply] = [item for item in replies if item.action == "reply"]
    assert reply.kind == "email" and reply.supplier_name and reply.supplier_name.startswith("Rhein")
    # The offer Bido read from that reply has its own line, after the reply itself.
    [received] = [item for item in replies if item.action == "quote.received"]
    assert (received.kind, received.actor) == ("agent", "Bido")
    assert received.supplier_name == reply.supplier_name
    assert received.summary == f"Quote received from {reply.supplier_name} (version 1)"
    assert received.at >= reply.at
    assert all("SECRET" not in item.summary for item in [*sent, *replies])
    assert [item.at for item in replies] == sorted((item.at for item in replies), reverse=True)

    outsider = Principal(tenant_id=rfq_rows.other_tenant, user_id=rfq_rows.other_approver)
    with pytest.raises(NotFound):
        await rfq_activity(app_engine, outsider, rfq.id)


# --- Over HTTP ------------------------------------------------------------------------------


def build_app() -> FastAPI:
    app = FastAPI()
    app.state.resources = ApiResources(
        Settings.model_validate({"environment": "test", "database_url": UNUSED_DB})
    )
    app.include_router(rfqs_router())
    return app


@pytest_asyncio.fixture
async def http(app_engine: AsyncEngine, rfq_rows: People) -> AsyncIterator[httpx.AsyncClient]:
    app = build_app()
    sessionmaker = create_sessionmaker(app_engine)

    def principal() -> Principal:
        return buyer(rfq_rows)

    def db() -> async_sessionmaker[AsyncSession]:
        return sessionmaker

    app.dependency_overrides[get_principal] = principal
    app.dependency_overrides[get_db] = db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.mark.db
async def test_list_and_activity_over_http(
    http: httpx.AsyncClient, app_engine: AsyncEngine, rfq_rows: People
) -> None:
    quoted = await quoted_rfq(app_engine, buyer(rfq_rows))

    listing = await http.get("/v1/rfqs", params={"q": quoted.rfq.ref, "limit": 5})
    assert listing.status_code == 200, listing.text
    assert int(listing.headers["x-total-count"]) >= 1
    [row] = [item for item in listing.json() if item["id"] == str(quoted.rfq.id)]
    assert row["stage"] == "draft" and row["invited_count"] == 1

    assert (await http.get("/v1/rfqs", params={"stage": "nope"})).status_code == 422
    assert (await http.get("/v1/rfqs", params={"limit": 0})).status_code == 422

    activity = await http.get(f"/v1/rfqs/{quoted.rfq.id}/activity", params={"limit": 10})
    assert activity.status_code == 200
    assert any(item["action"] == "reply" for item in activity.json())
    assert (await http.get(f"/v1/rfqs/{uuid4()}/activity")).status_code == 404
