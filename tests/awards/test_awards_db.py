"""Award, purchase orders and "not selected" notices against Postgres; fake writer and mailbox."""

import io
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.agents.catalog.contracts import AwardNoticeInput, AwardNoticeOutput
from bido_back.agents.context import ToolContext
from bido_back.approvals import (
    ActToolRegistry,
    ApprovalExecutor,
    ApprovalForbidden,
    decide_approval,
)
from bido_back.auth import Principal
from bido_back.awards import service
from bido_back.awards.render import XLSX_MEDIA_TYPE
from bido_back.awards.schemas import AwardRequest, AwardSelection
from bido_back.awards.send import AWARD_REJECT_HOOKS, AWARD_RFQ, register_award_act_tools
from bido_back.db import tenant_session
from bido_back.db.models import (
    Approval,
    AuditEvent,
    Award,
    AwardNotice,
    Message,
    Proposal,
    ProposalLine,
    PurchaseOrder,
    Rfq,
)
from bido_back.rfqs import service as rfq_service
from bido_back.rfqs import suppliers
from bido_back.rfqs.errors import InvalidState, NotFound
from bido_back.rfqs.schemas import ContactIn, CreateSupplierRequest, RfqDetail
from bido_back.rfqs.send import RfqActDeps, register_act_tools
from tests.approvals.conftest import FakeJobQueue, People
from tests.rfqs.conftest import FakeIntake, FakeMailboxes, FakeWriter

pytestmark = pytest.mark.db

EMAIL_DOMAIN = "quotes.example.test"
TODAY = date(2026, 10, 7)


def buyer(people: People) -> Principal:
    return Principal(tenant_id=people.tenant, user_id=people.owner, role="owner")


def approver(people: People) -> Principal:
    return Principal(tenant_id=people.tenant, user_id=people.approver, role="approver")


class FakeNoticeWriter:
    def __init__(self) -> None:
        self.calls: list[AwardNoticeInput] = []

    async def __call__(self, task: AwardNoticeInput, context: ToolContext) -> AwardNoticeOutput:
        self.calls.append(task)
        return AwardNoticeOutput(body=f"Thank you for your offer on {task.rfq_ref}.")


