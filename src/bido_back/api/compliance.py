"""Supplier compliance: the tenant's requirement set and each supplier's checklist.

  GET  /v1/compliance/requirements                       the set in force (or Bido's defaults)
  PUT  /v1/compliance/requirements                       replace it (approver or owner)
  GET  /v1/suppliers/{supplier_id}/compliance            the supplier's checklist
  POST /v1/suppliers/{supplier_id}/compliance/documents  confirm a document

The RFQ comparison carries a per-supplier summary under `compliance`.
"""

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bido_back.api.deps import get_db, get_principal
from bido_back.api.rfqs import error_responses
from bido_back.auth import Principal
from bido_back.compliance import service
from bido_back.compliance.schemas import (
    DocumentView,
    RecordDocumentRequest,
    RequirementsView,
    SupplierChecklist,
    UpdateRequirementsRequest,
)
from bido_back.rfqs.errors import NotFound

Db = Annotated[async_sessionmaker[AsyncSession], Depends(get_db)]
CurrentPrincipal = Annotated[Principal, Depends(get_principal)]


def compliance_http_error(exc: Exception) -> HTTPException:
    """Map a service refusal to an HTTP error. Messages carry ids only."""
    if isinstance(exc, NotFound):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    if isinstance(exc, service.ComplianceForbidden):
        return HTTPException(status.HTTP_403_FORBIDDEN, str(exc))
    if isinstance(exc, service.InvalidDocument):
        return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))
    return HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "compliance operation failed")


def compliance_router() -> APIRouter:
    router = APIRouter(tags=["compliance"])

    @router.get("/v1/compliance/requirements", responses=error_responses())
    async def get_requirements(  # pyright: ignore[reportUnusedFunction]
        principal: CurrentPrincipal, db: Db
    ) -> RequirementsView:
        return await service.get_requirements(db, principal)

    @router.put("/v1/compliance/requirements", responses=error_responses())
    async def put_requirements(  # pyright: ignore[reportUnusedFunction]
        body: UpdateRequirementsRequest, principal: CurrentPrincipal, db: Db
    ) -> RequirementsView:
        try:
            return await service.put_requirements(db, principal, body)
        except service.ComplianceError as exc:
            raise compliance_http_error(exc) from None

    @router.get(
        "/v1/suppliers/{supplier_id}/compliance",
        responses=error_responses(status.HTTP_404_NOT_FOUND),
    )
    async def supplier_checklist(  # pyright: ignore[reportUnusedFunction]
        supplier_id: UUID,
        principal: CurrentPrincipal,
        db: Db,
        rfq_id: Annotated[
            UUID | None, Query(description="Only the lines of this RFQ for per-line items")
        ] = None,
    ) -> SupplierChecklist:
        try:
            return await service.supplier_checklist(
                db, principal, supplier_id, datetime.now(UTC).date(), rfq_id
            )
        except NotFound as exc:
            raise compliance_http_error(exc) from None

    @router.post(
        "/v1/suppliers/{supplier_id}/compliance/documents",
        status_code=status.HTTP_201_CREATED,
        responses=error_responses(status.HTTP_404_NOT_FOUND),
    )
    async def record_document(  # pyright: ignore[reportUnusedFunction]
        supplier_id: UUID, body: RecordDocumentRequest, principal: CurrentPrincipal, db: Db
    ) -> DocumentView:
        try:
            return await service.record_document(db, principal, supplier_id, body)
        except (NotFound, service.ComplianceError) as exc:
            raise compliance_http_error(exc) from None

    return router
