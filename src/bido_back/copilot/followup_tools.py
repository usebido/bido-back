"""Copilot tools for what Bido asked each supplier and what waits for the buyer.

- `supplier_followups` (used by `what_is_missing`): per supplier, in plain words,
  where it stands (waiting for the supplier, needs your answer, needs your
  decision, an email awaiting approval, nothing to ask), what Bido asked and
  when (the clarification ledger), what the offer still lacks, and the
  supplier's questions. The same rules decide what a clarification email asks
  (`bido_back.followups.missing`): the buyer and the supplier see one list.
- `answer_supplier_question` (draft): the buyer's answer to a supplier's
  questions, recorded and proposed as an email that waits for approval (a
  negotiation mandate never approves it); or, with `dismiss`, the questions are
  closed without an email (answered elsewhere).
- `reopen_supplier_question` (draft): open again a question Bido considered
  superseded by the supplier's later complete offer (or one the buyer
  dismissed): the offer waits for the buyer's answer again.
- `use_current_offer` (draft, two calls): take a supplier's held offer as shown,
  releasing its holds. The first call only shows what is held; the buyer
  confirms in a later message.
- `release_negotiation_hold` (draft, two calls): negotiate a supplier's offer the
  negotiation holds for the buyer as it is (`POST .../release-hold`, the same
  service). The first call only shows the offer and why it is held; the buyer
  confirms in a later message, and only that offer version is released.

Nothing here sends an email or decides on its own.
"""

from collections.abc import Sequence
from datetime import UTC, date, datetime, time
from typing import TYPE_CHECKING, Final
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bido_back.agents.context import ToolContext
from bido_back.agents.tools import AnyTool, Tool, ToolError
from bido_back.auth import Principal
from bido_back.copilot import proposals, queries
from bido_back.copilot.proposals import (
    PROPOSAL_ARG,
    AwaitingConfirmation,
    ProposalPreview,
    ProposedHoldRelease,
)
from bido_back.corrections import service as corrections
from bido_back.corrections.schemas import UseCurrentOfferRequest
from bido_back.db import tenant_session
from bido_back.db.models import Invite, Proposal, Supplier
from bido_back.followups import ledger, questions
from bido_back.followups import missing as missing_rules
from bido_back.followups.missing import OpenItem
from bido_back.followups.schemas import AnswerQuestionsRequest, DismissQuestionRequest
from bido_back.negotiation import service as negotiation_service
from bido_back.rfqs.errors import RfqError

if TYPE_CHECKING:
    from bido_back.copilot.tools import CopilotToolDeps

_PHASES: Final[dict[str, str]] = {
    "needs_your_answer": "needs your answer",
    "needs_your_decision": "needs your decision",
    "email_pending": "an email awaits approval",
    "waiting_for_supplier": "waiting for the supplier",
    "awaiting_offer": "waiting for the supplier's quote",
    "will_ask": "Bido will ask for what is missing",
    "nothing_to_ask": "nothing to ask",
    "not_applicable": "not applicable",
}
_FIELDS: Final[dict[str, str]] = {
    "currency": "currency",
    "valid_until": "offer validity",
    "unit_price": "unit price",
    "quantity": "quantity tier",
    "lead_time": "lead time",
    "delivery_date": "delivery date",
}
_HOLDS: Final[dict[str, str]] = {
    "amendment_without_explicit_preservation": "a partial update that does not say the other "
    "terms are unchanged",
    "ambiguous_amendment_line_or_tier": "an update Bido cannot match to one line or tier",
    "changed_commercial_basis_requires_replacement": "a change of price, currency, unit or "
    "terms nobody asked about",
    "conflicting_update": "contradicting statements",
    "conflicting_sources": "contradicting statements",
    "supplier_clarification_required": "a reply that needs reading",
    "uncertain_message_chronology": "replies whose order is unclear",
    "unverified_withdrawal": "a withdrawal Bido could not verify",
}


