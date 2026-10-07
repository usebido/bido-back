"""`/v1/approvals`: the approval queue, and a person's decision on one act proposal.

`GET /v1/approvals?status=pending` lists the tenant's approvals with, for
`send_rfq_email`, the exact email (the stored, hashed arguments) to review.

The body carries only the decision. Who decides comes from `get_principal`;
what is decided is the payload already stored in `approvals`; everything is
checked by `bido_back.approvals.decide_approval`. Nothing here executes the
action, and Agno's own continue endpoints are never exposed.
"""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bido_back.api.deps import get_db, get_job_queue, get_principal
from bido_back.api.errors import ErrorResponse
from bido_back.approvals import (
    ApprovalError,
    ApprovalForbidden,
    ApprovalIntegrityError,
    ApprovalNotFound,
    ApprovalNotPending,
    decide_approval,
)
from bido_back.auth import Principal
from bido_back.followups.send import FOLLOWUP_REJECT_HOOKS
from bido_back.rfqs.schemas import ApprovalQueueItem, ApprovalStatus
from bido_back.rfqs.send import REJECT_HOOKS
from bido_back.rfqs.service import list_approvals
from bido_back.worker.jobs import JobQueue


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]


class DecisionResponse(BaseModel):
    approval_id: UUID
    status: Literal["approved", "rejected"]


_HTTP_STATUS: dict[type[ApprovalError], int] = {
    ApprovalForbidden: status.HTTP_403_FORBIDDEN,
    ApprovalNotFound: status.HTTP_404_NOT_FOUND,
    ApprovalNotPending: status.HTTP_409_CONFLICT,
    ApprovalIntegrityError: status.HTTP_409_CONFLICT,
}


def approvals_router() -> APIRouter:
    router = APIRouter(prefix="/v1/approvals", tags=["approvals"])

    @router.get(
        "",
        responses={
            code: {"model": ErrorResponse}
            for code in (
                status.HTTP_400_BAD_REQUEST,
                status.HTTP_401_UNAUTHORIZED,
                status.HTTP_403_FORBIDDEN,
            )
        },
    )
    async def queue(  # pyright: ignore[reportUnusedFunction]
        principal: Annotated[Principal, Depends(get_principal)],
        db: Annotated[async_sessionmaker[AsyncSession], Depends(get_db)],
        status_filter: Annotated[ApprovalStatus, Query(alias="status")] = "pending",
    ) -> list[ApprovalQueueItem]:
        """The approval queue, oldest first, with the email draft of each RFQ send."""
        return await list_approvals(db, principal, status_filter)

    @router.post(
        "/{approval_id}/decision",
        responses={
            code: {"model": ErrorResponse}
            for code in (
                status.HTTP_401_UNAUTHORIZED,
                status.HTTP_403_FORBIDDEN,
                status.HTTP_404_NOT_FOUND,
                status.HTTP_409_CONFLICT,
                status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        },
    )
    async def decide(  # pyright: ignore[reportUnusedFunction]
        approval_id: UUID,
        body: DecisionRequest,
        principal: Annotated[Principal, Depends(get_principal)],
        db: Annotated[async_sessionmaker[AsyncSession], Depends(get_db)],
        jobs: Annotated[JobQueue, Depends(get_job_queue)],
    ) -> DecisionResponse:
        try:
            outcome = await decide_approval(
                db,
                principal,
                approval_id,
                body.decision,
                jobs=jobs,
                on_reject={**REJECT_HOOKS, **FOLLOWUP_REJECT_HOOKS},
            )
        except (
            ApprovalForbidden,
            ApprovalNotFound,
            ApprovalNotPending,
            ApprovalIntegrityError,
        ) as exc:
            raise HTTPException(_HTTP_STATUS[type(exc)], str(exc)) from None
        return DecisionResponse(approval_id=outcome.approval_id, status=outcome.status)

    return router
