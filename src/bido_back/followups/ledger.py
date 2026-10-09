"""The clarification ledger: what Bido asked each supplier, and what each supplier asked.

Asks (`clarification_asks`, migration 0034): every item a clarification email
asks one supplier, at most once per (invite, requested line or the whole offer,
field), with its status:

- `proposed`: the email awaits approval; `withdrawn` if it is canceled or
  rejected (never asked, so it may be asked again);
- `open`: the email went out (`asked_at`, by `reply_by`);
- `answered` / `unusable`: settled by the supplier's first reply with an offer
  received after it went out: the item is no longer missing, or still is. An
  unusable answer is never asked again automatically: it needs the buyer's
  decision;
- `resolved`: the buyer decided ("use the current offer").

A reply that answers open asks is applied to the offer as an amendment of
exactly those fields (`bido_back.quotes.state`, `OfferEvent.answers`). A reply
without an offer (an acknowledgement, "we will answer tomorrow") settles
nothing: the asks stay open.

Supplier questions (`supplier_questions`): the questions a supplier's reply asks
the buyer, `open` until the buyer answers or dismisses them. While a supplier
has an open question, Bido sends it nothing automatically.

`supplier_followups` tells, per supplier, what Bido waits for (the one open
request), which email waits for approval, what needs the buyer's answer and
what needs the buyer's decision: the follow-up sweep acts on it, the API and
the Copilot show it. Question and answer texts are customer content: never
logged.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Final, Literal, cast
from uuid import UUID

from pydantic import JsonValue
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bido_back.agents.catalog.contracts import FollowupField, FollowupQuestion
from bido_back.db.models import (
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
    SupplierQuestion,
)
from bido_back.followups.missing import (
    ExpectedDelivery,
    ItemKey,
    OfferedLine,
    OpenItem,
    RequestedLine,
    late_deliveries,
    open_items,
)
from bido_back.quotes.state import AskedField
from bido_back.quotes.terms import offer_terms

AskStatus = Literal["proposed", "open", "answered", "unusable", "withdrawn", "resolved"]
QuestionStatus = Literal["open", "answered", "dismissed"]
# Asks that still count as asked (never asked again automatically).
_ASKED: Final = ("proposed", "open", "answered", "unusable", "resolved")
# A held offer: these `_offer_state` keys say the latest reply could not be applied.
_HELD_KINDS: Final = frozenset({"clarification"})


@dataclass(frozen=True, slots=True)
class Ask:
    """One ledger row."""

    id: UUID
    invite_id: UUID
    followup_id: UUID
    rfq_line_id: UUID | None
    position: int | None
    field: FollowupField
    question: FollowupQuestion
    need_by: date | None
    status: AskStatus
    asked_at: datetime | None
    reply_by: date | None
    answered_by_message_id: UUID | None
    settled_at: datetime | None

    @property
    def key(self) -> ItemKey:
        return (self.rfq_line_id, self.field)

    @property
    def asked_field(self) -> AskedField:
        return AskedField(self.position, self.field)


def _ask(row: ClarificationAsk) -> Ask:
    return Ask(
        id=row.id,
        invite_id=row.invite_id,
        followup_id=row.followup_id,
        rfq_line_id=row.rfq_line_id,
        position=row.position,
        field=cast(FollowupField, row.field),
        question=cast(FollowupQuestion, row.question),
        need_by=row.need_by,
        status=cast(AskStatus, row.status),
        asked_at=row.asked_at,
        reply_by=row.reply_by,
        answered_by_message_id=row.answered_by_message_id,
        settled_at=row.settled_at,
    )


@dataclass(frozen=True, slots=True)
class Question:
    """One question a supplier asked the buyer."""

    id: UUID
    invite_id: UUID
    message_id: UUID
    ordinal: int
    text: str
    status: QuestionStatus
    asked_at: datetime
    answer_followup_id: UUID | None
    decided_by: UUID | None
    decided_at: datetime | None


def _question(row: SupplierQuestion, received_at: datetime) -> Question:
    return Question(
        id=row.id,
        invite_id=row.invite_id,
        message_id=row.message_id,
        ordinal=row.ordinal,
        text=row.question,
        status=cast(QuestionStatus, row.status),
        asked_at=received_at,
        answer_followup_id=row.answer_followup_id,
        decided_by=row.decided_by,
        decided_at=row.decided_at,
    )


# --- Asks: writing (in the caller's transaction) -----------------------------------------------


async def record_asks(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    invite_id: UUID,
    followup_id: UUID,
    items: Sequence[OpenItem],
    reply_by: date | None,
) -> None:
    """The items a just-proposed clarification asks (status `proposed`)."""
    for item in items:
        session.add(
            ClarificationAsk(
                tenant_id=tenant_id,
                invite_id=invite_id,
                followup_id=followup_id,
                rfq_line_id=item.rfq_line_id,
                position=item.position if item.rfq_line_id is not None else None,
                field=item.field,
                question=item.question,
                need_by=item.need_by,
                status="proposed",
                reply_by=reply_by,
            )
        )
    await session.flush()


async def mark_asked(session: AsyncSession, followup_id: UUID, at: datetime) -> int:
    """The clarification went out: its asks are open."""
    result = await session.execute(
        update(ClarificationAsk)
        .where(ClarificationAsk.followup_id == followup_id, ClarificationAsk.status == "proposed")
        .values(status="open", asked_at=at)
        .returning(ClarificationAsk.id)
    )
    return len(result.all())


async def withdraw_asks(session: AsyncSession, followup_ids: Sequence[UUID]) -> int:
    """The clarification will not go out (canceled or rejected): nothing was asked."""
    if not followup_ids:
        return 0
    result = await session.execute(
        update(ClarificationAsk)
        .where(
            ClarificationAsk.followup_id.in_(followup_ids),
            ClarificationAsk.status == "proposed",
        )
        .values(status="withdrawn")
        .returning(ClarificationAsk.id)
    )
    return len(result.all())


async def asks_answered_by(
    session: AsyncSession, invite_id: UUID, received_at: datetime
) -> list[Ask]:
    """The open asks a reply received at `received_at` answers: those whose email went
    out before it. Locked: the reply's extraction settles them."""
    rows = await session.scalars(
        select(ClarificationAsk)
        .join(FollowupMessage, FollowupMessage.id == ClarificationAsk.followup_id)
        .where(
            ClarificationAsk.invite_id == invite_id,
            ClarificationAsk.status == "open",
            FollowupMessage.sent_at.is_not(None),
            FollowupMessage.sent_at <= received_at,
        )
        .order_by(ClarificationAsk.position.nulls_first(), ClarificationAsk.field)
        .with_for_update(of=ClarificationAsk)
    )
    return [_ask(row) for row in rows]


