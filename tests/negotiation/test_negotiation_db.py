"""The negotiation against Postgres: counters, rounds, approvals, early and deadline close.

Fake writer and fake mailbox; real strategy, approvals, guard and executor.
"""

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import cast
from uuid import UUID, uuid4

import pytest
from pydantic import JsonValue
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.agents.catalog.contracts import EmailNoteOutput, NegotiationEmailInput
from bido_back.agents.context import ToolContext
from bido_back.agents.tools import ToolError
from bido_back.approvals import ActToolRegistry, ApprovalExecutor, decide_approval
from bido_back.auth import Principal
from bido_back.db import tenant_session
from bido_back.db.models import (
    Approval,
    AuditEvent,
    ClarificationAsk,
    FollowupMessage,
    Invite,
    Message,
    NegotiationMessage,
    Proposal,
    ProposalLine,
    Rfq,
    RfqLine,
    Supplier,
    Tenant,
)
from bido_back.followups import ledger
from bido_back.followups import service as followup_service
from bido_back.followups.calendar import add_business_days
from bido_back.followups.schemas import FollowupSettingsUpdate
from bido_back.followups.send import register_followup_act_tools
from bido_back.negotiation import service
from bido_back.negotiation.hooks import cancel_on_new_offer, replan_envelope, request_replan
from bido_back.negotiation.jobs import (
    ALL_TENANTS,
    NegotiationReplanHandler,
    NegotiationSweepHandler,
)
from bido_back.negotiation.schemas import LineLimitsUpdate, NegotiationSettingsUpdate
from bido_back.negotiation.send import (
    NEGOTIATION_REJECT_HOOKS,
    SEND_NEGOTIATION_EMAIL,
    register_negotiation_act_tools,
)
from bido_back.rfqs import service as rfq_service
from bido_back.rfqs import suppliers
from bido_back.rfqs.activity import rfq_activity
from bido_back.rfqs.errors import AgentFailed, InvalidState, NotFound
from bido_back.rfqs.schemas import ContactIn, CreateSupplierRequest
from bido_back.rfqs.send import RfqActDeps, register_act_tools
from bido_back.worker.envelope import Envelope
from bido_back.worker.job_types import NEGOTIATION_SWEEP
from tests.approvals.conftest import FakeJobQueue, People
from tests.db.conftest import TwoTenants
from tests.followups.test_followups_db import FakeFollowupWriter, gate_refuses
from tests.rfqs.conftest import FakeIntake, FakeMailboxes, FakeWriter

pytestmark = pytest.mark.db

EMAIL_DOMAIN = "quotes.example.test"


class FakeNegotiationWriter:
    """The writer agent: code renders the email and every number; the agent adds a note at
    most. `invent` writes a competitor's price into the note (it must never go out)."""

    def __init__(self, *, invent: bool = False) -> None:
        self.calls: list[NegotiationEmailInput] = []
        self.invent = invent

    async def __call__(self, task: NegotiationEmailInput, context: ToolContext) -> EmailNoteOutput:
        self.calls.append(task)
        return EmailNoteOutput(note="Another supplier offers 0.0123 EUR." if self.invent else "")


class FailingNegotiationWriter(FakeNegotiationWriter):
    """The writer's model is down: every call fails."""

    async def __call__(self, task: NegotiationEmailInput, context: ToolContext) -> EmailNoteOutput:
        self.calls.append(task)
        raise AgentFailed("rfq_negotiation_writer failed (model_error)")


def buyer(people: People) -> Principal:
    return Principal(tenant_id=people.tenant, user_id=people.owner, role="owner")


def approver(people: People) -> Principal:
    return Principal(tenant_id=people.tenant, user_id=people.approver, role="approver")


class World:
    """One RFQ with two lines sent to two suppliers (and optionally a silent third)."""

    def __init__(self, engine: AsyncEngine, people: People) -> None:
        self.engine = engine
        self.people = people
        self.mailboxes = FakeMailboxes()
        self.jobs = FakeJobQueue()
        self.registry = ActToolRegistry()
        deps = RfqActDeps(db=engine, email_domain=EMAIL_DOMAIN, mailbox_senders=self.mailboxes)
        register_act_tools(self.registry, deps)
        register_followup_act_tools(self.registry, deps)
        register_negotiation_act_tools(self.registry, deps)
        self.executor = ApprovalExecutor(engine, self.registry)
        self.rfq_id: UUID
        self.lines: list[UUID] = []
        self.invites: dict[str, UUID] = {}

    async def setup(self, *, silent: bool = False) -> "World":
        principal = buyer(self.people)
        rfq = await rfq_service.create_rfq_from_text(
            self.engine, principal, "Need MLCC and connectors", FakeIntake(), currency="EUR"
        )
        self.rfq_id = rfq.id
        names = [("rhein", "de", "DE"), ("lyon", "fr", "FR")]
        if silent:
            names.append(("silent", "en", "US"))
        for name, language, country in names:
            supplier = await suppliers.create_supplier(
                self.engine,
                principal,
                CreateSupplierRequest(
                    name=name.title(),
                    language=language,
                    country=country,
                    type="authorized_distributor",
                    contacts=[ContactIn(name=name, email=f"sales@{name}.example.test")],
                ),
            )
            invite = await rfq_service.add_invite(
                self.engine, principal, rfq.id, supplier.id, supplier.contacts[0].id
            )
            self.invites[name] = invite.id
        prepared = await rfq_service.prepare_send(
            self.engine, principal, rfq.id, FakeWriter(), self.mailboxes
        )
        for item in prepared.approvals:
            await self.approve(item.approval_id)
        async with tenant_session(self.engine, self.people.tenant) as session:
            self.lines = list(
                await session.scalars(
                    select(RfqLine.id).where(RfqLine.rfq_id == rfq.id).order_by(RfqLine.position)
                )
            )
        return self

    async def approve(self, approval_id: UUID) -> None:
        self.jobs.jobs.clear()
        await decide_approval(
            self.engine, approver(self.people), approval_id, "approve", jobs=self.jobs
        )
        await self.executor(self.jobs.jobs[0])

    async def configure(self, **overrides: object) -> None:
        fields: dict[str, object] = {
            "deadline": None,
            "max_rounds": 3,
            "early_close": True,
            "lines": [
                LineLimitsUpdate(
                    rfq_line_id=self.lines[0],
                    target_unit_price=Decimal("0.040"),
                    max_unit_price=Decimal("0.060"),
                    max_lead_time_days=42,
                ),
                LineLimitsUpdate(
                    rfq_line_id=self.lines[1],
                    target_unit_price=Decimal("1.00"),
                    max_unit_price=Decimal("1.50"),
                    max_lead_time_days=42,
                ),
            ],
        }
        fields.update(overrides)
        await service.update_negotiation(
            self.engine,
            buyer(self.people),
            self.rfq_id,
            NegotiationSettingsUpdate.model_validate(fields),
        )

    async def offer(self, name: str, prices: tuple[str, str], lead: int = 30) -> UUID:
        invite_id = self.invites[name]
        async with tenant_session(self.engine, self.people.tenant) as session:
            version = 1 + len(
                list(
                    await session.scalars(
                        select(Proposal.id).where(Proposal.invite_id == invite_id)
                    )
                )
            )
            message = Message(
                tenant_id=self.people.tenant,
                invite_id=invite_id,
                direction="in",
                message_id_header=f"<offer-{name}-{version}@{name}.example.test>",
                status="extracted",
                occurred_at=datetime.now(UTC),
            )
            session.add(message)
            await session.execute(
                update(Invite).where(Invite.id == invite_id).values(status="responded")
            )
            await session.flush()
            proposal = Proposal(
                tenant_id=self.people.tenant,
                invite_id=invite_id,
                message_id=message.id,
                version=version,
                currency="EUR",
                incoterm="DAP",
                valid_until=datetime(2027, 12, 31).date(),
                fields={"_review": {"declined": False}},
            )
            session.add(proposal)
            await session.flush()
            for line_id, price in zip(self.lines, prices, strict=True):
                session.add(
                    ProposalLine(
                        tenant_id=self.people.tenant,
                        proposal_id=proposal.id,
                        rfq_line_id=line_id,
                        unit_price=Decimal(price),
                        unit="pcs",
                        min_quantity=Decimal(100),
                        lead_time_days=lead,
                        fields={},
                    )
                )
            # What quote extraction does after storing a new version.
            await cancel_on_new_offer(session, invite_id)
            await session.flush()
            return proposal.id

    async def reply(self, name: str) -> UUID:
        """A reply stored by the intake and not read yet (quote extraction pending)."""
        invite_id = self.invites[name]
        async with tenant_session(self.engine, self.people.tenant) as session:
            message = Message(
                tenant_id=self.people.tenant,
                invite_id=invite_id,
                direction="in",
                message_id_header=f"<reply-{name}-{uuid4()}@{name}.example.test>",
                status="stored",
                occurred_at=datetime.now(UTC),
            )
            session.add(message)
            await session.execute(
                update(Invite).where(Invite.id == invite_id).values(status="responded")
            )
            await session.flush()
            return message.id

    async def sweep(
        self, writer: FakeNegotiationWriter, now: datetime | None = None
    ) -> service.SweepResult:
        return await service.sweep_tenant(
            self.engine, self.people.tenant, writer=writer, now=now or datetime.now(UTC)
        )

    async def messages(self, name: str) -> list[NegotiationMessage]:
        async with tenant_session(self.engine, self.people.tenant) as session:
            return list(
                await session.scalars(
                    select(NegotiationMessage)
                    .where(NegotiationMessage.invite_id == self.invites[name])
                    .order_by(NegotiationMessage.sequence)
                )
            )

    async def approval(self, approval_id: UUID | None) -> Approval:
        assert approval_id is not None
        async with tenant_session(self.engine, self.people.tenant) as session:
            found = await session.get(Approval, approval_id)
        assert found is not None
        return found

    async def rfq(self) -> Rfq:
        async with tenant_session(self.engine, self.people.tenant) as session:
            found = await session.get(Rfq, self.rfq_id)
        assert found is not None
        return found