class World:
    """One open RFQ (2 lines) sent to Rhein and Lyon; both quoted, Rhein cheaper."""

    def __init__(self, engine: AsyncEngine, people: People) -> None:
        self.engine = engine
        self.people = people
        self.mailboxes = FakeMailboxes()
        self.jobs = FakeJobQueue()
        self.registry = ActToolRegistry()
        deps = RfqActDeps(db=engine, email_domain=EMAIL_DOMAIN, mailbox_senders=self.mailboxes)
        register_act_tools(self.registry, deps)
        register_award_act_tools(self.registry, deps)
        self.executor = ApprovalExecutor(engine, self.registry)
        self.rfq: RfqDetail
        self.invites: dict[str, UUID] = {}

    async def setup(self) -> "World":
        principal = buyer(self.people)
        self.rfq = await rfq_service.create_rfq_from_text(
            self.engine, principal, "Need MLCC and connectors", FakeIntake()
        )
        for name, language in (("Rhein", "de"), ("Lyon", "fr")):
            supplier = await suppliers.create_supplier(
                self.engine,
                principal,
                CreateSupplierRequest(
                    name=name,
                    language=language,
                    contacts=[ContactIn(name="Sales", email=f"sales@{name.lower()}.example.test")],
                ),
            )
            invite = await rfq_service.add_invite(
                self.engine, principal, self.rfq.id, supplier.id, supplier.contacts[0].id
            )
            self.invites[name] = invite.id
        prepared = await rfq_service.prepare_send(
            self.engine, principal, self.rfq.id, FakeWriter(), self.mailboxes
        )
        for item in prepared.approvals:
            await self.approve(item.approval_id)
        line1, line2 = self.rfq.lines
        await self.quote("Rhein", 1, {line1.id: "0.10", line2.id: "2.50"})
        await self.quote("Lyon", 1, {line1.id: "0.12"})
        return self

    async def quote(self, name: str, version: int, prices: dict[UUID, str]) -> None:
        invite_id = self.invites[name]
        async with tenant_session(self.engine, self.people.tenant) as session:
            message = Message(
                tenant_id=self.people.tenant,
                invite_id=invite_id,
                direction="in",
                message_id_header=f"<v{version}-{invite_id}@supplier.example.test>",
                occurred_at=datetime.now(UTC),
            )
            session.add(message)
            await session.flush()
            proposal = Proposal(
                tenant_id=self.people.tenant,
                invite_id=invite_id,
                message_id=message.id,
                version=version,
                currency="EUR",
                valid_until=date(2027, 12, 31),
                incoterm="DAP",
                payment_terms="30 days net",
            )
            session.add(proposal)
            await session.flush()
            for line_id, price in prices.items():
                session.add(
                    ProposalLine(
                        tenant_id=self.people.tenant,
                        proposal_id=proposal.id,
                        rfq_line_id=line_id,
                        min_quantity=Decimal(1),
                        unit_price=Decimal(price),
                        unit="pcs",
                        lead_time_days=21,
                    )
                )
            await session.flush()

    async def approve(self, approval_id: UUID) -> None:
        self.jobs.jobs.clear()
        await decide_approval(
            self.engine,
            approver(self.people),
            approval_id,
            "approve",
            jobs=self.jobs,
            on_reject=AWARD_REJECT_HOOKS,
        )
        await self.executor(self.jobs.jobs[0])

    async def reject(self, approval_id: UUID) -> None:
        await decide_approval(
            self.engine,
            approver(self.people),
            approval_id,
            "reject",
            jobs=self.jobs,
            on_reject=AWARD_REJECT_HOOKS,
        )

    async def award(self, body: AwardRequest | None = None) -> UUID:
        requested = await service.request_award(
            self.engine, buyer(self.people), self.rfq.id, body or AwardRequest(), TODAY
        )
        await self.approve(requested.approval_id)
        return requested.approval_id

    async def orders(self) -> list[PurchaseOrder]:
        async with tenant_session(self.engine, self.people.tenant) as session:
            return list(
                await session.scalars(
                    select(PurchaseOrder)
                    .where(PurchaseOrder.rfq_id == self.rfq.id)
                    .order_by(PurchaseOrder.number)
                )
            )


@pytest.fixture
async def world(app_engine: AsyncEngine, award_rows: People) -> World:
    return await World(app_engine, award_rows).setup()


async def test_the_recommendation_is_awarded_after_approval(world: World) -> None:
    line1, line2 = world.rfq.lines
    requested = await service.request_award(
        world.engine, buyer(world.people), world.rfq.id, AwardRequest(), TODAY
    )
    assert requested.differs_from_recommendation is False
    assert [(a.position, a.supplier_name, a.unit_price, a.quantity) for a in requested.lines] == [
        (1, "Rhein", "0.1", "4000"),
        (2, "Rhein", "2.5", "500"),
    ]
    # Nothing exists before the approval, and the requester cannot approve it.
    assert await world.orders() == []
    with pytest.raises(ApprovalForbidden):
        await decide_approval(
            world.engine, buyer(world.people), requested.approval_id, "approve", jobs=world.jobs
        )
    member = Principal(tenant_id=world.people.tenant, user_id=world.people.member, role="member")
    with pytest.raises(ApprovalForbidden):
        await decide_approval(
            world.engine, member, requested.approval_id, "approve", jobs=world.jobs
        )
    pending = await service.get_award(world.engine, buyer(world.people), world.rfq.id)
    assert pending.state == "pending_approval" and pending.approval_id == requested.approval_id
    queue = await rfq_service.list_approvals(world.engine, approver(world.people), "pending")
    [item] = [i for i in queue if i.tool == AWARD_RFQ]
    assert item.award is not None and [a.position for a in item.award.lines] == [1, 2]

    await world.approve(requested.approval_id)

    [po] = await world.orders()
    assert (po.ref, po.status, po.currency, po.incoterm) == ("PO-1", "draft", "EUR", "DAP")
    assert po.total == Decimal("1650.00")  # 4000 x 0.10 + 500 x 2.50
    view = await service.get_purchase_order(world.engine, buyer(world.people), po.id)
    assert [(line.position, line.line_total) for line in view.lines] == [
        (1, Decimal("400.00")),
        (2, Decimal("1250.00")),
    ]
    assert {line.rfq_line_id for line in view.lines} == {line1.id, line2.id}
    award = await service.get_award(world.engine, buyer(world.people), world.rfq.id)
    assert award.state == "awarded" and [p.ref for p in award.purchase_orders] == ["PO-1"]
    async with tenant_session(world.engine, world.people.tenant) as session:
        rfq = await session.get(Rfq, world.rfq.id)
        audit = await session.scalar(select(AuditEvent).where(AuditEvent.action == "rfq.awarded"))
    assert rfq is not None and rfq.status == "closed"
    assert audit is not None and audit.actor_user_id == world.people.approver

    # Awarded once.
    with pytest.raises(InvalidState):
        await service.request_award(
            world.engine, buyer(world.people), world.rfq.id, AwardRequest(), TODAY
        )


