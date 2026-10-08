"""The negotiation against Postgres: counters, rounds, approvals, early and deadline close.

Fake writer and fake mailbox; real strategy, approvals, guard and executor.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.agents.catalog.contracts import NegotiationEmailInput, NegotiationEmailOutput
from bido_back.agents.context import ToolContext
from bido_back.approvals import ActToolRegistry, ApprovalExecutor, decide_approval
from bido_back.auth import Principal
from bido_back.db import tenant_session
from bido_back.db.models import (
    Approval,
    FollowupMessage,
    Invite,
    Message,
    NegotiationMessage,
    Proposal,
    ProposalLine,
    Rfq,
    RfqLine,
)
from bido_back.followups import service as followup_service
from bido_back.followups.calendar import add_business_days
from bido_back.followups.send import register_followup_act_tools
from bido_back.negotiation import service
from bido_back.negotiation.hooks import cancel_on_new_offer
from bido_back.negotiation.jobs import ALL_TENANTS, NegotiationSweepHandler
from bido_back.negotiation.schemas import LineLimitsUpdate, NegotiationSettingsUpdate
from bido_back.negotiation.send import (
    NEGOTIATION_REJECT_HOOKS,
    SEND_NEGOTIATION_EMAIL,
    register_negotiation_act_tools,
)
from bido_back.rfqs import service as rfq_service
from bido_back.rfqs import suppliers
from bido_back.rfqs.errors import InvalidState, NotFound
from bido_back.rfqs.schemas import ContactIn, CreateSupplierRequest
from bido_back.rfqs.send import RfqActDeps, register_act_tools
from bido_back.worker.envelope import Envelope
from bido_back.worker.job_types import NEGOTIATION_SWEEP
from tests.approvals.conftest import FakeJobQueue, People
from tests.db.conftest import TwoTenants
from tests.followups.test_followups_db import FakeFollowupWriter
from tests.rfqs.conftest import FakeIntake, FakeMailboxes, FakeWriter

pytestmark = pytest.mark.db

EMAIL_DOMAIN = "quotes.example.test"


class FakeNegotiationWriter:
    """Writes every number exactly as given (what the real prompt asks for)."""

    def __init__(self, *, invent: bool = False) -> None:
        self.calls: list[NegotiationEmailInput] = []
        self.invent = invent

    async def __call__(
        self, task: NegotiationEmailInput, context: ToolContext
    ) -> NegotiationEmailOutput:
        self.calls.append(task)
        asks = "; ".join(
            f"pos {line.position}: target {line.target_unit_price} {line.currency}"
            if line.target_unit_price is not None
            else f"pos {line.position}: lead time {line.required_lead_time_days} days"
            for line in task.lines
        )
        extra = " Another supplier offers 0.0123 EUR." if self.invent else ""
        return NegotiationEmailOutput(body=f"About {task.rfq_ref} ({task.action}): {asks}.{extra}")


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
            self.engine, principal, "Need MLCC and connectors", FakeIntake()
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
                    .order_by(NegotiationMessage.round)
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
    statuses = {i.supplier_name: i.status for i in view.invites}
    assert statuses == {"Rhein": "countered", "Lyon": "accepted"}
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
    assert email.in_reply_to == rhein_rfq_email.message_id
    assert email.subject == f"Re: {rhein_rfq_email.subject}"
    assert "target 0.04 EUR" in email.text_body
    [counter] = await world.messages("rhein")
    assert counter.status == "sent" and counter.sent_at is not None


async def test_rounds_advance_on_new_offers_and_end_with_best_and_final(world: World) -> None:
    await world.configure(early_close=False)
    writer = FakeNegotiationWriter()
    await world.offer("rhein", ("0.050", "1.20"))
    await world.sweep(writer)
    [first] = await world.messages("rhein")
    await world.approve(first.approval_id or first.id)

    # Round 2: a better but still high offer gets another counter.
    await world.offer("rhein", ("0.045", "1.10"))
    await world.sweep(writer)
    rounds = await world.messages("rhein")
    assert [(m.round, m.action, m.status) for m in rounds] == [
        (1, "counter", "sent"),
        (2, "counter", "proposed"),
    ]
    # A newer offer arrives before round 2 is approved: round 2 is moot.
    await world.offer("rhein", ("0.044", "1.08"))
    rounds = await world.messages("rhein")
    assert (rounds[1].status, rounds[1].canceled_reason) == ("canceled", "newer_offer")
    assert (await world.approval(rounds[1].approval_id)).status == "expired"
    await world.sweep(writer)
    rounds = await world.messages("rhein")
    assert [(m.round, m.action, m.status) for m in rounds] == [
        (1, "counter", "sent"),
        (2, "counter", "canceled"),
        (3, "counter", "proposed"),
    ]
    await world.approve(rounds[2].approval_id or rounds[2].id)
    # Two rounds used out of three: the next one is the best-and-final request.
    await world.offer("rhein", ("0.042", "1.05"))
    await world.sweep(writer)
    last = (await world.messages("rhein"))[-1]
    assert (last.round, last.action) == (4, "bafo")
    assert writer.calls[-1].action == "bafo"
    await world.approve(last.approval_id or last.id)
    # The answer to the best-and-final is final: nothing more is sent.
    await world.offer("rhein", ("0.041", "1.02"))
    assert (await world.sweep(writer)).proposed == 0
    view = await service.get_negotiation(world.engine, buyer(world.people), world.rfq_id)
    assert {i.supplier_name: i.status for i in view.invites}["Rhein"] == "closed"


async def test_a_rejected_counter_is_not_sent_and_a_drafted_wrong_number_is_refused(
    world: World,
) -> None:
    await world.configure()
    await world.offer("rhein", ("0.050", "1.20"))
    assert (await world.sweep(FakeNegotiationWriter(invent=True))).skipped == 1
    assert await world.messages("rhein") == []

    await world.sweep(FakeNegotiationWriter())
    [counter] = await world.messages("rhein")
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


async def test_the_deadline_closes_with_the_best_offers_and_stops_follow_ups(
    app_engine: AsyncEngine, rfq_rows: People
) -> None:
    world = await World(app_engine, rfq_rows).setup(silent=True)
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


async def test_sent_refusals_stop_after_two_rounds_without_buyer_concession(world: World) -> None:
    await world.configure(max_rounds=8)
    await world.offer("rhein", ("0.050", "1.20"))
    writer = FakeNegotiationWriter()
    for _ in range(2):
        assert (await world.sweep(writer)).proposed == 1
        counter = (await world.messages("rhein"))[-1]
        assert counter.approval_id is not None
        await world.approve(counter.approval_id)
        await world.offer("rhein", ("0.050", "1.20"))
    result = await world.sweep(writer)
    assert result.proposed == 0
    assert len(writer.calls) == 2
    view = await service.get_negotiation(world.engine, buyer(world.people), world.rfq_id)
    rhein = next(i for i in view.invites if i.supplier_name == "Rhein")
    assert rhein.status == "closed"
    assert rhein.stalled_rounds == 2
    assert rhein.strategy_reasons == ["stalled_negotiation_requires_buyer"]


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
    assert rhein.strategy_reasons == ["supplier_clarification_requires_buyer"]


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
    ) -> NegotiationEmailOutput:
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
    assert await world.messages("rhein") == []
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
        world.engine, buyer(world.people), "New capacitor need", FakeIntake()
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
