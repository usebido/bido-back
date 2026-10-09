"""RFQ follow-ups: the chase schedule, auto-reply pauses, and the sweeper.

Long waits are rows. Nothing sleeps: the `followups.sweep` job (every 15
minutes, see `bido_back.followups.jobs`) reads the rows and does what is due,
per tenant, in three steps:

1. Reconcile: a proposed follow-up whose reason is gone is canceled and its
   pending approval expired. A chase is gone once the invite is no longer
   `sent` (the supplier answered or declined); a clarification once a newer
   proposal (a later reply) exists.
2. Chases: for every `sent` invite on an `open` RFQ with follow-ups enabled,
   the next chase is due `first_after` business days after the RFQ email,
   then every `every` business days after the previous chase, at most
   `max_chases` (rejected chases count: the buyer said no to that one).
   Nothing is due while a chase awaits approval, or before
   `invites.followups_paused_until` (an auto-reply).
3. Clarifications: for every `responded` invite on an `open` RFQ, the latest
   proposal is checked (`bido_back.followups.missing`); if anything required is
   missing or unclear, one clarification is proposed for that proposal.

A due follow-up is written by the follow-up writer agent, checked
(`followup_email_problems`), signed by code (`bido_back.templates.signature`;
the writer does not sign), and recorded as a PENDING approval of
`send_followup_email` requested on behalf of the buyer who sent the RFQ
email, exactly like that first send: another approver decides, and the worker
sends it once, in the invite's thread (`bido_back.followups.send`). It is
written in the language of the invite's RFQ email
(`bido_back.rfqs.language.thread_language`).

A draft the checks refuse is not written again every sweep: its row is
recorded `canceled` with reason `draft_refused` (no approval), an
`rfq.followup_draft_held` audit event puts it in the RFQ's activity feed for
the buyer, and the follow-up counts as done: a refused chase uses that chase
(the next one is due `every` business days later), a refused clarification
closes that offer's clarification (the next offer gets its own). A writer that
fails (the model is down) is retried on the next sweep.

Intake calls two hooks in its own transaction: `cancel_on_reply` when a
supplier answer is stored, and `pause_on_auto_reply` when an auto-reply
arrives.

Logs carry ids and counts only.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, cast
from uuid import UUID, uuid4

from pydantic import JsonValue
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.agents.approvals import ApprovalProposal
from bido_back.agents.catalog.contracts import (
    FollowupEmailInput,
    FollowupItem,
    FollowupKind,
)
from bido_back.agents.catalog.definitions import followup_email_problems
from bido_back.agents.context import ToolContext
from bido_back.agents.events import args_hash
from bido_back.approvals.errors import ApprovalConflict
from bido_back.approvals.sink import PostgresApprovalSink
from bido_back.auth import Principal
from bido_back.db import tenant_session
from bido_back.db.models import (
    Approval,
    AuditEvent,
    Contact,
    FollowupMessage,
    Invite,
    Message,
    Proposal,
    ProposalLine,
    Rfq,
    RfqLine,
    Supplier,
    Tenant,
    User,
)
from bido_back.followups.agents import FollowupWriter
from bido_back.followups.calendar import add_business_days
from bido_back.followups.hooks import cancel_followups
from bido_back.followups.missing import OfferedLine, RequestedLine, open_items
from bido_back.followups.schemas import (
    ChaseState,
    FollowupMessageView,
    FollowupSettings,
    FollowupSettingsUpdate,
    FollowupStatus,
    InviteFollowups,
    RfqFollowups,
)
from bido_back.followups.send import (
    SEND_FOLLOWUP_EMAIL,
    FollowupKindName,
    SendFollowupEmailArgs,
    followup_id,
    followup_proposal_ids,
)
from bido_back.rfqs.errors import AgentFailed, NotFound
from bido_back.rfqs.language import thread_language
from bido_back.rfqs.send import SEND_RFQ_EMAIL, send_proposal_ids, subject_marker
from bido_back.rfqs.service import buyer_display_name
from bido_back.templates.signature import signed

logger = logging.getLogger(__name__)

Db = AsyncEngine | async_sessionmaker[AsyncSession]

# Chases that count toward `max_chases`: sent, waiting, or turned down.
_COUNTED: Final = ("proposed", "sent", "rejected")
# A follow-up whose draft the checks refused: recorded, held for the buyer, not retried.
DRAFT_REFUSED: Final = "draft_refused"
_REPLY_PREFIXES: Final = ("re:", "aw:", "sv:", "réf:", "rif:", "res:")


# --- The schedule ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Schedule:
    enabled: bool
    first_after: int
    every: int
    max_chases: int


@dataclass(frozen=True, slots=True)
class ChaseRow:
    status: str
    created_at: datetime
    sent_at: datetime | None
    canceled_reason: str | None = None

    @property
    def counted(self) -> bool:
        """Sent, waiting, turned down, or refused by the checks: it used a chase."""
        return self.status in _COUNTED or (
            self.status == "canceled" and self.canceled_reason == DRAFT_REFUSED
        )


@dataclass(frozen=True, slots=True)
class ChasePlan:
    state: ChaseState
    due_at: datetime | None
    used: int


def plan_chase(
    *,
    invite_status: str,
    rfq_status: str,
    sent_at: datetime | None,
    chases: Sequence[ChaseRow],
    schedule: Schedule,
    country: str | None,
    paused: datetime | None,
    now: datetime,
) -> ChasePlan:
    """Where an invite stands on chasing, and when the next chase is due."""
    counted = sorted((c for c in chases if c.counted), key=lambda c: c.created_at)
    used = len(counted)
    if invite_status != "sent" or rfq_status != "open" or sent_at is None:
        return ChasePlan("not_applicable", None, used)
    if not schedule.enabled:
        return ChasePlan("disabled", None, used)
    if any(c.status == "proposed" for c in counted):
        return ChasePlan("awaiting_approval", None, used)
    if used >= schedule.max_chases:
        return ChasePlan("done", None, used)
    if counted:
        last = counted[-1]
        due = add_business_days(last.sent_at or last.created_at, schedule.every, country)
    else:
        due = add_business_days(sent_at, schedule.first_after, country)
    if paused is not None and paused > due:
        due = paused
    return ChasePlan("paused" if paused is not None and paused > now else "scheduled", due, used)


# --- Settings and history (API) ----------------------------------------------------


def _settings(rfq: Rfq) -> FollowupSettings:
    return FollowupSettings(
        enabled=rfq.followups_enabled,
        first_after_business_days=rfq.followup_first_after_days,
        every_business_days=rfq.followup_every_days,
        max_chases=rfq.followup_max_chases,
        response_due=rfq.response_due,
    )


def _schedule(rfq: Rfq) -> Schedule:
    return Schedule(
        enabled=rfq.followups_enabled,
        first_after=rfq.followup_first_after_days,
        every=rfq.followup_every_days,
        max_chases=rfq.followup_max_chases,
    )


async def _view(session: AsyncSession, rfq: Rfq, now: datetime) -> RfqFollowups:
    rows = (
        await session.execute(
            select(Invite, Supplier.name, Supplier.country)
            .join(Supplier, Supplier.id == Invite.supplier_id)
            .where(Invite.rfq_id == rfq.id)
            .order_by(Invite.created_at, Invite.id)
        )
    ).all()
    followups = list(
        await session.scalars(
            select(FollowupMessage)
            .join(Invite, Invite.id == FollowupMessage.invite_id)
            .where(Invite.rfq_id == rfq.id)
            .order_by(FollowupMessage.created_at, FollowupMessage.sequence)
        )
    )
    schedule = _schedule(rfq)
    invites: list[InviteFollowups] = []
    for invite, supplier_name, country in rows:
        mine = [f for f in followups if f.invite_id == invite.id]
        plan = plan_chase(
            invite_status=invite.status,
            rfq_status=rfq.status,
            sent_at=invite.sent_at,
            chases=[
                ChaseRow(f.status, f.created_at, f.sent_at, f.canceled_reason)
                for f in mine
                if f.kind == "chase"
            ],
            schedule=schedule,
            country=country,
            paused=invite.followups_paused_until,
            now=now,
        )
        invites.append(
            InviteFollowups(
                invite_id=invite.id,
                supplier_name=supplier_name,
                invite_status=invite.status,
                sent_at=invite.sent_at,
                chase_state=plan.state,
                next_chase_at=plan.due_at,
                paused_until=invite.followups_paused_until,
                chases_used=plan.used,
                messages=[
                    FollowupMessageView(
                        id=f.id,
                        kind=cast(FollowupKindName, f.kind),
                        sequence=f.sequence,
                        status=cast(FollowupStatus, f.status),
                        approval_id=f.approval_id,
                        canceled_reason=f.canceled_reason,
                        created_at=f.created_at,
                        sent_at=f.sent_at,
                    )
                    for f in mine
                ],
            )
        )
    return RfqFollowups(rfq_id=rfq.id, settings=_settings(rfq), invites=invites)


async def get_followups(db: Db, principal: Principal, rfq_id: UUID) -> RfqFollowups:
    async with tenant_session(db, principal.tenant_id) as session:
        rfq = await session.get(Rfq, rfq_id)
        if rfq is None:
            raise NotFound(f"rfq {rfq_id} not found")
        return await _view(session, rfq, datetime.now(UTC))


async def update_followups(
    db: Db, principal: Principal, rfq_id: UUID, changes: FollowupSettingsUpdate
) -> RfqFollowups:
    async with tenant_session(db, principal.tenant_id) as session:
        rfq = await session.scalar(select(Rfq).where(Rfq.id == rfq_id).with_for_update())
        if rfq is None:
            raise NotFound(f"rfq {rfq_id} not found")
        sent = changes.model_fields_set
        if "enabled" in sent and changes.enabled is not None:
            rfq.followups_enabled = changes.enabled
        if "first_after_business_days" in sent and changes.first_after_business_days is not None:
            rfq.followup_first_after_days = changes.first_after_business_days
        if "every_business_days" in sent and changes.every_business_days is not None:
            rfq.followup_every_days = changes.every_business_days
        if "max_chases" in sent and changes.max_chases is not None:
            rfq.followup_max_chases = changes.max_chases
        if "response_due" in sent:
            rfq.response_due = changes.response_due
        await session.flush()
        logger.info(
            "followup settings updated tenant=%s rfq=%s by=%s",
            principal.tenant_id,
            rfq_id,
            principal.user_id,
        )
        return await _view(session, rfq, datetime.now(UTC))


# --- The sweep -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SweepResult:
    canceled: int = 0
    chases: int = 0
    clarifications: int = 0
    skipped: int = 0
    # Drafts the checks refused, now held for the buyer.
    held: int = 0


@dataclass(frozen=True, slots=True)
class _Due:
    """One follow-up to write and propose. Everything the writer and the email need."""

    kind: FollowupKind
    sequence: int
    proposal_id: UUID | None
    invite_id: UUID
    rfq_id: UUID
    sender_user_id: UUID
    to: str
    subject: str
    task: FollowupEmailInput
    # Who signs: the writer does not (`bido_back.templates.signature`).
    buyer_name: str
    buyer_company: str


async def sweep_tenant(
    db: Db,
    tenant_id: UUID,
    *,
    writer: FollowupWriter,
    now: datetime | None = None,
) -> SweepResult:
    at = now or datetime.now(UTC)
    async with tenant_session(db, tenant_id) as session:
        canceled = await _reconcile(session)
        due = await _due_followups(session, tenant_id, at)

    proposed = {"chase": 0, "clarification": 0}
    skipped = 0
    held = 0
    sink = PostgresApprovalSink(db)
    for item in due:
        context = ToolContext(tenant_id=tenant_id, user_id=item.sender_user_id, run_id=uuid4())
        try:
            draft = await writer(item.task, context)
        except AgentFailed:
            # The model is down or answered nothing usable: the next sweep tries again.
            skipped += 1
            continue
        problems = followup_email_problems(item.task, draft)
        if problems:
            logger.warning(
                "followup draft refused tenant=%s invite=%s kind=%s problems=%s",
                tenant_id,
                item.invite_id,
                item.kind,
                ",".join(problems),
            )
            if await _hold(db, tenant_id, item, problems):
                held += 1
            else:
                skipped += 1
            continue
        body = signed(draft.body.strip(), item.buyer_name, item.buyer_company)
        if await _propose(db, sink, tenant_id, item, body):
            proposed[item.kind] += 1

    result = SweepResult(
        canceled=canceled,
        chases=proposed["chase"],
        clarifications=proposed["clarification"],
        skipped=skipped,
        held=held,
    )
    if result != SweepResult():
        logger.info(
            "followups swept tenant=%s canceled=%d chases=%d clarifications=%d skipped=%d held=%d",
            tenant_id,
            result.canceled,
            result.chases,
            result.clarifications,
            result.skipped,
            result.held,
        )
    return result


async def _reconcile(session: AsyncSession) -> int:
    """Cancel proposed follow-ups whose reason is gone (see the module docstring)."""
    stale_chases = list(
        await session.scalars(
            select(FollowupMessage)
            .join(Invite, Invite.id == FollowupMessage.invite_id)
            .join(Rfq, Rfq.id == Invite.rfq_id)
            .where(
                FollowupMessage.kind == "chase",
                FollowupMessage.status == "proposed",
                (Invite.status != "sent")
                | (Rfq.status != "open")
                | (Rfq.negotiation_status == "closed"),
            )
            .with_for_update(of=FollowupMessage)
        )
    )
    latest = (
        select(Proposal.invite_id, func.max(Proposal.version).label("version"))
        .group_by(Proposal.invite_id)
        .subquery()
    )
    stale_clarifications = list(
        await session.scalars(
            select(FollowupMessage)
            .join(Proposal, Proposal.id == FollowupMessage.proposal_id)
            .join(latest, latest.c.invite_id == Proposal.invite_id)
            .where(
                FollowupMessage.kind == "clarification",
                FollowupMessage.status == "proposed",
                Proposal.version < latest.c.version,
            )
            .with_for_update(of=FollowupMessage)
        )
    )
    count = 0
    if stale_chases:
        count += await cancel_followups(session, stale_chases, "invite_not_waiting")
    if stale_clarifications:
        count += await cancel_followups(session, stale_clarifications, "newer_offer")
    return count


async def _due_followups(session: AsyncSession, tenant_id: UUID, now: datetime) -> list[_Due]:
    rows = (
        await session.execute(
            select(Invite, Rfq, Supplier, Contact.email)
            .join(Rfq, Rfq.id == Invite.rfq_id)
            .join(Supplier, Supplier.id == Invite.supplier_id)
            .join(Contact, Contact.id == Invite.contact_id)
            .where(
                Rfq.status == "open",
                Rfq.negotiation_status != "closed",
                Invite.status.in_(("sent", "responded")),
            )
            .order_by(Invite.sent_at, Invite.id)
        )
    ).all()
    if not rows:
        return []
    invite_ids = [invite.id for invite, *_ in rows]
    followups = list(
        await session.scalars(
            select(FollowupMessage).where(FollowupMessage.invite_id.in_(invite_ids))
        )
    )
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        return []

    due: list[_Due] = []
    for invite, rfq, supplier, contact_email in rows:
        if not contact_email or invite.thread_message_id is None:
            continue
        mine = [f for f in followups if f.invite_id == invite.id]
        if invite.status == "sent":
            plan = plan_chase(
                invite_status=invite.status,
                rfq_status=rfq.status,
                sent_at=invite.sent_at,
                chases=[
                    ChaseRow(f.status, f.created_at, f.sent_at, f.canceled_reason)
                    for f in mine
                    if f.kind == "chase"
                ],
                schedule=_schedule(rfq),
                country=supplier.country,
                paused=invite.followups_paused_until,
                now=now,
            )
            if plan.state != "scheduled" or plan.due_at is None or plan.due_at > now:
                continue
            sequence = max((f.sequence for f in mine if f.kind == "chase"), default=0) + 1
            built = await _build(
                session,
                tenant,
                invite,
                rfq,
                supplier,
                contact_email,
                kind="chase",
                sequence=sequence,
                proposal_id=None,
                items=[],
            )
        else:
            proposal = await session.scalar(
                select(Proposal)
                .where(Proposal.invite_id == invite.id)
                .order_by(Proposal.version.desc())
                .limit(1)
            )
            if proposal is None or any(f.proposal_id == proposal.id for f in mine):
                continue
            items = await _open_items(session, rfq.id, proposal)
            if not items:
                continue
            built = await _build(
                session,
                tenant,
                invite,
                rfq,
                supplier,
                contact_email,
                kind="clarification",
                sequence=proposal.version,
                proposal_id=proposal.id,
                items=items,
            )
        if built is not None:
            due.append(built)
    return due


async def _open_items(
    session: AsyncSession, rfq_id: UUID, proposal: Proposal
) -> list[FollowupItem]:
    review = proposal.fields.get("_review")
    if isinstance(review, dict) and cast(dict[str, object], review).get("declined") is True:
        return []
    requested = [
        RequestedLine(
            id=line.id, position=line.position, description=line.description, mpn=line.mpn
        )
        for line in await session.scalars(select(RfqLine).where(RfqLine.rfq_id == rfq_id))
    ]
    offered = [
        OfferedLine(
            rfq_line_id=line.rfq_line_id,
            values={
                "unit_price": line.unit_price,
                "min_quantity": line.min_quantity,
                "lead_time_days": line.lead_time_days,
            },
            fields=line.fields,
        )
        for line in await session.scalars(
            select(ProposalLine).where(ProposalLine.proposal_id == proposal.id)
        )
    ]
    return open_items(
        header_values={"currency": proposal.currency, "valid_until": proposal.valid_until},
        header_fields=proposal.fields,
        requested=requested,
        offered=offered,
    )


async def _build(
    session: AsyncSession,
    tenant: Tenant,
    invite: Invite,
    rfq: Rfq,
    supplier: Supplier,
    contact_email: str,
    *,
    kind: FollowupKind,
    sequence: int,
    proposal_id: UUID | None,
    items: list[FollowupItem],
) -> _Due | None:
    # Whose mailbox: the buyer who requested the RFQ email (its executed approval).
    original = await session.scalar(
        select(Approval).where(
            Approval.tool == SEND_RFQ_EMAIL,
            Approval.tool_call_id == send_proposal_ids(invite.id)[1],
            Approval.status == "executed",
        )
    )
    if original is None:
        logger.warning("followup: no executed send for invite=%s", invite.id)
        return None
    sender_raw = original.args.get("sender_user_id")
    try:
        sender_user_id = UUID(str(sender_raw))
    except ValueError:
        return None
    user = await session.get(User, sender_user_id)
    if user is None:
        return None
    first_subject = await session.scalar(
        select(Message.subject).where(
            Message.invite_id == invite.id,
            Message.direction == "out",
            Message.message_id_header == invite.thread_message_id,
        )
    )
    return _Due(
        kind=kind,
        sequence=sequence,
        proposal_id=proposal_id,
        invite_id=invite.id,
        rfq_id=rfq.id,
        sender_user_id=sender_user_id,
        to=contact_email,
        subject=reply_subject(first_subject, rfq.ref, rfq.title),
        task=FollowupEmailInput(
            kind=kind,
            rfq_ref=rfq.ref,
            rfq_title=rfq.title,
            supplier_name=supplier.name,
            # The thread's language: the one its RFQ email went out in.
            supplier_language=thread_language(original.args, supplier),
            chase_number=sequence if kind == "chase" else 0,
            original_sent_on=invite.sent_at.date() if invite.sent_at else None,
            response_due=rfq.response_due,
            open_items=items,
        ),
        buyer_name=await buyer_display_name(session, user, tenant.name),
        buyer_company=tenant.name,
    )


def reply_subject(original: str | None, rfq_ref: str, title: str) -> str:
    """`Re: <the RFQ email's subject>`, always carrying the `[RFQ-n]` marker, one line."""
    base = " ".join((original or f"{subject_marker(rfq_ref)} {title}").split())
    if subject_marker(rfq_ref) not in base:
        base = f"{subject_marker(rfq_ref)} {base}"
    subject = base if base.lower().startswith(_REPLY_PREFIXES) else f"Re: {base}"
    return subject[:300]


async def _propose(
    db: Db, sink: PostgresApprovalSink, tenant_id: UUID, item: _Due, body: str
) -> bool:
    """Record the approval, then the follow-up row; once per (invite, kind, sequence)."""
    row_id = followup_id(item.invite_id, item.kind, item.sequence)
    args = SendFollowupEmailArgs(
        followup_id=row_id,
        invite_id=item.invite_id,
        kind=item.kind,
        sender_user_id=item.sender_user_id,
        to=[item.to],
        subject=item.subject,
        body=body,
    )
    payload = cast(dict[str, JsonValue], args.model_dump(mode="json"))
    run_id, call_id = followup_proposal_ids(row_id)
    try:
        await sink.request_approval(
            ApprovalProposal(
                tenant_id=tenant_id,
                run_id=run_id,
                session_id=f"rfq:{item.rfq_id}",
                tool_call_id=call_id,
                tool=SEND_FOLLOWUP_EMAIL,
                args=payload,
                args_hash=args_hash(payload),
                requested_by=item.sender_user_id,
            )
        )
    except ApprovalConflict:
        # Proposed before (a crash between the approval and the row, or a
        # concurrent sweep): the earlier approval stands and is linked below.
        logger.info("followup %s was already proposed", row_id)

    async with tenant_session(db, tenant_id) as session:
        approval_id = await session.scalar(
            select(Approval.id).where(
                Approval.run_id == str(run_id), Approval.tool_call_id == call_id
            )
        )
        if approval_id is None:
            return False
        inserted = await session.scalar(
            insert(FollowupMessage)
            .values(
                id=row_id,
                tenant_id=tenant_id,
                invite_id=item.invite_id,
                kind=item.kind,
                sequence=item.sequence,
                proposal_id=item.proposal_id,
                status="proposed",
                approval_id=approval_id,
            )
            .on_conflict_do_nothing()
            .returning(FollowupMessage.id)
        )
    return inserted is not None


async def _hold(db: Db, tenant_id: UUID, item: _Due, problems: Sequence[str]) -> bool:
    """Record a follow-up whose draft the checks refused, once, for the buyer to see.

    The row ends `canceled` with reason `draft_refused` (no approval: nothing
    was proposed), and an audit event puts it in the RFQ's activity feed with
    the problem codes. The row makes the follow-up done: the sweep does not
    call the writer again for it. True when the hold was recorded now.
    """
    row_id = followup_id(item.invite_id, item.kind, item.sequence)
    async with tenant_session(db, tenant_id) as session:
        inserted = await session.scalar(
            insert(FollowupMessage)
            .values(
                id=row_id,
                tenant_id=tenant_id,
                invite_id=item.invite_id,
                kind=item.kind,
                sequence=item.sequence,
                proposal_id=item.proposal_id,
                status="canceled",
                canceled_reason=DRAFT_REFUSED,
            )
            .on_conflict_do_nothing()
            .returning(FollowupMessage.id)
        )
        if inserted is None:
            return False
        session.add(
            AuditEvent(
                tenant_id=tenant_id,
                actor_user_id=None,
                action="rfq.followup_draft_held",
                payload={
                    "rfq_id": str(item.rfq_id),
                    "invite_id": str(item.invite_id),
                    "followup_id": str(row_id),
                    "kind": item.kind,
                    "sequence": item.sequence,
                    "problems": list(problems),
                },
            )
        )
    return True