@pytest.fixture
async def world(app_engine: AsyncEngine, rfq_rows: People) -> World:
    return await World(app_engine, rfq_rows).setup()


async def tenant_name(world: World) -> str:
    """Who signs: no buyer contact exists, so the company (as in production)."""
    async with tenant_session(world.engine, world.people.tenant) as session:
        tenant = await session.get(Tenant, world.people.tenant)
    assert tenant is not None
    return tenant.name


async def test_nothing_is_negotiated_before_the_buyer_sets_it_up(world: World) -> None:
    await world.offer("rhein", ("0.050", "1.20"))
    writer = FakeNegotiationWriter()
    assert (await world.sweep(writer)).proposed == 0
    view = await service.get_negotiation(world.engine, buyer(world.people), world.rfq_id)
    assert view.status == "not_started" and writer.calls == []


async def test_a_counter_is_proposed_for_approval_and_sent_in_the_thread(world: World) -> None:
    await world.configure()
    await world.offer("rhein", ("0.050", "1.20"))
    await world.offer("lyon", ("0.038", "0.95"))
    writer = FakeNegotiationWriter()

    result = await world.sweep(writer)
    assert result.proposed == 1 and result.ready == 1
    [task] = writer.calls
    assert (task.action, task.round, task.supplier_language) == ("counter", 1, "de")
    assert [line.target_unit_price for line in task.lines] == [Decimal("0.04"), Decimal("1")]
    # Lyon is cheaper on both lines: Rhein may be told a better offer exists.
    assert task.mention_competition is True
    [counter] = await world.messages("rhein")
    approval = await world.approval(counter.approval_id)
    assert (counter.round, counter.action, counter.status) == (1, "counter", "proposed")
    assert approval.tool == SEND_NEGOTIATION_EMAIL and approval.status == "pending"
    assert approval.requested_by == world.people.owner
    # Lyon is within target everywhere: accepted, nothing sent, and the RFQ can close.
    assert await world.messages("lyon") == []
    view = await service.get_negotiation(world.engine, buyer(world.people), world.rfq_id)
    # Rhein is not "countered" before its email goes out: it is held for approval.
    statuses = {i.supplier_name: i.status for i in view.invites}
    assert statuses == {"Rhein": "open", "Lyon": "accepted"}
    phases = {i.supplier_name: i.state.phase for i in view.invites if i.state}
    assert phases == {"Rhein": "held_for_approval", "Lyon": "accepted_internally"}
    assert view.status == "ready_to_close" and view.ready_at is not None

    # The approver sees the exact email in the queue, as a negotiation email.
    queue = await rfq_service.list_approvals(world.engine, approver(world.people), "pending")
    [item] = [q for q in queue if q.id == approval.id]
    assert item.rfq_email is None and item.negotiation_email is not None
    assert (item.negotiation_email.action, item.negotiation_email.round) == ("counter", 1)
    assert item.negotiation_email.supplier_name == "Rhein"

    # Nothing goes out before the approval.
    sent_before = len(world.mailboxes.sender.sent)
    assert (await world.sweep(writer)).proposed == 0
    assert len(world.mailboxes.sender.sent) == sent_before

    await world.approve(approval.id)
    email = world.mailboxes.sender.sent[-1]
    rhein_rfq_email = next(
        e for e in world.mailboxes.sender.sent if "sales@rhein.example.test" in e.to
    )
    # The latest message with Rhein is its offer: the counter replies to it, in the thread.
    assert email.in_reply_to == "<offer-rhein-1@rhein.example.test>"
    assert email.references == (rhein_rfq_email.message_id, email.in_reply_to)
    assert email.subject == f"Re: {rhein_rfq_email.subject}"
    # Rendered by code in German (Rhein's RFQ email language), the target exactly as decided.
    assert "Bitte um ein überarbeitetes Angebot – RFQ-" in email.text_body
    assert "   Zielpreis: 0,04\u00a0€/Stk.\n" in email.text_body
    assert "   Ihr Angebot: 0,05\u00a0€/Stk.\n" in email.text_body
    assert email.html_body is not None and "0,04\u00a0€/Stk." in email.html_body
    assert item.negotiation_email.html_body == email.html_body
    [counter] = await world.messages("rhein")
    assert counter.status == "sent" and counter.sent_at is not None
    view = await service.get_negotiation(world.engine, buyer(world.people), world.rfq_id)
    rhein = next(i for i in view.invites if i.supplier_name == "Rhein")
    assert rhein.status == "countered" and rhein.rounds_used == 1
    assert rhein.state is not None and rhein.state.phase == "waiting_for_supplier"
    assert rhein.state.label.startswith("Waiting for the supplier")


async def test_rounds_advance_on_new_offers_and_end_with_best_and_final(world: World) -> None:
    await world.configure(early_close=False)
    writer = FakeNegotiationWriter()
    await world.offer("rhein", ("0.050", "1.20"))
    await world.sweep(writer)
    [first] = await world.messages("rhein")
    await world.approve(first.approval_id or first.id)

    # Round 2: a better but still high offer gets another request, conceding a quarter.
    await world.offer("rhein", ("0.045", "1.10"))
    await world.sweep(writer)
    rows = await world.messages("rhein")
    assert [(m.sequence, m.round, m.action, m.status) for m in rows] == [
        (1, 1, "counter", "sent"),
        (2, 2, "counter", "proposed"),
    ]
    assert [line.target_unit_price for line in writer.calls[-1].lines] == [
        Decimal("0.041"),
        Decimal("1.02"),
    ]
    # A newer offer arrives before round 2 is approved: that email is moot.
    await world.offer("rhein", ("0.044", "1.08"))
    rows = await world.messages("rhein")
    assert (rows[1].status, rows[1].canceled_reason) == ("canceled", "newer_offer")
    assert (await world.approval(rows[1].approval_id)).status == "expired"
    await world.sweep(writer)
    rows = await world.messages("rhein")
    # The canceled email used no round: its replacement is round 2 again.
    assert [(m.sequence, m.round, m.action, m.status) for m in rows] == [
        (1, 1, "counter", "sent"),
        (2, 2, "counter", "canceled"),
        (3, 2, "counter", "proposed"),
    ]
    await world.approve(rows[2].approval_id or rows[2].id)
    # Two rounds sent out of three: the next one is the best-and-final request.
    await world.offer("rhein", ("0.042", "1.05"))
    await world.sweep(writer)
    last = (await world.messages("rhein"))[-1]
    assert (last.sequence, last.round, last.action, last.kind) == (4, 3, "bafo", "bafo")
    assert writer.calls[-1].action == "bafo"
    rfq = await world.rfq()
    assert rfq.negotiation_cutoff_at is not None
    await world.approve(last.approval_id or last.id)
    # The answer to the best-and-final is final: nothing more is sent.
    await world.offer("rhein", ("0.041", "1.02"))
    assert (await world.sweep(writer)).proposed == 0
    view = await service.get_negotiation(world.engine, buyer(world.people), world.rfq_id)
    rhein = next(i for i in view.invites if i.supplier_name == "Rhein")
    assert rhein.status == "closed" and rhein.rounds_used == 3
    assert rhein.state is not None and rhein.state.reason == "bafo_answered"
    assert len([m for m in await world.messages("rhein") if m.action == "bafo"]) == 1


async def _to_final_answer(
    world: World, writer: FakeNegotiationWriter, final: tuple[str, str]
) -> None:
    """Rhein: counter (round 1), best-and-final (round 2), then its final answer."""
    await world.offer("rhein", ("0.050", "1.20"))
    await world.sweep(writer)
    [counter] = await world.messages("rhein")
    await world.approve(counter.approval_id or counter.id)
    await world.offer("rhein", ("0.045", "1.10"))
    await world.sweep(writer)
    bafo = (await world.messages("rhein"))[-1]
    assert bafo.kind == "bafo"
    await world.approve(bafo.approval_id or bafo.id)
    await world.offer("rhein", final)


async def _decline(world: World, name: str) -> None:
    async with tenant_session(world.engine, world.people.tenant) as session:
        await session.execute(
            update(Invite).where(Invite.id == world.invites[name]).values(status="declined")
        )


