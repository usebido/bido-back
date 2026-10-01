"""`/v1/approvals`: a person decides on one recorded act proposal.

The body carries only the decision. Who decides comes from `get_principal`;
what is decided is the payload already stored in `approvals`; everything is
checked by `bido_back.approvals.decide_approval`. Nothing here executes the
action, and Agno's own continue endpoints are never exposed.
"""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
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
            outcome = await decide_approval(db, principal, approval_id, body.decision, jobs=jobs)
        except (
            ApprovalForbidden,
            ApprovalNotFound,
            ApprovalNotPending,
            ApprovalIntegrityError,
        ) as exc:
            raise HTTPException(_HTTP_STATUS[type(exc)], str(exc)) from None
        return DecisionResponse(approval_id=outcome.approval_id, status=outcome.status)

    return router