async def settle_asks(
    session: AsyncSession,
    asks: Sequence[Ask],
    *,
    message_id: UUID,
    still_missing: Iterable[ItemKey],
    at: datetime,
) -> None:
    """A reply with an offer answered these asks: each is `answered`, or `unusable` when
    the item is still missing from the offer after it."""
    missing = set(still_missing)
    for ask in asks:
        await session.execute(
            update(ClarificationAsk)
            .where(ClarificationAsk.id == ask.id, ClarificationAsk.status == "open")
            .values(
                status="unusable" if ask.key in missing else "answered",
                answered_by_message_id=message_id,
                settled_at=at,
            )
        )
    await session.flush()


async def resolve_asks(session: AsyncSession, invite_id: UUID, *, by: UUID, at: datetime) -> int:
    """The buyer decided: the asks whose answers were unusable are resolved."""
    result = await session.execute(
        update(ClarificationAsk)
        .where(ClarificationAsk.invite_id == invite_id, ClarificationAsk.status == "unusable")
        .values(status="resolved", resolved_by=by, resolved_at=at)
        .returning(ClarificationAsk.id)
    )
    return len(result.all())


# --- Asks: reading -----------------------------------------------------------------------------


async def asks_for(session: AsyncSession, invite_ids: Sequence[UUID]) -> dict[UUID, list[Ask]]:
    found: dict[UUID, list[Ask]] = {invite_id: [] for invite_id in invite_ids}
    if not invite_ids:
        return found
    for row in await session.scalars(
        select(ClarificationAsk)
        .where(ClarificationAsk.invite_id.in_(invite_ids))
        .order_by(ClarificationAsk.created_at, ClarificationAsk.id)
    ):
        found[row.invite_id].append(_ask(row))
    return found