async def test_every_final_offer_makes_the_rfq_ready_to_close_above_target(world: World) -> None:
    from bido_back.copilot.tools import CopilotToolDeps, NegotiationStatusOut, copilot_tools
    from tests.rfqs.conftest import fake_agents

    # RFQ-8 (2026-10-12): the best-and-final answer is above target but within the maximum.
    await world.configure(max_rounds=2)
    writer = FakeNegotiationWriter()
    await _to_final_answer(world, writer, ("0.044", "1.08"))
    assert (await world.sweep(writer)).proposed == 0
    view = await service.get_negotiation(world.engine, buyer(world.people), world.rfq_id)
    rhein = next(i for i in view.invites if i.supplier_name == "Rhein")
    assert rhein.status == "closed" and rhein.state is not None
    assert rhein.state.reason == "bafo_answered"
    # Lyon has not quoted yet: the negotiation stays open.
    assert (view.status, view.ready_at, view.recommendation) == ("open", None, None)

    # Lyon declines: every supplier gave its last word, nothing is left to negotiate.
    await _decline(world, "lyon")
    result = await world.sweep(writer)
    assert (result.ready, result.proposed) == (1, 0)
    rfq = await world.rfq()
    assert rfq.negotiation_status == "ready_to_close" and rfq.negotiation_ready_at is not None
    view = await service.get_negotiation(world.engine, buyer(world.people), world.rfq_id)
    assert view.status == "ready_to_close" and view.outcome is None
    assert view.recommendation is not None
    by_line = {o.rfq_line_id: o for o in view.recommendation if o.tier == 0}
    assert {o.supplier_name for o in by_line.values()} == {"Rhein"}
    assert [by_line[line].effective_unit_price_base for line in world.lines] == [
        Decimal("0.044"),
        Decimal("1.08"),
    ]
    # Above the target: said explicitly, never hidden.
    assert all(o.above_target and o.within_target is False for o in by_line.values())
    feed = await rfq_activity(world.engine, buyer(world.people), world.rfq_id)
    [ready] = [a for a in feed if a.action == "rfq.negotiation_ready"]
    assert ready.summary == "Negotiation ready to close: every supplier gave its final offer"

    tools = {
        tool.name: tool
        for tool in copilot_tools(
            CopilotToolDeps(
                db=world.engine,
                agents=fake_agents(),
                mailboxes=world.mailboxes,
                negotiation_writer=writer,
            )
        )
    }
    ctx = ToolContext(tenant_id=world.people.tenant, user_id=world.people.owner, run_id=uuid4())
    tool = tools["get_negotiation_status"]
    status = await tool.invoke(ctx, tool.parse_input({"rfq": rfq.ref}))
    assert isinstance(status, NegotiationStatusOut)
    assert status.status == "ready_to_close"
    assert "ABOVE TARGET on line(s) 1, 2" in status.note

    # Closing keeps that offer, with the same disclosure.
    outcome = await service.close(world.engine, buyer(world.people), world.rfq_id)
    assert [(o.supplier_name, o.above_target) for o in outcome if o.tier == 0] == [
        ("Rhein", True),
        ("Rhein", True),
    ]


async def test_a_final_offer_above_the_maximum_is_never_recommended(world: World) -> None:
    # Early close off: the "every supplier final" rule applies anyway.
    await world.configure(max_rounds=2, early_close=False)
    writer = FakeNegotiationWriter()
    await _decline(world, "lyon")
    # Line 1 within the maximum (above target), line 2 above the maximum (1.50).
    await _to_final_answer(world, writer, ("0.044", "1.60"))
    assert (await world.sweep(writer)).ready == 1
    view = await service.get_negotiation(world.engine, buyer(world.people), world.rfq_id)
    assert view.status == "ready_to_close" and view.recommendation is not None
    by_line = {o.rfq_line_id: o for o in view.recommendation if o.tier == 0}
    first, second = (by_line[line] for line in world.lines)
    assert (first.supplier_name, first.above_target) == ("Rhein", True)
    assert (second.invite_id, second.supplier_name, second.effective_unit_price_base) == (
        None,
        None,
        None,
    )
    assert second.readiness == "no_feasible_offer" and not second.above_target


async def test_early_close_on_target_is_unchanged(world: World) -> None:
    await world.configure()
    await world.offer("rhein", ("0.039", "0.99"))
    await _decline(world, "lyon")
    assert (await world.sweep(FakeNegotiationWriter())).ready == 1
    view = await service.get_negotiation(world.engine, buyer(world.people), world.rfq_id)
    assert view.status == "ready_to_close" and view.recommendation is not None
    assert all(o.within_target and not o.above_target for o in view.recommendation)
    feed = await rfq_activity(world.engine, buyer(world.people), world.rfq_id)
    [ready] = [a for a in feed if a.action == "rfq.negotiation_ready"]
    assert ready.summary == "Negotiation ready to close"


async def test_a_writer_outage_only_leaves_the_note_out(world: World) -> None:
    await world.configure()
    await world.offer("rhein", ("0.050", "1.20"))
    failing = FailingNegotiationWriter()
    assert (await world.sweep(failing)).proposed == 1
    assert len(failing.calls) == 1
    [row] = await world.messages("rhein")
    assert (row.status, row.attempts, row.sequence) == ("proposed", 1, 1)
    body = str((await world.approval(row.approval_id)).args["body"])
    # Rendered by code: the target is there, as decided.
    assert "0,04\u00a0€" in body


async def test_the_copilot_reads_each_suppliers_state_and_cannot_double_a_counter(
    world: World,
) -> None:
    from bido_back.copilot.tools import (
        CopilotToolDeps,
        NegotiationStatusOut,
        ProposeCounteroffersOut,
        copilot_tools,
    )
    from tests.rfqs.conftest import fake_agents

    await world.configure()
    await world.offer("rhein", ("0.050", "1.20"))
    await world.offer("lyon", ("0.038", "0.95"))
    writer = FakeNegotiationWriter()
    tools = {
        tool.name: tool
        for tool in copilot_tools(
            CopilotToolDeps(
                db=world.engine,
                agents=fake_agents(),
                mailboxes=world.mailboxes,
                negotiation_writer=writer,
            )
        )
    }
    ctx = ToolContext(tenant_id=world.people.tenant, user_id=world.people.owner, run_id=uuid4())
    ref = (await world.rfq()).ref

    async def propose() -> ProposeCounteroffersOut:
        tool = tools["propose_counteroffers"]
        out = await tool.invoke(ctx, tool.parse_input({"rfq": ref}))
        assert isinstance(out, ProposeCounteroffersOut)
        return out

    async def status() -> NegotiationStatusOut:
        tool = tools["get_negotiation_status"]
        out = await tool.invoke(ctx, tool.parse_input({"rfq": ref}))
        assert isinstance(out, NegotiationStatusOut)
        return out

    assert ((await propose()).proposed, (await propose()).proposed) == (1, 0)
    assert len(writer.calls) == 1
    suppliers = {s.supplier_name: s for s in (await status()).suppliers}
    assert (suppliers["Rhein"].state, suppliers["Rhein"].status) == ("held_for_approval", "open")
    assert suppliers["Rhein"].state_text.startswith("Held for your approval")
    assert [(e.kind, e.state) for e in suppliers["Rhein"].emails] == [("revised_offer", "proposed")]
    assert (suppliers["Lyon"].state, suppliers["Lyon"].status) == (
        "accepted_internally",
        "accepted",
    )
    assert suppliers["Lyon"].state_text.startswith("Accepted internally – nothing to ask")
    # Sent: still one email for this offer, and the buyer reads that Bido waits.
    [counter] = await world.messages("rhein")
    await world.approve(counter.approval_id or counter.id)
    assert (await propose()).proposed == 0
    rhein = next(s for s in (await status()).suppliers if s.supplier_name == "Rhein")
    assert (rhein.state, rhein.status) == ("waiting_for_supplier", "countered")
    assert rhein.state_text.startswith("Waiting for the supplier")


async def test_a_rejected_counter_is_not_sent_and_an_invented_number_never_goes_out(
    world: World,
) -> None:
    await world.configure()
    await world.offer("rhein", ("0.050", "1.20"))
    # The writer's note names a competitor's price: the note is left out, the email (every
    # number rendered by code) still goes to approval.
    assert (await world.sweep(FakeNegotiationWriter(invent=True))).proposed == 1
    [counter] = await world.messages("rhein")
    body = str((await world.approval(counter.approval_id)).args["body"])
    assert "0123" not in body and "Another supplier" not in body
    sent_before = len(world.mailboxes.sender.sent)
    await decide_approval(
        world.engine,
        approver(world.people),
        counter.approval_id or counter.id,
        "reject",
        jobs=FakeJobQueue(),
        on_reject=NEGOTIATION_REJECT_HOOKS,
    )
    [counter] = await world.messages("rhein")
    assert counter.status == "rejected"
    assert len(world.mailboxes.sender.sent) == sent_before
    # The buyer said no to that email: no other one for the same offer.
    writer = FakeNegotiationWriter()
    assert (await world.sweep(writer)).proposed == 0 and writer.calls == []
    view = await service.get_negotiation(world.engine, buyer(world.people), world.rfq_id)
    rhein = next(i for i in view.invites if i.supplier_name == "Rhein")
    assert rhein.state is not None and rhein.state.reason == "email_rejected"
    assert rhein.rounds_used == 0


