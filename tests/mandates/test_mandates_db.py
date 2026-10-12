"""Negotiation mandates against Postgres: one approval, then automatic in-bounds sends.

Fake writers and mailbox; real strategy, approvals, guard, executor and mandate checks.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.agents.catalog.contracts import (
    EmailNoteOutput,
    NegotiationEmailInput,
)
from bido_back.agents.context import ToolContext
from bido_back.agents.tools import AnyTool
from bido_back.approvals import (
    APPROVAL_EXECUTE_JOB,
    ApprovalExecutor,
    ApprovalRecipientsUnconfirmed,
    decide_approval,
)
from bido_back.copilot.journey_tools import ProposeMandateOut
from bido_back.copilot.tools import CopilotToolDeps, ProposeCounteroffersOut, copilot_tools
from bido_back.db import tenant_session
from bido_back.db.models import (
    Approval,
    AuditEvent,
    Invite,
    MailboxConnection,
    MandateSend,
    NegotiationMandate,
    NegotiationMessage,
    Rfq,
    RfqLine,
)
from bido_back.mandates import service as mandates
from bido_back.mandates.schemas import ApproveMandateArgs, ProposeMandateRequest
from bido_back.mandates.service import (
    APPROVE_NEGOTIATION_MANDATE,
    MandateActDeps,
    register_mandate_act_tools,
)
from bido_back.negotiation import service as negotiation_service
from bido_back.negotiation.jobs import NegotiationReplanHandler
from bido_back.negotiation.schemas import LineLimitsUpdate, NegotiationSettingsUpdate
from bido_back.negotiation.send import SEND_NEGOTIATION_EMAIL
from bido_back.rfqs import service as rfq_service
from bido_back.rfqs import suppliers
from bido_back.rfqs.activity import rfq_activity
from bido_back.rfqs.errors import InvalidState, ReplyByRequired
from bido_back.rfqs.overview import list_rfq_overviews
from bido_back.rfqs.schemas import ContactIn, CreateSupplierRequest
from bido_back.rfqs.send import REJECT_HOOKS as RFQ_REJECT_HOOKS
from bido_back.rfqs.send import SEND_RFQ_EMAIL
from bido_back.worker.envelope import Envelope
from bido_back.worker.job_types import NEGOTIATION_REPLAN
from tests.approvals.conftest import People
from tests.negotiation.test_negotiation_db import FakeNegotiationWriter, World, approver, buyer
from tests.rfqs.conftest import FakeIntake, FakeWriter

pytestmark = pytest.mark.db

DEADLINE = datetime.now(UTC) + timedelta(days=7)


class CommittingWriter(FakeNegotiationWriter):
    """A writer whose note commits the buyer: it must never go out."""

    async def __call__(self, task: NegotiationEmailInput, context: ToolContext) -> EmailNoteOutput:
        await super().__call__(task, context)
        return EmailNoteOutput(note="We accept your offer and will place the order.")


class MandateWorld(World):
    """Like World, but the RFQ emails go out under a mandate, not one by one."""

    def __init__(self, engine: AsyncEngine, people: People) -> None:
        super().__init__(engine, people)
        register_mandate_act_tools(self.registry, MandateActDeps(db=engine, jobs=self.jobs))
        self.executor = ApprovalExecutor(engine, self.registry)
        self.other_jobs: list[Envelope] = []

    async def setup(  # noqa: ARG002
        self,
        *,
        silent: bool = False,
        lyon_email: str = "sales@lyon.example.test",
        connected: bool = True,
    ) -> "MandateWorld":
        principal = buyer(self.people)
        if connected:
            await self.connect()
        rfq = await rfq_service.create_rfq_from_text(
            self.engine, principal, "Need MLCC and connectors", FakeIntake(), currency="EUR"
        )
        self.rfq_id = rfq.id
        for name, language, country in (("rhein", "de", "DE"), ("lyon", "fr", "FR")):
            supplier = await suppliers.create_supplier(
                self.engine,
                principal,
                CreateSupplierRequest(
                    name=name.title(),
                    language=language,
                    country=country,
                    type="authorized_distributor",
                    contacts=[
                        ContactIn(
                            name=name,
                            email=lyon_email if name == "lyon" else f"sales@{name}.example.test",
                        )
                    ],
                ),
            )
            invite = await rfq_service.add_invite(
                self.engine, principal, rfq.id, supplier.id, supplier.contacts[0].id
            )
            self.invites[name] = invite.id
        async with tenant_session(self.engine, self.people.tenant) as session:
            self.lines = list(
                await session.scalars(
                    select(RfqLine.id).where(RfqLine.rfq_id == rfq.id).order_by(RfqLine.position)
                )
            )
            # The limits the purchase analysis would have written.
            for line_id, target, maximum in zip(
                self.lines, ("0.040", "1.00"), ("0.060", "1.50"), strict=True
            ):
                await session.execute(
                    update(RfqLine)
                    .where(RfqLine.id == line_id)
                    .values(
                        target_unit_price=Decimal(target),
                        max_unit_price=Decimal(maximum),
                        max_lead_time_days=42,
                    )
                )
        return self

    async def connect(self) -> None:
        """The buyer's mailbox connection (the fake sender stands in for its API)."""
        async with tenant_session(self.engine, self.people.tenant) as session:
            session.add(
                MailboxConnection(
                    tenant_id=self.people.tenant,
                    user_id=self.people.owner,
                    provider="google",
                    email_address="buyer@acme.example.test",
                    token_ciphertext=b"not-real-ciphertext",
                    status="active",
                )
            )

    async def propose(self, **overrides: object) -> UUID:
        fields: dict[str, object] = {"deadline": DEADLINE}
        fields.update(overrides)
        proposed = await mandates.propose_mandate(
            self.engine,
            buyer(self.people),
            self.rfq_id,
            ProposeMandateRequest.model_validate(fields),
            writer=FakeWriter(),
            mailboxes=self.mailboxes,
        )
        return proposed.approval_id

    async def run_jobs(self) -> None:
        while self.jobs.jobs:
            job = self.jobs.jobs.pop(0)
            if job.type != APPROVAL_EXECUTE_JOB:
                # A negotiation re-plan: the tests sweep themselves.
                self.other_jobs.append(job)
                continue
            await self.executor(job)

    async def start(self, approval_id: UUID | None = None, **overrides: object) -> UUID:
        approval_id = approval_id or await self.propose(**overrides)
        self.jobs.jobs.clear()
        await decide_approval(
            self.engine, approver(self.people), approval_id, "approve", jobs=self.jobs
        )
        await self.run_jobs()
        return approval_id

    async def auto(self) -> mandates.AutoApproveResult:
        result = await mandates.auto_approve_pending(
            self.engine, self.people.tenant, jobs=self.jobs
        )
        await self.run_jobs()
        return result

    async def sends(self) -> list[MandateSend]:
        async with tenant_session(self.engine, self.people.tenant) as session:
            return list(await session.scalars(select(MandateSend).order_by(MandateSend.created_at)))

    async def sends_for(self, tool: str) -> list[MandateSend]:
        return [s for s in await self.sends() if s.tool == tool]