async def answers_by_message(
    session: AsyncSession, invite_id: UUID
) -> dict[UUID, tuple[AskedField, ...]]:
    """For each supplier reply that answered our clarification, the items it answered
    (what offer reconciliation applies as a supplement)."""
    found: dict[UUID, list[AskedField]] = {}
    for row in await session.scalars(
        select(ClarificationAsk)
        .where(
            ClarificationAsk.invite_id == invite_id,
            ClarificationAsk.answered_by_message_id.is_not(None),
        )
        .order_by(ClarificationAsk.position.nulls_first(), ClarificationAsk.field)
    ):
        assert row.answered_by_message_id is not None
        found.setdefault(row.answered_by_message_id, []).append(AskedField(row.position, row.field))
    return {message_id: tuple(items) for message_id, items in found.items()}


def asked_keys(asks: Iterable[Ask]) -> set[ItemKey]:
    """Items asked before (or being asked): never asked again automatically."""
    return {ask.key for ask in asks if ask.status in _ASKED}


# --- Supplier questions ------------------------------------------------------------------------


async def store_questions(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    invite_id: UUID,
    message_id: UUID,
    questions: Sequence[str],
) -> int:
    """The questions a supplier's reply asks, in order (once per reply)."""
    texts = [q.strip() for q in questions if q.strip()]
    if not texts:
        return 0
    existing = await session.scalar(
        select(func.count()).where(SupplierQuestion.message_id == message_id)
    )
    if existing:
        return 0
    for ordinal, text in enumerate(texts, start=1):
        session.add(
            SupplierQuestion(
                tenant_id=tenant_id,
                invite_id=invite_id,
                message_id=message_id,
                ordinal=ordinal,
                question=text,
                status="open",
            )
        )
    await session.flush()
    return len(texts)


async def questions_for(
    session: AsyncSession, invite_ids: Sequence[UUID]
) -> dict[UUID, list[Question]]:
    found: dict[UUID, list[Question]] = {invite_id: [] for invite_id in invite_ids}
    if not invite_ids:
        return found
    rows = await session.execute(
        select(SupplierQuestion, Message.created_at)
        .join(Message, Message.id == SupplierQuestion.message_id)
        .where(SupplierQuestion.invite_id.in_(invite_ids))
        .order_by(Message.created_at, SupplierQuestion.ordinal)
    )
    for row, received_at in rows:
        found[row.invite_id].append(_question(row, received_at))
    return found


async def open_question_texts(session: AsyncSession, invite_id: UUID) -> list[str]:
    questions = await questions_for(session, [invite_id])
    return [q.text for q in questions[invite_id] if q.status == "open"]


async def sync_offer_questions(session: AsyncSession, invite_id: UUID) -> None:
    """The supplier's latest offer lists exactly its open questions, which hold it for the
    buyer (`comparison.loader.negotiation_holds`); once none is open, the negotiation
    evaluates the offer again."""
    proposal = await session.scalar(
        select(Proposal)
        .where(Proposal.invite_id == invite_id)
        .order_by(Proposal.version.desc())
        .limit(1)
        .with_for_update()
    )
    if proposal is None:
        return
    texts = await open_question_texts(session, invite_id)
    raw = proposal.fields.get("_offer_state")
    offer_state: dict[str, object] = (
        dict(cast(Mapping[str, object], raw)) if isinstance(raw, Mapping) else {}
    )
    if offer_state.get("technical_questions") == texts:
        return
    issues = offer_state.get("issues")
    offer_state["technical_questions"] = texts
    offer_state["requires_clarification"] = (
        bool(issues) or offer_state.get("reply_kind") == "clarification" or bool(texts)
    )
    # A new dict, so SQLAlchemy sees the JSONB column change.
    proposal.fields = {**proposal.fields, "_offer_state": offer_state}
    invite = await session.get(Invite, invite_id)
    if invite is not None and invite.negotiation_evaluated_proposal_id == proposal.id:
        invite.negotiation_evaluated_proposal_id = None
    await session.flush()