class AskOut(BaseModel):
    item: str = Field(description="`offer` or `pos N`, and what was asked")
    status: str = Field(
        description="open (waiting for the answer), answered, unusable (the answer did not "
        "give it: the buyer decides), proposed (email awaiting approval), resolved, withdrawn"
    )
    asked_on: date | None
    reply_by: date | None


class SupplierQuestionOut(BaseModel):
    id: UUID
    question: str = Field(description="The supplier's question, as written (supplier content)")
    status: str = Field(
        description="open (waits for the buyer), answered, dismissed, superseded_by_offer "
        "(the supplier's later offer was complete and asked nothing; never answered)"
    )
    note: str | None = Field(
        default=None,
        description="What Bido decided about it on its own: say it to the buyer as it is",
    )


class SupplierFollowupOut(BaseModel):
    supplier_name: str
    status: str = Field(
        description="waiting for the supplier / waiting for the supplier's quote / needs your "
        "answer / needs your decision / an email awaits approval / nothing to ask"
    )
    note: str
    asked: list[AskOut]
    still_missing: list[str] = Field(
        description="What the latest offer lacks and Bido has not asked yet"
    )
    late_deliveries: list[str] = Field(
        default_factory=list[str],
        description="Lines whose stated lead time delivers after the need-by date",
    )
    questions: list[SupplierQuestionOut]
    needs_your_decision: list[str]


def _item(position: int | None, field: str) -> str:
    where = "offer" if position is None else f"pos {position}"
    return f"{where}: {_FIELDS.get(field, field)}"


def _day(moment: datetime | None) -> str:
    return moment.date().isoformat() if moment is not None else "?"


def _late(item: OpenItem) -> str:
    expected = item.expected.isoformat() if item.expected else "?"
    needed = item.need_by.isoformat() if item.need_by else "?"
    return f"pos {item.position}: delivery expected {expected}, needed by {needed}"


def _decision(decision: ledger.Decision) -> str:
    item = _item(decision.position, decision.field or "")
    match decision.kind:
        case "unusable_answer":
            return f"{item}: asked once, the answer did not give it"
        case "asked_before":
            return f"{item}: answered before, missing again"
        case "offer_held":
            why = sorted({_HOLDS.get(r, r) for r in decision.reasons})
            return (
                "the supplier's latest reply could not be applied as it was ("
                + "; ".join(why)
                + "): check it, correct it, or use the current offer"
            )
        case "no_answer":
            return f"no answer to our email of {_day(decision.since)}, even after the reminder"
        case "email_held":
            kind = decision.reasons[0] if decision.reasons else "follow-up"
            return (
                f"the {kind} Bido prepared on {_day(decision.since)} failed its checks and was "
                "not sent (see the RFQ's activity)"
            )


def _note(state: ledger.SupplierFollowup) -> str:
    match state.phase:
        case "needs_your_answer":
            count = len(state.open_questions)
            return (
                f"The supplier asked {count} question{'s' if count != 1 else ''}: Bido sends it "
                "nothing on its own until the buyer answers (answer_supplier_question). "
                "Waiting for the buyer."
            )
        case "needs_your_decision":
            return "Needs your decision: " + "; ".join(_decision(d) for d in state.decisions) + "."
        case "email_pending":
            kinds = ", ".join(dict.fromkeys(p.kind for p in state.pending))
            return f"An email to the supplier awaits approval ({kinds})."
        case "waiting_for_supplier":
            request = state.request
            assert request is not None
            sent = _day(request.sent_at)
            text = f"Waiting for the supplier to answer our {request.kind} of {sent}"
            if request.reply_by is not None:
                text += f" (reply by {request.reply_by.isoformat()})"
            if request.reminded:
                text += f"; reminded on {_day(request.reminded_at)}"
            return text + "."
        case "awaiting_offer":
            return "Waiting for the supplier's quote: Bido sends reminders on the RFQ's schedule."
        case "will_ask":
            missing = ", ".join(_item(i.position, i.field) for i in state.to_ask)
            return f"Bido will ask the supplier, in one email, for what is missing: {missing}."
        case "nothing_to_ask":
            return "Nothing to ask: the offer has everything Bido asks for; no email is waiting."
        case "not_applicable":
            return "Not applicable (not sent, declined, or the RFQ is closed)."