@pytest.fixture
async def world(app_engine: AsyncEngine, rfq_rows: People) -> MandateWorld:
    return await MandateWorld(app_engine, rfq_rows).setup()


async def test_one_approval_sends_the_rfq_and_opens_the_negotiation(world: MandateWorld) -> None:
    approval_id = await world.propose()
    # Proposing sends nothing: one pending mandate plus the initial emails it names.
    assert world.mailboxes.sender.sent == []
    mandate_approval = await world.approval(approval_id)
    assert mandate_approval.tool == APPROVE_NEGOTIATION_MANDATE
    assert mandate_approval.status == "pending"
    terms = ApproveMandateArgs.model_validate(mandate_approval.args)
    assert {s.supplier_name for s in terms.suppliers} == {"Rhein", "Lyon"}
    assert len(terms.initial_emails) == 2
    assert [line.max_unit_price for line in terms.lines] == [Decimal("0.06"), Decimal("1.5")]

    await world.start(approval_id)
    # The approver approved ONE thing; both RFQ emails went out.
    assert len(world.mailboxes.sender.sent) == 2
    async with tenant_session(world.engine, world.people.tenant) as session:
        initial = list(
            await session.scalars(select(Approval).where(Approval.tool == SEND_RFQ_EMAIL))
        )
        mandate = await session.scalar(select(NegotiationMandate))
    assert {a.status for a in initial} == {"executed"}
    assert {a.decided_by for a in initial} == {world.people.approver}
    assert mandate is not None and mandate.status == "active"
    rfq = await world.rfq()
    assert rfq.negotiation_status == "open" and rfq.negotiation_deadline == DEADLINE
    assert {s.outcome for s in await world.sends()} == {"auto_approved"}


async def test_counteroffers_within_bounds_go_out_without_another_approval(
    world: MandateWorld,
) -> None:
    await world.start()
    await world.offer("rhein", ("0.050", "1.20"))
    await world.offer("lyon", ("0.045", "1.10"))
    writer = FakeNegotiationWriter()
    assert (await world.sweep(writer)).proposed == 2
    sent_before = len(world.mailboxes.sender.sent)

    result = await world.auto()

    assert len(result.approved) == 2
    assert len(world.mailboxes.sender.sent) == sent_before + 2
    rhein = (await world.messages("rhein"))[0]
    assert rhein.status == "sent"
    approval = await world.approval(rhein.approval_id)
    assert approval.tool == SEND_NEGOTIATION_EMAIL and approval.status == "executed"
    # The writer never saw the buyer's maximum or another supplier's name.
    for task in writer.calls:
        dumped = task.model_dump_json()
        assert "0.06" not in dumped and "1.5" not in dumped
        assert "Lyon" not in dumped or task.supplier_name == "Lyon"
        assert "Rhein" not in dumped or task.supplier_name == "Rhein"
    assert "max" not in " ".join(NegotiationEmailInput.model_fields)


async def test_a_committing_note_never_reaches_the_supplier(world: MandateWorld) -> None:
    await world.start()
    await world.offer("rhein", ("0.050", "1.20"))
    await world.sweep(CommittingWriter())

    result = await world.auto()

    # The note was left out before the proposal: the email, rendered by code, is in bounds.
    assert len(result.approved) == 1
    sent = world.mailboxes.sender.sent[-1]
    assert "accept" not in sent.text_body.casefold()
    assert "place the order" not in sent.text_body


async def test_an_email_outside_the_mandate_is_held_for_a_person(world: MandateWorld) -> None:
    # The buyer's own title reads as a commitment: the subject of every reply carries it, and
    # the mandate's check (subject and body) holds the email for a person.
    async with tenant_session(world.engine, world.people.tenant) as session:
        await session.execute(
            update(Rfq).where(Rfq.id == world.rfq_id).values(title="Contract for MLCC")
        )
    await world.start()
    await world.offer("rhein", ("0.050", "1.20"))
    await world.sweep(FakeNegotiationWriter())
    sent_before = len(world.mailboxes.sender.sent)

    result = await world.auto()

    assert result.approved == [] and result.held == 1
    assert len(world.mailboxes.sender.sent) == sent_before
    [held] = [s for s in await world.sends() if s.outcome == "held"]
    assert held.reason == "commitment_language"
    approval = await world.approval(held.approval_id)
    assert approval.status == "pending"  # a person can still approve or reject it
    activity = await mandates.get_activity(world.engine, buyer(world.people), world.rfq_id)
    assert any(i.outcome == "held" and i.supplier_name == "Rhein" for i in activity.items)
    # Held once: the next pass does not record it again.
    assert (await world.auto()).held == 0


async def _audit(world: MandateWorld, action: str) -> list[AuditEvent]:
    async with tenant_session(world.engine, world.people.tenant) as session:
        return list(
            await session.scalars(
                select(AuditEvent)
                .where(AuditEvent.action == action)
                .order_by(AuditEvent.created_at)
            )
        )


