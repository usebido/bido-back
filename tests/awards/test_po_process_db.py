"""The purchase order end to end against Postgres: the PDF it carries, the supplier's
reply (routed, read, acknowledged or flagged, never applied), the buyer's review, the
cancellation told to the supplier once, and awards that break the approved limits."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.agents.catalog.contracts import (
    PoResponseInput,
    PoResponseLine,
    PoResponseOutput,
    Quoted,
)
from bido_back.agents.context import ToolContext
from bido_back.awards import lifecycle, service
from bido_back.awards.cancellation import SEND_PO_CANCELLATION, request_cancellation_notice
from bido_back.awards.responses import handle_po_response
from bido_back.awards.schemas import (
    AcknowledgeRequest,
    AwardRequest,
    AwardSelection,
    CancelRequest,
    ReviewRequest,
)
from bido_back.awards.service import ReasonRequired
from bido_back.db import tenant_session
from bido_back.db.models import (
    Approval,
    Invite,
    IssuedPurchaseOrder,
    MailboxConnection,
    Message,
    Proposal,
    RfqLine,
)
from bido_back.email.mime import build_mime
from bido_back.email.types import OutboundEmail
from bido_back.intake.service import InboundService
from bido_back.mailbox.service import _load_sync_work  # pyright: ignore[reportPrivateUsage]
from bido_back.rfqs.activity import rfq_activity
from bido_back.rfqs.errors import InvalidState
from bido_back.worker.envelope import Envelope
from bido_back.worker.job_types import PO_RESPONSE, QUOTE_EXTRACT
from tests.approvals.conftest import FakeJobQueue, People

from .test_awards_db import EMAIL_DOMAIN, TODAY, World, buyer
from .test_lifecycle import sent_po

pytestmark = pytest.mark.db


class FakeStorage:
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        self.blobs[key] = data

    async def get(self, key: str) -> bytes:
        return self.blobs[key]


class FakeReader:
    """The `po_response_reader` seam: answers the scripted reading, records each task."""

    def __init__(self, output: PoResponseOutput | None) -> None:
        self.output = output
        self.tasks: list[PoResponseInput] = []

    async def __call__(
        self, task: PoResponseInput, context: ToolContext
    ) -> PoResponseOutput | None:
        self.tasks.append(task)
        return self.output


@pytest.fixture
async def world(app_engine: AsyncEngine, award_rows: People) -> World:
    return await World(app_engine, award_rows).setup()


async def reply(world: World, po: IssuedPurchaseOrder, body: str) -> UUID:
    """The supplier's email in the PO's thread, stored as intake stores it."""
    async with tenant_session(world.engine, world.people.tenant) as session:
        message = Message(
            tenant_id=world.people.tenant,
            invite_id=po.invite_id,
            direction="in",
            message_id_header=f"<{uuid4()}@rhein.example.test>",
            subject=f"AW: {po.ref}",
            body_text=body,
            status="stored",
            occurred_at=datetime.now(UTC),
        )
        session.add(message)
        await session.flush()
        return message.id


async def read(world: World, message_id: UUID, reader: FakeReader) -> None:
    await handle_po_response(
        Envelope(
            type=PO_RESPONSE,
            tenant_id=world.people.tenant,
            idempotency_key=f"po-response:{message_id}",
            payload={"message_id": str(message_id)},
        ),
        db=world.engine,
        storage=FakeStorage(),
        reader=reader,
    )


async def proposals(world: World) -> int:
    async with tenant_session(world.engine, world.people.tenant) as session:
        return await session.scalar(select(func.count()).select_from(Proposal)) or 0


async def test_the_po_document_names_both_parties_lines_terms_and_the_approver(
    world: World,
) -> None:
    po = await sent_po(world)
    pdf = await service.render_purchase_order(world.engine, buyer(world.people), po.id, "pdf")
    assert pdf.media_type == "application/pdf" and pdf.content.startswith(b"%PDF")
    assert pdf.filename == f"{po.ref}.pdf"
    view = await service.get_purchase_order(world.engine, buyer(world.people), po.id)
    # The quotes carry DAP, 30 days net and 21 days lead time: nothing left blank.
    assert view.document_gaps == []
    assert view.supplier_responses == [] and not view.needs_review


async def test_a_reply_to_a_sent_po_is_routed_to_the_order_not_to_quote_extraction(
    world: World,
) -> None:
    jobs = FakeJobQueue()
    inbound = InboundService(world.engine, FakeStorage(), jobs, EMAIL_DOMAIN)

    def raw(body: str) -> bytes:
        return build_mime(
            OutboundEmail(
                from_name="Sales",
                from_address="sales@rhein.example.test",
                to=("buyer@example.test",),
                subject="AW: order",
                text_body=body,
                message_id=f"<{uuid4()}@rhein.example.test>",
            )
        ).as_bytes()

    invite = world.invites["Rhein"]
    await inbound.ingest_for_invite(
        raw("a question"), tenant_id=world.people.tenant, invite_id=invite
    )
    assert [j.type for j in jobs.jobs] == [QUOTE_EXTRACT]
    await sent_po(world)
    jobs.jobs.clear()
    await inbound.ingest_for_invite(
        raw("confirmed"), tenant_id=world.people.tenant, invite_id=invite
    )
    assert [j.type for j in jobs.jobs] == [PO_RESPONSE]


async def test_the_mailbox_keeps_reading_the_thread_of_a_sent_po_after_the_award(
    world: World,
) -> None:
    async with tenant_session(world.engine, world.people.tenant) as session:
        connection = MailboxConnection(
            tenant_id=world.people.tenant,
            user_id=world.people.owner,
            provider="google",
            email_address="owner@example.test",
            token_ciphertext=b"not-a-token",
            status="active",
        )
        session.add(connection)
        await session.flush()
        await session.execute(
            update(Invite)
            .where(Invite.rfq_id == world.rfq.id)
            .values(mailbox_connection_id=connection.id, provider_thread_id="thread-1")
        )

    async def threads() -> set[UUID]:
        return {
            t.invite_id
            for _, ts in await _load_sync_work(world.engine, world.people.tenant)
            for t in ts
        }

    assert world.invites["Rhein"] in await threads()
    await world.award()
    assert await threads() == set()  # awarded: the RFQ is closed, the PO not sent yet
    po = await sent_po_after_award(world)
    assert await threads() == {po.invite_id}
    await lifecycle.cancel_purchase_order(
        world.engine, buyer(world.people), po.id, CancelRequest(reason="test")
    )
    assert await threads() == set()


async def sent_po_after_award(world: World) -> IssuedPurchaseOrder:
    [po] = await world.orders()
    requested = await service.request_po_send(
        world.engine, buyer(world.people), po.id, world.mailboxes
    )
    await world.approve(requested.approval_id)
    [po] = await world.orders()
    return po


async def test_a_plain_confirmation_acknowledges_the_order_with_their_number(
    world: World,
) -> None:
    po = await sent_po(world)
    before = await proposals(world)
    body = "Wir bestätigen Ihre Bestellung PO-1. Auftragsbestätigung AB-4711."
    message_id = await reply(world, po, body)
    reader = FakeReader(
        PoResponseOutput(
            kind="confirmation",
            confirmation=Quoted(value=None, quote="Wir bestätigen Ihre Bestellung PO-1"),
            supplier_order_ref=Quoted(value="AB-4711", quote="AB-4711"),
            summary="Confirms PO-1.",
        )
    )
    await read(world, message_id, reader)
    [task] = reader.tasks
    assert task.po_ref == po.ref and [line.position for line in task.lines] == [1, 2]
    view = await service.get_purchase_order(world.engine, buyer(world.people), po.id)
    assert view.fulfilment.state == "acknowledged"
    assert view.fulfilment.supplier_order_ref == "AB-4711"
    assert not view.needs_review and await proposals(world) == before
    async with tenant_session(world.engine, world.people.tenant) as session:
        assert await session.scalar(select(Message.status).where(Message.id == message_id)) == (
            "extracted"
        )
    # Read once: the same job again changes nothing.
    await read(world, message_id, reader)
    assert len(reader.tasks) == 1
    feed = [i.summary for i in await rfq_activity(world.engine, buyer(world.people), world.rfq.id)]
    assert any("acknowledged" in s and "AB-4711" in s for s in feed)


async def test_a_new_price_is_flagged_for_the_buyer_and_never_applied(world: World) -> None:
    po = await sent_po(world)
    body = "Bestätigt. Position 2: neuer Einzelpreis 2,75 EUR."
    message_id = await reply(world, po, body)
    await read(
        world,
        message_id,
        FakeReader(
            PoResponseOutput(
                kind="confirmation_with_changes",
                confirmation=Quoted(value=None, quote="Bestätigt"),
                lines=[
                    PoResponseLine(
                        position=2, unit_price=Quoted(value="2,75", quote="Einzelpreis 2,75 EUR")
                    )
                ],
                summary="Line 2 at 2.75.",
            )
        ),
    )
    principal = buyer(world.people)
    view = await service.get_purchase_order(world.engine, principal, po.id)
    assert view.needs_review and view.fulfilment.state == "sent"
    [change] = view.supplier_responses[0].changes
    assert (change.position, change.field, change.stated) == (2, "unit_price", "2.75")
    assert view.lines[1].unit_price == Decimal("2.5")  # the order is unchanged
    [summary] = await service.list_purchase_orders(world.engine, principal, rfq_id=world.rfq.id)
    assert summary.needs_review

    await lifecycle.review_purchase_order_response(
        world.engine, principal, po.id, ReviewRequest(accepted=False, note="Price stays")
    )
    view = await service.get_purchase_order(world.engine, principal, po.id)
    assert not view.needs_review
    with pytest.raises(InvalidState):
        await lifecycle.review_purchase_order_response(
            world.engine, principal, po.id, ReviewRequest(accepted=True)
        )
    feed = [i.summary for i in await rfq_activity(world.engine, principal, world.rfq.id)]
    assert any("needs your review" in s and "unit price 2.5 -> 2.75" in s for s in feed)
    assert any("reviewed: not accepted" in s for s in feed)


async def test_a_reply_bido_could_not_read_is_flagged(world: World) -> None:
    po = await sent_po(world)
    message_id = await reply(world, po, "Siehe Anhang.")
    await read(world, message_id, FakeReader(None))
    view = await service.get_purchase_order(world.engine, buyer(world.people), po.id)
    assert view.needs_review and view.supplier_responses[0].kind == "unread"
    # Acknowledging by hand settles it.
    await lifecycle.acknowledge_purchase_order(
        world.engine, buyer(world.people), po.id, AcknowledgeRequest(supplier_order_ref="R-9")
    )
    view = await service.get_purchase_order(world.engine, buyer(world.people), po.id)
    assert not view.needs_review


async def test_cancelling_a_sent_po_tells_the_supplier_once_in_the_thread(world: World) -> None:
    po = await sent_po(world)
    principal = buyer(world.people)
    await lifecycle.cancel_purchase_order(
        world.engine, principal, po.id, CancelRequest(reason="Not needed")
    )
    notice = await request_cancellation_notice(world.engine, principal, po.id, world.mailboxes)
    assert notice is not None
    # Asking again returns the same proposal.
    again = await request_cancellation_notice(world.engine, principal, po.id, world.mailboxes)
    assert again is not None and again.approval_id == notice.approval_id
    queue = await service.get_purchase_order(world.engine, principal, po.id)
    assert queue.cancellation_approval_id == notice.approval_id and not queue.cancellation_sent

    sent_before = len(world.mailboxes.sender.sent)
    await world.approve(notice.approval_id)
    [email] = world.mailboxes.sender.sent[sent_before:]
    assert email.to == ("sales@rhein.example.test",)
    assert "PO-1" in email.text_body and "stornieren" in email.text_body  # Rhein writes German
    assert not email.attachments and email.in_reply_to is not None and email.possibly_sent
    view = await service.get_purchase_order(world.engine, principal, po.id)
    assert view.cancellation_sent

    # A second execution of the same approval sends nothing.
    async with tenant_session(world.engine, world.people.tenant) as session:
        approval = await session.get(Approval, notice.approval_id)
    assert approval is not None and approval.tool == SEND_PO_CANCELLATION
    tool = world.registry.get(SEND_PO_CANCELLATION)
    assert tool is not None
    result = await tool.handler(
        ToolContext(tenant_id=world.people.tenant, user_id=world.people.owner, run_id=uuid4()),
        tool.parse_input(approval.args),
    )
    assert result.outcome == "already_sent"
    assert len(world.mailboxes.sender.sent) == sent_before + 1


async def test_a_po_never_sent_needs_no_cancellation_email(world: World) -> None:
    await world.award()
    [po] = await world.orders()
    principal = buyer(world.people)
    await lifecycle.cancel_purchase_order(
        world.engine, principal, po.id, CancelRequest(reason="Changed plans")
    )
    assert (
        await request_cancellation_notice(world.engine, principal, po.id, world.mailboxes) is None
    )


async def test_an_award_above_the_approved_maximum_needs_a_reason(world: World) -> None:
    line1, line2 = world.rfq.lines
    async with tenant_session(world.engine, world.people.tenant) as session:
        await session.execute(
            update(RfqLine).where(RfqLine.id == line1.id).values(max_unit_price=Decimal("0.05"))
        )
    principal = buyer(world.people)
    chosen = AwardRequest(
        lines=[
            AwardSelection(rfq_line_id=line1.id, invite_id=world.invites["Rhein"]),
            AwardSelection(rfq_line_id=line2.id),
        ]
    )
    with pytest.raises(ReasonRequired, match="line\\(s\\) 1 break the approved limits"):
        await service.request_award(world.engine, principal, world.rfq.id, chosen, TODAY)
    requested = await service.request_award(
        world.engine,
        principal,
        world.rfq.id,
        chosen.model_copy(update={"reason": "Only qualified source this month"}),
        TODAY,
    )
    by_position = {line.position: line for line in requested.lines}
    assert by_position[1].buyer_limit_issues is not None
    assert "above_buyer_maximum" in by_position[1].buyer_limit_issues
    assert by_position[2].buyer_limit_issues is None


def test_the_po_document_lists_what_it_leaves_blank() -> None:
    from datetime import date

    from bido_back.awards.render import PoDocument, PoLine, document_gaps

    line = PoLine(1, "MLCC", None, Decimal(1), "pcs", Decimal(1), Decimal(1), None, None)
    doc = PoDocument("PO-1", "RFQ-1", date(2026, 10, 9), "Voltwerk", "Rhein", "EUR", None, None,
                     Decimal(1), (line,))  # fmt: skip
    assert document_gaps(doc) == [
        "incoterm",
        "payment_terms",
        "line 1 delivery_date",
        "delivery_location",
    ]