async def reopen_questions(session: AsyncSession, invite_id: UUID, followup_id: UUID) -> int:
    """The answer email was rejected: the questions it answered are open again."""
    result = await session.execute(
        update(SupplierQuestion)
        .where(
            SupplierQuestion.invite_id == invite_id,
            SupplierQuestion.answer_followup_id == followup_id,
            SupplierQuestion.status == "answered",
        )
        .values(
            status="open", answer=None, answer_followup_id=None, decided_by=None, decided_at=None
        )
        .returning(SupplierQuestion.id)
    )
    reopened = len(result.all())
    if reopened:
        await sync_offer_questions(session, invite_id)
    return reopened


# --- What an offer still lacks -----------------------------------------------------------------


async def requested_lines(session: AsyncSession, rfq_id: UUID) -> list[RequestedLine]:
    return [
        RequestedLine(
            id=line.id,
            position=line.position,
            description=line.description,
            mpn=line.mpn,
            part_number=line.part_number,
            need_by=line.need_by,
        )
        for line in await session.scalars(
            select(RfqLine).where(RfqLine.rfq_id == rfq_id).order_by(RfqLine.position)
        )
    ]


def _positions(offer_state: object, key: str) -> frozenset[int]:
    if not isinstance(offer_state, Mapping):
        return frozenset()
    raw = cast(Mapping[str, object], offer_state).get(key)
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(p for p in cast(list[object], raw) if isinstance(p, int))


def declined_positions(proposal_fields: Mapping[str, object]) -> frozenset[int]:
    """Positions the supplier declined or withdrew and has not offered since
    (`_offer_state`, which also keeps what earlier replies declined), and those the
    latest reply declines (its signals, `quotes.terms.offer_terms`)."""
    offer_state = proposal_fields.get("_offer_state")
    return (
        _positions(offer_state, "declined_line_positions")
        | _positions(offer_state, "withdrawn_line_positions")
        | frozenset(offer_terms(proposal_fields).signals.declined_line_positions)
    )


def declined_offer(proposal_fields: Mapping[str, object]) -> bool:
    """The whole offer is declined or withdrawn: nothing to ask."""
    review = proposal_fields.get("_review")
    if isinstance(review, Mapping) and cast(Mapping[str, object], review).get("declined") is True:
        return True
    offer_state = proposal_fields.get("_offer_state")
    return (
        isinstance(offer_state, Mapping)
        and cast(Mapping[str, object], offer_state).get("fully_withdrawn") is True
    )


def offer_holds(proposal_fields: Mapping[str, object]) -> tuple[str, ...]:
    """Why the latest offer is held for the buyer (empty when it is not)."""
    offer_state = proposal_fields.get("_offer_state")
    if not isinstance(offer_state, Mapping):
        return ()
    state = cast(Mapping[str, object], offer_state)
    raw = state.get("issues")
    reasons = [str(i) for i in cast(list[object], raw)] if isinstance(raw, list) else []
    if state.get("reply_kind") in _HELD_KINDS and not reasons:
        reasons.append("supplier_clarification_required")
    return tuple(reasons)