async def test_the_daily_cap_defers_without_holding_and_says_until_when(
    world: MandateWorld,
) -> None:
    await world.start(max_messages_per_supplier_per_day=1, max_rounds=3)
    await world.offer("rhein", ("0.050", "1.20"))
    await world.sweep(FakeNegotiationWriter())
    # The RFQ email does not count: today's one automatic email is this counter.
    first = await world.auto()
    assert len(first.approved) == 1
    [sent] = await world.sends_for(SEND_NEGOTIATION_EMAIL)

    # The supplier moves at once: the next email waits for the cap; it is not held.
    await world.offer("rhein", ("0.045", "1.10"))
    await world.sweep(FakeNegotiationWriter())
    result = await world.auto()
    assert result.approved == [] and result.held == 0 and result.waiting == 1
    rows = await world.messages("rhein")
    assert [(r.round, r.status) for r in rows] == [(1, "sent"), (2, "proposed")]
    # Recorded once, with when it can go.
    [waiting] = await _audit(world, "mandate.waiting")
    until = sent.created_at + timedelta(hours=24)
    assert waiting.payload["reason"] == "daily_cap"
    assert waiting.payload["until"] == until.isoformat()
    assert waiting.payload["invite_id"] == str(world.invites["rhein"])
    assert (await world.auto()).waiting == 0
    activity = await mandates.get_activity(world.engine, buyer(world.people), world.rfq_id)
    [wait] = activity.waits
    assert (wait.reason, wait.supplier_name, wait.until) == ("daily_cap", "Rhein", until)
    view = await negotiation_service.get_negotiation(
        world.engine, buyer(world.people), world.rfq_id
    )
    rhein = next(i for i in view.invites if i.supplier_name == "Rhein")
    assert rhein.state is not None
    assert (rhein.state.phase, rhein.state.reason, rhein.state.until) == (
        "waiting",
        "daily_limit",
        until,
    )
    # The feed says so, and the RFQ list does not ask the buyer to approve it.
    feed = await rfq_activity(world.engine, buyer(world.people), world.rfq_id)
    assert any(
        i.action == "mandate.waiting" and i.supplier_name == "Rhein" and "daily" in i.summary
        for i in feed
    )
    page = await list_rfq_overviews(world.engine, buyer(world.people), today=date.today())
    [row] = [r for r in page.items if r.id == world.rfq_id]
    assert row.next_step != "approve"


async def test_a_canceled_email_uses_no_round_so_the_last_round_still_goes_out(
    world: MandateWorld,
) -> None:
    await world.start(max_messages_per_supplier_per_day=1)
    rfq = await world.rfq()
    assert rfq.negotiation_max_rounds == 2
    writer = FakeNegotiationWriter()
    await world.offer("rhein", ("0.050", "1.20"))
    await world.sweep(writer)
    await world.auto()
    # The supplier moves: round 2 is the best-and-final request; it waits for the cap.
    await world.offer("rhein", ("0.045", "1.10"))
    await world.sweep(writer)
    assert (await world.auto()).waiting == 1
    # It moves again before the request can go: the waiting one is replaced, same round.
    await world.offer("rhein", ("0.044", "1.08"))
    await world.sweep(writer)
    later = datetime.now(UTC) + timedelta(hours=25)
    result = await mandates.auto_approve_pending(
        world.engine, world.people.tenant, jobs=world.jobs, now=later
    )
    await world.run_jobs()
    assert result.held == 0 and len(result.approved) == 1
    rows = await world.messages("rhein")
    assert [(r.sequence, r.round, r.action, r.status) for r in rows] == [
        (1, 1, "counter", "sent"),
        (2, 2, "bafo", "canceled"),
        (3, 2, "bafo", "sent"),
    ]
    # Both best-and-final requests carried the RFQ's one cut-off.
    cutoffs = {r.terms["decision"]["cutoff_at"] for r in rows if r.action == "bafo"}
    assert len(cutoffs) == 1
    assert not [s for s in await world.sends() if s.outcome == "held"]


async def test_the_mandate_never_approves_a_second_best_and_final_request(
    world: MandateWorld,
) -> None:
    await world.start()
    writer = FakeNegotiationWriter()
    await world.offer("rhein", ("0.050", "1.20"))
    await world.sweep(writer)
    await world.auto()
    async with tenant_session(world.engine, world.people.tenant) as session:
        # The sent email was a best-and-final request as far as the mandate can read
        # (an older Bido wrote no typed decision): the guard reads what was sent.
        await session.execute(
            update(NegotiationMessage)
            .where(NegotiationMessage.invite_id == world.invites["rhein"])
            .values(action="bafo")
        )
    await world.offer("rhein", ("0.045", "1.10"))
    await world.sweep(writer)
    [_, last] = await world.messages("rhein")
    assert (last.action, last.round, last.status) == ("bafo", 2, "proposed")
    result = await world.auto()
    assert result.approved == [] and result.held == 1
    [held] = [s for s in await world.sends() if s.outcome == "held"]
    assert held.reason == "bafo_already_sent"


async def test_the_activity_feed_of_a_mandated_negotiation_reads_every_decision(
    world: MandateWorld,
) -> None:
    await world.start()
    await world.offer("rhein", ("0.050", "1.20"))
    await world.offer("lyon", ("0.039", "0.99"))
    await world.sweep(FakeNegotiationWriter())
    await world.auto()
    feed = await rfq_activity(world.engine, buyer(world.people), world.rfq_id)
    summaries = [i.summary for i in feed]
    assert "Negotiation mandate approved for 2 suppliers" in summaries
    accepted = next(i for i in feed if i.action == "rfq.negotiation_accepted")
    assert accepted.supplier_name == "Lyon" and accepted.actor == "Bido"
    assert "nothing to ask" in accepted.summary
    decided = next(i for i in feed if i.action == "rfq.negotiation_decided")
    assert decided.supplier_name == "Rhein" and "revised offer" in decided.summary
    sent = next(i for i in feed if i.action == "rfq.negotiation_sent")
    assert sent.supplier_name == "Rhein" and "round 1" in sent.summary
    # The same sweep again records nothing new.
    await world.sweep(FakeNegotiationWriter())
    again = await rfq_activity(world.engine, buyer(world.people), world.rfq_id)
    assert len(again) == len(feed)


async def test_a_held_email_names_its_supplier_and_reason_in_the_feed(
    world: MandateWorld,
) -> None:
    # The buyer's own title reads as a commitment: the mandate holds the email.
    async with tenant_session(world.engine, world.people.tenant) as session:
        await session.execute(
            update(Rfq).where(Rfq.id == world.rfq_id).values(title="Contract for MLCC")
        )
    await world.start()
    await world.offer("rhein", ("0.050", "1.20"))
    await world.sweep(FakeNegotiationWriter())
    await world.auto()
    feed = await rfq_activity(world.engine, buyer(world.people), world.rfq_id)
    held = next(i for i in feed if i.action == "mandate.held")
    assert held.supplier_name == "Rhein"
    assert "commitment language" in held.summary
    view = await negotiation_service.get_negotiation(
        world.engine, buyer(world.people), world.rfq_id
    )
    rhein = next(i for i in view.invites if i.supplier_name == "Rhein")
    assert rhein.state is not None and rhein.state.phase == "held_for_approval"
    assert rhein.state.reason == "held_by_mandate:commitment_language"
    page = await list_rfq_overviews(world.engine, buyer(world.people), today=date.today())
    [row] = [r for r in page.items if r.id == world.rfq_id]
    assert row.next_step == "approve"