async def test_forcing_counter_offers_never_doubles_an_offers_email(world: World) -> None:
    await world.configure()
    await world.offer("rhein", ("0.050", "1.20"))
    writer = FakeNegotiationWriter()
    first = await service.evaluate_rfq(
        world.engine, world.people.tenant, world.rfq_id, writer=writer, force=True
    )
    assert first.proposed == 1
    again = await service.evaluate_rfq(
        world.engine, world.people.tenant, world.rfq_id, writer=writer, force=True
    )
    assert again.proposed == 0
    [counter] = await world.messages("rhein")
    await world.approve(counter.approval_id or counter.id)
    # Sent: still no second email for the same offer.
    after = await service.evaluate_rfq(
        world.engine, world.people.tenant, world.rfq_id, writer=writer, force=True
    )
    assert after.proposed == 0 and len(writer.calls) == 1
    assert len(await world.messages("rhein")) == 1


async def test_a_refused_email_is_held_once_for_a_person(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Email audit F3: a refused counter was written again every 15-minute sweep, unseen.
    The decision is stored before the email is rendered; code renders it, so only the send
    gate refuses one (a rendering bug): it is recorded once, with its codes, for a person."""
    await world.configure()
    await world.offer("rhein", ("0.050", "1.20"))
    gate_refuses(monkeypatch)
    writer = FakeNegotiationWriter()
    result = await world.sweep(writer)
    assert (result.held, result.proposed, result.skipped) == (1, 0, 0)
    # Recorded once (no approval was proposed): canceled as refused, the codes in its terms;
    # it waits for a person.
    [held] = await world.messages("rhein")
    assert (held.sequence, held.round, held.status, held.canceled_reason, held.approval_id) == (
        1,
        1,
        "canceled",
        "draft_refused",
        None,
    )
    assert held.terms["refused"] == ["number_not_decided"]
    assert held.held_reason == "email_refused:number_not_decided" and held.attempts == 1
    assert held.terms["decision"]["kind"] == "revised_offer"
    # Later sweeps (every 15 minutes) never decide or render it again; the model is never
    # asked for a note on an email the gate refused.
    for _ in range(3):
        assert (await world.sweep(writer)).held == 0
    assert writer.calls == [] and len(await world.messages("rhein")) == 1
    view = await service.get_negotiation(world.engine, buyer(world.people), world.rfq_id)
    rhein = next(i for i in view.invites if i.supplier_name == "Rhein")
    assert rhein.strategy_reasons == ["draft_refused_requires_buyer"]
    assert rhein.state is not None
    assert (rhein.state.phase, rhein.state.reason) == ("needs_decision", "email_not_prepared")
    assert (rhein.messages[0].state, rhein.messages[0].status) == ("held", "canceled")
    feed = await rfq_activity(world.engine, buyer(world.people), world.rfq_id)
    [entry] = [a for a in feed if a.action == "rfq.negotiation_draft_held"]
    assert (entry.supplier_name, entry.actor) == ("Rhein", "Bido")
    assert entry.summary.endswith("Needs your decision")

    # Once fixed, a person asks for counter-offers now (the copilot's `propose_counteroffers`):
    # decided again, the same round (nothing went out).
    monkeypatch.undo()
    result = await service.evaluate_rfq(
        world.engine, world.people.tenant, world.rfq_id, writer=FakeNegotiationWriter(), force=True
    )
    assert result.proposed == 1
    rows = await world.messages("rhein")
    assert [(m.sequence, m.round, m.status) for m in rows] == [
        (1, 1, "canceled"),
        (2, 1, "proposed"),
    ]
    # The refusal stays on record; it no longer waits for a person.
    assert (rows[0].canceled_reason, rows[0].held_reason) == ("draft_refused", None)
    view = await service.get_negotiation(world.engine, buyer(world.people), world.rfq_id)
    rhein = next(i for i in view.invites if i.supplier_name == "Rhein")
    assert rhein.state is not None and rhein.state.phase == "held_for_approval"


async def test_a_round_target_is_asked_in_plain_digits_and_the_counter_is_signed_once(
    world: World,
) -> None:
    """Email audit F1 and F4: a target of 100.00 reached the writer as 1E+2 (the draft was
    refused), and the company signed twice. No buyer contact exists, as in production."""
    await world.configure(
        lines=[
            LineLimitsUpdate(
                rfq_line_id=world.lines[0],
                target_unit_price=Decimal("0.040"),
                max_unit_price=Decimal("0.060"),
                max_lead_time_days=42,
            ),
            LineLimitsUpdate(
                rfq_line_id=world.lines[1],
                target_unit_price=Decimal("100.00"),
                max_unit_price=Decimal("150.00"),
                max_lead_time_days=42,
            ),
        ]
    )
    await world.offer("rhein", ("0.039", "120.00"))
    writer = FakeNegotiationWriter()
    assert (await world.sweep(writer)).proposed == 1
    [task] = writer.calls
    [ask] = task.lines
    assert (str(ask.target_unit_price), str(ask.offered_unit_price)) == ("100", "120")
    assert '"target_unit_price":"100"' in task.model_dump_json()
    [counter] = await world.messages("rhein")
    assert counter.terms["lines"][1]["ask"] == "100"
    approval = await world.approval(counter.approval_id)
    tenant = await tenant_name(world)
    body = str(approval.args["body"])
    # Rendered by code in German: the target exactly as decided, never in exponent form.
    assert "   Zielpreis: 100,00\u00a0€/Stk.\n" in body and "E+" not in body
    assert body.endswith(f"\n\nMit freundlichen Grüßen\n{tenant}\n")
    assert body.count("Mit freundlichen Grüßen") == 1


async def test_counters_keep_the_language_the_rfq_email_went_out_in(world: World) -> None:
    await world.configure()
    async with tenant_session(world.engine, world.people.tenant) as session:
        invite = await session.get(Invite, world.invites["rhein"])
        assert invite is not None
        # The supplier's language is edited after its RFQ email went out in German.
        await session.execute(
            update(Supplier).where(Supplier.id == invite.supplier_id).values(language="fr")
        )
    await world.offer("rhein", ("0.050", "1.20"))
    writer = FakeNegotiationWriter()
    await world.sweep(writer)
    assert [t.supplier_language for t in writer.calls] == ["de"]
    [counter] = await world.messages("rhein")
    body = str((await world.approval(counter.approval_id)).args["body"])
    assert body.startswith("Sehr geehrte Damen und Herren,\n")


async def test_the_deadline_closes_with_the_best_offers_and_stops_follow_ups(
    app_engine: AsyncEngine, rfq_rows: People
) -> None:
    world = await World(app_engine, rfq_rows).setup(silent=True)
    # Quotes were due today: chasing the silent supplier may start (never before that).
    await followup_service.update_followups(
        world.engine,
        buyer(world.people),
        world.rfq_id,
        FollowupSettingsUpdate(response_due=datetime.now(UTC).date()),
    )
    # A week: the first chase (2 business days, US calendar for the silent
    # supplier) must fall before the deadline even across a holiday weekend.
    deadline = datetime.now(UTC) + timedelta(days=7)
    await world.configure(deadline=deadline)
    await world.offer("rhein", ("0.050", "1.20"))
    await world.offer("lyon", ("0.038", "1.10"))
    writer = FakeNegotiationWriter()
    result = await world.sweep(writer)
    # Silent has not answered: not ready to close; Rhein and Lyon get counters.
    assert result.ready == 0 and result.proposed == 2
    [rhein_counter] = await world.messages("rhein")

    # A chase for the silent supplier is due before the deadline.
    chases = FakeFollowupWriter()
    later = add_business_days(datetime.now(UTC), 2, "US") + timedelta(minutes=1)
    await followup_service.sweep_tenant(world.engine, world.people.tenant, writer=chases, now=later)
    assert [t.kind for t in chases.calls] == ["chase"]

    after = deadline + timedelta(minutes=1)
    result = await world.sweep(writer, now=after)
    assert result.closed == 1
    rfq = await world.rfq()
    assert rfq.negotiation_status == "closed" and rfq.negotiation_closed_at == after
    view = await service.get_negotiation(world.engine, buyer(world.people), world.rfq_id)
    assert view.outcome is not None
    first_line = [o for o in view.outcome if o.rfq_line_id == world.lines[0] and o.tier == 0]
    assert first_line[0].supplier_name == "Lyon" and first_line[0].within_target is True
    assert {i.supplier_name: i.status for i in view.invites}["Rhein"] == "closed"
    # The pending counter is canceled and its approval expired; nothing more is chased.
    [rhein_counter] = await world.messages("rhein")
    assert (rhein_counter.status, rhein_counter.canceled_reason) == (
        "canceled",
        "negotiation_closed",
    )
    async with tenant_session(world.engine, world.people.tenant) as session:
        chase_rows = list(
            await session.scalars(
                select(FollowupMessage).where(FollowupMessage.invite_id == world.invites["silent"])
            )
        )
    assert [c.status for c in chase_rows] == ["proposed"]
    await followup_service.sweep_tenant(
        world.engine, world.people.tenant, writer=chases, now=after + timedelta(days=10)
    )
    async with tenant_session(world.engine, world.people.tenant) as session:
        chase_rows = list(
            await session.scalars(
                select(FollowupMessage).where(FollowupMessage.invite_id == world.invites["silent"])
            )
        )
    assert [c.status for c in chase_rows] == ["canceled"]
    # Closed is final.
    with pytest.raises(InvalidState):
        await world.configure()


async def test_the_buyer_closes_early_and_the_scheduled_job_sweeps_every_tenant(
    world: World,
) -> None:
    await world.configure()
    await world.offer("rhein", ("0.039", "0.99"))
    await world.offer("lyon", ("0.041", "0.97"))
    handler = NegotiationSweepHandler(db=world.engine, writer=FakeNegotiationWriter())
    await handler(Envelope(type=NEGOTIATION_SWEEP, tenant_id=ALL_TENANTS, idempotency_key="t"))
    assert (await world.rfq()).negotiation_status == "ready_to_close"

    outcome = await service.close(world.engine, buyer(world.people), world.rfq_id)
    by_line = {(o.rfq_line_id, o.tier): o for o in outcome}
    assert by_line[(world.lines[0], 0)].supplier_name == "Rhein"
    assert by_line[(world.lines[1], 0)].supplier_name == "Lyon"
    # Closing twice returns the same outcome.
    again = await service.close(world.engine, buyer(world.people), world.rfq_id)
    assert again == outcome


async def test_limits_must_belong_to_the_rfq_and_tenants_are_isolated(
    world: World, two_tenants: TwoTenants
) -> None:
    with pytest.raises(NotFound, match="not on rfq"):
        await service.update_negotiation(
            world.engine,
            buyer(world.people),
            world.rfq_id,
            NegotiationSettingsUpdate(
                lines=[
                    LineLimitsUpdate(rfq_line_id=world.people.tenant, target_unit_price=Decimal(1))
                ]
            ),
        )
    other = Principal(tenant_id=two_tenants.b.tenant_id, user_id=two_tenants.b.user_id)
    with pytest.raises(NotFound, match="not found"):
        await service.get_negotiation(world.engine, other, world.rfq_id)


async def test_an_unchanged_reply_stops_asking_and_needs_the_buyer(world: World) -> None:
    await world.configure(max_rounds=8)
    await world.offer("rhein", ("0.050", "1.20"))
    writer = FakeNegotiationWriter()
    assert (await world.sweep(writer)).proposed == 1
    counter = (await world.messages("rhein"))[-1]
    assert counter.approval_id is not None
    await world.approve(counter.approval_id)
    await world.offer("rhein", ("0.050", "1.20"))
    assert (await world.sweep(writer)).proposed == 0
    assert len(writer.calls) == 1
    view = await service.get_negotiation(world.engine, buyer(world.people), world.rfq_id)
    rhein = next(i for i in view.invites if i.supplier_name == "Rhein")
    assert rhein.status == "closed"
    assert rhein.stalled_rounds == 1
    assert rhein.strategy_reasons == ["no_progress"]
    assert rhein.state is not None and rhein.state.phase == "needs_decision"


async def test_a_reply_being_read_waits_and_only_an_offerless_one_needs_the_buyer(
    world: World,
) -> None:
    await world.configure()
    await world.offer("lyon", ("0.038", "0.95"))
    message_id = await world.reply("rhein")
    writer = FakeNegotiationWriter()

    async def needs_answer_events() -> list[AuditEvent]:
        async with tenant_session(world.engine, world.people.tenant) as session:
            return list(
                await session.scalars(
                    select(AuditEvent).where(
                        AuditEvent.action == "rfq.negotiation_needs_answer",
                        AuditEvent.payload["invite_id"].astext == str(world.invites["rhein"]),
                    )
                )
            )

    # Rhein's reply may be its quote: Bido reads it, nobody is asked to answer it, and
    # the RFQ does not become ready to close on Lyon's offer meanwhile.
    assert (await world.sweep(writer)).ready == 0
    view = await service.get_negotiation(world.engine, buyer(world.people), world.rfq_id)
    rhein = next(i for i in view.invites if i.supplier_name == "Rhein")
    assert rhein.state is not None
    assert (rhein.state.phase, rhein.state.reason, rhein.state.next_action) == (
        "waiting",
        "reading_reply",
        "wait",
    )
    assert rhein.state.label == "Waiting: Bido is reading the supplier's reply."
    assert rhein.strategy_reasons == ["reading_reply"]
    assert await needs_answer_events() == []

    # Read, and it held no offer (a question, a request): now the buyer answers.
    async with tenant_session(world.engine, world.people.tenant) as session:
        await session.execute(
            update(Message).where(Message.id == message_id).values(status="extracted")
        )
    assert (await world.sweep(writer)).ready == 1
    view = await service.get_negotiation(world.engine, buyer(world.people), world.rfq_id)
    rhein = next(i for i in view.invites if i.supplier_name == "Rhein")
    assert rhein.state is not None
    assert (rhein.state.phase, rhein.state.reason, rhein.state.next_action) == (
        "needs_answer",
        "reply_without_offer",
        "hold",
    )
    assert len(await needs_answer_events()) == 1


async def test_policy_change_cancels_pending_email_without_changing_negotiation_bounds(
    world: World,
) -> None:
    from bido_back.comparison.policy import ComparisonPolicy

    deadline = datetime.now(UTC) + timedelta(days=10)
    await world.configure(max_rounds=5, deadline=deadline)
    await world.offer("rhein", ("0.050", "1.20"))
    writer = FakeNegotiationWriter()
    assert (await world.sweep(writer)).proposed == 1
    before = await world.rfq()
    [pending] = await world.messages("rhein")
    policy = ComparisonPolicy(require_complete_costs=True)
    await service.set_comparison_policy(world.engine, buyer(world.people), world.rfq_id, policy)
    after = await world.rfq()
    assert after.negotiation_deadline == before.negotiation_deadline
    assert after.negotiation_max_rounds == before.negotiation_max_rounds
    [canceled] = await world.messages("rhein")
    assert canceled.status == "canceled"
    assert canceled.canceled_reason == "comparison_policy_changed"
    assert (await world.approval(pending.approval_id)).status == "expired"
    assert (await world.sweep(writer)).proposed == 0
    assert len(writer.calls) == 1


async def test_clarification_pauses_price_negotiation_with_existing_offer(world: World) -> None:
    await world.configure()
    proposal_id = await world.offer("rhein", ("0.050", "1.20"))
    async with tenant_session(world.engine, world.people.tenant) as session:
        proposal = await session.get(Proposal, proposal_id)
        assert proposal is not None
        proposal.fields = {"_offer_state": {"reply_kind": "clarification", "issues": []}}
    writer = FakeNegotiationWriter()
    assert (await world.sweep(writer)).proposed == 0
    assert writer.calls == []
    view = await service.get_negotiation(world.engine, buyer(world.people), world.rfq_id)
    rhein = next(i for i in view.invites if i.supplier_name == "Rhein")
    assert rhein.strategy_reasons == ["supplier_question"]
    assert rhein.state is not None and rhein.state.phase == "needs_answer"


async def test_the_buyer_releases_a_held_offer_and_it_is_negotiated_as_it_is(
    world: World,
) -> None:
    from bido_back.rfqs.activity import rfq_activity

    await world.configure()
    proposal_id = await world.offer("rhein", ("0.050", "1.20"))
    async with tenant_session(world.engine, world.people.tenant) as session:
        proposal = await session.get(Proposal, proposal_id)
        assert proposal is not None
        proposal.fields = {
            "_offer_state": {
                "reply_kind": "amendment",
                "issues": ["amendment_without_explicit_preservation"],
                "requires_clarification": True,
            }
        }
    writer = FakeNegotiationWriter()
    assert (await world.sweep(writer)).proposed == 0
    view = await service.get_negotiation(world.engine, buyer(world.people), world.rfq_id)
    rhein = next(i for i in view.invites if i.supplier_name == "Rhein")
    assert rhein.state is not None
    assert (rhein.state.phase, rhein.state.reason) == (
        "needs_decision",
        "offer_state_requires_review",
    )
    # Only the offer the buyer reviewed is released: not one that arrived meanwhile.
    with pytest.raises(InvalidState):
        await service.release_hold(
            world.engine, buyer(world.people), world.rfq_id, world.invites["rhein"], uuid4()
        )
    released = await service.release_hold(
        world.engine, buyer(world.people), world.rfq_id, world.invites["rhein"], proposal_id
    )
    rhein = next(i for i in released.invites if i.supplier_name == "Rhein")
    assert rhein.state is not None and rhein.state.next_action == "revised_offer"
    assert (await world.sweep(writer)).proposed == 1
    feed = await rfq_activity(world.engine, buyer(world.people), world.rfq_id)
    assert any(i.action == "rfq.negotiation_hold_released" for i in feed)
    with pytest.raises(NotFound):
        await service.release_hold(
            world.engine, buyer(world.people), world.rfq_id, world.people.tenant, proposal_id
        )


async def test_closed_outcome_keeps_its_policy_and_evidence_snapshot(world: World) -> None:
    from bido_back.comparison.policy import ComparisonPolicy

    await world.configure()
    proposal_id = await world.offer("rhein", ("0.039", "0.95"))
    outcome = await service.close(world.engine, buyer(world.people), world.rfq_id)
    assert outcome[0].proposal_id == proposal_id
    assert "freight_cost_unknown" in outcome[0].evidence_gaps
    assert outcome[0].readiness == "needs_review"
    await service.set_comparison_policy(
        world.engine,
        buyer(world.people),
        world.rfq_id,
        ComparisonPolicy(preference="reliability_first"),
    )
    view = await service.get_negotiation(world.engine, buyer(world.people), world.rfq_id)
    assert view.status == "closed"
    assert view.settings.comparison_policy.preference == "reliability_first"
    assert view.outcome is not None
    assert view.outcome[0].policy.preference == "cost_first"
    assert view.outcome[0].proposal_id == proposal_id


async def test_comparison_loads_item_specific_history_and_policy(world: World) -> None:
    from sqlalchemy import delete

    from bido_back.comparison.loader import load_comparison
    from bido_back.comparison.policy import ComparisonPolicy
    from bido_back.db.models import Item, PoLine, PurchaseOrder

    await world.offer("rhein", ("0.039", "0.95"))
    await world.offer("lyon", ("0.050", "1.20"))
    today = datetime.now(UTC).date()
    item_ids: list[UUID] = []
    order_ids: list[UUID] = []
    try:
        async with tenant_session(world.engine, world.people.tenant) as session:
            first = await session.get(RfqLine, world.lines[0])
            assert first is not None
            matched = Item(
                tenant_id=world.people.tenant,
                part_number=first.part_number or "MATCHED",
                description="Capacitor",
                mpn=first.mpn,
            )
            unrelated = Item(
                tenant_id=world.people.tenant,
                part_number="UNRELATED",
                description="Other",
                mpn="OTHER",
            )
            session.add_all([matched, unrelated])
            await session.flush()
            item_ids = [matched.id, unrelated.id]
            for name in ("rhein", "lyon"):
                invite = await session.get(Invite, world.invites[name])
                assert invite is not None
                order = PurchaseOrder(
                    tenant_id=world.people.tenant,
                    supplier_id=invite.supplier_id,
                    po_number=f"history-{name}",
                    ordered_at=today - timedelta(days=40),
                )
                session.add(order)
                await session.flush()
                order_ids.append(order.id)
                for n in range(5):
                    session.add(
                        PoLine(
                            tenant_id=world.people.tenant,
                            purchase_order_id=order.id,
                            line_no=n + 1,
                            item_id=matched.id,
                            quantity=Decimal(100),
                            unit_price=Decimal("0.05"),
                            promised_at=today - timedelta(days=10),
                            received_at=today - timedelta(days=5 if name == "rhein" else 12),
                            received_qty=Decimal(100),
                            rejected_qty=Decimal(10 if name == "rhein" else 0),
                        )
                    )
                session.add(
                    PoLine(
                        tenant_id=world.people.tenant,
                        purchase_order_id=order.id,
                        line_no=99,
                        item_id=unrelated.id,
                        quantity=Decimal(100),
                        unit_price=Decimal("0.05"),
                        promised_at=today - timedelta(days=10),
                        received_at=today - timedelta(days=12),
                        received_qty=Decimal(100),
                        rejected_qty=Decimal(0),
                    )
                )
        await service.set_comparison_policy(
            world.engine,
            buyer(world.people),
            world.rfq_id,
            ComparisonPolicy(preference="reliability_first"),
        )
        comparison = await load_comparison(world.engine, buyer(world.people), world.rfq_id, today)
        rec = next(
            r for r in comparison.recommendations if r.rfq_line_id == world.lines[0] and r.tier == 0
        )
        assert rec.supplier_name == "Lyon"
        assert rec.performance.delivery_samples == 5
        assert rec.performance.on_time_rate == 1
        rhein = next(s for s in comparison.suppliers if s.supplier_name == "Rhein")
        cell = next(c for c in rhein.cells if c.rfq_line_id == world.lines[0] and c.tier == 0)
        assert cell.performance.delivery_samples == 5
        assert cell.performance.on_time_rate == 0
        assert cell.performance.accepted_quantity_rate == Decimal("0.9")
        second = next(c for c in rhein.cells if c.rfq_line_id == world.lines[1] and c.tier == 0)
        assert second.performance.observations == 0
    finally:
        async with tenant_session(world.engine, world.people.tenant) as session:
            await session.execute(delete(PurchaseOrder).where(PurchaseOrder.id.in_(order_ids)))
            await session.execute(delete(Item).where(Item.id.in_(item_ids)))


async def test_comparison_policy_race_does_not_relabel_stale_offers(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import date

    from bido_back.comparison.compute import Comparison
    from bido_back.comparison.policy import ComparisonPolicy

    await world.configure()
    await world.offer("rhein", ("0.050", "1.20"))
    original = service.load_comparison

    async def changed_after_read(
        db: service.Db,
        principal: Principal,
        rfq_id: UUID,
        today: date,
    ) -> Comparison:
        stale = await original(db, principal, rfq_id, today)
        await service.set_comparison_policy(
            db, principal, rfq_id, ComparisonPolicy(require_complete_costs=True)
        )
        return stale

    monkeypatch.setattr(service, "load_comparison", changed_after_read)
    writer = FakeNegotiationWriter()
    result = await world.sweep(writer)
    assert result.skipped == 1
    assert result.proposed == 0
    assert writer.calls == []


async def test_correction_during_writer_call_invalidates_unrecorded_proposal(world: World) -> None:
    from bido_back.corrections import service as corrections
    from bido_back.corrections.schemas import LineCorrectionRequest

    await world.configure()
    proposal_id = await world.offer("rhein", ("0.050", "1.20"))
    async with tenant_session(world.engine, world.people.tenant) as session:
        line_id = await session.scalar(
            select(ProposalLine.id).where(
                ProposalLine.proposal_id == proposal_id,
                ProposalLine.rfq_line_id == world.lines[0],
            )
        )
    assert line_id is not None
    fake = FakeNegotiationWriter()

    async def correcting_writer(
        task: NegotiationEmailInput, context: ToolContext
    ) -> EmailNoteOutput:
        await corrections.correct_line(
            world.engine,
            buyer(world.people),
            proposal_id,
            line_id,
            LineCorrectionRequest(conditional_terms="Only with full package"),
        )
        return await fake(task, context)

    result = await service.evaluate_rfq(
        world.engine, world.people.tenant, world.rfq_id, writer=correcting_writer
    )
    assert result.proposed == 0
    # The correction canceled the decided email while it was being written.
    [row] = await world.messages("rhein")
    assert (row.status, row.canceled_reason) == ("canceled", "offer_corrected")
    async with tenant_session(world.engine, world.people.tenant) as session:
        approvals = list(
            await session.scalars(select(Approval).where(Approval.tool == SEND_NEGOTIATION_EMAIL))
        )
    assert len(approvals) == 1
    assert approvals[0].status == "expired"


async def test_final_quotation_excludes_offers_above_buyer_limits(world: World) -> None:
    await world.configure()
    await world.offer("rhein", ("0.080", "2.00"))
    outcome = await service.close(world.engine, buyer(world.people), world.rfq_id)
    assert all(line.invite_id is None for line in outcome)
    assert all(line.readiness == "no_feasible_offer" for line in outcome)
    assert all(
        any("above_buyer_maximum" in offer.reasons for offer in line.excluded) for line in outcome
    )


async def test_historical_target_uses_latest_comparable_quantity_and_price_basis(
    world: World,
) -> None:
    from bido_back.negotiation.history import quoted_price_history

    await world.offer("rhein", ("0.001", "1.00"))
    latest = await world.offer("rhein", ("0.50", "1.00"))
    current = await rfq_service.create_rfq_from_text(
        world.engine, buyer(world.people), "New capacitor need", FakeIntake(), currency="EUR"
    )
    mpn = current.lines[0].mpn
    assert mpn is not None
    async with tenant_session(world.engine, world.people.tenant) as session:
        rfq = await session.get(Rfq, current.id)
        line = await session.scalar(
            select(ProposalLine).where(
                ProposalLine.proposal_id == latest,
                ProposalLine.rfq_line_id == world.lines[0],
            )
        )
        assert rfq is not None and line is not None
        line.fields = {
            "price_basis_quantity": {"parsed": "100", "confidence": 1, "needs_review": False}
        }
        line.min_quantity = Decimal(50000)
        await session.flush()
        # The older cheap version is not current, and the current break is inapplicable.
        assert await quoted_price_history(session, rfq, mpn) is None
        line.min_quantity = Decimal(100)
        await session.flush()
        assert await quoted_price_history(session, rfq, mpn) == Decimal("0.005")
        line.unit = "box"
        await session.flush()
        assert await quoted_price_history(session, rfq, mpn) is None


async def test_a_rejected_best_and_final_leaves_no_cut_off_behind(world: World) -> None:
    await world.configure(max_rounds=2, early_close=False)
    writer = FakeNegotiationWriter()
    await world.offer("rhein", ("0.050", "1.20"))
    await world.sweep(writer)
    [first] = await world.messages("rhein")
    await world.approve(first.approval_id or first.id)
    await world.offer("rhein", ("0.045", "1.10"))
    await world.sweep(writer)
    bafo = (await world.messages("rhein"))[-1]
    assert bafo.kind == "bafo" and (await world.rfq()).negotiation_cutoff_at is not None
    await decide_approval(
        world.engine,
        approver(world.people),
        bafo.approval_id or bafo.id,
        "reject",
        jobs=FakeJobQueue(),
        on_reject=NEGOTIATION_REJECT_HOOKS,
    )
    # No supplier was told that cut-off: it binds nobody, and nobody is forced to a final round.
    await world.sweep(writer)
    assert (await world.rfq()).negotiation_cutoff_at is None
    view = await service.get_negotiation(world.engine, buyer(world.people), world.rfq_id)
    assert view.cutoff_at is None
    rhein = next(i for i in view.invites if i.supplier_name == "Rhein")
    assert rhein.state is not None and rhein.state.reason == "email_rejected"


async def test_an_email_approved_after_its_reply_by_date_is_decided_again(world: World) -> None:
    await world.configure()
    await world.offer("rhein", ("0.050", "1.20"))
    writer = FakeNegotiationWriter()
    await world.sweep(writer)
    [counter] = await world.messages("rhein")
    # Approved late: the reply-by date the email states has passed.
    async with tenant_session(world.engine, world.people.tenant) as session:
        row = await session.get(NegotiationMessage, counter.id)
        assert row is not None
        terms = dict(row.terms)
        decision = dict(cast(dict[str, object], terms["decision"]))
        decision["reply_by"] = (datetime.now(UTC).date() - timedelta(days=1)).isoformat()
        terms["decision"] = cast(JsonValue, decision)
        row.terms = terms
    sent_before = len(world.mailboxes.sender.sent)
    await world.approve(counter.approval_id or counter.id)
    assert len(world.mailboxes.sender.sent) == sent_before
    [counter] = await world.messages("rhein")
    assert (counter.status, counter.canceled_reason) == ("canceled", "reply_by_passed")
    # Nothing went out: decided again, with a new reply-by date, in the same round.
    assert (await world.sweep(writer)).proposed == 1
    rows = await world.messages("rhein")
    assert [(m.sequence, m.round, m.status) for m in rows] == [
        (1, 1, "canceled"),
        (2, 1, "proposed"),
    ]


async def test_an_interrupted_send_counts_as_sent(world: World) -> None:
    await world.configure()
    writer = FakeNegotiationWriter()
    await world.offer("rhein", ("0.050", "1.20"))
    await world.sweep(writer)
    [counter] = await world.messages("rhein")
    # The send claimed its Message-ID and was interrupted; a newer offer then canceled it.
    async with tenant_session(world.engine, world.people.tenant) as session:
        await session.execute(
            update(NegotiationMessage)
            .where(NegotiationMessage.id == counter.id)
            .values(message_id_header="<claimed@quotes.example.test>")
        )
    await world.offer("rhein", ("0.045", "1.10"))
    await world.sweep(writer)
    rows = await world.messages("rhein")
    assert rows[0].status == "canceled"
    # It may have reached the supplier: its round is used.
    assert [(m.sequence, m.round, m.status) for m in rows] == [
        (1, 1, "canceled"),
        (2, 2, "proposed"),
    ]


async def test_an_unanswered_request_gets_one_reminder(world: World) -> None:
    await world.configure()
    writer = FakeNegotiationWriter()
    await world.offer("rhein", ("0.050", "1.20"))
    await world.sweep(writer)
    [counter] = await world.messages("rhein")
    await world.approve(counter.approval_id or counter.id)
    reply_by = date.fromisoformat(str((await world.messages("rhein"))[0].terms["reply_by"]))
    # No reply by the reply-by date: the same request once more, with a new reply-by date.
    after = datetime.combine(reply_by + timedelta(days=1), time(10), tzinfo=UTC)
    assert (await world.sweep(writer, now=after)).proposed == 1
    rows = await world.messages("rhein")
    assert [(m.sequence, m.round, m.kind, m.status) for m in rows] == [
        (1, 1, "revised_offer", "sent"),
        (2, 1, "reminder", "proposed"),
    ]
    reminder = rows[1]
    assert reminder.terms["decision"]["reminder_of"] == 1
    assert date.fromisoformat(str(reminder.terms["reply_by"])) > reply_by
    await world.approve(reminder.approval_id or reminder.id)
    view = await service.get_negotiation(world.engine, buyer(world.people), world.rfq_id, now=after)
    rhein = next(i for i in view.invites if i.supplier_name == "Rhein")
    # A reminder is no round.
    assert rhein.rounds_used == 1
    assert rhein.state is not None and rhein.state.reason == "reminder_sent"
    assert [m.kind for m in rhein.messages] == ["revised_offer", "reminder"]
    # Never a second one: unanswered after it, the last offer stands.
    much_later = after + timedelta(days=7)
    assert (await world.sweep(writer, now=much_later)).proposed == 0
    view = await service.get_negotiation(
        world.engine, buyer(world.people), world.rfq_id, now=much_later
    )
    rhein = next(i for i in view.invites if i.supplier_name == "Rhein")
    assert rhein.state is not None and rhein.state.reason == "no_reply_after_reminder"
    assert len(await world.messages("rhein")) == 2


# --- One email per supplier answer -----------------------------------------------------------


async def _without_validity(world: World, *proposal_ids: UUID) -> None:
    async with tenant_session(world.engine, world.people.tenant) as session:
        for proposal_id in proposal_ids:
            proposal = await session.get(Proposal, proposal_id)
            assert proposal is not None
            proposal.valid_until = None


async def _asks(world: World, name: str) -> list[ClarificationAsk]:
    async with tenant_session(world.engine, world.people.tenant) as session:
        return list(
            await session.scalars(
                select(ClarificationAsk).where(ClarificationAsk.invite_id == world.invites[name])
            )
        )


async def test_one_email_asks_what_the_offer_lacks_and_for_a_revised_offer(world: World) -> None:
    """No validity and prices above target: ONE email asks the validity first, then for a
    revised offer, and it is the round. The follow-up sweep writes no clarification of its
    own for that supplier (only for one with nothing to negotiate), and the validity is
    asked by that email, once, when it goes out."""
    await world.configure()
    rhein = await world.offer("rhein", ("0.050", "1.20"))
    lyon = await world.offer("lyon", ("0.038", "0.95"))
    await _without_validity(world, rhein, lyon)

    followup_writer = FakeFollowupWriter()
    swept = await followup_service.sweep_tenant(
        world.engine, world.people.tenant, writer=followup_writer
    )
    # Lyon has nothing to negotiate: its validity goes out alone. Rhein's waits for the
    # negotiation's email.
    assert swept.clarifications == 1
    assert [t.supplier_name for t in followup_writer.calls] == ["Lyon"]

    writer = FakeNegotiationWriter()
    assert (await world.sweep(writer)).proposed == 1
    [row] = await world.messages("rhein")
    decision = service.stored_decision(row)
    assert decision is not None
    assert (decision.kind, decision.round, row.round, row.action) == (
        "revised_offer",
        1,
        1,
        "counter",
    )
    assert [(q.position, q.field, q.problem) for q in decision.questions] == [
        (None, "valid_until", "missing")
    ]
    assert decision.deferred_lines == []
    assert await world.messages("lyon") == []
    approval = await world.approval(row.approval_id)
    body = str(approval.args["body"])
    # The question first, then the revised offer: two numbered asks, one reply-by date.
    first, second = body.index("\n1. "), body.index("\n2. ")
    assert first < second and "\n3. " not in body
    assert "Gültigkeit" in body[first:second]
    assert "überarbeitetes Angebot" in body[second:]
    assert body.count("bis ") >= 1 and "Bitte antworten Sie auf diese E-Mail bis" in body
    # Nothing is asked before the email goes out (a rejected one never asked anything).
    assert await _asks(world, "rhein") == []
    feed = await rfq_activity(world.engine, buyer(world.people), world.rfq_id)
    assert any(
        i.summary == "Bido prepared a request for a revised offer that also asks for what the "
        "offer lacks (round 1) (Rhein)"
        for i in feed
    )

    await world.approve(approval.id)
    [asked] = await _asks(world, "rhein")
    assert (asked.position, asked.field, asked.status) == (None, "valid_until", "open")
    assert (asked.negotiation_message_id, asked.followup_id) == (row.id, None)
    assert asked.reply_by == decision.reply_by
    async with tenant_session(world.engine, world.people.tenant) as session:
        states = await ledger.supplier_followups(session, world.rfq_id, now=datetime.now(UTC))
        state = states[world.invites["rhein"]]
        assert state.request is not None and state.request.kind == "counter"
        assert [a.id for a in state.request.asks] == [asked.id]
        assert state.to_ask == ()
        # The supplier's answer settles what that email asked.
        later = datetime.now(UTC) + timedelta(minutes=5)
        answered = await ledger.asks_answered_by(session, world.invites["rhein"], later)
        assert [a.id for a in answered] == [asked.id]
    # Nothing else goes to Rhein while that request waits for its answer.
    followup_writer.calls.clear()
    await followup_service.sweep_tenant(world.engine, world.people.tenant, writer=followup_writer)
    assert "Rhein" not in [t.supplier_name for t in followup_writer.calls]
    view = await service.get_negotiation(world.engine, buyer(world.people), world.rfq_id)
    shown = next(i for i in view.invites if i.supplier_name == "Rhein")
    assert shown.rounds_used == 1 and shown.status == "countered"
    assert shown.state is not None and shown.state.reason == "revised_offer_sent"


async def test_an_email_asks_the_ledger_once_and_the_database_says_which(world: World) -> None:
    """Exactly one email asks an item: a clarification or a negotiation email."""
    from sqlalchemy.exc import DBAPIError

    await world.configure()
    await world.offer("rhein", ("0.050", "1.20"))
    await world.sweep(FakeNegotiationWriter())
    [row] = await world.messages("rhein")
    for followup_id, negotiation_id in ((None, None), (uuid4(), row.id)):
        with pytest.raises(DBAPIError, match="clarification_asks_email_check"):
            async with tenant_session(world.engine, world.people.tenant) as session:
                session.add(
                    ClarificationAsk(
                        tenant_id=world.people.tenant,
                        invite_id=world.invites["rhein"],
                        followup_id=followup_id,
                        negotiation_message_id=negotiation_id,
                        field="valid_until",
                        question="value",
                        status="proposed",
                    )
                )
                await session.flush()


async def test_a_planner_that_cannot_run_only_delays_its_clarifications(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    await world.configure()
    lyon = await world.offer("lyon", ("0.038", "0.95"))
    await _without_validity(world, lyon)

    async def broken(*_: object, **__: object) -> frozenset[UUID]:
        raise RuntimeError("comparison unavailable")

    with monkeypatch.context() as patched:
        patched.setattr(followup_service, "planned_clarifications", broken)
        writer = FakeFollowupWriter()
        swept = await followup_service.sweep_tenant(
            world.engine, world.people.tenant, writer=writer
        )
    # Never two emails for one answer: the clarification waits for the next sweep.
    assert (swept.clarifications, writer.calls) == (0, [])
    swept = await followup_service.sweep_tenant(
        world.engine, world.people.tenant, writer=FakeFollowupWriter()
    )
    assert swept.clarifications == 1


# --- A supplier reply re-plans its RFQ right away ----------------------------------------


async def _ask_replan(world: World) -> datetime:
    """What quote extraction does after storing a reply (`request_replan`)."""
    async with tenant_session(world.engine, world.people.tenant) as session:
        rfq = await session.scalar(select(Rfq).where(Rfq.id == world.rfq_id).with_for_update())
        assert rfq is not None
        at = datetime.now(UTC)
        assert request_replan(rfq, at)
        return at


class BumpingWriter(FakeNegotiationWriter):
    """Another reply lands while the re-plan writes its emails."""

    def __init__(self, world: World) -> None:
        super().__init__()
        self.world = world

    async def __call__(self, task: NegotiationEmailInput, context: ToolContext) -> EmailNoteOutput:
        async with tenant_session(self.world.engine, self.world.people.tenant) as session:
            rfq = await session.scalar(
                select(Rfq).where(Rfq.id == self.world.rfq_id).with_for_update()
            )
            assert rfq is not None
            # Pending already: the reply bumps the request but enqueues nothing.
            assert not request_replan(rfq, datetime.now(UTC))
        return await super().__call__(task, context)


async def test_a_reply_is_planned_at_once_and_a_duplicate_job_does_nothing(world: World) -> None:
    await world.configure()
    await world.offer("rhein", ("0.050", "1.20"))
    requested_at = await _ask_replan(world)
    jobs = FakeJobQueue()
    handler = NegotiationReplanHandler(db=world.engine, writer=FakeNegotiationWriter(), jobs=jobs)
    envelope = replan_envelope(world.people.tenant, world.rfq_id, requested_at)

    await handler(envelope)

    [rhein] = await world.messages("rhein")
    assert rhein.status == "proposed"  # the counter-offer awaits approval, no sweep needed
    assert (await world.rfq()).negotiation_replan_requested_at is None
    assert jobs.jobs == []
    # Redelivered: nothing is pending, nothing is planned again.
    writer = FakeNegotiationWriter()
    result = await service.replan_rfq(
        world.engine, world.people.tenant, world.rfq_id, writer=writer
    )
    assert not result.ran and writer.calls == []


async def test_a_reply_during_the_replan_gets_one_more_job(world: World) -> None:
    await world.configure()
    await world.offer("rhein", ("0.050", "1.20"))
    requested_at = await _ask_replan(world)
    jobs = FakeJobQueue()
    handler = NegotiationReplanHandler(db=world.engine, writer=BumpingWriter(world), jobs=jobs)

    await handler(replan_envelope(world.people.tenant, world.rfq_id, requested_at))

    pending = (await world.rfq()).negotiation_replan_requested_at
    assert pending is not None and pending > requested_at
    [again] = jobs.jobs
    assert again.payload == {"rfq_id": str(world.rfq_id)}
    assert again.idempotency_key.endswith(pending.isoformat())


async def test_the_sweep_settles_a_replan_whose_job_was_lost(world: World) -> None:
    await world.configure()
    await world.offer("rhein", ("0.050", "1.20"))
    await _ask_replan(world)  # the enqueue after the commit failed: no job

    assert (await world.sweep(FakeNegotiationWriter())).proposed == 1

    assert (await world.rfq()).negotiation_replan_requested_at is None
    # The next reply enqueues again.
    await _ask_replan(world)


# --- Releasing a hold from the Copilot -------------------------------------------------------


async def test_the_copilot_releases_a_held_offer_only_after_the_buyer_confirms(
    world: World,
) -> None:
    from tests.copilot.conftest import context_of, make_tools

    await world.configure()
    proposal_id = await world.offer("rhein", ("0.050", "1.20"))
    async with tenant_session(world.engine, world.people.tenant) as session:
        proposal = await session.get(Proposal, proposal_id)
        assert proposal is not None
        proposal.fields = {
            "_offer_state": {
                "reply_kind": "amendment",
                "issues": ["amendment_without_explicit_preservation"],
                "requires_clarification": True,
            }
        }
    tools = make_tools(world.engine)
    ctx = context_of(buyer(world.people))
    rfq_ref = (await world.rfq()).ref

    # Lyon has no offer on hold: nothing to release, nothing proposed.
    with pytest.raises(ToolError, match="Lyon has sent no offer|not held"):
        await tools.call(ctx, "release_negotiation_hold", rfq=rfq_ref, supplier="Lyon")

    preview = await tools.call(ctx, "release_negotiation_hold", rfq=rfq_ref, supplier="rhein")

    assert preview.awaiting_confirmation and preview.proposal_id is not None
    shown = preview.preview.negotiation_hold
    assert (shown.supplier_name, shown.offer_version) == ("Rhein", 1)
    assert shown.reason == "offer_state_requires_review"
    assert "offer_state_requires_review" in shown.holds
    # The preview stores the proposal and nothing else: the offer is still held.
    async with tenant_session(world.engine, world.people.tenant) as session:
        invite = await session.get(Invite, world.invites["rhein"])
    assert invite is not None and invite.negotiation_released_proposal_id is None
    feed = await rfq_activity(world.engine, buyer(world.people), world.rfq_id)
    assert not any(i.action == "rfq.negotiation_hold_released" for i in feed)
    # Not in the same run: the buyer has not answered yet.
    with pytest.raises(ToolError, match="not confirmed"):
        await tools.call(
            ctx,
            "release_negotiation_hold",
            rfq=rfq_ref,
            supplier="Rhein",
            proposal_id=str(preview.proposal_id),
        )

    done = await tools.call(
        replace(ctx, run_id=uuid4()),
        "release_negotiation_hold",
        rfq=rfq_ref,
        supplier="Rhein",
        proposal_id=str(preview.proposal_id),
    )

    assert not done.awaiting_confirmation
    async with tenant_session(world.engine, world.people.tenant) as session:
        invite = await session.get(Invite, world.invites["rhein"])
    assert invite is not None and invite.negotiation_released_proposal_id == proposal_id
    feed = await rfq_activity(world.engine, buyer(world.people), world.rfq_id)
    assert any(i.action == "rfq.negotiation_hold_released" for i in feed)
    assert (await world.sweep(FakeNegotiationWriter())).proposed == 1
