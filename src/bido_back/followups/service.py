"""RFQ follow-ups: chases, clarifications, reminders, and the sweeper.

Long waits are rows. Nothing sleeps: the `followups.sweep` job (every 15
minutes, see `bido_back.followups.jobs`) reads the rows and does what is due,
per tenant:

1. Reconcile: a proposed follow-up whose reason is gone is canceled and its
   pending approval expired: a chase once the supplier sent an offer (or
   declined), a clarification once a newer offer exists, any of them once the
   RFQ or its negotiation is closed or while the supplier waits for the buyer's
   answer to a question. A canceled clarification asked nothing (its asks are
   withdrawn in the ledger).
2. Per supplier (`bido_back.followups.ledger.supplier_followups`), one email at a
   time and nothing while it waits for the buyer:
   - an open question from the supplier pauses every automatic email until the
     buyer answers it (`bido_back.followups.questions`);
   - an email already waiting for approval (a follow-up or a negotiation email)
     is the next one: nothing else is proposed;
   - no offer yet (the RFQ email unanswered, or answered without an offer):
     chases, the first on the RFQ's response date at the earliest and
     `first_after` business days after the RFQ email, then every `every`
     business days, at most `max_chases` (rejected chases count), never within
     `every` business days of the supplier's last email, never before
     `invites.followups_paused_until` (an auto-reply);
   - an email the supplier has not answered (a clarification whose asks are
     open, a counter-offer or best-and-final request for its latest offer): one
     reminder, the business day after that email's reply-by date (never before),
     and nothing else until the supplier answers;
   - otherwise, what the latest offer still lacks (`bido_back.followups.missing`)
     and was never asked goes out in ONE clarification, asked once. Items asked
     before are never asked again: they need the buyer's decision. A held offer
     (its latest reply could not be applied) needs the buyer's decision first.

A due follow-up is written by the follow-up writer agent, checked
(`followup_email_problems`), and recorded as a PENDING approval of
`send_followup_email` requested on behalf of the buyer who sent the RFQ email,
exactly like that first send; the worker sends it once, in the invite's thread
(`bido_back.followups.send`). A clarification records its asks in the ledger
with the approval; they open when it goes out. Each clarification and reminder
carries a reply-by date: two business days in the supplier's calendar, never
after the negotiation deadline.

Intake calls `cancel_on_reply` (a stored reply makes a proposed chase or
reminder moot) and `pause_on_auto_reply` (an out-of-office) in its own
transaction; quote extraction stores the supplier's questions and settles the
asks its reply answers.

Logs carry ids and counts only.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Final, cast
from uuid import UUID, uuid4

from pydantic import JsonValue
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.agents.approvals import ApprovalProposal
from bido_back.agents.catalog.contracts import FollowupEmailInput, FollowupItem, FollowupKind
from bido_back.agents.catalog.definitions import followup_email_problems
from bido_back.agents.context import ToolContext
from bido_back.agents.events import args_hash
from bido_back.approvals.errors import ApprovalConflict
from bido_back.approvals.sink import PostgresApprovalSink
from bido_back.auth import Principal
from bido_back.db import tenant_session
from bido_back.db.models import (
    Approval,
    Contact,
    FollowupMessage,
    Invite,
    Message,
    Proposal,
    Rfq,
    Supplier,
    SupplierQuestion,
    Tenant,
    User,
)
from bido_back.followups import ledger
from bido_back.followups import missing as missing_rules
from bido_back.followups.agents import FollowupWriter
from bido_back.followups.calendar import add_business_days, is_business_day
from bido_back.followups.hooks import AUTOMATIC, cancel_followups
from bido_back.followups.missing import ExpectedDelivery, OpenItem
from bido_back.followups.schemas import (
    AskView,
    ChaseState,
    DecisionView,
    FollowupKindView,
    FollowupMessageView,
    FollowupSettings,
    FollowupSettingsUpdate,
    FollowupStatus,
    InviteFollowups,
    OpenItemView,
    RfqFollowups,
    SupplierQuestionView,
    WaitingView,
)
from bido_back.followups.send import (
    SEND_FOLLOWUP_EMAIL,
    FollowupKindName,
    SendFollowupEmailArgs,
    followup_id,
    followup_proposal_ids,
)
from bido_back.rfqs.errors import AgentFailed, NotFound
from bido_back.rfqs.language import supplier_email_language
from bido_back.rfqs.send import SEND_RFQ_EMAIL, send_proposal_ids, subject_marker
from bido_back.rfqs.service import buyer_display_name

logger = logging.getLogger(__name__)

Db = AsyncEngine | async_sessionmaker[AsyncSession]

# Chases that count toward `max_chases`: sent, waiting, or turned down.
_COUNTED: Final = ("proposed", "sent", "rejected")
_REPLY_PREFIXES: Final = ("re:", "aw:", "sv:", "réf:", "rif:", "res:")
# How long a supplier has to answer a clarification or a reminder.
REPLY_BUSINESS_DAYS: Final = 2


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


@dataclass(frozen=True, slots=True)
class ChasePlan:
    state: ChaseState
    due_at: datetime | None
    used: int


def on_business_day(moment: datetime, country: str | None) -> datetime:
    """`moment`, or the next business day at the same time."""
    while not is_business_day(moment.date(), country):
        moment += timedelta(days=1)
    return moment


def first_chase_due(
    *, sent_at: datetime, first_after: int, response_due: date | None, country: str | None
) -> datetime:
    """The first chase: `first_after` business days after the RFQ email, and never
    before the RFQ's own response date (on that day, at the hour the RFQ went out)."""
    due = add_business_days(sent_at, first_after, country)
    if response_due is not None:
        on_due_day = datetime.combine(response_due, sent_at.timetz())
        due = max(due, on_business_day(on_due_day, country))
    return due


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
    response_due: date | None = None,
    last_reply_at: datetime | None = None,
    awaiting_offer: bool | None = None,
) -> ChasePlan:
    """Where an invite stands on chasing, and when the next chase is due.

    `awaiting_offer`: the supplier has sent no offer yet (by default: the invite is
    still `sent`); a reply without an offer does not stop chasing.
    """
    counted = sorted((c for c in chases if c.status in _COUNTED), key=lambda c: c.created_at)
    used = len(counted)
    waiting = awaiting_offer if awaiting_offer is not None else invite_status == "sent"
    if not waiting or rfq_status != "open" or sent_at is None:
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
        due = first_chase_due(
            sent_at=sent_at,
            first_after=schedule.first_after,
            response_due=response_due,
            country=country,
        )
    if last_reply_at is not None:
        # Never right after the supplier wrote ("we will send it next week").
        due = max(due, add_business_days(last_reply_at, schedule.every, country))
    if paused is not None and paused > due:
        due = paused
    return ChasePlan("paused" if paused is not None and paused > now else "scheduled", due, used)