async def test_pause_stops_automatic_sends_and_resume_restarts_them(
    world: MandateWorld,
) -> None:
    await world.start()
    await world.offer("rhein", ("0.050", "1.20"))
    await world.sweep(FakeNegotiationWriter())

    paused = await mandates.set_paused(world.engine, buyer(world.people), world.rfq_id, paused=True)
    assert paused.status == "paused"
    assert (await world.auto()).approved == []

    await mandates.set_paused(world.engine, buyer(world.people), world.rfq_id, paused=False)
    assert len((await world.auto()).approved) == 1


async def test_limits_cannot_change_under_a_live_mandate(world: MandateWorld) -> None:
    await world.start()
    with pytest.raises(InvalidState):
        await negotiation_service.update_negotiation(
            world.engine,
            buyer(world.people),
            world.rfq_id,
            NegotiationSettingsUpdate(
                lines=[
                    LineLimitsUpdate(
                        rfq_line_id=world.lines[0],
                        target_unit_price=Decimal("0.01"),
                        max_unit_price=Decimal("9"),
                    )
                ]
            ),
        )


async def test_closing_the_negotiation_ends_the_mandate(world: MandateWorld) -> None:
    await world.start()
    await negotiation_service.close(world.engine, buyer(world.people), world.rfq_id)
    async with tenant_session(world.engine, world.people.tenant) as session:
        mandate = await session.scalar(select(NegotiationMandate))
    assert mandate is not None and mandate.status == "ended" and mandate.ended_at is not None


async def test_a_mandate_needs_a_target_and_a_maximum_on_every_line(
    world: MandateWorld,
) -> None:
    async with tenant_session(world.engine, world.people.tenant) as session:
        await session.execute(
            update(RfqLine).where(RfqLine.id == world.lines[1]).values(max_unit_price=None)
        )
    with pytest.raises(InvalidState, match="line"):
        await world.propose()
    assert world.mailboxes.sender.sent == []


@pytest.mark.parametrize("due", [None, datetime.now(UTC).date() - timedelta(days=2)])
async def test_a_mandate_needs_a_reply_by_date_for_its_initial_emails(
    world: MandateWorld, due: date | None
) -> None:
    async with tenant_session(world.engine, world.people.tenant) as session:
        await session.execute(update(Rfq).where(Rfq.id == world.rfq_id).values(response_due=due))

    with pytest.raises(ReplyByRequired, match="reply-by date"):
        await world.propose()

    # No mandate, no initial email approvals, invites untouched, nothing sent.
    async with tenant_session(world.engine, world.people.tenant) as session:
        approvals = list(await session.scalars(select(Approval)))
        statuses = set(
            await session.scalars(select(Invite.status).where(Invite.rfq_id == world.rfq_id))
        )
    assert approvals == [] and statuses == {"draft"}
    assert world.mailboxes.sender.sent == []

    async with tenant_session(world.engine, world.people.tenant) as session:
        await session.execute(
            update(Rfq)
            .where(Rfq.id == world.rfq_id)
            .values(response_due=datetime.now(UTC).date() + timedelta(days=14))
        )
    approval_id = await world.propose()
    assert (await world.approval(approval_id)).tool == APPROVE_NEGOTIATION_MANDATE


async def test_a_rejected_mandate_releases_its_emails(world: MandateWorld) -> None:
    approval_id = await world.propose()
    await decide_approval(
        world.engine,
        approver(world.people),
        approval_id,
        "reject",
        jobs=world.jobs,
        on_reject=mandates.MANDATE_REJECT_HOOKS,
    )
    async with tenant_session(world.engine, world.people.tenant) as session:
        initial = list(
            await session.scalars(select(Approval).where(Approval.tool == SEND_RFQ_EMAIL))
        )
    assert {a.status for a in initial} == {"expired"}
    assert world.mailboxes.sender.sent == []
    # Proposed again from scratch.
    assert await world.propose() != approval_id


async def test_the_buyer_can_approve_their_own_mandate(world: MandateWorld) -> None:
    approval_id = await world.propose()
    world.jobs.jobs.clear()
    await decide_approval(
        world.engine, buyer(world.people), approval_id, "approve", jobs=world.jobs
    )
    await world.run_jobs()

    assert len(world.mailboxes.sender.sent) == 2
    async with tenant_session(world.engine, world.people.tenant) as session:
        initial = list(
            await session.scalars(select(Approval).where(Approval.tool == SEND_RFQ_EMAIL))
        )
    assert {(a.status, a.self_approved) for a in initial} == {("executed", True)}


class RealDomains:
    async def has_mx(self, domain: str) -> bool | None:
        return True


async def test_a_real_company_domain_needs_confirmation_before_the_mandate_starts(
    app_engine: AsyncEngine, rfq_rows: People
) -> None:
    world = await MandateWorld(app_engine, rfq_rows).setup(lyon_email="vendas@lyon-composants.fr")
    proposed = await mandates.propose_mandate(
        world.engine,
        buyer(world.people),
        world.rfq_id,
        ProposeMandateRequest(deadline=DEADLINE),
        writer=FakeWriter(),
        mailboxes=world.mailboxes,
        resolver=RealDomains(),
    )
    by_address = {r.address: (r.status, r.will_send) for r in proposed.recipients}
    assert by_address == {
        "sales@rhein.example.test": ("test", True),
        "vendas@lyon-composants.fr": ("real_domain", False),
    }

    # The queue shows what the mandate covers and who would get email.
    queue = await rfq_service.list_approvals(
        world.engine, buyer(world.people), "pending", resolver=RealDomains()
    )
    [item] = [i for i in queue if i.id == proposed.approval_id]
    assert item.mandate is not None
    assert item.mandate.rfq_title
    assert {s.supplier_name for s in item.mandate.suppliers} == {"Rhein", "Lyon"}
    assert [line.max_unit_price for line in item.mandate.lines] == [
        Decimal("0.06"),
        Decimal("1.5"),
    ]
    assert item.recipient_checks is not None
    assert {c.domain for c in item.recipient_checks if not c.will_send} == {"lyon-composants.fr"}

    with pytest.raises(ApprovalRecipientsUnconfirmed) as raised:
        await decide_approval(
            world.engine, approver(world.people), proposed.approval_id, "approve", jobs=world.jobs
        )
    assert raised.value.domains == ["lyon-composants.fr"]
    assert world.mailboxes.sender.sent == []

    await decide_approval(
        world.engine,
        approver(world.people),
        proposed.approval_id,
        "approve",
        jobs=world.jobs,
        confirmed_domains=["lyon-composants.fr"],
    )
    await world.run_jobs()
    assert len(world.mailboxes.sender.sent) == 2


