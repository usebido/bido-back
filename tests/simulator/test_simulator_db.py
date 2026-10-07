"""The sandbox transport and the simulator job against Postgres; fake writer, no network."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.agents.context import ToolContext
from bido_back.approvals import ActToolRegistry, ApprovalExecutor, decide_approval
from bido_back.auth import Principal
from bido_back.db import tenant_session
from bido_back.db.models import Invite, Message, SandboxOutbound, Tenant
from bido_back.email.types import OutboundEmail
from bido_back.intake.service import InboundService
from bido_back.rfqs import service as rfq_service
from bido_back.rfqs import suppliers
from bido_back.rfqs.schemas import ContactIn, CreateSupplierRequest
from bido_back.rfqs.send import RfqActDeps, register_act_tools
from bido_back.simulator import service as sandbox_service
from bido_back.simulator.agent import SupplierPersonaInput, SupplierPersonaOutput
from bido_back.simulator.jobs import SimulatorDeps, SimulatorReplyHandler
from bido_back.simulator.personas import Persona
from bido_back.simulator.sender import SandboxEmailSender, reply_envelope, sandbox_aware
from bido_back.storage.local import LocalFileStorage
from bido_back.worker.job_types import QUOTE_EXTRACT
from tests.approvals.conftest import FakeJobQueue, People
from tests.rfqs.conftest import FakeIntake, FakeMailboxes, FakeWriter

pytestmark = pytest.mark.db

EMAIL_DOMAIN = "quotes.example.test"


class FakeScheduler:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, UUID, int]] = []

    async def schedule(self, tenant_id: UUID, outbound_id: UUID, delay_seconds: int) -> None:
        self.calls.append((tenant_id, outbound_id, delay_seconds))


class StubPersonaWriter:
    def __init__(self) -> None:
        self.calls: list[SupplierPersonaInput] = []

    async def __call__(
        self, task: SupplierPersonaInput, context: ToolContext
    ) -> SupplierPersonaOutput:
        self.calls.append(task)
        return SupplierPersonaOutput(
            opening=f"[{task.language}] {task.stance} round {task.round}",
            closing=f"{task.contact_name}\n{task.supplier_company}",
        )


def buyer(people: People) -> Principal:
    return Principal(tenant_id=people.tenant, user_id=people.owner, role="owner")


def approver(people: People) -> Principal:
    return Principal(tenant_id=people.tenant, user_id=people.approver, role="approver")


async def make_sandbox(migrator_engine: AsyncEngine, tenant_id: UUID) -> None:
    async with tenant_session(migrator_engine, tenant_id) as session:
        await session.execute(update(Tenant).where(Tenant.id == tenant_id).values(sandbox=True))


class Sandbox:
    """One sandbox RFQ, one invite to a simulated supplier, its RFQ email "sent"."""

    def __init__(self, engine: AsyncEngine, people: People, tmp_path: Path) -> None:
        self.engine = engine
        self.people = people
        self.inner = FakeMailboxes()
        self.scheduler = FakeScheduler()
        self.mailboxes = sandbox_aware(self.inner, lambda: self.scheduler)
        self.jobs = FakeJobQueue()
        self.extract_jobs = FakeJobQueue()
        self.inbound = InboundService(
            engine, LocalFileStorage(tmp_path), self.extract_jobs, EMAIL_DOMAIN
        )
        self.writer = StubPersonaWriter()
        registry = ActToolRegistry()
        register_act_tools(
            registry,
            RfqActDeps(db=engine, email_domain=EMAIL_DOMAIN, mailbox_senders=self.mailboxes),
        )
        self.executor = ApprovalExecutor(engine, registry)
        self.now = datetime.now(UTC)
        self.handler = SimulatorReplyHandler(
            SimulatorDeps(
                db=engine,
                ingest=self.inbound.ingest_for_invite,
                writer=self.writer,
                schedulers=lambda: self.scheduler,
                clock=lambda: self.now,
            )
        )
        self.invite_id: UUID
        self.supplier_id: UUID

    async def setup(self, persona: Persona) -> "Sandbox":
        principal = buyer(self.people)
        rfq = await rfq_service.create_rfq_from_text(
            self.engine, principal, "Need MLCC and connectors", FakeIntake()
        )
        supplier = await suppliers.create_supplier(
            self.engine,
            principal,
            CreateSupplierRequest(
                name="Acciaierie Test",
                language="it",
                country="IT",
                contacts=[ContactIn(name="Marco Rossi", email="marco@acciaierie.example.test")],
            ),
        )
        await sandbox_service.put_persona(self.engine, principal, supplier.id, persona)
        invite = await rfq_service.add_invite(
            self.engine, principal, rfq.id, supplier.id, supplier.contacts[0].id
        )
        prepared = await rfq_service.prepare_send(
            self.engine, principal, rfq.id, FakeWriter(), self.mailboxes
        )
        await decide_approval(
            self.engine,
            approver(self.people),
            prepared.approvals[0].approval_id,
            "approve",
            jobs=self.jobs,
        )
        await self.executor(self.jobs.jobs[0])
        self.invite_id, self.supplier_id = invite.id, supplier.id
        return self

    async def outbound(self) -> list[SandboxOutbound]:
        async with tenant_session(self.engine, self.people.tenant) as session:
            return list(
                await session.scalars(
                    select(SandboxOutbound)
                    .where(SandboxOutbound.invite_id == self.invite_id)
                    .order_by(SandboxOutbound.created_at)
                )
            )

    async def run(self, outbound_id: UUID) -> None:
        await self.handler(reply_envelope(self.people.tenant, outbound_id, self.now))

    async def inbound_messages(self) -> list[Message]:
        async with tenant_session(self.engine, self.people.tenant) as session:
            return list(
                await session.scalars(
                    select(Message).where(
                        Message.invite_id == self.invite_id, Message.direction == "in"
                    )
                )
            )


async def test_a_real_tenant_keeps_its_own_sender(
    app_engine: AsyncEngine, sandbox_rows: People
) -> None:
    inner = FakeMailboxes()
    scheduler = FakeScheduler()
    factory = sandbox_aware(inner, lambda: scheduler)
    async with tenant_session(app_engine, sandbox_rows.tenant) as session:
        sender = await factory(session, sandbox_rows.tenant, sandbox_rows.owner)

    assert sender is inner.sender
    assert inner.calls == [(sandbox_rows.tenant, sandbox_rows.owner)]
    assert scheduler.calls == []


async def test_a_sandbox_tenant_never_sends_and_its_supplier_answers(
    app_engine: AsyncEngine, migrator_engine: AsyncEngine, sandbox_rows: People, tmp_path: Path
) -> None:
    await make_sandbox(migrator_engine, sandbox_rows.tenant)
    box = await Sandbox(app_engine, sandbox_rows, tmp_path).setup(Persona(language="it"))

    # Nothing went through a real transport; the email waits for the simulator.
    assert box.inner.sender.sent == [] and box.inner.calls == []
    [row] = await box.outbound()
    assert row.status == "pending" and row.subject.startswith("[RFQ-")
    assert box.scheduler.calls == [(sandbox_rows.tenant, row.id, 0)]

    await box.run(row.id)

    [reply] = await box.inbound_messages()
    assert reply.subject is not None and reply.subject.startswith("R: [RFQ-")
    assert [job.type for job in box.extract_jobs.jobs] == [QUOTE_EXTRACT]
    async with tenant_session(app_engine, sandbox_rows.tenant) as session:
        invite = await session.get(Invite, box.invite_id)
        stored = await session.get(SandboxOutbound, row.id)
    assert invite is not None and invite.status == "responded"
    assert stored is not None and stored.status == "replied"
    assert stored.offer is not None and stored.offer["round"] == 1
    assert box.writer.calls[0].language == "it" and box.writer.calls[0].stance == "initial"

    # Re-delivered job: nothing new.
    await box.run(row.id)
    assert len(await box.inbound_messages()) == 1


async def test_the_next_round_concedes_from_the_previous_offer(
    app_engine: AsyncEngine, migrator_engine: AsyncEngine, sandbox_rows: People, tmp_path: Path
) -> None:
    await make_sandbox(migrator_engine, sandbox_rows.tenant)
    box = await Sandbox(app_engine, sandbox_rows, tmp_path).setup(Persona(language="it"))
    [first] = await box.outbound()
    await box.run(first.id)

    # The buyer answers in the thread (a counteroffer, as a negotiation would).
    async with tenant_session(app_engine, sandbox_rows.tenant) as session:
        sender = SandboxEmailSender(session, sandbox_rows.tenant, lambda: box.scheduler)
        await sender.send(
            OutboundEmail(
                from_name="Buyer",
                from_address="buyer@example.test",
                to=("marco@acciaierie.example.test",),
                subject=f"Re: {first.subject}",
                text_body="Can you do better on price?",
                message_id="<counter-1@example.test>",
                in_reply_to=first.message_id,
                references=(first.message_id,),
            )
        )
    second = (await box.outbound())[-1]
    assert second.message_id == "<counter-1@example.test>" and second.invite_id == box.invite_id

    await box.run(second.id)

    first_offer = (await box.outbound())[0].offer
    second_offer = (await box.outbound())[-1].offer
    assert first_offer is not None and second_offer is not None
    assert second_offer["round"] == 2 and second_offer["stance"] == "concession"
    first_price = float(first_offer["lines"][0]["tiers"][0]["unit_price"])
    second_price = float(second_offer["lines"][0]["tiers"][0]["unit_price"])
    assert second_price < first_price
    assert len(await box.inbound_messages()) == 2


async def test_a_slow_responder_ignores_the_first_email(
    app_engine: AsyncEngine, migrator_engine: AsyncEngine, sandbox_rows: People, tmp_path: Path
) -> None:
    await make_sandbox(migrator_engine, sandbox_rows.tenant)
    box = await Sandbox(app_engine, sandbox_rows, tmp_path).setup(
        Persona(language="de", behaviors=("slow_responder",))
    )
    [row] = await box.outbound()

    await box.run(row.id)

    assert (await box.outbound())[0].status == "ignored"
    assert await box.inbound_messages() == []


async def test_an_answer_that_is_not_due_is_rescheduled(
    app_engine: AsyncEngine, migrator_engine: AsyncEngine, sandbox_rows: People, tmp_path: Path
) -> None:
    await make_sandbox(migrator_engine, sandbox_rows.tenant)
    box = await Sandbox(app_engine, sandbox_rows, tmp_path).setup(
        Persona(language="it", response_delay_minutes=30)
    )
    [row] = await box.outbound()
    assert box.scheduler.calls[-1][2] == 1800

    box.now = row.due_at - timedelta(minutes=10)
    await box.run(row.id)

    assert (await box.outbound())[0].status == "pending"
    assert 590 <= box.scheduler.calls[-1][2] <= 600
    assert await box.inbound_messages() == []
