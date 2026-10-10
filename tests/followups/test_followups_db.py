"""Chases, clarifications, cancellation and pauses against Postgres; fake writer and mailbox."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from email.message import EmailMessage
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.agents.catalog.contracts import EmailNoteOutput, FollowupEmailInput
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
    Supplier,
    Tenant,
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
from bido_back.rfqs.activity import rfq_activity
from bido_back.rfqs.errors import AgentFailed
from bido_back.rfqs.schemas import ContactIn, CreateSupplierRequest
from bido_back.rfqs.send import RfqActDeps, register_act_tools
from bido_back.storage.local import LocalFileStorage
from bido_back.supplier_email import compose
from bido_back.worker.envelope import Envelope
from bido_back.worker.job_types import FOLLOWUPS_SWEEP
from tests.approvals.conftest import FakeJobQueue, People
from tests.rfqs.conftest import FakeIntake, FakeMailboxes, FakeWriter

pytestmark = pytest.mark.db

EMAIL_DOMAIN = "quotes.example.test"


class FakeFollowupWriter:
    """The writer agent: code renders the email, the agent returns at most a note."""

    def __init__(self, *, fail: bool = False, note: str = "") -> None:
        self.calls: list[FollowupEmailInput] = []
        self.fail = fail
        self.note = note

    async def __call__(self, task: FollowupEmailInput, context: ToolContext) -> EmailNoteOutput:
        self.calls.append(task)
        if self.fail:
            raise AgentFailed("writer down")
        return EmailNoteOutput(note=self.note)


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
        # The first business-day moment at or after the RFQ email (the supplier's calendar):
        # a sweep "now" that a weekend or a holiday does not push behind the schedule.
        self.now: datetime

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
        self.now = service.on_business_day(self.sent_at, "DE")
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
    # Rendered by code in the thread's language (Rhein got its RFQ email in German).
    ref = rfq_email.subject.split("]")[0].lstrip("[")
    assert followup_email.text_body.startswith(
        f"Sehr geehrte Damen und Herren,\n\nErinnerung – {ref}\n"
    )
    assert "Bitte antworten Sie auf diese E-Mail bis " in followup_email.text_body
    assert followup_email.html_body is not None and '<html lang="de">' in followup_email.html_body
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
    now = world.now
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
            message_id_header=f"<offer-{complete}-{uuid4().hex}@rhein.example.test>",
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
    assert task.reply_by is not None and task.reply_by >= now.date()
    assert clarification.reply_by == task.reply_by
    # The email asks exactly that: the validity, then the values marked per position, by a
    # reply-by date; no price in a clarification.
    approval = await world.approval(clarification.approval_id)
    body = str(approval.args["body"])
    assert "Bitte nennen Sie uns die Gültigkeit Ihres Angebots." in body
    assert "   Fehlt: Lieferzeit\n" in body
    assert "   Fehlt: Stückpreis\n" in body
    assert "Bitte antworten Sie auf diese E-Mail bis " in body
    assert "EUR" not in body and "€" not in body
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


async def test_a_failing_writer_still_proposes_the_email_without_a_note(world: World) -> None:
    later = world.sent_at + timedelta(days=5)
    result = await world.sweep(FakeFollowupWriter(fail=True), later)
    # Code renders the email: a writer problem never means "no email".
    assert (result.chases, result.skipped) == (1, 0)
    [chase] = await world.followups()
    assert chase.status == "proposed"


async def test_the_writers_note_is_kept_when_it_follows_the_rules_and_dropped_when_not(
    world: World,
) -> None:
    later = world.sent_at + timedelta(days=5)
    note = "Wir freuen uns auf Ihre Rückmeldung."
    await world.sweep(FakeFollowupWriter(note=note), later)
    [chase] = await world.followups()
    approval = await world.approval(chase.approval_id)
    assert note in str(approval.args["body"]) and note in str(approval.args["html_body"])

    # A note with a price never reaches the email; the email still goes to approval.
    async with tenant_session(world.engine, world.people.tenant) as session:
        await session.execute(
            update(FollowupMessage).where(FollowupMessage.id == chase.id).values(status="rejected")
        )
    priced = "Ein anderer Lieferant bietet 0,40 EUR."
    await world.sweep(FakeFollowupWriter(note=priced), later + timedelta(days=7))
    second = (await world.followups())[-1]
    assert second.sequence == 2
    body = str((await world.approval(second.approval_id)).args["body"])
    assert "0,40" not in body and "Lieferant" not in body


async def test_the_scheduled_job_finds_tenants_through_the_lookup_function(
    world: World, writer: FakeFollowupWriter
) -> None:
    assert world.people.tenant in await followup_tenants(world.engine)
    handler = FollowupSweepHandler(
        db=world.engine, writer=writer, now=world.sent_at + timedelta(days=5)
    )
    await handler(Envelope(type=FOLLOWUPS_SWEEP, tenant_id=ALL_TENANTS, idempotency_key="sweep:1"))
    assert [f.kind for f in await world.followups()] == ["chase"]


async def _tenant_name(world: World) -> str:
    async with tenant_session(world.engine, world.people.tenant) as session:
        tenant = await session.get(Tenant, world.people.tenant)
    assert tenant is not None
    return tenant.name


def gate_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """The send gate refuses every email, as it would after a rendering bug or an
    inconsistent decision: code renders the email, so nothing else can refuse it."""

    def refused(*_: object) -> list[str]:
        return ["number_not_decided"]

    monkeypatch.setattr(compose, "email_problems", refused)


async def test_a_refused_chase_is_held_once_and_the_schedule_moves_on(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Email audit F3: a refused email was written again every 15-minute sweep, unseen."""
    await service.update_followups(
        world.engine,
        buyer(world.people),
        world.rfq_id,
        FollowupSettingsUpdate(first_after_business_days=0, every_business_days=2),
    )
    gate_refuses(monkeypatch)
    writer = FakeFollowupWriter()
    now = world.now
    result = await world.sweep(writer, now)
    assert (result.chases, result.held, result.skipped) == (0, 1, 0)
    [held] = await world.followups()
    assert (held.kind, held.sequence, held.status, held.canceled_reason, held.approval_id) == (
        "chase",
        1,
        "canceled",
        "draft_refused",
        None,
    )
    # The next sweeps do not render the same chase again, and the model is never asked.
    for minutes in (15, 30, 45):
        assert await world.sweep(writer, now + timedelta(minutes=minutes)) == service.SweepResult()
    assert writer.calls == []
    view = await service.get_followups(world.engine, buyer(world.people), world.rfq_id)
    [invite_view] = view.invites
    assert (invite_view.chase_state, invite_view.chases_used) == ("scheduled", 1)
    assert invite_view.next_chase_at is not None and invite_view.next_chase_at > now
    feed = await rfq_activity(world.engine, buyer(world.people), world.rfq_id)
    [entry] = [a for a in feed if a.action == "rfq.followup_draft_held"]
    assert entry.summary == (
        "Reminder to Rhein not sent: the draft failed Bido's checks. Needs your decision"
    )
    # Once fixed, the next chase comes when the schedule says, and is proposed normally.
    monkeypatch.undo()
    later = now + timedelta(days=7)
    assert (await world.sweep(FakeFollowupWriter(), later)).chases == 1
    assert [(r.sequence, r.status) for r in await world.followups()] == [
        (1, "canceled"),
        (2, "proposed"),
    ]