async def test_another_supplier_needs_a_reason_and_gets_its_own_order(world: World) -> None:
    line1, line2 = world.rfq.lines
    lyon_for_line1 = AwardRequest(
        lines=[
            AwardSelection(rfq_line_id=line1.id, invite_id=world.invites["Lyon"]),
            AwardSelection(rfq_line_id=line2.id),
        ]
    )
    with pytest.raises(service.ReasonRequired):
        await service.request_award(
            world.engine, buyer(world.people), world.rfq.id, lyon_for_line1, TODAY
        )
    with_reason = lyon_for_line1.model_copy(update={"reason": "Lyon is the approved source"})
    requested = await service.request_award(
        world.engine, buyer(world.people), world.rfq.id, with_reason, TODAY
    )
    assert requested.differs_from_recommendation is True
    await world.approve(requested.approval_id)

    orders = await world.orders()
    assert [(po.ref, po.total) for po in orders] == [
        ("PO-1", Decimal("480.00")),  # Lyon: 4000 x 0.12
        ("PO-2", Decimal("1250.00")),  # Rhein: 500 x 2.50
    ]
    async with tenant_session(world.engine, world.people.tenant) as session:
        award = await session.scalar(select(Award).where(Award.rfq_id == world.rfq.id))
    assert award is not None and award.reason == "Lyon is the approved source"


async def test_unpriced_or_unknown_selections_are_refused(world: World) -> None:
    _, line2 = world.rfq.lines
    nobody_priced = AwardRequest(
        lines=[AwardSelection(rfq_line_id=line2.id, invite_id=world.invites["Lyon"])],
        reason="try",
    )
    with pytest.raises(service.AwardSelectionInvalid, match="no price"):
        await service.request_award(
            world.engine, buyer(world.people), world.rfq.id, nobody_priced, TODAY
        )
    no_tier = AwardRequest(lines=[AwardSelection(rfq_line_id=line2.id, tier=3)])
    with pytest.raises(service.AwardSelectionInvalid, match="tier"):
        await service.request_award(world.engine, buyer(world.people), world.rfq.id, no_tier, TODAY)


async def test_a_newer_offer_makes_the_approved_award_stale(world: World) -> None:
    line1, _ = world.rfq.lines
    requested = await service.request_award(
        world.engine, buyer(world.people), world.rfq.id, AwardRequest(), TODAY
    )
    await world.quote("Rhein", 2, {line1.id: "0.09"})
    await world.approve(requested.approval_id)

    assert await world.orders() == []
    async with tenant_session(world.engine, world.people.tenant) as session:
        approval = await session.get(Approval, requested.approval_id)
        rfq = await session.get(Rfq, world.rfq.id)
    assert approval is not None and approval.status == "executed"
    assert rfq is not None and rfq.status == "open"
    # The buyer asks again, on the new offer.
    again = await service.request_award(
        world.engine, buyer(world.people), world.rfq.id, AwardRequest(), TODAY
    )
    assert again.approval_id != requested.approval_id
    assert again.lines[0].proposal_version == 2 and again.lines[0].unit_price == "0.09"


