"""The whole RFQ flow in one process, for the flow evals.

`World` wires the production pieces the worker wires (`worker.main`): the act
tools behind the approval executor, mandates, quote extraction, the follow-up
and negotiation sweeps, and supplier-reply intake (`ingest_for_invite`). Two
things are replaced:

* the job queue is a list, drained in-process (`drain`): approval executions
  run one by one, extractions concurrently;
* the mailbox transport is `Outbox`, which records every email instead of
  sending it. It sits under the production recipient guard (`guarded`), so
  the guard runs for real. Nothing ever leaves the process.

The clock is simulated: `World.now` is the real start time plus an offset the
scenario advances (`advance`, business days for follow-ups, days for
negotiation rounds). The sweeps and extraction take it as `now` / `today`;
supplier replies carry it as their Date header.
"""

import asyncio
import io
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pdfplumber
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bido_back.agents.context import ToolContext
from bido_back.approvals import ActToolRegistry, ApprovalExecutor, decide_approval
from bido_back.approvals.executor import APPROVAL_EXECUTE_JOB
from bido_back.auth import Principal
from bido_back.awards.agents import default_notice_writer
from bido_back.awards.send import register_award_act_tools
from bido_back.db import tenant_session
from bido_back.db.models import (
    Invite,
    MailboxConnection,
    Message,
    Proposal,
    ProposalLine,
    RfqLine,
    User,
)
from bido_back.email.addresses import make_message_id
from bido_back.email.mime import build_mime
from bido_back.email.types import OutboundAttachment, OutboundEmail, SentEmail
from bido_back.followups import service as followup_service
from bido_back.followups.agents import default_followup_writer
from bido_back.followups.send import register_followup_act_tools
from bido_back.intake.service import InboundService, IngestResult
from bido_back.mandates.service import MandateActDeps, register_mandate_act_tools
from bido_back.negotiation.agents import default_negotiation_writer
from bido_back.negotiation.jobs import NegotiationSweepHandler
from bido_back.negotiation.send import register_negotiation_act_tools
from bido_back.outbound.sender import guarded
from bido_back.quotes.extract_job import handle_quote_extract
from bido_back.rfqs import service as rfq_service
from bido_back.rfqs import suppliers
from bido_back.rfqs.agents import run_email_writer, run_intake
from bido_back.rfqs.document import PDF_CONTENT_TYPE
from bido_back.rfqs.schemas import ContactIn, CreateSupplierRequest
from bido_back.rfqs.send import RfqActDeps, register_act_tools
from bido_back.rfqs.service import NewRfqLine
from bido_back.worker.envelope import Envelope
from bido_back.worker.job_types import NEGOTIATION_SWEEP, QUOTE_EXTRACT
from evals.harness import Env, new_company
from evals.personas import ITEMS, Item, Persona, Reply, reply_for
from evals.scripted import ScriptedModel

# Bido's own reply domain in the evals (Message-IDs of the emails it sends).
EMAIL_DOMAIN = "quotes.evals.example.test"


# --- The outbox: a mailbox transport that records ----------------------------------------------


@dataclass(frozen=True, slots=True)
class Captured:
    tenant_id: UUID
    sender_user_id: UUID
    to: tuple[str, ...]
    cc: tuple[str, ...]
    subject: str
    body: str
    message_id: str
    in_reply_to: str | None
    references: tuple[str, ...]
    attachments: tuple[str, ...]
    at: datetime
    # The HTML part, when the email has one.
    html: str | None = None
    # The text of the PDF attachments (the RFQ document), for checks on what it carries.
    attachment_text: str = ""


def _pdf_text(data: bytes) -> str:
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)


class _Recorder:
    def __init__(self, outbox: "Outbox", tenant_id: UUID, user_id: UUID, address: str) -> None:
        self._outbox = outbox
        self._tenant_id = tenant_id
        self._user_id = user_id
        self.address = address

    async def send(self, email: OutboundEmail) -> SentEmail:
        message_id = email.message_id or make_message_id(EMAIL_DOMAIN)
        self._outbox.sent.append(
            Captured(
                tenant_id=self._tenant_id,
                sender_user_id=self._user_id,
                to=email.to,
                cc=email.cc,
                subject=email.subject,
                body=email.text_body,
                message_id=message_id,
                in_reply_to=email.in_reply_to,
                references=email.references,
                attachments=tuple(a.filename for a in email.attachments),
                at=self._outbox.clock(),
                html=email.html_body,
                attachment_text="\n".join(
                    _pdf_text(a.data)
                    for a in email.attachments
                    if a.content_type == PDF_CONTENT_TYPE
                ),
            )
        )
        return SentEmail(message_id=message_id, raw=b"")


