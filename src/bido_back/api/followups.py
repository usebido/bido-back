"""Follow-up settings and history of an RFQ: `/v1/rfqs/{rfq_id}/followups`.

GET shows the chase schedule and, per invite, where chasing stands and every
chase or clarification proposed. PATCH changes the schedule (and the response
date); the sweeper applies it on its next run. Any member of the tenant may
change it: nothing is sent without an approval.
"""

from uuid import UUID

from fastapi import APIRouter, status

from bido_back.api.rfqs import CurrentPrincipal, Db, error_responses, rfq_http_error
from bido_back.followups import service
from bido_back.followups.schemas import FollowupSettingsUpdate, RfqFollowups
from bido_back.rfqs.errors import RfqError


def followups_router() -> APIRouter:
    router = APIRouter(prefix="/v1/rfqs", tags=["followups"])

    @router.get("/{rfq_id}/followups", responses=error_responses(status.HTTP_404_NOT_FOUND))
    async def get_followups(  # pyright: ignore[reportUnusedFunction]
        rfq_id: UUID, principal: CurrentPrincipal, db: Db
    ) -> RfqFollowups:
        try:
            return await service.get_followups(db, principal, rfq_id)
        except RfqError as exc:
            raise rfq_http_error(exc) from None

    @router.patch("/{rfq_id}/followups", responses=error_responses(status.HTTP_404_NOT_FOUND))
    async def update_followups(  # pyright: ignore[reportUnusedFunction]
        rfq_id: UUID, body: FollowupSettingsUpdate, principal: CurrentPrincipal, db: Db
    ) -> RfqFollowups:
        try:
            return await service.update_followups(db, principal, rfq_id, body)
        except RfqError as exc:
            raise rfq_http_error(exc) from None

    return router