def _superseded(state: ledger.SupplierFollowup) -> str:
    """The questions Bido closed on its own, said once (dates only)."""
    notes = list(dict.fromkeys(q.note for q in state.questions if q.note))
    if not notes:
        return ""
    return " The supplier's earlier question: " + " ".join(notes)


def followup_out(name: str, state: ledger.SupplierFollowup) -> SupplierFollowupOut:
    return SupplierFollowupOut(
        supplier_name=name,
        status=_PHASES[state.phase],
        note=_note(state) + _superseded(state),
        asked=[
            AskOut(
                item=_item(a.position, a.field),
                status=a.status,
                asked_on=a.asked_at.date() if a.asked_at else None,
                reply_by=a.reply_by,
            )
            for a in state.asks
            if a.status != "withdrawn"
        ],
        still_missing=[_item(i.position, i.field) for i in state.to_ask],
        late_deliveries=[_late(i) for i in state.late],
        questions=[
            SupplierQuestionOut(id=q.id, question=q.text, status=q.status, note=q.note)
            for q in state.questions
        ],
        needs_your_decision=[_decision(d) for d in state.decisions],
    )


def _moment(today: date) -> datetime:
    return datetime.combine(today, time(12), tzinfo=UTC)


async def supplier_followups(
    session: AsyncSession, rfq_id: UUID, today: date
) -> list[SupplierFollowupOut]:
    """Every invited supplier's follow-up state, in the RFQ's invite order."""
    states = await ledger.supplier_followups(
        session,
        rfq_id,
        now=_moment(today),
        expected_delivery=missing_rules.DEFAULT_EXPECTED_DELIVERY,
    )
    names = {invite.id: name for invite, name in await queries.invites(session, rfq_id)}
    return [followup_out(names[invite_id], state) for invite_id, state in states.items()]


def missing_items(out: SupplierFollowupOut) -> list[str]:
    """What the supplier's offer still lacks: asked and waiting, or not asked yet."""
    waiting = [a.item for a in out.asked if a.status in ("open", "proposed", "unusable")]
    return list(dict.fromkeys([*waiting, *out.still_missing]))


# --- Actions -----------------------------------------------------------------------------------


class AnswerQuestionIn(BaseModel):
    rfq: str = Field(description="The RFQ's reference (e.g. RFQ-12) or id")
    supplier: str = Field(description="The supplier's name, as what_is_missing shows it")
    answer: str | None = Field(
        default=None,
        description="The email text exactly as the buyer approved it in this conversation, in "
        "the supplier's language. Null only with dismiss",
    )
    dismiss: bool = Field(
        default=False,
        description="True: no email; the buyer says the question needs no answer (answered "
        "by phone, or nothing to answer)",
    )


class AnswerQuestionOut(BaseModel):
    supplier_name: str
    questions: int
    approval_id: UUID | None = Field(description="The answer email, waiting for approval")
    note: str


class ReopenQuestionIn(BaseModel):
    rfq: str = Field(description="The RFQ's reference (e.g. RFQ-12) or id")
    supplier: str = Field(description="The supplier's name, as what_is_missing shows it")
    question_id: UUID | None = Field(
        default=None,
        description="The question to reopen, as what_is_missing lists it; null: every "
        "question Bido considered superseded by this supplier's offer",
    )


class ReopenQuestionOut(BaseModel):
    supplier_name: str
    questions: int = Field(description="How many questions are open again")
    note: str


class UseCurrentOfferIn(BaseModel):
    rfq: str = Field(description="The RFQ's reference (e.g. RFQ-12) or id")
    supplier: str = Field(description="The supplier's name")
    reason: str | None = Field(default=None, description="The buyer's reason, if they gave one")
    proposal_id: UUID | None = Field(default=None, description=PROPOSAL_ARG)


