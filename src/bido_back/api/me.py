"""`GET /v1/me`: who the caller is, in which tenant, with which role.

Replaces the 501 stub in `api/app.py` (`_v1_router`); include this router
INSTEAD of that one, or the stub (registered first) keeps answering.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bido_back.api.deps import get_db, get_principal
from bido_back.api.rfqs import error_responses
from bido_back.auth import Principal, Role
from bido_back.db import tenant_session
from bido_back.db.models import Tenant, User


class MeResponse(BaseModel):
    user_id: UUID
    email: str
    tenant_id: UUID
    tenant_name: str
    role: Role | None
    # The Clerk organization this company is; null for tenants not linked to one.
    clerk_org_id: str | None = None


def me_router() -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["v1"])

    @router.get("/me", responses=error_responses(status.HTTP_404_NOT_FOUND))
    async def get_me(  # pyright: ignore[reportUnusedFunction]
        principal: Annotated[Principal, Depends(get_principal)],
        db: Annotated[async_sessionmaker[AsyncSession], Depends(get_db)],
    ) -> MeResponse:
        async with tenant_session(db, principal.tenant_id) as session:
            tenant = await session.get(Tenant, principal.tenant_id)
            user = await session.get(User, principal.user_id)
        if tenant is None or user is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "user or tenant not found")
        return MeResponse(
            user_id=user.id,
            email=user.email,
            tenant_id=tenant.id,
            tenant_name=tenant.name,
            role=principal.role,
            clerk_org_id=tenant.clerk_org_id,
        )

    return router