def plan_reminder(
    request: ledger.OpenRequest,
    *,
    country: str | None,
    every: int,
    last_reply_at: datetime | None,
) -> datetime | None:
    """When to remind the supplier, once, of our email it has not answered: the
    business day after its reply-by date (never before), at the hour it went out.
    None: reminded already, or nothing to refer to."""
    if request.reminded or request.reply_by is None or request.message_row_id is None:
        return None
    day_after = request.reply_by + timedelta(days=1)
    due = on_business_day(datetime.combine(day_after, request.sent_at.timetz()), country)
    if last_reply_at is not None and last_reply_at > request.sent_at:
        # The supplier wrote since (no offer yet): give it time.
        due = max(due, add_business_days(last_reply_at, every, country))
    return due


def reply_by_date(now: datetime, country: str | None, deadline: datetime | None) -> date:
    """The date an email asks for an answer by: two business days in the supplier's
    calendar, never after the negotiation deadline."""
    due = add_business_days(now, REPLY_BUSINESS_DAYS, country).date()
    if deadline is not None and deadline.date() < due:
        due = max(deadline.date(), now.date())
    return due


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


async def _last_inbound(session: AsyncSession, invite_ids: Sequence[UUID]) -> dict[UUID, datetime]:
    """When each supplier last wrote (auto-replies and bounces aside)."""
    if not invite_ids:
        return {}
    rows = await session.execute(
        select(Message.invite_id, func.max(Message.created_at))
        .where(
            Message.invite_id.in_(invite_ids),
            Message.direction == "in",
            Message.status != "quarantined",
        )
        .group_by(Message.invite_id)
    )
    return {invite_id: at for invite_id, at in rows if invite_id is not None}


