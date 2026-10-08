"""Persist source-backed procurement assessments and hand validated lines to draft RFQs."""

from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status

from bido_back.agents.context import ToolContext
from bido_back.api.purchasing import CurrentPrincipal, Db, Storage, purchasing_http_error
from bido_back.api.rfqs import error_responses
from bido_back.purchasing.service import PurchasingError
from bido_back.purchasing.sources.contracts import SourceAssessment
from bido_back.purchasing.sources.mapping import Mapper, default_mapper
from bido_back.purchasing.sources.service import SourceAccessError, analyze_uploads
from bido_back.purchasing.sources.snapshots import (
    AssessmentRequest,
    AssessmentRfqRequest,
    AssessmentRfqResult,
    AssessmentSnapshotSummary,
    draft_from_snapshot,
    get_snapshot,
    list_snapshots,
)


def get_source_mapper() -> Mapper:
    return default_mapper()


def procurement_assessments_router() -> APIRouter:
    router = APIRouter(prefix="/v1/procurement-assessments", tags=["purchasing"])

    @router.get("")
    async def list_assessments(  # pyright: ignore[reportUnusedFunction]
        principal: CurrentPrincipal,
        db: Db,
        limit: Annotated[int, Query(ge=1, le=50)] = 20,
        search: Annotated[str | None, Query(max_length=2000)] = None,
    ) -> list[AssessmentSnapshotSummary]:
        return await list_snapshots(db, principal, limit=limit, search=search)

    @router.post("", responses=error_responses(status.HTTP_404_NOT_FOUND))
    async def create_assessment(  # pyright: ignore[reportUnusedFunction]
        request: AssessmentRequest,
        principal: CurrentPrincipal,
        db: Db,
        storage: Storage,
        mapper: Annotated[Mapper, Depends(get_source_mapper)],
    ) -> SourceAssessment:
        try:
            return await analyze_uploads(
                db,
                storage,
                principal,
                request.upload_ids,
                request.objective,
                ToolContext(
                    tenant_id=principal.tenant_id, user_id=principal.user_id, run_id=uuid4()
                ),
                mapper,
                persist=True,
            )
        except SourceAccessError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None

    @router.get("/{assessment_id}", responses=error_responses(status.HTTP_404_NOT_FOUND))
    async def read_assessment(  # pyright: ignore[reportUnusedFunction]
        assessment_id: UUID,
        principal: CurrentPrincipal,
        db: Db,
    ) -> SourceAssessment:
        try:
            return await get_snapshot(db, principal, assessment_id)
        except PurchasingError as exc:
            raise purchasing_http_error(exc) from None

    @router.post(
        "/{assessment_id}/rfq",
        responses=error_responses(
            status.HTTP_404_NOT_FOUND,
            status.HTTP_409_CONFLICT,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
        ),
    )
    async def create_assessment_rfq(  # pyright: ignore[reportUnusedFunction]
        assessment_id: UUID,
        request: AssessmentRfqRequest,
        principal: CurrentPrincipal,
        db: Db,
    ) -> AssessmentRfqResult:
        try:
            return await draft_from_snapshot(db, principal, assessment_id, request)
        except PurchasingError as exc:
            raise purchasing_http_error(exc) from None

    return router