def offered_lines(
    proposal: Proposal, lines: Sequence[ProposalLine], country: str | None
) -> list[OfferedLine]:
    """A stored offer's lines as the missing-field rules read them."""
    return [
        OfferedLine(
            rfq_line_id=line.rfq_line_id,
            values={
                "unit_price": line.unit_price,
                "min_quantity": line.min_quantity,
                "lead_time_days": line.lead_time_days,
            },
            fields=line.fields,
            offer_fields=proposal.fields,
            supplier_country=country,
        )
        for line in lines
    ]


def proposal_items(
    proposal: Proposal,
    lines: Sequence[ProposalLine],
    requested: Sequence[RequestedLine],
    *,
    country: str | None,
    today: date,
    expected_delivery: ExpectedDelivery | None,
) -> list[OpenItem]:
    """What a stored offer (a proposal and its lines) still lacks."""
    return open_items(
        header_values={"currency": proposal.currency, "valid_until": proposal.valid_until},
        header_fields=proposal.fields,
        requested=requested,
        offered=offered_lines(proposal, lines, country),
        declined_positions=declined_positions(proposal.fields),
        reference_date=today,
        expected_delivery=expected_delivery,
    )


# --- Per supplier: what Bido waits for and what the buyer must do ------------------------------

RequestKind = Literal["clarification", "counter", "bafo"]
DecisionKind = Literal["unusable_answer", "asked_before", "offer_held", "no_answer"]
FollowupPhase = Literal[
    "needs_your_answer",
    "needs_your_decision",
    "email_pending",
    "waiting_for_supplier",
    "awaiting_offer",
    "will_ask",
    "nothing_to_ask",
    "not_applicable",
]


@dataclass(frozen=True, slots=True)
class OpenRequest:
    """The email Bido sent that the supplier has not answered yet (one at a time)."""

    kind: RequestKind
    sent_at: datetime
    reply_by: date | None
    # Our outbound `messages` row: what a reminder refers to.
    message_row_id: UUID | None
    asks: tuple[Ask, ...] = ()
    # Reminded once already (proposed, sent, or turned down by the buyer).
    reminded: bool = False
    reminded_at: datetime | None = None
    # The reminder's own reply-by date.
    reminder_reply_by: date | None = None


@dataclass(frozen=True, slots=True)
class PendingEmail:
    """An email to the supplier that waits for approval (a person or the mandate)."""

    kind: str
    created_at: datetime
    approval_id: UUID | None


@dataclass(frozen=True, slots=True)
class Decision:
    """Something only the buyer can settle for this supplier."""

    kind: DecisionKind
    position: int | None = None
    field: FollowupField | None = None
    reasons: tuple[str, ...] = ()
    since: datetime | None = None


@dataclass(frozen=True, slots=True)
class SupplierFollowup:
    invite_id: UUID
    invite_status: str
    has_offer: bool
    asks: tuple[Ask, ...] = ()
    questions: tuple[Question, ...] = ()
    request: OpenRequest | None = None
    pending: tuple[PendingEmail, ...] = ()
    decisions: tuple[Decision, ...] = ()
    # What the latest offer still lacks and Bido has not asked yet.
    to_ask: tuple[OpenItem, ...] = ()
    # Lines whose stated lead time definitely misses the need-by date: the negotiation's
    # delivery-date request ("can you deliver by <need-by>?"), typed for it.
    late: tuple[OpenItem, ...] = ()
    applicable: bool = True

    @property
    def open_questions(self) -> tuple[Question, ...]:
        return tuple(q for q in self.questions if q.status == "open")

    @property
    def phase(self) -> FollowupPhase:
        if not self.applicable:
            return "not_applicable"
        if self.open_questions:
            return "needs_your_answer"
        if self.decisions:
            return "needs_your_decision"
        if self.pending:
            return "email_pending"
        if self.request is not None:
            return "waiting_for_supplier"
        if not self.has_offer:
            return "awaiting_offer"
        if self.to_ask:
            return "will_ask"
        return "nothing_to_ask"


@dataclass(slots=True)
class _Rows:
    followups: list[FollowupMessage] = field(default_factory=list[FollowupMessage])
    negotiations: list[NegotiationMessage] = field(default_factory=list[NegotiationMessage])