async def test_a_mandate_can_be_approved_before_the_mailbox_is_connected(
    app_engine: AsyncEngine, rfq_rows: People
) -> None:
    world = await MandateWorld(app_engine, rfq_rows).setup(connected=False)
    proposed = await mandates.propose_mandate(
        world.engine,
        buyer(world.people),
        world.rfq_id,
        ProposeMandateRequest(deadline=DEADLINE),
        writer=FakeWriter(),
        mailboxes=world.mailboxes,
    )
    assert proposed.mailbox_connected is False
    assert proposed.initial_emails == 2

    await world.start(proposed.approval_id)
    # Approved, but nothing went out: the RFQ emails wait for the mailbox.
    assert world.mailboxes.sender.sent == []
    async with tenant_session(world.engine, world.people.tenant) as session:
        mandate = await session.scalar(select(NegotiationMandate))
        initial = list(
            await session.scalars(select(Approval).where(Approval.tool == SEND_RFQ_EMAIL))
        )
    assert mandate is not None and mandate.status == "active"
    assert {a.status for a in initial} == {"pending"}
    assert await world.sends() == []
    activity = await mandates.get_activity(world.engine, buyer(world.people), world.rfq_id)
    assert (activity.waiting_reason, activity.waiting_emails) == ("mailbox_not_connected", 2)

    # Sweeps before the connection keep waiting (not held for a person).
    result = await world.auto()
    assert (result.approved, result.held) == ([], 0)
    assert world.mailboxes.sender.sent == []

    await world.connect()
    result = await world.auto()
    assert len(result.approved) == 2
    assert len(world.mailboxes.sender.sent) == 2
    activity = await mandates.get_activity(world.engine, buyer(world.people), world.rfq_id)
    assert (activity.waiting_reason, activity.waiting_emails) == (None, 0)

    # Released once: later sweeps send nothing more.
    await world.auto()
    await world.auto()
    assert len(world.mailboxes.sender.sent) == 2
    assert {s.outcome for s in await world.sends()} == {"auto_approved"}


async def test_the_recipient_guard_still_applies_while_waiting_for_the_mailbox(
    app_engine: AsyncEngine, rfq_rows: People
) -> None:
    world = await MandateWorld(app_engine, rfq_rows).setup(
        lyon_email="vendas@lyon-composants.fr", connected=False
    )
    proposed = await mandates.propose_mandate(
        world.engine,
        buyer(world.people),
        world.rfq_id,
        ProposeMandateRequest(deadline=DEADLINE),
        writer=FakeWriter(),
        mailboxes=world.mailboxes,
        resolver=RealDomains(),
    )
    with pytest.raises(ApprovalRecipientsUnconfirmed):
        await decide_approval(
            world.engine, approver(world.people), proposed.approval_id, "approve", jobs=world.jobs
        )
    await decide_approval(
        world.engine,
        approver(world.people),
        proposed.approval_id,
        "approve",
        jobs=world.jobs,
        confirmed_domains=["lyon-composants.fr"],
    )
    await world.run_jobs()
    assert world.mailboxes.sender.sent == []

    await world.connect()
    await world.auto()
    assert sorted(m.to[0] for m in world.mailboxes.sender.sent) == [
        "sales@rhein.example.test",
        "vendas@lyon-composants.fr",
    ]


def test_a_need_by_date_bounds_the_lead_time() -> None:
    today = date(2026, 10, 8)
    assert mandates.lead_time_until(date(2026, 10, 19), today) == 11
    assert mandates.lead_time_until(date(2026, 10, 9), today) == 1
    # Today or past: no lead time meets it, so no limit (never "0 days").
    assert mandates.lead_time_until(today, today) is None
    assert mandates.lead_time_until(date(2026, 10, 1), today) is None
    assert mandates.lead_time_until(None, today) is None


async def test_a_line_with_a_need_by_date_gets_a_lead_time_limit_the_buyer_approves(
    world: MandateWorld,
) -> None:
    now = datetime.now(UTC)
    async with tenant_session(world.engine, world.people.tenant) as session:
        await session.execute(
            update(RfqLine)
            .where(RfqLine.id == world.lines[0])
            .values(max_lead_time_days=None, need_by=now.date() + timedelta(days=12))
        )
        await session.execute(
            update(RfqLine)
            .where(RfqLine.id == world.lines[1])
            .values(max_lead_time_days=None, need_by=None)
        )
    proposed = await mandates.propose_mandate(
        world.engine,
        buyer(world.people),
        world.rfq_id,
        ProposeMandateRequest(deadline=DEADLINE),
        writer=FakeWriter(),
        mailboxes=world.mailboxes,
        now=now,
    )
    terms = ApproveMandateArgs.model_validate((await world.approval(proposed.approval_id)).args)
    assert [(line.max_lead_time_days, line.lead_time_need_by) for line in terms.lines] == [
        (12, now.date() + timedelta(days=12)),
        (None, None),
    ]

    await world.start(proposed.approval_id)
    async with tenant_session(world.engine, world.people.tenant) as session:
        limits = list(
            await session.scalars(
                select(RfqLine.max_lead_time_days)
                .where(RfqLine.rfq_id == world.rfq_id)
                .order_by(RfqLine.position)
            )
        )
    # Approving applies it like any other limit, so later sends see the limits unchanged.
    assert limits == [12, None]
    assert {s.outcome for s in await world.sends()} == {"auto_approved"}


async def test_each_line_gets_the_lead_time_of_its_own_need_by(world: MandateWorld) -> None:
    """Regression (2026-10-10): an RFQ whose lines all said 12/10 got "≤ 2 d" on every line
    and no real offer could be awarded. Lines with their own need-by dates get their own
    limits, and a buyer's edit of one date moves only that line's limit."""
    now = datetime.now(UTC)
    soon, later = now.date() + timedelta(days=16), now.date() + timedelta(days=58)
    async with tenant_session(world.engine, world.people.tenant) as session:
        for line_id, need_by in zip(world.lines, (soon, later), strict=True):
            await session.execute(
                update(RfqLine)
                .where(RfqLine.id == line_id)
                .values(max_lead_time_days=None, need_by=need_by)
            )
    proposed = await mandates.propose_mandate(
        world.engine,
        buyer(world.people),
        world.rfq_id,
        ProposeMandateRequest(deadline=DEADLINE),
        writer=FakeWriter(),
        mailboxes=world.mailboxes,
        now=now,
    )
    terms = ApproveMandateArgs.model_validate((await world.approval(proposed.approval_id)).args)
    assert [(line.max_lead_time_days, line.lead_time_need_by) for line in terms.lines] == [
        (16, soon),
        (58, later),
    ]