def _chase_plan(
    invite: Invite,
    rfq: Rfq,
    country: str | None,
    followups: Sequence[FollowupMessage],
    state: ledger.SupplierFollowup | None,
    last_reply_at: datetime | None,
    now: datetime,
) -> ChasePlan:
    chases = [ChaseRow(f.status, f.created_at, f.sent_at) for f in followups if f.kind == "chase"]
    return plan_chase(
        invite_status=invite.status,
        rfq_status=rfq.status,
        sent_at=invite.sent_at,
        chases=chases,
        schedule=_schedule(rfq),
        country=country,
        paused=invite.followups_paused_until,
        now=now,
        response_due=rfq.response_due,
        last_reply_at=last_reply_at,
        awaiting_offer=invite.status in ("sent", "responded")
        and (state is None or not state.has_offer),
    )


def _ask_views(state: ledger.SupplierFollowup) -> list[AskView]:
    return [
        AskView(
            position=a.position,
            field=a.field,
            question=a.question,
            need_by=a.need_by,
            status=a.status,
            asked_at=a.asked_at,
            reply_by=a.reply_by,
            settled_at=a.settled_at,
        )
        for a in state.asks
    ]


def _state_views(
    state: ledger.SupplierFollowup | None,
) -> dict[str, object]:
    if state is None:
        return {}
    request = state.request
    return {
        "phase": state.phase,
        "waiting_for_supplier": WaitingView(
            kind=request.kind,
            sent_at=request.sent_at,
            reply_by=request.reply_by,
            reminded_at=request.reminded_at,
        )
        if request is not None
        else None,
        "asks": _ask_views(state),
        "open_items": [
            OpenItemView(
                position=i.position,
                field=i.field,
                question=i.question,
                need_by=i.need_by,
                expected_delivery=i.expected,
            )
            for i in state.to_ask
        ],
        "questions": [
            SupplierQuestionView(
                id=q.id,
                question=q.text,
                status=q.status,
                asked_at=q.asked_at,
                answer_followup_id=q.answer_followup_id,
                decided_at=q.decided_at,
            )
            for q in state.questions
        ],
        "needs_your_decision": [
            DecisionView(
                kind=d.kind,
                position=d.position,
                field=d.field,
                reasons=list(d.reasons),
                since=d.since,
            )
            for d in state.decisions
        ],
    }