async def _outbound_rows(session: AsyncSession, headers: Sequence[str]) -> dict[str, UUID]:
    if not headers:
        return {}
    rows = await session.execute(
        select(Message.message_id_header, Message.id).where(
            Message.direction == "out", Message.message_id_header.in_(headers)
        )
    )
    return {header: row_id for header, row_id in rows if header is not None}


def _reply_by(value: JsonValue) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _open_request(
    rows: _Rows,
    asks: Sequence[Ask],
    versions: Mapping[UUID, int],
    latest_version: int | None,
    outbound: Mapping[str, UUID],
    deadline: date | None,
) -> OpenRequest | None:
    """The latest email the supplier has not answered: a clarification whose asks are
    still open, or a counter / best-and-final request sent for the latest offer."""
    candidates: list[OpenRequest] = []
    open_asks = [a for a in asks if a.status == "open"]
    by_followup = {f.id: f for f in rows.followups}
    for followup_id in dict.fromkeys(a.followup_id for a in open_asks):
        sent = by_followup.get(followup_id)
        if sent is None or sent.sent_at is None:
            continue
        candidates.append(
            OpenRequest(
                kind="clarification",
                sent_at=sent.sent_at,
                reply_by=sent.reply_by,
                message_row_id=outbound.get(sent.message_id_header or ""),
                asks=tuple(a for a in open_asks if a.followup_id == followup_id),
            )
        )
    for message in rows.negotiations:
        if message.status != "sent" or message.sent_at is None:
            continue
        version = versions.get(message.proposal_id)
        if version is None or latest_version is None or version < latest_version:
            continue  # the supplier sent an offer since
        terms = cast(dict[str, JsonValue], message.terms)
        candidates.append(
            OpenRequest(
                kind="bafo" if message.action == "bafo" else "counter",
                sent_at=message.sent_at,
                reply_by=_reply_by(terms.get("reply_by")) or deadline,
                message_row_id=outbound.get(message.message_id_header or ""),
            )
        )
    if not candidates:
        return None
    request = max(candidates, key=lambda r: r.sent_at)
    reminders = [
        f
        for f in rows.followups
        if f.kind == "reminder"
        and f.status != "canceled"
        and request.message_row_id is not None
        and f.regarding_message_id == request.message_row_id
    ]
    if not reminders:
        return request
    reminder = max(reminders, key=lambda f: f.created_at)
    return OpenRequest(
        kind=request.kind,
        sent_at=request.sent_at,
        reply_by=request.reply_by,
        message_row_id=request.message_row_id,
        asks=request.asks,
        reminded=True,
        reminded_at=reminder.sent_at,
        reminder_reply_by=reminder.reply_by,
    )


