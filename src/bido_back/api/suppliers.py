"""`/v1/suppliers`: the tenant's suppliers and their contacts."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bido_back.api.deps import get_db, get_principal
from bido_back.api.rfqs import error_responses, rfq_http_error
from bido_back.auth import Principal
from bido_back.rfqs import suppliers
from bido_back.rfqs.errors import RfqError
from bido_back.rfqs.schemas import ContactIn, ContactView, CreateSupplierRequest, SupplierView

Db = Annotated[async_sessionmaker[AsyncSession], Depends(get_db)]
CurrentPrincipal = Annotated[Principal, Depends(get_principal)]


def suppliers_router() -> APIRouter:
    router = APIRouter(prefix="/v1/suppliers", tags=["suppliers"])

    @router.post("", status_code=status.HTTP_201_CREATED, responses=error_responses())
    async def create_supplier(  # pyright: ignore[reportUnusedFunction]
        body: CreateSupplierRequest, principal: CurrentPrincipal, db: Db
    ) -> SupplierView:
        return await suppliers.create_supplier(db, principal, body)

    @router.get("", responses=error_responses())
    async def list_suppliers(  # pyright: ignore[reportUnusedFunction]
        principal: CurrentPrincipal, db: Db
    ) -> list[SupplierView]:
        return await suppliers.list_suppliers(db, principal)

    @router.post(
        "/{supplier_id}/contacts",
        status_code=status.HTTP_201_CREATED,
        responses=error_responses(status.HTTP_404_NOT_FOUND),
    )
    async def add_contact(  # pyright: ignore[reportUnusedFunction]
        supplier_id: UUID, body: ContactIn, principal: CurrentPrincipal, db: Db
    ) -> ContactView:
        try:
            return await suppliers.add_contact(db, principal, supplier_id, body)
        except RfqError as exc:
            raise rfq_http_error(exc) from None

    return router
