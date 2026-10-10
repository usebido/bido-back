"""Persist source-backed procurement assessments and hand validated lines to draft RFQs."""

from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from bido_back.agents.context import ToolContext
from bido_back.api.purchasing import CurrentPrincipal, Db, Storage, purchasing_http_error
from bido_back.api.rfqs import error_responses
from bido_back.exports.procurement_plan import PlanLanguage, plan_filename, plan_workbook
from bido_back.exports.render import MEDIA_TYPES
from bido_back.purchasing.service import PurchasingError
from bido_back.purchasing.sources.contracts import SourceAssessment
from bido_back.purchasing.sources.mapping import Mapper, default_mapper
from bido_back.purchasing.sources.service import (
    ScopeValidationError,
    SourceAccessError,
    analyze_uploads,
)
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

    @router.post(
        "",
        responses=error_responses(status.HTTP_400_BAD_REQUEST, status.HTTP_404_NOT_FOUND),
    )
    async def create_assessment(  # pyright: ignore[reportUnusedFunction]
        request: AssessmentRequest,
        principal: CurrentPrincipal,
        db: Db,
        storage: Storage,
        mapper: Annotated[Mapper, Depends(get_source_mapper)],
    ) -> SourceAssessment:
        """Assess the uploads and save the evidence snapshot.

        `scope` carries the buyer's planning boundaries as stated, all optional (a product
        name or code, plant, reference date, horizon end or days, safety stock). They are
        resolved against the mapped evidence: `scope` in the response is the resolved scope
        and `scope_resolution` the assumptions made and the questions still open. A plant,
        MRP element or product the sources do not hold is a 400 naming the valid values.
        """
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
                scope=request.scope,
            )
        except SourceAccessError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None
        except ScopeValidationError as exc:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"{exc.code}: {exc} (scope.{exc.field}={exc.value!r}; valid: "
                + ", ".join(exc.valid_values)
                + ")",
            ) from None

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

    @router.get(
        "/{assessment_id}/plan.xlsx",
        response_class=Response,
        responses={
            status.HTTP_200_OK: {
                "content": {
                    MEDIA_TYPES["xlsx"]: {"schema": {"type": "string", "format": "binary"}}
                },
                "description": "The whole plan, one row per material and plant, as an attachment",
            },
            **error_responses(status.HTTP_404_NOT_FOUND),
        },
    )
    async def download_plan(  # pyright: ignore[reportUnusedFunction]
        assessment_id: UUID,
        principal: CurrentPrincipal,
        db: Db,
        lang: PlanLanguage = "pt",
    ) -> Response:
        """The assessment's full purchase plan as .xlsx: a summary, every material and plant
        (with nothing to buy too), one sheet per plant, the actions and the assumptions.

        Rendered on request from the saved assessment; nothing new is stored."""
        try:
            assessment = await get_snapshot(db, principal, assessment_id)
        except PurchasingError as exc:
            raise purchasing_http_error(exc) from None
        return Response(
            content=plan_workbook(assessment, lang),
            media_type=MEDIA_TYPES["xlsx"],
            headers={
                "Content-Disposition": (f'attachment; filename="{plan_filename(assessment, lang)}"')
            },
        )

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
