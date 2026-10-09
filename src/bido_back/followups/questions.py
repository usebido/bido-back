"""The buyer answers (or dismisses) a supplier's questions.

A supplier's question waits for the buyer (`bido_back.followups.ledger`): while
one is open, Bido sends that supplier nothing on its own. The buyer either:

- answers it: the text is recorded on the questions and proposed, as written,
  as an email in the supplier's thread for `send_supplier_answer`. A
  negotiation mandate never approves that tool: a person always decides the
  answer (by default the buyer themself; a second person in four-eyes
  tenants). The email is requested on behalf of the buyer who sent the RFQ
  email (it goes out from their mailbox, like every email in the thread); who
  wrote the answer is recorded. Rejecting the email opens the questions again.
- or dismisses it (answered elsewhere, or nothing to answer).

Either way the supplier's offer no longer waits for this question
(`ledger.sync_offer_questions`), so automatic emails and the negotiation resume.
Any member of the tenant may answer or dismiss; every change is audited (ids
only, never the texts).
"""

import logging
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from pydantic import JsonValue
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.agents.approvals import ApprovalProposal
from bido_back.agents.events import args_hash
from bido_back.approvals.audit import approval_audit
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
    Rfq,
    SupplierQuestion,
)
from bido_back.followups.ledger import sync_offer_questions
from bido_back.followups.schemas import (
    AnsweredQuestions,
    AnswerQuestionsRequest,
    DismissQuestionRequest,
    SupplierQuestionView,
)
from bido_back.followups.send import (
    SEND_SUPPLIER_ANSWER,
    SendFollowupEmailArgs,
    followup_id,
    followup_proposal_ids,
)
from bido_back.followups.service import rfq_sender, thread_subject
from bido_back.rfqs.errors import InvalidState, NotFound

logger = logging.getLogger(__name__)

Db = AsyncEngine | async_sessionmaker[AsyncSession]


async def _invite(session: AsyncSession, rfq_id: UUID, invite_id: UUID) -> tuple[Rfq, Invite]:
    # The RFQ first, like corrections and negotiation writes.
    rfq = await session.scalar(select(Rfq).where(Rfq.id == rfq_id).with_for_update())
    invite = await session.scalar(
        select(Invite).where(Invite.id == invite_id, Invite.rfq_id == rfq_id).with_for_update()
    )
    if rfq is None or invite is None:
        raise NotFound(f"invite {invite_id} of rfq {rfq_id} not found")
    return rfq, invite


