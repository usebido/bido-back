"""`/v1/proposals`: a supplier's extracted quote, and correcting its values.

`GET /v1/proposals/{id}` shows a proposal with its lines, what is still
flagged for review and what a person corrected. The two `PATCH` routes
correct (or, with the current value, confirm) header and line fields; each
field becomes a `field_corrections` row and the comparison uses the new
value. Any member of the tenant may correct.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bido_back.api.deps import get_db, get_principal
from bido_back.api.rfqs import error_responses, rfq_http_error
from bido_back.auth import Principal
from bido_back.corrections import service
from bido_back.corrections.schemas import (
    FieldCorrectionView,
    HeaderCorrectionRequest,
    LineCorrectionRequest,
    ProposalLineView,
    ProposalView,
)
from bido_back.rfqs.errors import RfqError

Db = Annotated[async_sessionmaker[AsyncSession], Depends(get_db)]
CurrentPrincipal = Annotated[Principal, Depends(get_principal)]


def proposals_router() -> APIRouter:
    router = APIRouter(prefix="/v1/proposals", tags=["proposals"])

    @router.get("/{proposal_id}", responses=error_responses(status.HTTP_404_NOT_FOUND))
    async def get_proposal(  # pyright: ignore[reportUnusedFunction]
        proposal_id: UUID, principal: CurrentPrincipal, db: Db
    ) -> ProposalView:
        try:
            return await service.get_proposal(db, principal, proposal_id)
        except RfqError as exc:
            raise rfq_http_error(exc) from None

    @router.patch(
        "/{proposal_id}",
        responses=error_responses(status.HTTP_404_NOT_FOUND, status.HTTP_409_CONFLICT),
    )
    async def correct_header(  # pyright: ignore[reportUnusedFunction]
        proposal_id: UUID, body: HeaderCorrectionRequest, principal: CurrentPrincipal, db: Db
    ) -> ProposalView:
        """Correct or confirm currency, validity, Incoterm or payment terms."""
        try:
            return await service.correct_header(db, principal, proposal_id, body)
        except RfqError as exc:
            raise rfq_http_error(exc) from None

    @router.patch(
        "/{proposal_id}/lines/{line_id}",
        responses=error_responses(status.HTTP_404_NOT_FOUND, status.HTTP_409_CONFLICT),
    )
    async def correct_line(  # pyright: ignore[reportUnusedFunction]
        proposal_id: UUID,
        line_id: UUID,
        body: LineCorrectionRequest,
        principal: CurrentPrincipal,
        db: Db,
    ) -> ProposalLineView:
        """Correct or confirm a quoted line: price, quantity break, MPN, lead time."""
        try:
            return await service.correct_line(db, principal, proposal_id, line_id, body)
        except RfqError as exc:
            raise rfq_http_error(exc) from None

    @router.get("/{proposal_id}/corrections", responses=error_responses(status.HTTP_404_NOT_FOUND))
    async def list_corrections(  # pyright: ignore[reportUnusedFunction]
        proposal_id: UUID, principal: CurrentPrincipal, db: Db
    ) -> list[FieldCorrectionView]:
        try:
            return await service.list_corrections(db, principal, proposal_id)
        except RfqError as exc:
            raise rfq_http_error(exc) from None

    return router