@dataclass(slots=True)
class Outbox:
    """The mailbox sender factory seam: every buyer is "connected", nothing is sent."""

    clock: Callable[[], datetime]
    addresses: dict[UUID, str] = field(default_factory=dict[UUID, str])
    sent: list[Captured] = field(default_factory=list[Captured])

    async def __call__(self, session: AsyncSession, tenant_id: UUID, user_id: UUID) -> _Recorder:
        return _Recorder(self, tenant_id, user_id, self.addresses.get(user_id, ""))

    def to(self, address: str) -> list[Captured]:
        return [c for c in self.sent if address in c.to]


class MemoryQueue:
    def __init__(self) -> None:
        self.jobs: list[Envelope] = []

    async def enqueue(self, envelope: Envelope) -> None:
        self.jobs.append(envelope)


class Recording[In, Out]:
    """A writer seam that keeps every task and draft (None when the run failed)."""

    def __init__(self, inner: Callable[[In, ToolContext], Awaitable[Out]]) -> None:
        self.inner = inner
        self.calls: list[tuple[In, Out | None]] = []
        self.seconds: list[float] = []

    async def __call__(self, task: In, context: ToolContext) -> Out:
        started = asyncio.get_running_loop().time()
        try:
            out = await self.inner(task, context)
        except Exception:
            self.calls.append((task, None))
            raise
        finally:
            self.seconds.append(asyncio.get_running_loop().time() - started)
        self.calls.append((task, out))
        return out


class _NoMx:
    async def has_mx(self, domain: str) -> bool | None:
        return None  # never a DNS lookup from the evals


@dataclass(frozen=True, slots=True)
class Party:
    persona: Persona
    supplier_id: UUID
    contact_id: UUID


@dataclass(frozen=True, slots=True)
class RfqRef:
    id: UUID
    ref: str
    lines: dict[int, UUID]  # position -> rfq line id


