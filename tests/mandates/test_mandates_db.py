"""Negotiation mandates against Postgres: one approval, then automatic in-bounds sends.

Fake writers and mailbox; real strategy, approvals, guard, executor and mandate checks.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.agents.catalog.contracts import (
    NegotiationEmailInput,
    NegotiationEmailOutput,
)
from bido_back.agents.context import ToolContext
from bido_back.approvals import (
    ApprovalExecutor,
    ApprovalRecipientsUnconfirmed,
    decide_approval,
)
from bido_back.db import tenant_session
from bido_back.db.models import (
    Approval,
    MailboxConnection,
    MandateSend,
    NegotiationMandate,
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
from bido_back.negotiation.schemas import LineLimitsUpdate, NegotiationSettingsUpdate
from bido_back.negotiation.send import SEND_NEGOTIATION_EMAIL
from bido_back.rfqs import service as rfq_service
from bido_back.rfqs import suppliers
from bido_back.rfqs.errors import InvalidState
from bido_back.rfqs.schemas import ContactIn, CreateSupplierRequest
from bido_back.rfqs.send import SEND_RFQ_EMAIL
from tests.approvals.conftest import People
from tests.negotiation.test_negotiation_db import FakeNegotiationWriter, World, approver, buyer
from tests.rfqs.conftest import FakeIntake, FakeWriter

pytestmark = pytest.mark.db

DEADLINE = datetime.now(UTC) + timedelta(days=7)


class CommittingWriter(FakeNegotiationWriter):
    """A writer whose draft commits the buyer: must never go out on its own."""

    async def __call__(
        self, task: NegotiationEmailInput, context: ToolContext
    ) -> NegotiationEmailOutput:
        draft = await super().__call__(task, context)
        return NegotiationEmailOutput(body=draft.body + " We accept your offer.")


class MandateWorld(World):
    """Like World, but the RFQ emails go out under a mandate, not one by one."""

    def __init__(self, engine: AsyncEngine, people: People) -> None:
        super().__init__(engine, people)
        register_mandate_act_tools(self.registry, MandateActDeps(db=engine, jobs=self.jobs))
        self.executor = ApprovalExecutor(engine, self.registry)

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
            self.engine, principal, "Need MLCC and connectors", FakeIntake()
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


async def test_a_draft_outside_the_mandate_is_held_for_a_person(world: MandateWorld) -> None:
    await world.start()
    await world.offer("rhein", ("0.050", "1.20"))
    await world.sweep(CommittingWriter())
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


async def test_the_daily_cap_defers_without_holding(world: MandateWorld) -> None:
    await world.start(max_messages_per_supplier_per_day=1)
    await world.offer("rhein", ("0.050", "1.20"))
    await world.sweep(FakeNegotiationWriter())

    result = await world.auto()

    # The RFQ email already used today's message: the counter waits, it is not held.
    assert result.approved == [] and result.held == 0
    [counter] = await world.messages("rhein")
    assert counter.status == "proposed"


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