async def test_a_buyers_lead_time_limit_is_never_replaced_by_the_need_by_date(
    world: MandateWorld,
) -> None:
    async with tenant_session(world.engine, world.people.tenant) as session:
        await session.execute(
            update(RfqLine)
            .where(RfqLine.rfq_id == world.rfq_id)
            .values(need_by=datetime.now(UTC).date() + timedelta(days=12))
        )
    no_limit = {
        "rfq_line_id": world.lines[1],
        "target_unit_price": "1.00",
        "max_unit_price": "1.50",
        "max_lead_time_days": None,
    }
    approval_id = await world.propose(lines=[no_limit])
    terms = ApproveMandateArgs.model_validate((await world.approval(approval_id)).args)
    # The RFQ's own limit (42 days) stays; an explicit "no limit" override stays empty.
    assert [line.max_lead_time_days for line in terms.lines] == [42, None]
    assert {line.lead_time_need_by for line in terms.lines} == {None}


# --- Replacing a live mandate (new limits) ---------------------------------------------------


async def _propose_replacement(world: MandateWorld) -> mandates.ProposedMandate:
    return await mandates.propose_mandate(
        world.engine,
        buyer(world.people),
        world.rfq_id,
        ProposeMandateRequest(
            deadline=DEADLINE,
            lines=[
                LineLimitsUpdate(
                    rfq_line_id=world.lines[0],
                    target_unit_price=Decimal("0.045"),
                    max_unit_price=Decimal("0.070"),
                    max_lead_time_days=42,
                )
            ],
        ),
        writer=FakeWriter(),
        mailboxes=world.mailboxes,
    )


async def _mandates(world: MandateWorld) -> list[NegotiationMandate]:
    async with tenant_session(world.engine, world.people.tenant) as session:
        return list(
            await session.scalars(
                select(NegotiationMandate).order_by(NegotiationMandate.created_at)
            )
        )


async def test_an_approved_replacement_ends_the_old_mandate_and_withdraws_stale_emails(
    world: MandateWorld,
) -> None:
    await world.start()
    [old] = await _mandates(world)
    await world.offer("rhein", ("0.050", "1.20"))
    await world.offer("lyon", ("0.045", "1.10"))
    assert (await world.sweep(FakeNegotiationWriter())).proposed == 2
    # Both counters auto-approved under the old limits, their sends still queued.
    result = await mandates.auto_approve_pending(world.engine, world.people.tenant, jobs=world.jobs)
    assert len(result.approved) == 2
    queued = list(world.jobs.jobs)
    sent_before = len(world.mailboxes.sender.sent)

    proposed = await _propose_replacement(world)

    assert proposed.replaces_mandate_id == old.id
    approval = await world.approval(proposed.approval_id)
    assert approval.args["replaces_mandate_id"] == str(old.id)
    # Proposing changes nothing: the old mandate runs, the old limits apply.
    assert [m.status for m in await _mandates(world)] == ["active"]
    async with tenant_session(world.engine, world.people.tenant) as session:
        line = await session.get(RfqLine, world.lines[0])
    assert line is not None and line.max_unit_price == Decimal("0.060")

    await world.start(proposed.approval_id)

    first, second = await _mandates(world)
    assert (first.id, first.status) == (old.id, "ended") and first.ended_at is not None
    assert second.status == "active" and second.approval_id == proposed.approval_id
    assert ApproveMandateArgs.model_validate(second.terms).replaces_mandate_id == old.id
    async with tenant_session(world.engine, world.people.tenant) as session:
        line = await session.get(RfqLine, world.lines[0])
    assert line is not None and line.max_unit_price == Decimal("0.070")
    [event] = await _audit(world, "mandate.replaced")
    assert event.payload["mandate_id"] == str(old.id)
    assert event.payload["replaced_by"] == str(second.id)
    assert event.payload["negotiation_emails_withdrawn"] == 2
    # Decided under the old limits: withdrawn; their queued sends go out as nothing.
    for name in ("rhein", "lyon"):
        [row] = await world.messages(name)
        assert (row.status, row.canceled_reason) == ("canceled", "mandate_replaced")
    world.jobs.jobs.extend(queued)
    await world.run_jobs()
    assert len(world.mailboxes.sender.sent) == sent_before
    # The new limits are planned at once.
    assert [j.type for j in world.other_jobs] == [NEGOTIATION_REPLAN]
    activity = await mandates.get_activity(world.engine, buyer(world.people), world.rfq_id)
    assert activity.mandate is not None and activity.mandate.replaces_mandate_id == old.id

    # Decided again with the new limits, and sent under the new mandate.
    assert (await world.sweep(FakeNegotiationWriter())).proposed == 2
    result = await world.auto()
    assert len(result.approved) == 2
    assert len(world.mailboxes.sender.sent) == sent_before + 2
    async with tenant_session(world.engine, world.people.tenant) as session:
        decided_by = set(
            await session.scalars(
                select(MandateSend.mandate_id).where(MandateSend.approval_id.in_(result.approved))
            )
        )
    assert decided_by == {second.id}


async def test_a_rejected_replacement_leaves_the_current_mandate_untouched(
    app_engine: AsyncEngine, rfq_rows: People
) -> None:
    # The initial emails still wait for the mailbox under the current mandate.
    world = await MandateWorld(app_engine, rfq_rows).setup(connected=False)
    await world.start()
    [current] = await _mandates(world)
    proposed = await _propose_replacement(world)
    replacement = ApproveMandateArgs.model_validate(
        (await world.approval(proposed.approval_id)).args
    )
    # It carries the initial emails the current mandate waits to send, unchanged.
    assert {e.approval_id for e in replacement.initial_emails} == {
        e.approval_id for e in ApproveMandateArgs.model_validate(current.terms).initial_emails
    }

    await decide_approval(
        world.engine,
        approver(world.people),
        proposed.approval_id,
        "reject",
        jobs=world.jobs,
        on_reject=mandates.MANDATE_REJECT_HOOKS,
    )

    [still] = await _mandates(world)
    assert (still.id, still.status, still.ended_at) == (current.id, "active", None)
    async with tenant_session(world.engine, world.people.tenant) as session:
        initial = list(
            await session.scalars(select(Approval).where(Approval.tool == SEND_RFQ_EMAIL))
        )
        line = await session.get(RfqLine, world.lines[0])
    assert {a.status for a in initial} == {"pending"}
    assert line is not None and line.max_unit_price == Decimal("0.060")
    assert await _audit(world, "mandate.replaced") == []
    # The current mandate still sends them once the mailbox is connected.
    await world.connect()
    assert len((await world.auto()).approved) == 2
    assert len(world.mailboxes.sender.sent) == 2