class UseCurrentOfferOut(BaseModel):
    awaiting_confirmation: bool = Field(
        description="True: NOTHING was done yet. Show what is held, ask the buyer to confirm, "
        "and call again with proposal_id after they answer"
    )
    proposal_id: UUID | None = None
    supplier_name: str
    holds: list[str]
    note: str


class ReleaseHoldIn(BaseModel):
    rfq: str = Field(description="The RFQ's reference (e.g. RFQ-12) or id")
    supplier: str = Field(description="The supplier's name")
    proposal_id: UUID | None = Field(default=None, description=PROPOSAL_ARG)


class ReleaseHoldOut(AwaitingConfirmation):
    supplier_name: str
    note: str
    # Done: where the supplier stands now (what the negotiation does next).
    state: str | None = None


def _principal(ctx: ToolContext) -> Principal:
    if ctx.user_id is None:
        raise ToolError("the copilot needs a signed-in user")
    return Principal(tenant_id=ctx.tenant_id, user_id=ctx.user_id)


async def _invite(session: AsyncSession, rfq_ref: str, supplier: str) -> tuple[UUID, UUID, str]:
    rfq = await queries.find_rfq(session, rfq_ref)
    if rfq is None:
        raise ToolError(f"no RFQ {rfq_ref!r}; use list_rfqs to find it")
    wanted = supplier.strip().casefold()
    rows: Sequence[tuple[UUID, str]] = (
        await session.execute(
            select(Invite.id, Supplier.name)
            .join(Supplier, Supplier.id == Invite.supplier_id)
            .where(Invite.rfq_id == rfq.id, func.lower(Supplier.name) == wanted)
        )
    ).all()
    if len(rows) != 1:
        raise ToolError(f"no single supplier named {supplier!r} is invited to {rfq.ref}")
    invite_id, name = rows[0]
    return rfq.id, invite_id, name


