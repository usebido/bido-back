"""`/v1/search`: find RFQs, requested lines, suppliers and quoted parts in the tenant."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bido_back.api.deps import get_db, get_principal
from bido_back.api.rfqs import error_responses
from bido_back.auth import Principal
from bido_back.search.service import MAX_LIMIT, MAX_QUERY, MIN_QUERY, SearchResults, search

Db = Annotated[async_sessionmaker[AsyncSession], Depends(get_db)]
CurrentPrincipal = Annotated[Principal, Depends(get_principal)]


def search_router() -> APIRouter:
    router = APIRouter(prefix="/v1/search", tags=["search"])

    @router.get("", responses=error_responses())
    async def search_tenant(  # pyright: ignore[reportUnusedFunction]
        principal: CurrentPrincipal,
        db: Db,
        q: Annotated[str, Query(min_length=MIN_QUERY, max_length=MAX_QUERY)],
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 20,
        offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
    ) -> SearchResults:
        """Case-insensitive substring match on RFQ ref/title, line description/MPN,
        supplier name and offered MPN. Ordered by kind, then newest first."""
        return await search(db, principal, q, limit=limit, offset=offset)

    return router