async def answer_questions(
    db: Db,
    principal: Principal,
    rfq_id: UUID,
    invite_id: UUID,
    request: AnswerQuestionsRequest,
) -> AnsweredQuestions:
    """Record the buyer's answer and propose it, as written, for approval."""
    now = datetime.now(UTC)
    async with tenant_session(db, principal.tenant_id) as session:
        rfq, invite = await _invite(session, rfq_id, invite_id)
        query = select(SupplierQuestion).where(
            SupplierQuestion.invite_id == invite.id, SupplierQuestion.status == "open"
        )
        if request.question_ids:
            query = query.where(SupplierQuestion.id.in_(request.question_ids))
        questions = list(await session.scalars(query.with_for_update()))
        if not questions or (
            request.question_ids and len(questions) != len(set(request.question_ids))
        ):
            raise InvalidState("no such open question for this supplier")
        if invite.thread_message_id is None:
            raise InvalidState(f"invite {invite.id} has no email thread to answer in")
        sender = await rfq_sender(session, invite.id)
        contact = await session.scalar(select(Contact.email).where(Contact.id == invite.contact_id))
        if sender is None or not contact:
            raise InvalidState(f"invite {invite.id} has no sent RFQ email to answer in")
        sequence = (
            await session.scalar(
                select(func.max(FollowupMessage.sequence)).where(
                    FollowupMessage.invite_id == invite.id, FollowupMessage.kind == "answer"
                )
            )
            or 0
        ) + 1
        subject = await thread_subject(session, invite, rfq)
        regarding = max(questions, key=lambda q: q.created_at).message_id

    row_id = followup_id(invite_id, "answer", sequence)
    args = SendFollowupEmailArgs(
        followup_id=row_id,
        invite_id=invite_id,
        kind="answer",
        sender_user_id=sender,
        to=[contact],
        subject=subject,
        body=request.answer,
    )
    payload = cast(dict[str, JsonValue], args.model_dump(mode="json"))
    run_id, call_id = followup_proposal_ids(row_id, SEND_SUPPLIER_ANSWER)
    try:
        await PostgresApprovalSink(db).request_approval(
            ApprovalProposal(
                tenant_id=principal.tenant_id,
                run_id=run_id,
                session_id=f"rfq:{rfq_id}",
                tool_call_id=call_id,
                tool=SEND_SUPPLIER_ANSWER,
                args=payload,
                args_hash=args_hash(payload),
                requested_by=sender,
            )
        )
    except ApprovalConflict:
        raise InvalidState("this answer was proposed already") from None

    async with tenant_session(db, principal.tenant_id) as session:
        await _invite(session, rfq_id, invite_id)
        approval = await session.scalar(
            select(Approval)
            .where(Approval.run_id == str(run_id), Approval.tool_call_id == call_id)
            .with_for_update()
        )
        if approval is None:
            raise InvalidState("the answer could not be proposed")
        approval_id = approval.id
        query = select(SupplierQuestion).where(
            SupplierQuestion.invite_id == invite_id, SupplierQuestion.status == "open"
        )
        if request.question_ids:
            query = query.where(SupplierQuestion.id.in_(request.question_ids))
        answered = list(await session.scalars(query.with_for_update()))
        if not answered and approval.status == "pending":
            # Answered or dismissed meanwhile by someone else: this email is not needed.
            approval.status = "expired"
            session.add(approval_audit(approval, "approval.expired", None))
        if answered:
            await session.execute(
                insert(FollowupMessage)
                .values(
                    id=row_id,
                    tenant_id=principal.tenant_id,
                    invite_id=invite_id,
                    kind="answer",
                    sequence=sequence,
                    status="proposed",
                    approval_id=approval_id,
                    regarding_message_id=regarding,
                )
                .on_conflict_do_nothing()
            )
        for question in answered:
            question.status = "answered"
            question.answer = request.answer
            question.answer_followup_id = row_id
            question.decided_by = principal.user_id
            question.decided_at = now
        await session.flush()
        if answered:
            await sync_offer_questions(session, invite_id)
            session.add(
                AuditEvent(
                    tenant_id=principal.tenant_id,
                    actor_user_id=principal.user_id,
                    action="rfq.supplier_question_answered",
                    payload={
                        "rfq_id": str(rfq_id),
                        "invite_id": str(invite_id),
                        "question_ids": [str(q.id) for q in answered],
                        "followup_id": str(row_id),
                        "approval_id": str(approval_id),
                    },
                )
            )
        await session.flush()
    if not answered:
        raise InvalidState("these questions were answered meanwhile")
    logger.info(
        "supplier questions answered tenant=%s invite=%s questions=%d approval=%s by=%s",
        principal.tenant_id,
        invite_id,
        len(answered),
        approval_id,
        principal.user_id,
    )
    return AnsweredQuestions(
        invite_id=invite_id,
        followup_id=row_id,
        approval_id=approval_id,
        question_ids=[q.id for q in answered],
    )


async def dismiss_question(
    db: Db,
    principal: Principal,
    rfq_id: UUID,
    question_id: UUID,
    request: DismissQuestionRequest,
) -> SupplierQuestionView:
    """No answer is needed (answered by phone, or nothing to answer)."""
    now = datetime.now(UTC)
    async with tenant_session(db, principal.tenant_id) as session:
        invite_id = await session.scalar(
            select(SupplierQuestion.invite_id).where(SupplierQuestion.id == question_id)
        )
        if invite_id is None:
            raise NotFound(f"question {question_id} not found")
        await _invite(session, rfq_id, invite_id)
        question = await session.scalar(
            select(SupplierQuestion).where(SupplierQuestion.id == question_id).with_for_update()
        )
        if question is None:
            raise NotFound(f"question {question_id} not found")
        if question.status != "open":
            raise InvalidState(f"question {question_id} is {question.status}")
        question.status = "dismissed"
        question.answer = request.reason
        question.decided_by = principal.user_id
        question.decided_at = now
        await session.flush()
        await sync_offer_questions(session, invite_id)
        session.add(
            AuditEvent(
                tenant_id=principal.tenant_id,
                actor_user_id=principal.user_id,
                action="rfq.supplier_question_dismissed",
                payload={
                    "rfq_id": str(rfq_id),
                    "invite_id": str(invite_id),
                    "question_id": str(question_id),
                    "with_reason": request.reason is not None,
                },
            )
        )
        received_at = question.created_at
        view = SupplierQuestionView(
            id=question.id,
            question=question.question,
            status="dismissed",
            asked_at=received_at,
            answer_followup_id=question.answer_followup_id,
            decided_at=now,
        )
    logger.info(
        "supplier question dismissed tenant=%s question=%s by=%s",
        principal.tenant_id,
        question_id,
        principal.user_id,
    )
    return view
