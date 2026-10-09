"""Chases, clarifications, cancellation and pauses against Postgres; fake writer and mailbox."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from email.message import EmailMessage
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.agents.catalog.contracts import FollowupEmailInput, FollowupEmailOutput
from bido_back.agents.context import ToolContext
from bido_back.approvals import ActToolRegistry, ApprovalExecutor, decide_approval
from bido_back.auth import Principal
from bido_back.db import tenant_session
from bido_back.db.models import (
    Approval,
    ClarificationAsk,
    FollowupMessage,
    Invite,
    Message,
    Proposal,
    ProposalLine,
    Rfq,
    RfqLine,
)
from bido_back.followups import service
from bido_back.followups.jobs import ALL_TENANTS, FollowupSweepHandler, followup_tenants
from bido_back.followups.schemas import FollowupSettingsUpdate
from bido_back.followups.send import (
    FOLLOWUP_REJECT_HOOKS,
    SEND_FOLLOWUP_EMAIL,
    register_followup_act_tools,
)
from bido_back.intake.service import InboundService
from bido_back.rfqs import service as rfq_service
from bido_back.rfqs import suppliers
from bido_back.rfqs.errors import AgentFailed
from bido_back.rfqs.schemas import ContactIn, CreateSupplierRequest
from bido_back.rfqs.send import RfqActDeps, register_act_tools
from bido_back.storage.local import LocalFileStorage
from bido_back.worker.envelope import Envelope
from bido_back.worker.job_types import FOLLOWUPS_SWEEP
from tests.approvals.conftest import FakeJobQueue, People
from tests.rfqs.conftest import FakeIntake, FakeMailboxes, FakeWriter

pytestmark = pytest.mark.db

EMAIL_DOMAIN = "quotes.example.test"


class FakeFollowupWriter:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[FollowupEmailInput] = []
        self.fail = fail

    async def __call__(self, task: FollowupEmailInput, context: ToolContext) -> FollowupEmailOutput:
        self.calls.append(task)
        if self.fail:
            raise AgentFailed("writer down")
        if task.kind == "chase":
            return FollowupEmailOutput(body=f"Reminder {task.chase_number} about {task.rfq_ref}.")
        asks = "; ".join(f"{i.position}:{i.field}:{i.problem}" for i in task.open_items)
        return FollowupEmailOutput(
            body=f"Thanks for your offer on {task.rfq_ref}. Please send {asks}."
        )


@pytest.fixture
def writer() -> FakeFollowupWriter:
    return FakeFollowupWriter()


def buyer(people: People) -> Principal:
    return Principal(tenant_id=people.tenant, user_id=people.owner, role="owner")


def approver(people: People) -> Principal:
    return Principal(tenant_id=people.tenant, user_id=people.approver, role="approver")


class World:
    """One RFQ, one invite, its RFQ email approved and sent through the fake mailbox."""

    def __init__(self, engine: AsyncEngine, people: People) -> None:
        self.engine = engine
        self.people = people
        self.mailboxes = FakeMailboxes()
        self.jobs = FakeJobQueue()
        self.registry = ActToolRegistry()
        deps = RfqActDeps(db=engine, email_domain=EMAIL_DOMAIN, mailbox_senders=self.mailboxes)
        register_act_tools(self.registry, deps)
        register_followup_act_tools(self.registry, deps)
        self.executor = ApprovalExecutor(engine, self.registry)
        self.rfq_id: UUID
        self.invite_id: UUID
        self.sent_at: datetime

    async def setup(self) -> "World":
        principal = buyer(self.people)
        rfq = await rfq_service.create_rfq_from_text(
            self.engine, principal, "Need MLCC and connectors", FakeIntake(), currency="EUR"
        )
        supplier = await suppliers.create_supplier(
            self.engine,
            principal,
            CreateSupplierRequest(
                name="Rhein",
                language="de",
                country="DE",
                contacts=[ContactIn(name="Jonas", email="sales@rhein.example.test")],
            ),
        )
        invite = await rfq_service.add_invite(
            self.engine, principal, rfq.id, supplier.id, supplier.contacts[0].id
        )
        prepared = await rfq_service.prepare_send(
            self.engine, principal, rfq.id, FakeWriter(), self.mailboxes
        )
        await self.approve(prepared.approvals[0].approval_id)
        self.rfq_id, self.invite_id = rfq.id, invite.id
        # Quotes due today: chases follow the schedule from now on (never before that date).
        await service.update_followups(
            self.engine,
            principal,
            rfq.id,
            FollowupSettingsUpdate(response_due=datetime.now(UTC).date()),
        )
        async with tenant_session(self.engine, self.people.tenant) as session:
            stored = await session.get(Invite, invite.id)
            assert stored is not None and stored.sent_at is not None
            self.sent_at = stored.sent_at
        return self

    async def approve(self, approval_id: UUID) -> None:
        self.jobs.jobs.clear()
        await decide_approval(
            self.engine, approver(self.people), approval_id, "approve", jobs=self.jobs
        )
        await self.executor(self.jobs.jobs[0])

    async def sweep(self, writer: FakeFollowupWriter, now: datetime) -> service.SweepResult:
        return await service.sweep_tenant(self.engine, self.people.tenant, writer=writer, now=now)

    async def followups(self) -> list[FollowupMessage]:
        async with tenant_session(self.engine, self.people.tenant) as session:
            return list(
                await session.scalars(
                    select(FollowupMessage)
                    .where(FollowupMessage.invite_id == self.invite_id)
                    .order_by(FollowupMessage.created_at)
                )
            )

    async def approval(self, approval_id: UUID | None) -> Approval:
        assert approval_id is not None
        async with tenant_session(self.engine, self.people.tenant) as session:
            found = await session.get(Approval, approval_id)
        assert found is not None
        return found


@pytest.fixture
async def world(app_engine: AsyncEngine, rfq_rows: People) -> World:
    return await World(app_engine, rfq_rows).setup()


async def test_a_due_chase_is_proposed_once_then_sent_in_the_thread(
    world: World, writer: FakeFollowupWriter
) -> None:
    # Not due yet: first chase 2 business days after the RFQ email.
    assert (await world.sweep(writer, world.sent_at)).chases == 0

    later = world.sent_at + timedelta(days=5)
    assert (await world.sweep(writer, later)).chases == 1
    [chase] = await world.followups()
    approval = await world.approval(chase.approval_id)
    assert (chase.kind, chase.sequence, chase.status) == ("chase", 1, "proposed")
    assert approval.tool == SEND_FOLLOWUP_EMAIL and approval.status == "pending"
    # Requested on behalf of the buyer who sent the RFQ email: another person approves.
    assert approval.requested_by == world.people.owner
    [task] = writer.calls
    assert (task.kind, task.chase_number, task.supplier_language) == ("chase", 1, "de")

    # Waiting for approval: nothing new, however late it gets.
    assert (await world.sweep(writer, later + timedelta(days=30))).chases == 0

    await world.approve(approval.id)
    rfq_email, followup_email = world.mailboxes.sender.sent
    assert followup_email.in_reply_to == rfq_email.message_id
    assert followup_email.references == (rfq_email.message_id,)
    assert followup_email.subject == f"Re: {rfq_email.subject}"
    assert followup_email.text_body.startswith("Reminder 1 about RFQ-")
    [chase] = await world.followups()
    assert chase.status == "sent" and chase.sent_at is not None
    async with tenant_session(world.engine, world.people.tenant) as session:
        outbound = list(
            await session.scalars(
                select(Message.message_id_header).where(
                    Message.invite_id == world.invite_id, Message.direction == "out"
                )
            )
        )
    assert set(outbound) == {rfq_email.message_id, followup_email.message_id}

    # The next one is due `every` business days after the chase was sent.
    view = await service.get_followups(world.engine, buyer(world.people), world.rfq_id)
    [invite_view] = view.invites
    assert invite_view.chase_state == "scheduled" and invite_view.chases_used == 1
    assert invite_view.next_chase_at is not None and invite_view.next_chase_at > chase.sent_at


async def test_max_chases_and_rejections(world: World, writer: FakeFollowupWriter) -> None:
    await service.update_followups(
        world.engine,
        buyer(world.people),
        world.rfq_id,
        FollowupSettingsUpdate(first_after_business_days=0, every_business_days=1, max_chases=2),
    )
    now = world.sent_at
    await world.sweep(writer, now)
    [first] = await world.followups()
    await decide_approval(
        world.engine,
        approver(world.people),
        first.approval_id or first.id,
        "reject",
        jobs=FakeJobQueue(),
        on_reject=FOLLOWUP_REJECT_HOOKS,
    )
    [first] = await world.followups()
    assert first.status == "rejected"

    await world.sweep(writer, now + timedelta(days=7))
    await world.sweep(writer, now + timedelta(days=14))
    rows = await world.followups()
    assert [(r.sequence, r.status) for r in rows] == [(1, "rejected"), (2, "proposed")]
    view = await service.get_followups(world.engine, buyer(world.people), world.rfq_id)
    assert view.invites[0].chase_state == "awaiting_approval"


async def test_disabled_followups_propose_nothing(world: World, writer: FakeFollowupWriter) -> None:
    view = await service.update_followups(
        world.engine, buyer(world.people), world.rfq_id, FollowupSettingsUpdate(enabled=False)
    )
    assert view.settings.enabled is False and view.invites[0].chase_state == "disabled"
    assert (await world.sweep(writer, world.sent_at + timedelta(days=30))).chases == 0


def _eml(subject: str, body: str, *, auto: bool = False) -> bytes:
    msg = EmailMessage()
    msg["From"] = "Jonas <sales@rhein.example.test>"
    msg["To"] = "buyer@example.test"
    msg["Subject"] = subject
    msg["Message-ID"] = f"<{abs(hash((subject, body)))}@rhein.example.test>"
    if auto:
        msg["Auto-Submitted"] = "auto-replied"
    msg.set_content(body)
    return msg.as_bytes()


def _inbound(world: World, tmp_path: Path) -> InboundService:
    return InboundService(world.engine, LocalFileStorage(tmp_path), FakeJobQueue(), EMAIL_DOMAIN)


async def test_a_reply_cancels_the_pending_chase_and_a_late_approval_sends_nothing(
    world: World, writer: FakeFollowupWriter, tmp_path: Path
) -> None:
    await world.sweep(writer, world.sent_at + timedelta(days=5))
    [chase] = await world.followups()
    approval_id = chase.approval_id

    result = await _inbound(world, tmp_path).ingest_for_invite(
        _eml("AW: RFQ", "Angebot folgt."), tenant_id=world.people.tenant, invite_id=world.invite_id
    )
    assert result.outcome == "stored"
    [chase] = await world.followups()
    assert (chase.status, chase.canceled_reason) == ("canceled", "supplier_replied")
    assert (await world.approval(approval_id)).status == "expired"


async def test_a_stale_chase_is_skipped_by_the_send_tool(
    world: World, writer: FakeFollowupWriter
) -> None:
    await world.sweep(writer, world.sent_at + timedelta(days=5))
    [chase] = await world.followups()
    # The supplier sent its offer after the approval (no intake hook ran: worst case).
    await decide_approval(
        world.engine,
        approver(world.people),
        chase.approval_id or chase.id,
        "approve",
        jobs=world.jobs,
    )
    await _store_proposal(world, complete=True)
    await world.executor(world.jobs.jobs[-1])

    assert len(world.mailboxes.sender.sent) == 1  # only the RFQ email
    [chase] = await world.followups()
    assert (chase.status, chase.canceled_reason) == ("canceled", "offer_received")


async def test_a_declined_invite_is_never_chased(world: World, writer: FakeFollowupWriter) -> None:
    await world.sweep(writer, world.sent_at + timedelta(days=5))
    [chase] = await world.followups()
    await decide_approval(
        world.engine,
        approver(world.people),
        chase.approval_id or chase.id,
        "approve",
        jobs=world.jobs,
    )
    async with tenant_session(world.engine, world.people.tenant) as session:
        await session.execute(
            update(Invite).where(Invite.id == world.invite_id).values(status="declined")
        )
    await world.executor(world.jobs.jobs[-1])

    assert len(world.mailboxes.sender.sent) == 1
    [chase] = await world.followups()
    assert (chase.status, chase.canceled_reason) == ("canceled", "invite_declined")


async def test_an_auto_reply_pauses_chasing(
    world: World, writer: FakeFollowupWriter, tmp_path: Path
) -> None:
    received = world.sent_at + timedelta(hours=1)
    back = (received + timedelta(days=20)).date()
    result = await _inbound(world, tmp_path).ingest_for_invite(
        _eml(
            "Abwesenheitsnotiz",
            f"Ich bin bis {back.strftime('%d.%m.%Y')} nicht im Büro.",
            auto=True,
        ),
        tenant_id=world.people.tenant,
        invite_id=world.invite_id,
    )
    assert result.outcome == "quarantined"
    view = await service.get_followups(world.engine, buyer(world.people), world.rfq_id)
    paused = view.invites[0].paused_until
    assert paused is not None and paused.date() > back

    # Due by the schedule, but paused.
    assert (await world.sweep(writer, world.sent_at + timedelta(days=5))).chases == 0
    assert (await world.sweep(writer, paused + timedelta(minutes=1))).chases == 1


async def _store_proposal(world: World, *, complete: bool) -> UUID:
    async with tenant_session(world.engine, world.people.tenant) as session:
        message = Message(
            tenant_id=world.people.tenant,
            invite_id=world.invite_id,
            direction="in",
            message_id_header=f"<offer-{complete}@rhein.example.test>",
            status="extracted",
            occurred_at=datetime.now(UTC),
        )
        session.add(message)
        await session.execute(
            update(Invite).where(Invite.id == world.invite_id).values(status="responded")
        )
        await session.flush()
        version = 1 + len(
            list(
                await session.scalars(
                    select(Proposal.id).where(Proposal.invite_id == world.invite_id)
                )
            )
        )
        proposal = Proposal(
            tenant_id=world.people.tenant,
            invite_id=world.invite_id,
            message_id=message.id,
            version=version,
            currency="EUR",
            valid_until=None if not complete else datetime(2026, 12, 31).date(),
            fields={"_review": {"declined": False}},
        )
        session.add(proposal)
        await session.flush()
        lines = list(await session.scalars(select(RfqLine).where(RfqLine.rfq_id == world.rfq_id)))
        for line in lines if complete else lines[:1]:
            session.add(
                ProposalLine(
                    tenant_id=world.people.tenant,
                    proposal_id=proposal.id,
                    rfq_line_id=line.id,
                    unit_price=Decimal("0.5"),
                    min_quantity=Decimal(100),
                    lead_time_days=None if not complete else 30,
                    fields={},
                )
            )
        await session.flush()
        return proposal.id


async def test_a_clarification_asks_for_exactly_what_is_missing(
    world: World, writer: FakeFollowupWriter
) -> None:
    proposal_id = await _store_proposal(world, complete=False)
    now = datetime.now(UTC)
    assert (await world.sweep(writer, now)).clarifications == 1
    [clarification] = await world.followups()
    assert (clarification.kind, clarification.proposal_id) == ("clarification", proposal_id)
    task = writer.calls[-1]
    # Line 2 has no price: one question about it ("the price, or tell us you do not
    # quote it"); line 1's single price applies to the requested quantity.
    assert [(i.position, i.field, i.problem, i.question) for i in task.open_items] == [
        (None, "valid_until", "missing", "value"),
        (1, "lead_time", "missing", "value"),
        (2, "unit_price", "missing", "price_or_decline"),
    ]
    assert task.reply_by is not None and task.reply_by > now.date()
    assert clarification.reply_by == task.reply_by
    # The ledger: asked once, in this email, waiting for approval.
    async with tenant_session(world.engine, world.people.tenant) as session:
        asks = list(
            await session.scalars(
                select(ClarificationAsk).where(ClarificationAsk.invite_id == world.invite_id)
            )
        )
    assert sorted((a.position or 0, a.field, a.status) for a in asks) == [
        (0, "valid_until", "proposed"),
        (1, "lead_time", "proposed"),
        (2, "unit_price", "proposed"),
    ]
    assert {a.followup_id for a in asks} == {clarification.id}
    # One email at a time: nothing more while it waits.
    assert (await world.sweep(writer, now)).clarifications == 0

    # A newer, complete offer: the old clarification is canceled (nothing was asked), and
    # nothing new is asked.
    await _store_proposal(world, complete=True)
    result = await world.sweep(writer, now)
    assert (result.canceled, result.clarifications) == (1, 0)
    [clarification] = await world.followups()
    assert (clarification.status, clarification.canceled_reason) == ("canceled", "newer_offer")
    async with tenant_session(world.engine, world.people.tenant) as session:
        statuses = set(
            await session.scalars(
                select(ClarificationAsk.status).where(ClarificationAsk.invite_id == world.invite_id)
            )
        )
    assert statuses == {"withdrawn"}


async def test_a_failing_writer_skips_and_retries_next_time(world: World) -> None:
    later = world.sent_at + timedelta(days=5)
    result = await world.sweep(FakeFollowupWriter(fail=True), later)
    assert (result.chases, result.skipped) == (0, 1)
    assert await world.followups() == []
    assert (await world.sweep(FakeFollowupWriter(), later)).chases == 1


async def test_the_scheduled_job_finds_tenants_through_the_lookup_function(
    world: World, writer: FakeFollowupWriter
) -> None:
    assert world.people.tenant in await followup_tenants(world.engine)
    handler = FollowupSweepHandler(
        db=world.engine, writer=writer, now=world.sent_at + timedelta(days=5)
    )
    await handler(Envelope(type=FOLLOWUPS_SWEEP, tenant_id=ALL_TENANTS, idempotency_key="sweep:1"))
    assert [f.kind for f in await world.followups()] == ["chase"]


async def test_followups_of_another_tenant_are_invisible(world: World) -> None:
    other = Principal(tenant_id=world.people.other_tenant, user_id=world.people.other_approver)
    with pytest.raises(Exception, match="not found"):
        await service.get_followups(world.engine, other, world.rfq_id)
    async with tenant_session(world.engine, world.people.other_tenant) as session:
        assert list(await session.scalars(select(Rfq.id))) == []