async def _view(
    session: AsyncSession,
    rfq: Rfq,
    now: datetime,
    expected_delivery: ExpectedDelivery | None = None,
) -> RfqFollowups:
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
    helper = expected_delivery or missing_rules.DEFAULT_EXPECTED_DELIVERY
    states = await ledger.supplier_followups(session, rfq.id, now=now, expected_delivery=helper)
    last_reply = await _last_inbound(session, [invite.id for invite, _, _ in rows])
    invites: list[InviteFollowups] = []
    for invite, supplier_name, country in rows:
        mine = [f for f in followups if f.invite_id == invite.id]
        state = states.get(invite.id)
        plan = _chase_plan(invite, rfq, country, mine, state, last_reply.get(invite.id), now)
        invites.append(
            InviteFollowups.model_validate(
                {
                    "invite_id": invite.id,
                    "supplier_name": supplier_name,
                    "invite_status": invite.status,
                    "sent_at": invite.sent_at,
                    "chase_state": plan.state,
                    "next_chase_at": plan.due_at,
                    "paused_until": invite.followups_paused_until,
                    "chases_used": plan.used,
                    "messages": [
                        FollowupMessageView(
                            id=f.id,
                            kind=cast(FollowupKindView, f.kind),
                            sequence=f.sequence,
                            status=cast(FollowupStatus, f.status),
                            approval_id=f.approval_id,
                            canceled_reason=f.canceled_reason,
                            created_at=f.created_at,
                            sent_at=f.sent_at,
                            reply_by=f.reply_by,
                        )
                        for f in mine
                    ],
                    **_state_views(state),
                }
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
    reminders: int = 0


@dataclass(frozen=True, slots=True)
class _Due:
    """One follow-up to write and propose. Everything the writer and the email need."""

    kind: FollowupKindName
    sequence: int
    proposal_id: UUID | None
    invite_id: UUID
    rfq_id: UUID
    sender_user_id: UUID
    to: str
    subject: str
    task: FollowupEmailInput
    # A clarification: what it asks (recorded in the ledger with the proposal).
    items: tuple[OpenItem, ...] = ()
    reply_by: date | None = None
    # A reminder: our outbound email it is about.
    regarding_message_id: UUID | None = None


async def sweep_tenant(
    db: Db,
    tenant_id: UUID,
    *,
    writer: FollowupWriter,
    now: datetime | None = None,
    expected_delivery: ExpectedDelivery | None = None,
) -> SweepResult:
    at = now or datetime.now(UTC)
    helper = expected_delivery or missing_rules.DEFAULT_EXPECTED_DELIVERY
    async with tenant_session(db, tenant_id) as session:
        canceled = await _reconcile(session)
        due = await _due_followups(session, tenant_id, at, helper)

    proposed: dict[str, int] = {"chase": 0, "clarification": 0, "reminder": 0}
    skipped = 0
    sink = PostgresApprovalSink(db)
    for item in due:
        context = ToolContext(tenant_id=tenant_id, user_id=item.sender_user_id, run_id=uuid4())
        try:
            draft = await writer(item.task, context)
        except AgentFailed:
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
            skipped += 1
            continue
        if await _propose(db, sink, tenant_id, item, draft.body.strip()):
            proposed[item.kind] += 1

    result = SweepResult(
        canceled=canceled,
        chases=proposed["chase"],
        clarifications=proposed["clarification"],
        reminders=proposed["reminder"],
        skipped=skipped,
    )
    if result != SweepResult():
        logger.info(
            "followups swept tenant=%s canceled=%d chases=%d clarifications=%d reminders=%d "
            "skipped=%d",
            tenant_id,
            result.canceled,
            result.chases,
            result.clarifications,
            result.reminders,
            result.skipped,
        )
    return result


async def _reconcile(session: AsyncSession) -> int:
    """Cancel proposed follow-ups whose reason is gone (see the module docstring)."""
    pending = (
        await session.execute(
            select(FollowupMessage, Invite, Rfq)
            .join(Invite, Invite.id == FollowupMessage.invite_id)
            .join(Rfq, Rfq.id == Invite.rfq_id)
            .where(FollowupMessage.status == "proposed", FollowupMessage.kind.in_(AUTOMATIC))
            .with_for_update(of=FollowupMessage)
        )
    ).all()
    if not pending:
        return 0
    invite_ids = list({invite.id for _, invite, _ in pending})
    latest: dict[UUID, int] = {
        invite_id: version
        for invite_id, version in await session.execute(
            select(Proposal.invite_id, func.max(Proposal.version))
            .where(Proposal.invite_id.in_(invite_ids))
            .group_by(Proposal.invite_id)
        )
    }
    asked_for = [row.proposal_id for row, _, _ in pending if row.proposal_id is not None]
    versions: dict[UUID, int] = (
        {
            proposal_id: version
            for proposal_id, version in await session.execute(
                select(Proposal.id, Proposal.version).where(Proposal.id.in_(asked_for))
            )
        }
        if asked_for
        else {}
    )
    questioned = set(
        await session.scalars(
            select(SupplierQuestion.invite_id).where(
                SupplierQuestion.invite_id.in_(invite_ids), SupplierQuestion.status == "open"
            )
        )
    )
    stale: dict[str, list[FollowupMessage]] = {}
    for row, invite, rfq in pending:
        closed = rfq.status != "open" or rfq.negotiation_status == "closed"
        reason: str | None = None
        if invite.id in questioned:
            reason = "supplier_question"
        elif row.kind == "chase":
            waiting = invite.status in ("sent", "responded") and invite.id not in latest
            reason = "invite_not_waiting" if closed or not waiting else None
        elif closed:
            reason = "rfq_closed"
        elif row.kind == "clarification" and row.proposal_id is not None:
            version = versions.get(row.proposal_id)
            if version is not None and version < latest.get(invite.id, version):
                reason = "newer_offer"
        if reason is not None:
            stale.setdefault(reason, []).append(row)
    count = 0
    for reason, rows in stale.items():
        count += await cancel_followups(session, rows, reason)
    return count


async def _due_followups(
    session: AsyncSession,
    tenant_id: UUID,
    now: datetime,
    expected_delivery: ExpectedDelivery | None,
) -> list[_Due]:
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
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        return []
    invite_ids = [invite.id for invite, *_ in rows]
    followups = list(
        await session.scalars(
            select(FollowupMessage).where(FollowupMessage.invite_id.in_(invite_ids))
        )
    )
    states: dict[UUID, ledger.SupplierFollowup] = {}
    for rfq_id in dict.fromkeys(rfq.id for _, rfq, _, _ in rows):
        states.update(
            await ledger.supplier_followups(
                session, rfq_id, now=now, expected_delivery=expected_delivery
            )
        )
    last_reply = await _last_inbound(session, invite_ids)

    due: list[_Due] = []
    for invite, rfq, supplier, contact_email in rows:
        state = states.get(invite.id)
        if not contact_email or invite.thread_message_id is None or state is None:
            continue
        if state.open_questions or state.pending:
            # The supplier waits for the buyer's answer, or an email waits for approval.
            continue
        mine = [f for f in followups if f.invite_id == invite.id]
        target = _Target(tenant, invite, rfq, supplier, contact_email)
        if not state.has_offer:
            plan = _chase_plan(
                invite, rfq, supplier.country, mine, state, last_reply.get(invite.id), now
            )
            if plan.state != "scheduled" or plan.due_at is None or plan.due_at > now:
                continue
            # The RFQ's reply-by date is today or past: the chase asks for a new one.
            built = await _build(
                session,
                target,
                kind="chase",
                writer_kind="chase",
                sequence=_next_sequence(mine, "chase"),
                chase_number=_next_sequence(mine, "chase"),
                reply_by=reply_by_date(now, supplier.country, rfq.negotiation_deadline),
            )
        elif state.request is not None:
            built = await _reminder(session, target, state.request, mine, last_reply, now)
        elif state.to_ask and not any(d.kind == "offer_held" for d in state.decisions):
            built = await _clarification(session, target, state.to_ask, mine, now)
        else:
            continue
        if built is not None:
            due.append(built)
    return due


@dataclass(frozen=True, slots=True)
class _Target:
    tenant: Tenant
    invite: Invite
    rfq: Rfq
    supplier: Supplier
    contact_email: str


def _next_sequence(rows: Sequence[FollowupMessage], kind: str) -> int:
    return max((f.sequence for f in rows if f.kind == kind), default=0) + 1


async def _clarification(
    session: AsyncSession,
    target: _Target,
    items: Sequence[OpenItem],
    mine: Sequence[FollowupMessage],
    now: datetime,
) -> _Due | None:
    """One clarification asking every item the latest offer lacks and nobody asked."""
    proposal_id = await session.scalar(
        select(Proposal.id)
        .where(Proposal.invite_id == target.invite.id)
        .order_by(Proposal.version.desc())
        .limit(1)
    )
    if proposal_id is None:
        return None
    reply_by = reply_by_date(now, target.supplier.country, target.rfq.negotiation_deadline)
    return await _build(
        session,
        target,
        kind="clarification",
        writer_kind="clarification",
        sequence=_next_sequence(mine, "clarification"),
        proposal_id=proposal_id,
        items=tuple(items),
        open_items=[item.followup_item() for item in items],
        reply_by=reply_by,
    )


async def _reminder(
    session: AsyncSession,
    target: _Target,
    request: ledger.OpenRequest,
    mine: Sequence[FollowupMessage],
    last_reply: Mapping[UUID, datetime],
    now: datetime,
) -> _Due | None:
    """Once, after its reply-by date: our email the supplier has not answered."""
    due_at = plan_reminder(
        request,
        country=target.supplier.country,
        every=target.rfq.followup_every_days,
        last_reply_at=last_reply.get(target.invite.id),
    )
    if due_at is None or due_at > now:
        return None
    # A clarification is asked again as it was; a counter-offer is recalled, no numbers.
    open_items: list[FollowupItem] = []
    if request.kind == "clarification":
        labels = await _line_labels(session, target.rfq.id)
        open_items = [
            FollowupItem(
                position=ask.position,
                line=labels.get(ask.position) if ask.position is not None else None,
                field=ask.field,
                problem="missing" if ask.question in ("value", "price_or_decline") else "unclear",
                question=ask.question,
                need_by=ask.need_by,
            )
            for ask in request.asks
        ]
    return await _build(
        session,
        target,
        kind="reminder",
        writer_kind="clarification" if request.kind == "clarification" else "chase",
        sequence=_next_sequence(mine, "reminder"),
        chase_number=1,
        open_items=open_items,
        reply_by=reply_by_date(now, target.supplier.country, target.rfq.negotiation_deadline),
        reminder_of=request.sent_at.date(),
        regarding_message_id=request.message_row_id,
    )


async def _line_labels(session: AsyncSession, rfq_id: UUID) -> dict[int, str]:
    return {line.position: line.label for line in await ledger.requested_lines(session, rfq_id)}


async def _build(
    session: AsyncSession,
    target: _Target,
    *,
    kind: FollowupKindName,
    writer_kind: FollowupKind,
    sequence: int,
    proposal_id: UUID | None = None,
    items: tuple[OpenItem, ...] = (),
    open_items: Sequence[FollowupItem] = (),
    chase_number: int = 0,
    reply_by: date | None = None,
    reminder_of: date | None = None,
    regarding_message_id: UUID | None = None,
) -> _Due | None:
    invite, rfq, supplier, tenant = target.invite, target.rfq, target.supplier, target.tenant
    # Whose mailbox: the buyer who requested the RFQ email (its executed approval).
    sender_user_id = await rfq_sender(session, invite.id)
    if sender_user_id is None:
        logger.warning("followup: no executed send for invite=%s", invite.id)
        return None
    user = await session.get(User, sender_user_id)
    if user is None:
        return None
    return _Due(
        kind=kind,
        sequence=sequence,
        proposal_id=proposal_id,
        invite_id=invite.id,
        rfq_id=rfq.id,
        sender_user_id=sender_user_id,
        to=target.contact_email,
        subject=await thread_subject(session, invite, rfq),
        task=FollowupEmailInput(
            kind=writer_kind,
            rfq_ref=rfq.ref,
            rfq_title=rfq.title,
            buyer_company=tenant.name,
            buyer_name=await buyer_display_name(session, user, tenant.name),
            supplier_name=supplier.name,
            supplier_language=supplier_email_language(supplier),
            chase_number=chase_number if writer_kind == "chase" else 0,
            original_sent_on=invite.sent_at.date() if invite.sent_at else None,
            # The date the supplier is asked to answer by: the RFQ's, or this email's own
            # once that one is today or past.
            response_due=_later(rfq.response_due, reply_by),
            open_items=list(open_items),
            reply_by=reply_by,
            reminder_of=reminder_of,
        ),
        items=items,
        reply_by=reply_by,
        regarding_message_id=regarding_message_id,
    )


def _later(first: date | None, second: date | None) -> date | None:
    dates = [d for d in (first, second) if d is not None]
    return max(dates) if dates else None


async def rfq_sender(session: AsyncSession, invite_id: UUID) -> UUID | None:
    """The buyer who requested the invite's RFQ email (its executed approval): every
    email in the thread goes out from their mailbox."""
    original = await session.scalar(
        select(Approval).where(
            Approval.tool == SEND_RFQ_EMAIL,
            Approval.tool_call_id == send_proposal_ids(invite_id)[1],
            Approval.status == "executed",
        )
    )
    if original is None:
        return None
    try:
        return UUID(str(original.args.get("sender_user_id")))
    except ValueError:
        return None


async def thread_subject(session: AsyncSession, invite: Invite, rfq: Rfq) -> str:
    first_subject = await session.scalar(
        select(Message.subject).where(
            Message.invite_id == invite.id,
            Message.direction == "out",
            Message.message_id_header == invite.thread_message_id,
        )
    )
    return reply_subject(first_subject, rfq.ref, rfq.title)


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
    """Record the approval, then the follow-up row (and a clarification's asks); once per
    (invite, kind, sequence)."""
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
                reply_by=item.reply_by,
                regarding_message_id=item.regarding_message_id,
            )
            .on_conflict_do_nothing()
            .returning(FollowupMessage.id)
        )
        if inserted is not None and item.items:
            await ledger.record_asks(
                session,
                tenant_id=tenant_id,
                invite_id=item.invite_id,
                followup_id=row_id,
                items=item.items,
                reply_by=item.reply_by,
            )
    return inserted is not None
