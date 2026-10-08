"""Company data imports and purchase analyses.

* `POST /v1/data/imports`: import a spreadsheet uploaded with
  `POST /v1/onboarding/uploads` as items, a BOM, inventory, purchase history
  or a production plan. `dry_run` checks it without writing.
* `POST /v1/data/workbook-imports`: import ANY workbook (every sheet, any
  layout; supplier contacts too). Answers `needs_confirmation` with questions
  when unsure; send `confirmed: true` after the buyer answered.
* `GET  /v1/data/products`: products with a BOM and/or a production plan.
* `POST /v1/analyses`: what to buy to build a product (quantity, or the
  production plan from today on), from whom and at what price.
* `GET  /v1/analyses/{id}`: one analysis.
* `POST /v1/analyses/{id}/rfq`: a DRAFT RFQ with the lines to buy and the
  suggested suppliers as DRAFT invites. Nothing is sent.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bido_back.api.deps import get_db, get_principal, get_storage
from bido_back.api.rfqs import error_responses
from bido_back.auth import Principal
from bido_back.purchasing import service
from bido_back.purchasing.schemas import (
    AnalysisRequest,
    AnalysisView,
    ImportReport,
    ImportRequest,
    NewRequirementsRequest,
    ProductView,
    RequirementsAnalysisRequest,
    RequirementView,
    RfqFromAnalysis,
    RfqFromAnalysisRequest,
)
from bido_back.purchasing.workbook import (
    SheetMapper,
    WorkbookFileError,
    WorkbookImportReport,
    default_sheet_mapper,
    import_workbook,
)
from bido_back.storage import BlobStorage

Db = Annotated[async_sessionmaker[AsyncSession], Depends(get_db)]
CurrentPrincipal = Annotated[Principal, Depends(get_principal)]
Storage = Annotated[BlobStorage, Depends(get_storage)]


def purchasing_http_error(exc: service.PurchasingError) -> HTTPException:
    if isinstance(exc, service.NotFound):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    if isinstance(exc, service.InvalidRequest):
        return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))
    if isinstance(exc, service.Conflict):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    return HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "purchasing operation failed")


class WorkbookImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # A file uploaded with POST /v1/onboarding/uploads (.xlsx or .csv).
    upload_id: UUID
    # True after the buyer answered the questions of a previous call.
    confirmed: bool = False


def get_sheet_mapper() -> SheetMapper:
    """The sheet mapper over the real runner; tests override it."""
    return default_sheet_mapper()


def purchasing_router() -> APIRouter:
    router = APIRouter(tags=["purchasing"])

    @router.post(
        "/v1/data/workbook-imports",
        responses=error_responses(status.HTTP_422_UNPROCESSABLE_CONTENT),
    )
    async def import_any_workbook(  # pyright: ignore[reportUnusedFunction]
        request: WorkbookImportRequest,
        principal: CurrentPrincipal,
        db: Db,
        storage: Storage,
        mapper: Annotated[SheetMapper, Depends(get_sheet_mapper)],
    ) -> WorkbookImportReport:
        try:
            return await import_workbook(
                db,
                storage,
                principal,
                request.upload_id,
                mapper=mapper,
                confirmed=request.confirmed,
            )
        except WorkbookFileError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None

    @router.post(
        "/v1/data/imports",
        responses=error_responses(status.HTTP_404_NOT_FOUND, status.HTTP_422_UNPROCESSABLE_CONTENT),
    )
    async def import_data(  # pyright: ignore[reportUnusedFunction]
        request: ImportRequest, principal: CurrentPrincipal, db: Db, storage: Storage
    ) -> ImportReport:
        try:
            return await service.run_import(db, storage, principal, request)
        except service.PurchasingError as exc:
            raise purchasing_http_error(exc) from None

    @router.get("/v1/data/products", responses=error_responses())
    async def products(  # pyright: ignore[reportUnusedFunction]
        principal: CurrentPrincipal, db: Db
    ) -> list[ProductView]:
        return await service.list_products(db, principal)

    @router.post(
        "/v1/analyses",
        status_code=status.HTTP_201_CREATED,
        responses=error_responses(status.HTTP_404_NOT_FOUND, status.HTTP_422_UNPROCESSABLE_CONTENT),
    )
    async def create_analysis(  # pyright: ignore[reportUnusedFunction]
        request: AnalysisRequest, principal: CurrentPrincipal, db: Db
    ) -> AnalysisView:
        try:
            return await service.create_analysis(db, principal, request)
        except service.PurchasingError as exc:
            raise purchasing_http_error(exc) from None

    @router.get("/v1/requirements", responses=error_responses())
    async def requirements(  # pyright: ignore[reportUnusedFunction]
        principal: CurrentPrincipal, db: Db, open_only: bool = True
    ) -> list[RequirementView]:
        return await service.list_requirements(db, principal, open_only=open_only)

    @router.post(
        "/v1/requirements",
        status_code=status.HTTP_201_CREATED,
        responses=error_responses(status.HTTP_422_UNPROCESSABLE_CONTENT),
    )
    async def add_requirements(  # pyright: ignore[reportUnusedFunction]
        request: NewRequirementsRequest, principal: CurrentPrincipal, db: Db
    ) -> list[RequirementView]:
        try:
            return await service.add_requirements(
                db,
                principal,
                [
                    service.NewRequirement(
                        part_number=line.part_number,
                        quantity=line.quantity,
                        need_by=line.need_by,
                        unit=line.unit,
                        description=line.description,
                        reference=line.reference,
                    )
                    for line in request.lines
                ],
            )
        except service.PurchasingError as exc:
            raise purchasing_http_error(exc) from None

    @router.post(
        "/v1/analyses/requirements",
        status_code=status.HTTP_201_CREATED,
        responses=error_responses(status.HTTP_404_NOT_FOUND, status.HTTP_422_UNPROCESSABLE_CONTENT),
    )
    async def create_requirements_analysis(  # pyright: ignore[reportUnusedFunction]
        request: RequirementsAnalysisRequest, principal: CurrentPrincipal, db: Db
    ) -> AnalysisView:
        try:
            return await service.create_requirements_analysis(db, principal, request)
        except service.PurchasingError as exc:
            raise purchasing_http_error(exc) from None

    @router.get("/v1/analyses/{analysis_id}", responses=error_responses(status.HTTP_404_NOT_FOUND))
    async def get_analysis(  # pyright: ignore[reportUnusedFunction]
        analysis_id: UUID, principal: CurrentPrincipal, db: Db
    ) -> AnalysisView:
        try:
            return await service.get_analysis(db, principal, analysis_id)
        except service.PurchasingError as exc:
            raise purchasing_http_error(exc) from None

    @router.post(
        "/v1/analyses/{analysis_id}/rfq",
        status_code=status.HTTP_201_CREATED,
        responses=error_responses(
            status.HTTP_404_NOT_FOUND,
            status.HTTP_409_CONFLICT,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
        ),
    )
    async def rfq_from_analysis(  # pyright: ignore[reportUnusedFunction]
        analysis_id: UUID,
        principal: CurrentPrincipal,
        db: Db,
        request: RfqFromAnalysisRequest | None = None,
    ) -> RfqFromAnalysis:
        try:
            return await service.create_rfq_from_analysis(
                db, principal, analysis_id, request or RfqFromAnalysisRequest()
            )
        except service.PurchasingError as exc:
            raise purchasing_http_error(exc) from None

    return router