async def test_only_one_replacement_waits_at_a_time(world: MandateWorld) -> None:
    await world.start()
    proposed = await _propose_replacement(world)

    with pytest.raises(InvalidState, match="already waiting for approval"):
        await _propose_replacement(world)

    # Once that one is decided, another may be proposed.
    await decide_approval(
        world.engine,
        approver(world.people),
        proposed.approval_id,
        "reject",
        jobs=world.jobs,
        on_reject=mandates.MANDATE_REJECT_HOOKS,
    )
    assert (await _propose_replacement(world)).approval_id != proposed.approval_id


async def test_a_replacement_of_a_mandate_no_longer_live_starts_nothing(
    world: MandateWorld,
) -> None:
    await world.start()
    proposed = await _propose_replacement(world)
    await negotiation_service.close(world.engine, buyer(world.people), world.rfq_id)

    with pytest.raises(InvalidState, match="no longer live"):
        await world.start(proposed.approval_id)

    assert [m.status for m in await _mandates(world)] == ["ended"]


async def _send_without_a_mandate(world: MandateWorld, *, reject: str | None = None) -> None:
    """The RFQ emails approved one by one, before any mandate; `reject` stays unsent."""
    prepared = await rfq_service.prepare_send(
        world.engine, buyer(world.people), world.rfq_id, FakeWriter(), world.mailboxes
    )
    rejected = world.invites[reject] if reject is not None else None
    for item in prepared.approvals:
        if item.invite_id == rejected:
            await decide_approval(
                world.engine,
                approver(world.people),
                item.approval_id,
                "reject",
                jobs=world.jobs,
                on_reject=RFQ_REJECT_HOOKS,
            )
        else:
            await world.approve(item.approval_id)
    world.jobs.jobs.clear()


def _replan_handler(world: MandateWorld) -> NegotiationReplanHandler:
    return NegotiationReplanHandler(
        db=world.engine, writer=FakeNegotiationWriter(), jobs=world.jobs
    )


async def test_a_first_mandate_plans_an_offer_already_in_at_once(world: MandateWorld) -> None:
    # 2026-10-12: the quote arrived before the mandate (negotiation not started), and the
    # approved mandate sat idle until the 15-minute sweep: nothing decided, nothing sent.
    await _send_without_a_mandate(world)
    assert (await world.rfq()).status == "open"
    await world.offer("rhein", ("0.050", "1.20"))
    approval_id = await world.propose()
    assert (
        ApproveMandateArgs.model_validate((await world.approval(approval_id)).args).initial_emails
        == []
    )
    sent_before = len(world.mailboxes.sender.sent)

    await world.start(approval_id)

    # Exactly one re-plan, enqueued after the commit that opened the negotiation.
    [job] = world.other_jobs
    assert job.type == NEGOTIATION_REPLAN and job.payload == {"rfq_id": str(world.rfq_id)}
    rfq = await world.rfq()
    assert rfq.negotiation_status == "open"
    assert rfq.negotiation_replan_requested_at is not None
    assert job.idempotency_key.endswith(rfq.negotiation_replan_requested_at.isoformat())

    await _replan_handler(world)(job)
    await world.run_jobs()

    # Decided, approved under the mandate and sent, without waiting for a sweep.
    [counter] = await world.messages("rhein")
    assert (counter.action, counter.status) == ("counter", "sent")
    assert len(world.mailboxes.sender.sent) == sent_before + 1
    assert [s.outcome for s in await world.sends_for(SEND_NEGOTIATION_EMAIL)] == ["auto_approved"]
    assert await world.messages("lyon") == []


async def test_the_replan_leaves_a_supplier_whose_rfq_email_is_not_sent_yet(
    world: MandateWorld,
) -> None:
    await _send_without_a_mandate(world, reject="lyon")
    await world.offer("rhein", ("0.050", "1.20"))
    approval_id = await world.propose()
    terms = ApproveMandateArgs.model_validate((await world.approval(approval_id)).args)
    assert [e.invite_id for e in terms.initial_emails] == [world.invites["lyon"]]
    await decide_approval(
        world.engine, approver(world.people), approval_id, "approve", jobs=world.jobs
    )
    [mandate_job] = world.jobs.jobs
    world.jobs.jobs.clear()
    await world.executor(mandate_job)
    # Lyon's RFQ email is queued for sending; the re-plan is queued after it.
    assert [j.type for j in world.jobs.jobs] == [APPROVAL_EXECUTE_JOB, NEGOTIATION_REPLAN]
    replan = world.jobs.jobs.pop()

    # The re-plan runs first: Rhein is countered, Lyon (RFQ email unsent) is left waiting.
    await _replan_handler(world)(replan)

    [counter] = await world.messages("rhein")
    assert counter.action == "counter" and counter.status in ("proposed", "approved")
    assert await world.messages("lyon") == []
    await world.run_jobs()
    assert (await world.messages("rhein"))[0].status == "sent"
    async with tenant_session(world.engine, world.people.tenant) as session:
        lyon = await session.get(Invite, world.invites["lyon"])
    assert lyon is not None and lyon.status == "sent"
    assert await world.messages("lyon") == []


async def test_the_queue_lists_the_mandate_not_the_emails_it_covers(world: MandateWorld) -> None:
    # 2026-10-10: the queue listed every supplier's "Send RFQ" next to the mandate, and
    # still for ~20 s after the mandate was approved (until its act ran).
    approval_id = await world.propose()

    pending = await rfq_service.list_approvals(world.engine, buyer(world.people), "pending")
    assert [a.id for a in pending] == [approval_id]

    world.jobs.jobs.clear()
    await decide_approval(
        world.engine, buyer(world.people), approval_id, "approve", jobs=world.jobs
    )
    # Approved, the act not run yet: the initial emails are still pending rows, but no
    # decision is left for the buyer.
    assert await rfq_service.list_approvals(world.engine, buyer(world.people), "pending") == []
    async with tenant_session(world.engine, world.people.tenant) as session:
        rows = list(await session.scalars(select(Approval).where(Approval.tool == SEND_RFQ_EMAIL)))
    assert {a.status for a in rows} == {"pending"}

    await world.run_jobs()
    assert await rfq_service.list_approvals(world.engine, buyer(world.people), "pending") == []
    assert len(world.mailboxes.sender.sent) == 2


