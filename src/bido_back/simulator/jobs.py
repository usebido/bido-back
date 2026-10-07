"""The `simulator.reply` job: a simulated supplier answers one sandbox email.

Steps, for the `sandbox_outbound` row named in the payload:

1. Read everything in one tenant-scoped transaction: the row (pending and due,
   else re-scheduled or skipped), its invite, the supplier, its persona and
   contact, the RFQ lines, and the supplier's earlier offers in this thread.
   A slow responder ignores the first RFQ email (status `ignored`).
2. Code decides the offer (`offers.decide_offer`); the `supplier_persona`
   agent writes the opening and closing (outside any transaction); code
   renders the email and its attachment.
3. The reply is ingested through `InboundService.ingest_for_invite`, exactly
   like a reply read from a buyer's mailbox.
4. The row is marked `replied` with the offer, so the next round concedes
   from it.

Idempotent: the reply's Message-ID is derived from the row id, so a retried
job is a duplicate for intake, and the row update only moves `pending` rows.
Logs carry ids only.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Final
from uuid import UUID, uuid5

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.agents.context import ToolContext
from bido_back.db import tenant_session
from bido_back.db.models import (
    Contact,
    Invite,
    ProposalLine,
    Rfq,
    RfqLine,
    SandboxOutbound,
    Supplier,
    SupplierPersona,
    Tenant,
)
from bido_back.email.addresses import address_domain
from bido_back.intake.service import IngestResult
from bido_back.simulator.agent import (
    MAX_BUYER_MESSAGE_CHARS,
    PersonaWriter,
    SupplierPersonaInput,
    SupplierPersonaOutput,
    fallback_prose,
)
from bido_back.simulator.offers import (
    LineSpec,
    Offer,
    decide_offer,
    is_final_request,
    should_answer,
)
from bido_back.simulator.personas import Persona, persona_from_row
from bido_back.simulator.render import build_reply
from bido_back.simulator.sender import SANDBOX_DOMAIN, ReplyScheduler
from bido_back.worker.envelope import Envelope

logger = logging.getLogger(__name__)

Ingest = Callable[..., Awaitable[IngestResult]]
_RUN_NAMESPACE: Final = UUID("8d3f0a52-6c1e-4c7b-9a0e-3e2f6a7d1b44")
_DUE_SLACK_SECONDS: Final = 2


class OutboundNotVisible(RuntimeError):
    """The row is not committed yet (the send's transaction): retried by SQS."""


@dataclass(frozen=True, slots=True)
class SimulatorDeps:
    db: AsyncEngine | async_sessionmaker[AsyncSession]
    ingest: Ingest
    writer: PersonaWriter
    schedulers: Callable[[], ReplyScheduler]
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class _Plan:
    invite_id: UUID
    persona: Persona
    offer: Offer
    supplier_company: str
    contact_name: str
    contact_address: str
    buyer_name: str
    buyer_address: str
    subject: str
    body: str
    message_id: str
    references: list[str]


class SimulatorReplyHandler:
    def __init__(self, deps: SimulatorDeps) -> None:
        self._deps = deps

    async def __call__(self, envelope: Envelope) -> None:
        outbound_id = UUID(str(envelope.payload["outbound_id"]))
        tenant_id = envelope.tenant_id
        plan = await self._plan(tenant_id, outbound_id)
        if plan is None:
            return
        context = ToolContext(
            tenant_id=tenant_id, user_id=None, run_id=uuid5(_RUN_NAMESPACE, str(outbound_id))
        )
        task = _persona_input(plan)
        try:
            prose = await self._deps.writer(task, context)
        except Exception:  # the simulation never stops on a model error
            logger.warning("simulator prose failed outbound=%s; using fallback", outbound_id)
            prose = fallback_prose(task, buyer_name=plan.buyer_name)
        raw = _render(plan, prose, outbound_id)
        result = await self._deps.ingest(raw, tenant_id=tenant_id, invite_id=plan.invite_id)
        async with tenant_session(self._deps.db, tenant_id) as session:
            await session.execute(
                update(SandboxOutbound)
                .where(SandboxOutbound.id == outbound_id, SandboxOutbound.status == "pending")
                .values(
                    status="replied",
                    offer=plan.offer.model_dump(mode="json"),
                    reply_message_id=_reply_message_id(outbound_id, plan.contact_address),
                    replied_at=self._deps.clock(),
                )
            )
        logger.info(
            "simulator replied tenant=%s outbound=%s invite=%s round=%s outcome=%s",
            tenant_id,
            outbound_id,
            plan.invite_id,
            plan.offer.round,
            result.outcome,
        )

    async def _plan(self, tenant_id: UUID, outbound_id: UUID) -> _Plan | None:
        now = self._deps.clock()
        async with tenant_session(self._deps.db, tenant_id) as session:
            row = await session.get(SandboxOutbound, outbound_id)
            if row is None:
                raise OutboundNotVisible(f"sandbox outbound {outbound_id} not visible yet")
            if not await session.scalar(select(Tenant.sandbox).where(Tenant.id == tenant_id)):
                logger.error("simulator job for a non-sandbox tenant=%s ignored", tenant_id)
                return None
            if row.status != "pending":
                return None
            remaining = (row.due_at - now).total_seconds()
            if remaining > _DUE_SLACK_SECONDS:
                await self._deps.schedulers().schedule(tenant_id, outbound_id, int(remaining))
                return None
            return await self._load(session, row, now.date())

    async def _load(self, session: AsyncSession, row: SandboxOutbound, today: date) -> _Plan | None:
        invite = await session.get(Invite, row.invite_id) if row.invite_id else None
        if invite is None:
            row.status, row.error = "failed", "no_invite"
            return None
        supplier = await session.get(Supplier, invite.supplier_id)
        contact = await session.get(Contact, invite.contact_id)
        rfq = await session.get(Rfq, invite.rfq_id)
        if supplier is None or contact is None or contact.email is None or rfq is None:
            row.status, row.error = "failed", "missing_records"
            return None
        persona = persona_from_row(
            await session.scalar(
                select(SupplierPersona).where(SupplierPersona.supplier_id == supplier.id)
            )
        )
        is_first = row.in_reply_to is None and not row.references
        if not should_answer(persona, is_first_email=is_first):
            row.status = "ignored"
            return None

        earlier = list(
            (
                await session.scalars(
                    select(SandboxOutbound)
                    .where(
                        SandboxOutbound.invite_id == invite.id,
                        SandboxOutbound.status == "replied",
                        SandboxOutbound.offer.is_not(None),
                    )
                    .order_by(SandboxOutbound.created_at)
                )
            ).all()
        )
        previous = Offer.model_validate(earlier[-1].offer) if earlier else None
        lines = [
            LineSpec(
                position=line.position,
                description=line.description,
                mpn=line.mpn,
                unit=line.unit,
                quantities=tuple(Decimal(str(q)) for q in line.quantities),
                need_by=line.need_by,
                incoterm=line.incoterm,
            )
            for line in (
                await session.scalars(
                    select(RfqLine).where(RfqLine.rfq_id == rfq.id).order_by(RfqLine.position)
                )
            ).all()
        ]
        offer = decide_offer(
            persona,
            lines,
            round_number=len(earlier) + 1,
            currency=rfq.base_currency,
            today=today,
            history=await _history(session, lines),
            previous=previous,
            final_request=is_final_request(f"{row.subject}\n{row.body_text}"),
            article_prefix=_initials(supplier.name),
        )
        return _Plan(
            invite_id=invite.id,
            persona=persona,
            offer=offer,
            supplier_company=supplier.name,
            contact_name=contact.name or supplier.name,
            contact_address=contact.email,
            buyer_name=row.from_name,
            buyer_address=row.from_address,
            subject=row.subject,
            body=row.body_text,
            message_id=row.message_id,
            references=[str(ref) for ref in row.references],
        )


async def _history(session: AsyncSession, lines: list[LineSpec]) -> dict[str, Decimal]:
    """The tenant's latest quoted unit price per MPN of these lines (any supplier)."""
    mpns = [line.mpn for line in lines if line.mpn]
    if not mpns:
        return {}
    rows = await session.execute(
        select(ProposalLine.offered_mpn, ProposalLine.unit_price)
        .where(ProposalLine.offered_mpn.in_(mpns), ProposalLine.unit_price.is_not(None))
        .order_by(ProposalLine.created_at.desc())
    )
    history: dict[str, Decimal] = {}
    for mpn, price in rows.all():
        if mpn and price is not None:
            history.setdefault(mpn.lower(), price)
    return history


def _initials(name: str) -> str:
    """`Fonderia Veneta S.p.A.` -> `FV`: the supplier's own article-number prefix."""
    words = [word for word in name.replace(".", " ").split() if word[:1].isupper()]
    return "".join(word[0] for word in words[:2]) or "ART"


def _persona_input(plan: _Plan) -> SupplierPersonaInput:
    lines = plan.offer.lines
    return SupplierPersonaInput(
        supplier_company=plan.supplier_company,
        contact_name=plan.contact_name,
        language=plan.persona.language,
        behaviors=list(plan.persona.behaviors),
        round=plan.offer.round,
        stance=plan.offer.stance,
        authorized=plan.offer.authorized,
        quote_location="below" if plan.persona.quote_format == "body" else "attachment",
        declined_lines=[line.description for line in lines if line.declined],
        alternative_lines=[line.description for line in lines if line.is_alternative],
        mentions_other_departments=bool(plan.offer.noise),
        buyer_subject=plan.subject,
        buyer_message=plan.body[:MAX_BUYER_MESSAGE_CHARS],
    )


def _reply_message_id(outbound_id: UUID, contact_address: str) -> str:
    return f"<sim-{outbound_id}@{address_domain(contact_address) or SANDBOX_DOMAIN}>"


def _render(plan: _Plan, prose: SupplierPersonaOutput, outbound_id: UUID) -> bytes:
    return build_reply(
        offer=plan.offer,
        language=plan.persona.language,
        quote_format=plan.persona.quote_format,
        opening=prose.opening,
        closing=prose.closing,
        supplier_company=plan.supplier_company,
        from_name=plan.contact_name,
        from_address=plan.contact_address,
        to_name=plan.buyer_name,
        to_address=plan.buyer_address,
        subject=plan.subject,
        message_id=_reply_message_id(outbound_id, plan.contact_address),
        in_reply_to=plan.message_id,
        references=plan.references,
    )