async def test_a_refused_clarification_is_held_once_per_offer(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _store_proposal(world, complete=False)
    gate_refuses(monkeypatch)
    writer = FakeFollowupWriter()
    now = datetime.now(UTC)
    assert (await world.sweep(writer, now)).held == 1
    assert (await world.sweep(writer, now + timedelta(minutes=15))).held == 0
    assert writer.calls == []
    [held] = await world.followups()
    assert (held.kind, held.status, held.canceled_reason) == (
        "clarification",
        "canceled",
        "draft_refused",
    )
    # The buyer sees it: nothing was asked (no asks in the ledger), the buyer decides.
    view = await service.get_followups(world.engine, buyer(world.people), world.rfq_id)
    [invite_view] = view.invites
    assert invite_view.phase == "needs_your_decision" and invite_view.asks == []
    assert [(d.kind, d.reasons) for d in invite_view.needs_your_decision] == [
        ("email_held", ["clarification"])
    ]
    # A newer (still incomplete) offer gets its own clarification.
    monkeypatch.undo()
    await _store_proposal(world, complete=False)
    assert (await world.sweep(FakeFollowupWriter(), now)).clarifications == 1


async def test_followups_are_signed_once_in_the_language_of_the_rfq_email(world: World) -> None:
    """Email audit F4 and F5: the company signed twice (no buyer contact exists), and a
    supplier edited mid-thread switched the thread's language."""
    async with tenant_session(world.engine, world.people.tenant) as session:
        invite = await session.get(Invite, world.invite_id)
        assert invite is not None
        await session.execute(
            update(Supplier).where(Supplier.id == invite.supplier_id).values(language="fr")
        )
    company = await _tenant_name(world)
    # A writer that signs anyway: its note is left out, and code signs once.
    writer = FakeFollowupWriter(note=f"Mit freundlichen Grüßen, {company}")
    assert (await world.sweep(writer, world.sent_at + timedelta(days=5))).chases == 1
    [task] = writer.calls
    assert task.supplier_language == "de"  # the RFQ email's, not the edited one
    [chase] = await world.followups()
    approval = await world.approval(chase.approval_id)
    body = str(approval.args["body"])
    assert body.startswith("Sehr geehrte Damen und Herren,\n")
    assert body.endswith(f"\n\nMit freundlichen Grüßen\n{company}\n")
    assert body.count("Mit freundlichen Grüßen") == 1


async def test_followups_of_another_tenant_are_invisible(world: World) -> None:
    other = Principal(tenant_id=world.people.other_tenant, user_id=world.people.other_approver)
    with pytest.raises(Exception, match="not found"):
        await service.get_followups(world.engine, other, world.rfq_id)
    async with tenant_session(world.engine, world.people.other_tenant) as session:
        assert list(await session.scalars(select(Rfq.id))) == []