# --- What the copilot tells the buyer -----------------------------------------------------
# 2026-10-12, RFQ-8 under an active mandate: propose_counteroffers answered "nothing was
# sent, it waits for approval" and the counter went out 10 s later, approved by the
# mandate. Proposing the mandate on an RFQ already sent read as "the RFQ email waits".


def _copilot_tools(world: MandateWorld, *, jobs: bool) -> dict[str, AnyTool]:
    from tests.rfqs.conftest import fake_agents

    return {
        tool.name: tool
        for tool in copilot_tools(
            CopilotToolDeps(
                db=world.engine,
                agents=fake_agents(),
                mailboxes=world.mailboxes,
                negotiation_writer=FakeNegotiationWriter(),
                jobs=world.jobs if jobs else None,
            )
        )
    }


async def _counteroffers(world: MandateWorld, *, jobs: bool = True) -> ProposeCounteroffersOut:
    tool = _copilot_tools(world, jobs=jobs)["propose_counteroffers"]
    ctx = ToolContext(tenant_id=world.people.tenant, user_id=world.people.owner, run_id=uuid4())
    out = await tool.invoke(ctx, tool.parse_input({"rfq": (await world.rfq()).ref}))
    assert isinstance(out, ProposeCounteroffersOut)
    return out


async def test_the_copilot_reports_an_in_bounds_counter_as_going_out_under_the_mandate(
    world: MandateWorld,
) -> None:
    await world.start()
    await world.offer("rhein", ("0.050", "1.20"))
    sent_before = len(world.mailboxes.sender.sent)
    world.jobs.jobs.clear()

    out = await _counteroffers(world)

    [email] = out.emails
    assert email.approval_id in out.approval_ids
    assert (email.supplier_name, email.status) == ("Rhein", "sending")
    assert email.approved_by_mandate and email.will_send_automatically
    assert email.held_reason is None
    # Approved by the mandate within the tool call, and its execution enqueued.
    assert (await world.approval(email.approval_id)).status == "approved"
    assert [s.outcome for s in await world.sends_for(SEND_NEGOTIATION_EMAIL)] == ["auto_approved"]
    await world.run_jobs()
    assert len(world.mailboxes.sender.sent) == sent_before + 1


async def test_the_copilot_reports_an_out_of_bounds_counter_as_held_with_its_reason(
    world: MandateWorld,
) -> None:
    async with tenant_session(world.engine, world.people.tenant) as session:
        await session.execute(
            update(Rfq).where(Rfq.id == world.rfq_id).values(title="Contract for MLCC")
        )
    await world.start()
    await world.offer("rhein", ("0.050", "1.20"))

    out = await _counteroffers(world)

    [email] = out.emails
    assert (email.status, email.held_reason) == ("awaiting_approval", "commitment_language")
    assert not email.will_send_automatically and not email.approved_by_mandate
    assert (await world.approval(email.approval_id)).status == "pending"
    [held] = await world.sends_for(SEND_NEGOTIATION_EMAIL)
    assert (held.outcome, held.reason) == ("held", "commitment_language")


async def test_without_a_job_queue_the_copilot_predicts_with_the_mandates_own_checks(
    world: MandateWorld,
) -> None:
    await world.start()
    await world.offer("rhein", ("0.050", "1.20"))
    await world.offer("lyon", ("0.045", "1.10"))
    # Lyon taken out of the mandate's suppliers: the mandate would hold its counter.
    async with tenant_session(world.engine, world.people.tenant) as session:
        mandate = await session.scalar(select(NegotiationMandate))
        assert mandate is not None
        terms = ApproveMandateArgs.model_validate(mandate.terms)
        lyon = next(s for s in terms.suppliers if s.supplier_name == "Lyon")
        mandate.terms = {
            **mandate.terms,
            "suppliers": [
                s.model_dump(mode="json") for s in terms.suppliers if s.invite_id != lyon.invite_id
            ],
        }

    out = await _counteroffers(world, jobs=False)

    by_supplier = {e.supplier_name: e for e in out.emails}
    rhein, lyon_email = by_supplier["Rhein"], by_supplier["Lyon"]
    assert (rhein.status, rhein.will_send_automatically) == ("will_send_automatically", True)
    assert (lyon_email.status, lyon_email.held_reason) == (
        "awaiting_approval",
        "supplier_not_in_mandate",
    )
    # A prediction only: nothing approved, nothing recorded as held.
    assert {(await world.approval(e.approval_id)).status for e in out.emails} == {"pending"}
    assert await world.sends_for(SEND_NEGOTIATION_EMAIL) == []


async def test_under_a_paused_mandate_the_copilot_says_the_counter_waits_for_approval(
    world: MandateWorld,
) -> None:
    await world.start()
    await world.offer("rhein", ("0.050", "1.20"))
    await mandates.set_paused(world.engine, buyer(world.people), world.rfq_id, paused=True)

    out = await _counteroffers(world)

    [email] = out.emails
    assert (email.status, email.held_reason) == ("awaiting_approval", "mandate_paused")
    assert (await world.approval(email.approval_id)).status == "pending"


async def test_the_mandate_proposal_says_which_rfq_emails_were_already_sent(
    world: MandateWorld,
) -> None:
    tools = _copilot_tools(world, jobs=True)
    ctx = ToolContext(tenant_id=world.people.tenant, user_id=world.people.owner, run_id=uuid4())
    ref = (await world.rfq()).ref
    args = {"rfq": ref, "deadline": (DEADLINE.date()).isoformat()}
    tool = tools["propose_negotiation_mandate"]

    first = await tool.invoke(ctx, tool.parse_input(args))
    assert isinstance(first, ProposeMandateOut)
    assert first.initial_emails == 2
    assert {(i.supplier_name, i.rfq_email) for i in first.invites} == {
        ("Rhein", "in_this_approval"),
        ("Lyon", "in_this_approval"),
    }

    await world.start(first.approval_id)
    await world.offer("rhein", ("0.050", "1.20"))
    # New limits under the running mandate: a replacement, with no RFQ email in it.
    again = await tool.invoke(ctx, tool.parse_input(args))
    assert isinstance(again, ProposeMandateOut)
    assert again.initial_emails == 0 and again.replaces_current_mandate
    assert {(i.supplier_name, i.rfq_email) for i in again.invites} == {
        ("Rhein", "sent_and_answered"),
        ("Lyon", "sent"),
    }