class World:
    def __init__(self, env: Env, principal: Principal, model: ScriptedModel | None) -> None:
        self.env = env
        self.engine = env.engine
        self.principal = principal
        self.tenant_id = principal.tenant_id
        self.model = model
        self.start = datetime.now(UTC).replace(microsecond=0)
        self.offset = timedelta(0)
        self.outbox = Outbox(clock=lambda: self.now)
        self.mailboxes = guarded(self.outbox)
        self.jobs = MemoryQueue()
        self.registry = ActToolRegistry()
        deps = RfqActDeps(db=self.engine, email_domain=EMAIL_DOMAIN, mailbox_senders=self.mailboxes)
        register_act_tools(self.registry, deps)
        register_followup_act_tools(self.registry, deps)
        register_negotiation_act_tools(self.registry, deps)
        register_award_act_tools(self.registry, deps)
        register_mandate_act_tools(self.registry, MandateActDeps(db=self.engine, jobs=self.jobs))
        self.executor = ApprovalExecutor(self.engine, self.registry)
        self.inbound = InboundService(self.engine, env.storage, self.jobs, EMAIL_DOMAIN)
        config = env.runner_config
        self.intake = Recording(run_intake)
        self.email_writer = Recording(run_email_writer)
        self.followup_writer = Recording(default_followup_writer(config))
        self.negotiation_writer = Recording(default_negotiation_writer(config))
        self.notice_writer = Recording(default_notice_writer(config))
        self.resolver = _NoMx()
        self.extraction_seconds: list[float] = []
        self.parties: dict[str, Party] = {}
        self.invites: dict[str, UUID] = {}
        self.buyer_email = ""

    @classmethod
    async def create(cls, env: Env, label: str, model: ScriptedModel | None) -> "World":
        principal = await new_company(env, label)
        world = cls(env, principal, model)
        async with tenant_session(env.engine, principal.tenant_id) as session:
            user = await session.get(User, principal.user_id)
            if user is not None:
                world.outbox.addresses[user.id] = user.email
                world.buyer_email = user.email
                # The buyer's "connected" mailbox: the outbox stands in for its API (the
                # token is not a real one and is never used).
                session.add(
                    MailboxConnection(
                        tenant_id=principal.tenant_id,
                        user_id=user.id,
                        provider="google",
                        email_address=user.email,
                        token_ciphertext=b"eval-not-a-real-token",
                        status="active",
                    )
                )
        return world

    # --- The clock ---------------------------------------------------------------------------

    @property
    def now(self) -> datetime:
        return self.start + self.offset

    @property
    def today(self) -> date:
        return self.now.date()

    def at(self, moment: datetime) -> None:
        if moment < self.now:
            raise ValueError("the eval clock only moves forward")
        self.offset = moment - self.start

    # --- Jobs --------------------------------------------------------------------------------

    async def drain(self) -> None:
        """Run every queued job, and the jobs they queue, until the queue is empty."""
        while self.jobs.jobs:
            batch, self.jobs.jobs = self.jobs.jobs, []
            extractions = [j for j in batch if j.type == QUOTE_EXTRACT]
            for job in batch:
                if job.type == APPROVAL_EXECUTE_JOB:
                    await self.executor(job)
                elif job.type != QUOTE_EXTRACT:
                    raise RuntimeError(f"the eval world has no handler for {job.type}")
            await asyncio.gather(*(self._extract(job) for job in extractions))

    async def _extract(self, job: Envelope) -> None:
        started = asyncio.get_running_loop().time()
        await handle_quote_extract(
            job,
            db=self.engine,
            storage=self.env.storage,
            runner_config=self.env.runner_config,
            today=self.today,
        )
        self.extraction_seconds.append(asyncio.get_running_loop().time() - started)

    async def approve(self, approval_id: UUID, confirmed_domains: Sequence[str] = ()) -> None:
        await decide_approval(
            self.engine,
            self.principal,
            approval_id,
            "approve",
            jobs=self.jobs,
            confirmed_domains=confirmed_domains,
        )
        await self.drain()

    async def sweep_negotiation(self) -> None:
        handler = NegotiationSweepHandler(
            db=self.engine, writer=self.negotiation_writer, now=self.now, jobs=self.jobs
        )
        await handler(
            Envelope(
                type=NEGOTIATION_SWEEP,
                tenant_id=self.tenant_id,
                idempotency_key=f"eval-negotiation:{uuid4()}",
            )
        )
        await self.drain()

    async def sweep_followups(self) -> followup_service.SweepResult:
        result = await followup_service.sweep_tenant(
            self.engine, self.tenant_id, writer=self.followup_writer, now=self.now
        )
        await self.drain()
        return result

    # --- Setup -------------------------------------------------------------------------------

    async def supplier(self, persona: Persona) -> Party:
        domain = persona.email.rpartition("@")[2]
        view = await suppliers.create_supplier(
            self.engine,
            self.principal,
            CreateSupplierRequest(
                name=persona.name,
                type=persona.kind,
                country=persona.country,
                language=persona.language,
                email_domain=domain,
                contacts=[ContactIn(name=persona.contact, email=persona.email)],
            ),
        )
        party = Party(persona, view.id, view.contacts[0].id)
        self.parties[persona.key] = party
        return party

    async def draft_rfq(
        self, title: str, need_by: date | None, items: tuple[Item, ...] = ITEMS
    ) -> RfqRef:
        async with tenant_session(self.engine, self.tenant_id) as session:
            rfq, lines = await rfq_service.create_draft_rfq(
                session,
                self.principal,
                title=title,
                # Suppliers answer within two weeks of the eval clock's today.
                response_due=self.today + timedelta(days=14),
                base_currency="EUR",
                lines=[
                    NewRfqLine(
                        description=item.description,
                        quantity=item.quantity,
                        mpn=item.mpn,
                        need_by=need_by,
                    )
                    for item in items
                ],
            )
            for line in lines:
                line.incoterm = "DAP"
            await session.flush()
            return RfqRef(rfq.id, rfq.ref, {line.position: line.id for line in lines})

    async def invite(self, rfq: RfqRef, persona: Persona) -> UUID:
        party = self.parties.get(persona.key) or await self.supplier(persona)
        view = await rfq_service.add_invite(
            self.engine, self.principal, rfq.id, party.supplier_id, party.contact_id
        )
        self.invites[persona.key] = view.id
        return view.id

    async def send_rfq(self, rfq: RfqRef) -> list[UUID]:
        prepared = await rfq_service.prepare_send(
            self.engine, self.principal, rfq.id, self.email_writer, self.mailboxes
        )
        ids = [item.approval_id for item in prepared.approvals]
        for approval_id in ids:
            await self.approve(approval_id)
        return ids

    # --- The supplier side -----------------------------------------------------------------

    def last_to(self, persona: Persona) -> Captured:
        received = self.outbox.to(persona.email)
        if not received:
            raise RuntimeError(f"{persona.key} never received an email")
        return received[-1]

    async def deliver(
        self,
        persona: Persona,
        *,
        subject: str,
        body: str,
        attachments: Sequence[tuple[str, str, bytes]] = (),
        auto_reply: bool = False,
    ) -> IngestResult:
        """A supplier's email in the thread of the last email it received."""
        thread = self.last_to(persona)
        email = OutboundEmail(
            from_name=f"{persona.contact} | {persona.name}",
            from_address=persona.email,
            to=(self.buyer_email,),
            subject=subject,
            text_body=body,
            message_id=make_message_id(persona.email.rpartition("@")[2]),
            in_reply_to=thread.message_id,
            references=(*thread.references, thread.message_id),
            attachments=tuple(OutboundAttachment(n, t, d) for n, t, d in attachments),
        )
        message = build_mime(email, now=self.now)
        if auto_reply:
            message["Auto-Submitted"] = "auto-replied"
        result = await self.inbound.ingest_for_invite(
            message.as_bytes(), tenant_id=self.tenant_id, invite_id=self.invites[persona.key]
        )
        if result.message_id is not None:
            # Receipt time shares the simulated business clock. Keeping the DB
            # wall clock here would make every advanced scenario look forged.
            async with tenant_session(self.engine, self.tenant_id) as session:
                await session.execute(
                    update(Message)
                    .where(Message.id == result.message_id)
                    .values(created_at=self.now)
                )
        return result

    async def quote(
        self,
        persona: Persona,
        rfq: RfqRef,
        round: int,
        *,
        previous: Reply | None = None,
        drop: tuple[str, ...] = (),
        items: tuple[Item, ...] = ITEMS,
    ) -> Reply:
        reply = reply_for(
            persona,
            rfq_ref=rfq.ref,
            subject=self.last_to(persona).subject,
            round=round,
            valid_until=self.start.date() + timedelta(days=90),
            items=items,
            previous=previous.quote if previous else None,
            drop=drop,
        )
        if self.model is not None:
            self.model.extraction[reply.body.strip()] = reply.truth
        await self.deliver(
            persona, subject=reply.subject, body=reply.body, attachments=reply.attachments
        )
        return reply

    # --- Reading back ------------------------------------------------------------------------

    async def offers(self, persona: Persona) -> list[dict[int, tuple[Decimal | None, int | None]]]:
        """Every stored proposal version of the persona: position -> (price, lead)."""
        invite_id = self.invites[persona.key]
        async with tenant_session(self.engine, self.tenant_id) as session:
            proposals = list(
                await session.scalars(
                    select(Proposal)
                    .where(Proposal.invite_id == invite_id)
                    .order_by(Proposal.version)
                )
            )
            positions = {
                line.id: line.position
                for line in await session.scalars(
                    select(RfqLine)
                    .join(Invite, Invite.rfq_id == RfqLine.rfq_id)
                    .where(Invite.id == invite_id)
                )
            }
            versions: list[dict[int, tuple[Decimal | None, int | None]]] = []
            for proposal in proposals:
                rows = await session.scalars(
                    select(ProposalLine).where(ProposalLine.proposal_id == proposal.id)
                )
                versions.append(
                    {
                        positions[row.rfq_line_id]: (row.unit_price, row.lead_time_days)
                        for row in rows
                        if row.rfq_line_id is not None and row.rfq_line_id in positions
                    }
                )
            return versions
