"""The negotiation of an RFQ: `/v1/rfqs/{rfq_id}/negotiation`.

GET shows the settings (deadline, round cap, per-line limits), where each
supplier stands (rounds used, how its latest offer compares to the limits,
every counter-offer proposed) and, once closed, the outcome. PUT saves the
settings and opens the negotiation; the sweeper applies it on its next run.
POST `/close` closes it now (the buyer saw "ready to close", or decides not
to wait) and returns the outcome: the best eligible offer per line and tier.
Nothing is sent without an approval and nothing is awarded here.
"""

from uuid import UUID

from fastapi import APIRouter, status

from bido_back.api.rfqs import CurrentPrincipal, Db, error_responses, rfq_http_error
from bido_back.negotiation import service
from bido_back.negotiation.schemas import NegotiationSettingsUpdate, RfqNegotiation
from bido_back.rfqs.errors import RfqError


def negotiation_router() -> APIRouter:
    router = APIRouter(prefix="/v1/rfqs", tags=["negotiation"])

    @router.get("/{rfq_id}/negotiation", responses=error_responses(status.HTTP_404_NOT_FOUND))
    async def get_negotiation(  # pyright: ignore[reportUnusedFunction]
        rfq_id: UUID, principal: CurrentPrincipal, db: Db
    ) -> RfqNegotiation:
        try:
            return await service.get_negotiation(db, principal, rfq_id)
        except RfqError as exc:
            raise rfq_http_error(exc) from None

    @router.put(
        "/{rfq_id}/negotiation",
        responses=error_responses(status.HTTP_404_NOT_FOUND, status.HTTP_409_CONFLICT),
    )
    async def put_negotiation(  # pyright: ignore[reportUnusedFunction]
        rfq_id: UUID, body: NegotiationSettingsUpdate, principal: CurrentPrincipal, db: Db
    ) -> RfqNegotiation:
        try:
            return await service.update_negotiation(db, principal, rfq_id, body)
        except RfqError as exc:
            raise rfq_http_error(exc) from None

    @router.post(
        "/{rfq_id}/negotiation/close",
        responses=error_responses(status.HTTP_404_NOT_FOUND),
    )
    async def close_negotiation(  # pyright: ignore[reportUnusedFunction]
        rfq_id: UUID, principal: CurrentPrincipal, db: Db
    ) -> RfqNegotiation:
        try:
            await service.close(db, principal, rfq_id)
            return await service.get_negotiation(db, principal, rfq_id)
        except RfqError as exc:
            raise rfq_http_error(exc) from None

    return router