def followup_tools(deps: "CopilotToolDeps") -> tuple[AnyTool, ...]:
    db = deps.db

    async def answer_supplier_question(
        ctx: ToolContext, args: AnswerQuestionIn
    ) -> AnswerQuestionOut:
        principal = _principal(ctx)
        async with tenant_session(db, ctx.tenant_id) as session:
            rfq_id, invite_id, name = await _invite(session, args.rfq, args.supplier)
            asked = await ledger.questions_for(session, [invite_id])
        open_ids = [q.id for q in asked[invite_id] if q.status == "open"]
        if not open_ids:
            raise ToolError(f"{name} has no open question")
        try:
            if args.dismiss:
                for question_id in open_ids:
                    await questions.dismiss_question(
                        db, principal, rfq_id, question_id, DismissQuestionRequest()
                    )
                return AnswerQuestionOut(
                    supplier_name=name,
                    questions=len(open_ids),
                    approval_id=None,
                    note="Closed without an email; automatic emails to this supplier resume.",
                )
            if not args.answer or not args.answer.strip():
                raise ToolError("give the answer the buyer approved, or dismiss")
            answered = await questions.answer_questions(
                db,
                principal,
                rfq_id,
                invite_id,
                AnswerQuestionsRequest(answer=args.answer, question_ids=open_ids),
            )
        except RfqError as exc:
            raise ToolError(str(exc)) from None
        return AnswerQuestionOut(
            supplier_name=name,
            questions=len(answered.question_ids),
            approval_id=answered.approval_id,
            note=(
                "Nothing was sent. The answer email waits in the approvals queue for a person "
                "(never for the negotiation mandate); it goes out only once approved."
            ),
        )

    async def reopen_supplier_question(
        ctx: ToolContext, args: ReopenQuestionIn
    ) -> ReopenQuestionOut:
        principal = _principal(ctx)
        async with tenant_session(db, ctx.tenant_id) as session:
            rfq_id, invite_id, name = await _invite(session, args.rfq, args.supplier)
            asked = await ledger.questions_for(session, [invite_id])
        if args.question_id is not None:
            ids = [q.id for q in asked[invite_id] if q.id == args.question_id]
            if not ids:
                raise ToolError(f"{name} has no such question")
        else:
            ids = [q.id for q in asked[invite_id] if q.status == "superseded_by_offer"]
            if not ids:
                raise ToolError(f"{name} has no question Bido considered superseded")
        try:
            for question_id in ids:
                await questions.reopen_question(db, principal, rfq_id, question_id)
        except RfqError as exc:
            raise ToolError(str(exc)) from None
        return ReopenQuestionOut(
            supplier_name=name,
            questions=len(ids),
            note=(
                "Open again: the supplier's offer waits for the buyer's answer "
                "(answer_supplier_question) and Bido sends this supplier nothing on its own "
                "meanwhile."
            ),
        )

    async def use_current_offer(ctx: ToolContext, args: UseCurrentOfferIn) -> UseCurrentOfferOut:
        principal = _principal(ctx)
        if deps.conversation_id is None:
            raise ToolError("this action needs a conversation to ask the buyer in")
        async with tenant_session(db, ctx.tenant_id) as session:
            rfq_id, invite_id, name = await _invite(session, args.rfq, args.supplier)
            proposal_id = await session.scalar(
                select(Proposal.id)
                .where(Proposal.invite_id == invite_id)
                .order_by(Proposal.version.desc())
                .limit(1)
            )
            states = await ledger.supplier_followups(session, rfq_id, now=_moment(deps.today()))
        if proposal_id is None:
            raise ToolError(f"{name} has sent no offer yet")
        state = states.get(invite_id)
        held = [
            _decision(d)
            for d in (state.decisions if state is not None else ())
            if d.kind in ("offer_held", "unusable_answer")
        ]
        if args.proposal_id is None:
            if not held:
                raise ToolError(f"{name}'s offer is not held: nothing to release")
            confirmation = await proposals.propose(
                db,
                principal,
                conversation_id=deps.conversation_id,
                run_id=ctx.run_id,
                action="use_current_offer",
                args={"proposal_id": str(proposal_id), "reason": args.reason},
                preview={"shown": {"supplier": name, "holds": held}},
            )
            return UseCurrentOfferOut(
                awaiting_confirmation=True,
                proposal_id=confirmation,
                supplier_name=name,
                holds=held,
                note=(
                    "Nothing was done. If the buyer confirms in their next message, the offer "
                    "as Bido shows it becomes the offer and the negotiation goes on with it."
                ),
            )
        try:
            claimed = await proposals.claim(
                db,
                principal,
                conversation_id=deps.conversation_id,
                run_id=ctx.run_id,
                action="use_current_offer",
                proposal_id=args.proposal_id,
            )
        except proposals.ProposalRefused as exc:
            raise ToolError(str(exc)) from None
        stored = claimed.args
        try:
            await corrections.use_current_offer(
                db,
                principal,
                UUID(str(stored["proposal_id"])),
                UseCurrentOfferRequest(reason=stored.get("reason")),
            )
        except RfqError as exc:
            await proposals.release(db, principal, args.proposal_id)
            raise ToolError(str(exc)) from None
        return UseCurrentOfferOut(
            awaiting_confirmation=False,
            proposal_id=args.proposal_id,
            supplier_name=name,
            holds=held,
            note="Done: the offer as shown is the offer; the negotiation evaluates it again.",
        )

    async def release_negotiation_hold(ctx: ToolContext, args: ReleaseHoldIn) -> ReleaseHoldOut:
        principal = _principal(ctx)
        if deps.conversation_id is None:
            raise ToolError("this action needs a conversation to ask the buyer in")
        async with tenant_session(db, ctx.tenant_id) as session:
            rfq_id, invite_id, name = await _invite(session, args.rfq, args.supplier)
        if args.proposal_id is None:
            try:
                hold = await negotiation_service.offer_hold(db, principal, rfq_id, invite_id)
            except RfqError as exc:
                raise ToolError(str(exc)) from None
            if hold.proposal_id is None or hold.proposal_version is None:
                raise ToolError(f"{name} has sent no offer yet")
            if not hold.held or hold.phase == "closed":
                raise ToolError(
                    f"{name}'s offer is not held in the negotiation: nothing to release "
                    f"({hold.label})"
                )
            preview = ProposalPreview(
                action="release_negotiation_hold",
                rfq=args.rfq,
                negotiation_hold=ProposedHoldRelease(
                    supplier_name=name,
                    offer_version=hold.proposal_version,
                    state=hold.label,
                    reason=hold.reason,
                    holds=list(hold.holds),
                ),
            )
            confirmation = await proposals.propose(
                db,
                principal,
                conversation_id=deps.conversation_id,
                run_id=ctx.run_id,
                action="release_negotiation_hold",
                args={
                    "rfq_id": str(rfq_id),
                    "invite_id": str(invite_id),
                    "proposal_id": str(hold.proposal_id),
                },
                preview={"shown": preview.model_dump(mode="json")},
            )
            return ReleaseHoldOut(
                awaiting_confirmation=True,
                proposal_id=confirmation,
                preview=preview,
                supplier_name=name,
                note=(
                    "Nothing was done. Show the buyer the offer version and why it is held. "
                    "If they confirm in their next message, Bido negotiates that offer as it "
                    "is (nothing is sent without its own approval or the mandate's checks)."
                ),
            )
        try:
            claimed = await proposals.claim(
                db,
                principal,
                conversation_id=deps.conversation_id,
                run_id=ctx.run_id,
                action="release_negotiation_hold",
                proposal_id=args.proposal_id,
            )
        except proposals.ProposalRefused as exc:
            raise ToolError(str(exc)) from None
        stored = claimed.args
        try:
            view = await negotiation_service.release_hold(
                db,
                principal,
                UUID(str(stored["rfq_id"])),
                UUID(str(stored["invite_id"])),
                UUID(str(stored["proposal_id"])),
            )
        except RfqError as exc:
            await proposals.release(db, principal, args.proposal_id)
            raise ToolError(str(exc)) from None
        released = next(
            (i for i in view.invites if i.invite_id == UUID(str(stored["invite_id"]))), None
        )
        return ReleaseHoldOut(
            awaiting_confirmation=False,
            proposal_id=args.proposal_id,
            supplier_name=name,
            state=released.state.label if released and released.state else None,
            note="Done: the offer is negotiated as it is from the next planning run.",
        )

    return (
        Tool(
            "answer_supplier_question",
            "Answer a supplier's open questions with the text the buyer approved in this "
            "conversation (the email then waits for approval), or dismiss them when the buyer "
            "says no answer is needed. Only when the buyer asks for it.",
            AnswerQuestionIn,
            AnswerQuestionOut,
            "draft",
            answer_supplier_question,
        ),
        Tool(
            "reopen_supplier_question",
            "Open again a supplier's question that Bido considered superseded by the "
            "supplier's later complete offer (what_is_missing says so), or one dismissed "
            "earlier: the offer then waits for the buyer's answer. Only when the buyer asks "
            "for it.",
            ReopenQuestionIn,
            ReopenQuestionOut,
            "draft",
            reopen_supplier_question,
        ),
        Tool(
            "use_current_offer",
            "When a supplier's offer is held for the buyer (what_is_missing says 'needs your "
            "decision'), take it as Bido shows it and release the hold. Two calls: the first "
            "shows what is held and does nothing; call again with proposal_id only after the "
            "buyer confirms in a later message. Only when the buyer asks for it.",
            UseCurrentOfferIn,
            UseCurrentOfferOut,
            "draft",
            use_current_offer,
        ),
        Tool(
            "release_negotiation_hold",
            "When get_negotiation_status shows a supplier needing the buyer's decision or "
            "answer because its offer is on hold (a question the buyer answered outside Bido, "
            "a reply Bido could not apply) and the buyer says to negotiate that offer as it "
            "is: release the hold. Two calls: the first shows the offer version and why it is "
            "held and does nothing; call again with proposal_id only after the buyer confirms "
            "in a later message. A newer offer that arrived meanwhile is not released. Only "
            "when the buyer asks for it.",
            ReleaseHoldIn,
            ReleaseHoldOut,
            "draft",
            release_negotiation_hold,
        ),
    )
