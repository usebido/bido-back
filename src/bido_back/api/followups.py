"""Follow-ups of an RFQ: `/v1/rfqs/{rfq_id}/followups` and the supplier questions.

GET shows the chase schedule and, per invite, where it stands (`phase`: waiting
for the supplier, an email waiting for approval, needs your answer, needs your
decision...), what Bido asked and when (the clarification ledger), what the
latest offer still lacks, the supplier's questions, and every follow-up
proposed. PATCH changes the schedule (and the response date); the sweeper
applies it on its next run. Any member of the tenant may change it: nothing is
sent without an approval.

`POST .../invites/{invite_id}/answers` records the buyer's answer to the
supplier's open questions and proposes it, as written, for approval (a
negotiation mandate never approves it). `POST .../questions/{id}/dismiss`
closes a question that needs no answer. Either way, automatic emails to that
supplier resume. `POST .../questions/{id}/reopen` opens again a question Bido
considered superseded by the supplier's later offer (or one dismissed): the
offer waits for the buyer's answer again.
"""

from uuid import UUID

from fastapi import APIRouter, status

from bido_back.api.rfqs import CurrentPrincipal, Db, error_responses, rfq_http_error
from bido_back.followups import questions, service
from bido_back.followups.schemas import (
    AnsweredQuestions,
    AnswerQuestionsRequest,
    DismissQuestionRequest,
    FollowupSettingsUpdate,
    RfqFollowups,
    SupplierQuestionView,
)
from bido_back.rfqs.errors import RfqError


def followups_router() -> APIRouter:
    router = APIRouter(prefix="/v1/rfqs", tags=["followups"])

    @router.get("/{rfq_id}/followups", responses=error_responses(status.HTTP_404_NOT_FOUND))
    async def get_followups(  # pyright: ignore[reportUnusedFunction]
        rfq_id: UUID, principal: CurrentPrincipal, db: Db
    ) -> RfqFollowups:
        try:
            return await service.get_followups(db, principal, rfq_id)
        except RfqError as exc:
            raise rfq_http_error(exc) from None

    @router.patch("/{rfq_id}/followups", responses=error_responses(status.HTTP_404_NOT_FOUND))
    async def update_followups(  # pyright: ignore[reportUnusedFunction]
        rfq_id: UUID, body: FollowupSettingsUpdate, principal: CurrentPrincipal, db: Db
    ) -> RfqFollowups:
        try:
            return await service.update_followups(db, principal, rfq_id, body)
        except RfqError as exc:
            raise rfq_http_error(exc) from None

    @router.post(
        "/{rfq_id}/invites/{invite_id}/answers",
        status_code=status.HTTP_201_CREATED,
        responses=error_responses(status.HTTP_404_NOT_FOUND, status.HTTP_409_CONFLICT),
    )
    async def answer_questions(  # pyright: ignore[reportUnusedFunction]
        rfq_id: UUID,
        invite_id: UUID,
        body: AnswerQuestionsRequest,
        principal: CurrentPrincipal,
        db: Db,
    ) -> AnsweredQuestions:
        """Answer the supplier's questions: the email waits for approval."""
        try:
            return await questions.answer_questions(db, principal, rfq_id, invite_id, body)
        except RfqError as exc:
            raise rfq_http_error(exc) from None

    @router.post(
        "/{rfq_id}/questions/{question_id}/dismiss",
        responses=error_responses(status.HTTP_404_NOT_FOUND, status.HTTP_409_CONFLICT),
    )
    async def dismiss_question(  # pyright: ignore[reportUnusedFunction]
        rfq_id: UUID,
        question_id: UUID,
        body: DismissQuestionRequest,
        principal: CurrentPrincipal,
        db: Db,
    ) -> SupplierQuestionView:
        """No answer needed (answered elsewhere): automatic emails resume."""
        try:
            return await questions.dismiss_question(db, principal, rfq_id, question_id, body)
        except RfqError as exc:
            raise rfq_http_error(exc) from None

    @router.post(
        "/{rfq_id}/questions/{question_id}/reopen",
        responses=error_responses(status.HTTP_404_NOT_FOUND, status.HTTP_409_CONFLICT),
    )
    async def reopen_question(  # pyright: ignore[reportUnusedFunction]
        rfq_id: UUID,
        question_id: UUID,
        principal: CurrentPrincipal,
        db: Db,
    ) -> SupplierQuestionView:
        """Open again a question considered superseded by the supplier's later offer (or
        dismissed): the offer waits for your answer, automatic emails pause."""
        try:
            return await questions.reopen_question(db, principal, rfq_id, question_id)
        except RfqError as exc:
            raise rfq_http_error(exc) from None

    return router