async def test_a_purchase_order_is_sent_in_the_thread_after_approval(world: World) -> None:
    await world.award()
    [po] = await world.orders()

    requested = await service.request_po_send(
        world.engine, buyer(world.people), po.id, world.mailboxes
    )
    [po] = await world.orders()
    assert po.status == "pending_approval"
    queue = await rfq_service.list_approvals(world.engine, approver(world.people), "pending")
    [item] = [i for i in queue if i.id == requested.approval_id]
    assert item.thread_email is not None
    assert (item.thread_email.kind, item.thread_email.purchase_order_ref) == (
        "purchase_order",
        "PO-1",
    )
    assert "Bestellung PO-1" in item.thread_email.body  # Rhein writes German
    with pytest.raises(InvalidState):
        await service.request_po_send(world.engine, buyer(world.people), po.id, world.mailboxes)

    await world.approve(requested.approval_id)

    rfq_email = next(
        e for e in world.mailboxes.sender.sent if e.to == ("sales@rhein.example.test",)
    )
    po_email = world.mailboxes.sender.sent[-1]
    assert po_email.in_reply_to == rfq_email.message_id
    assert po_email.subject.startswith("Re: ") and po_email.subject.endswith("PO-1")
    [attachment] = po_email.attachments
    assert (attachment.filename, attachment.content_type) == ("PO-1.xlsx", XLSX_MEDIA_TYPE)
    from openpyxl import load_workbook  # pyright: ignore[reportMissingTypeStubs]

    sheet = cast(Any, load_workbook(io.BytesIO(attachment.data))).active
    assert sheet["B1"].value == "PO-1"
    [po] = await world.orders()
    assert po.status == "sent" and po.sent_at is not None
    assert po.message_id_header == po_email.message_id


async def test_a_rejected_po_send_goes_back_to_draft(world: World) -> None:
    await world.award()
    [po] = await world.orders()
    requested = await service.request_po_send(
        world.engine, buyer(world.people), po.id, world.mailboxes
    )
    await world.reject(requested.approval_id)
    [po] = await world.orders()
    assert po.status == "draft"
    # And it can be requested again (a new attempt).
    again = await service.request_po_send(world.engine, buyer(world.people), po.id, world.mailboxes)
    assert again.approval_id != requested.approval_id


async def test_suppliers_that_did_not_win_get_a_notice(world: World) -> None:
    writer = FakeNoticeWriter()
    with pytest.raises(InvalidState):  # not awarded yet
        await service.request_notices(world.engine, buyer(world.people), world.rfq.id, writer)
    await world.award()

    result = await service.request_notices(world.engine, buyer(world.people), world.rfq.id, writer)
    [proposed] = result.proposed
    assert proposed.supplier_name == "Lyon" and proposed.invite_id == world.invites["Lyon"]
    [task] = writer.calls
    assert (task.supplier_language, task.rfq_ref) == ("fr", world.rfq.ref)
    # Asking again proposes nothing new.
    again = await service.request_notices(world.engine, buyer(world.people), world.rfq.id, writer)
    assert again.proposed == [] and again.skipped == [world.invites["Lyon"]]

    await world.approve(proposed.approval_id)
    notice_email = world.mailboxes.sender.sent[-1]
    assert notice_email.to == ("sales@lyon.example.test",)
    assert notice_email.text_body == f"Thank you for your offer on {world.rfq.ref}."
    async with tenant_session(world.engine, world.people.tenant) as session:
        notice = await session.scalar(select(AwardNotice))
    assert notice is not None and notice.status == "sent"


async def test_another_tenant_sees_nothing(world: World) -> None:
    await world.award()
    [po] = await world.orders()
    other = Principal(
        tenant_id=world.people.other_tenant, user_id=world.people.other_approver, role="approver"
    )
    assert await service.list_purchase_orders(world.engine, other) == []
    with pytest.raises(NotFound):
        await service.get_purchase_order(world.engine, other, po.id)
    with pytest.raises(NotFound):
        await service.get_award(world.engine, other, world.rfq.id)