async def supplier_followups(
    session: AsyncSession,
    rfq_id: UUID,
    *,
    now: datetime,
    expected_delivery: ExpectedDelivery | None = None,
) -> dict[UUID, SupplierFollowup]:
    """Per invite of an RFQ: what Bido waits for, what waits for approval, what needs
    the buyer's answer or decision, and what is still to ask."""
    rfq = await session.get(Rfq, rfq_id)
    if rfq is None:
        return {}
    invites = list(
        (
            await session.execute(
                select(Invite, Supplier.country)
                .join(Supplier, Supplier.id == Invite.supplier_id)
                .where(Invite.rfq_id == rfq_id)
                .order_by(Invite.created_at, Invite.id)
            )
        ).all()
    )
    invite_ids = [invite.id for invite, _ in invites]
    if not invite_ids:
        return {}
    rows: dict[UUID, _Rows] = {invite_id: _Rows() for invite_id in invite_ids}
    for followup in await session.scalars(
        select(FollowupMessage).where(FollowupMessage.invite_id.in_(invite_ids))
    ):
        rows[followup.invite_id].followups.append(followup)
    for message in await session.scalars(
        select(NegotiationMessage).where(NegotiationMessage.invite_id.in_(invite_ids))
    ):
        rows[message.invite_id].negotiations.append(message)
    proposals = list(
        await session.scalars(
            select(Proposal).where(Proposal.invite_id.in_(invite_ids)).order_by(Proposal.version)
        )
    )
    versions = {p.id: p.version for p in proposals}
    latest: dict[UUID, Proposal] = {p.invite_id: p for p in proposals}
    lines: dict[UUID, list[ProposalLine]] = {p.id: [] for p in latest.values()}
    if lines:
        for line in await session.scalars(
            select(ProposalLine).where(ProposalLine.proposal_id.in_(lines.keys()))
        ):
            lines[line.proposal_id].append(line)
    headers = [
        header
        for r in rows.values()
        for header in (
            *(f.message_id_header for f in r.followups),
            *(m.message_id_header for m in r.negotiations),
        )
        if header is not None
    ]
    outbound = await _outbound_rows(session, headers)
    asks = await asks_for(session, invite_ids)
    questions = await questions_for(session, invite_ids)
    requested = await requested_lines(session, rfq_id)
    deadline = rfq.negotiation_deadline.date() if rfq.negotiation_deadline else None
    active = rfq.status == "open" and rfq.negotiation_status != "closed"

    result: dict[UUID, SupplierFollowup] = {}
    for invite, country in invites:
        mine = rows[invite.id]
        proposal = latest.get(invite.id)
        applicable = active and invite.status in ("sent", "responded")
        pending = tuple(
            PendingEmail(kind=f.kind, created_at=f.created_at, approval_id=f.approval_id)
            for f in mine.followups
            if f.status == "proposed"
        ) + tuple(
            PendingEmail(kind=m.action, created_at=m.created_at, approval_id=m.approval_id)
            for m in mine.negotiations
            if m.status == "proposed"
        )
        request = _open_request(
            mine,
            asks[invite.id],
            versions,
            proposal.version if proposal is not None else None,
            outbound,
            deadline,
        )
        decisions: list[Decision] = []
        to_ask: tuple[OpenItem, ...] = ()
        late: tuple[OpenItem, ...] = ()
        if proposal is not None and not declined_offer(proposal.fields):
            holds = offer_holds(proposal.fields)
            if holds:
                decisions.append(Decision(kind="offer_held", reasons=holds))
            items = proposal_items(
                proposal,
                lines[proposal.id],
                requested,
                country=country,
                today=now.date(),
                expected_delivery=expected_delivery,
            )
            settled = {
                a.key: a
                for a in asks[invite.id]
                if a.status in ("answered", "unusable", "resolved")
            }
            live = {a.key for a in asks[invite.id] if a.status in ("proposed", "open")}
            # Asked once and still missing: never asked again, the buyer decides.
            for item in items:
                ask = settled.get(item.key)
                if ask is None or ask.status == "resolved":
                    continue
                decisions.append(
                    Decision(
                        kind="unusable_answer" if ask.status == "unusable" else "asked_before",
                        position=item.position,
                        field=item.field,
                        since=ask.settled_at,
                    )
                )
            to_ask = tuple(
                item for item in items if item.key not in settled and item.key not in live
            )
            late = tuple(
                late_deliveries(
                    requested=requested,
                    offered=offered_lines(proposal, lines[proposal.id], country),
                    declined_positions=declined_positions(proposal.fields),
                    reference_date=now.date(),
                    expected_delivery=expected_delivery,
                )
            )
        if (
            request is not None
            and request.reminded_at is not None
            and request.reminder_reply_by is not None
            and now.date() > request.reminder_reply_by
        ):
            decisions.append(Decision(kind="no_answer", since=request.sent_at))
        result[invite.id] = SupplierFollowup(
            invite_id=invite.id,
            invite_status=invite.status,
            has_offer=proposal is not None,
            asks=tuple(asks[invite.id]),
            questions=tuple(questions[invite.id]),
            request=request,
            pending=pending,
            decisions=tuple(decisions),
            to_ask=to_ask,
            late=late,
            applicable=applicable,
        )
    return result
