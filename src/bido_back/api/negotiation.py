"""The negotiation of an RFQ: `/v1/rfqs/{rfq_id}/negotiation`.

GET shows the settings (deadline, round cap, per-line limits), where each
supplier stands (rounds used, how its latest offer compares to the limits,
every counter-offer proposed) and, once closed, the outcome. PUT saves the
settings and opens the negotiation; the sweeper applies it on its next run.
POST `/close` closes it now (the buyer saw "ready to close", or decides not
to wait) and returns the outcome: the best eligible offer per line and tier.
Nothing is sent without an approval and nothing is awarded here.

The mandate: POST `/mandate` renders the initial emails and records ONE
approval covering them and the negotiation's bounds; once a person approves
it, in-bounds follow-ups and counter-offers go out without further approvals
(`bido_back.mandates`). POST `/mandate/pause` is the kill switch, `/resume`
undoes it, GET `/mandate` shows what went out automatically and what was held.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status

from bido_back.api.deps import get_mailbox_senders, get_rfq_agents
from bido_back.api.rfqs import CurrentPrincipal, Db, error_responses, rfq_http_error
from bido_back.mandates import service as mandates
from bido_back.mandates.schemas import (
    MandateActivity,
    MandateView,
    ProposedMandate,
    ProposeMandateRequest,
)
from bido_back.negotiation import service
from bido_back.negotiation.schemas import NegotiationSettingsUpdate, RfqNegotiation
from bido_back.rfqs.agents import RfqAgents
from bido_back.rfqs.errors import RfqError
from bido_back.rfqs.mailbox import MailboxSenderFactory


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

    @router.post(
        "/{rfq_id}/negotiation/mandate",
        status_code=status.HTTP_201_CREATED,
        responses=error_responses(
            status.HTTP_404_NOT_FOUND, status.HTTP_409_CONFLICT, status.HTTP_502_BAD_GATEWAY
        ),
    )
    async def propose_mandate(  # pyright: ignore[reportUnusedFunction]
        rfq_id: UUID,
        body: ProposeMandateRequest,
        principal: CurrentPrincipal,
        db: Db,
        agents: Annotated[RfqAgents, Depends(get_rfq_agents)],
        mailboxes: Annotated[MailboxSenderFactory, Depends(get_mailbox_senders)],
    ) -> ProposedMandate:
        """Render the initial emails and record one approval for the whole negotiation."""
        try:
            return await mandates.propose_mandate(
                db, principal, rfq_id, body, writer=agents.email_writer, mailboxes=mailboxes
            )
        except RfqError as exc:
            raise rfq_http_error(exc) from None

    @router.get(
        "/{rfq_id}/negotiation/mandate", responses=error_responses(status.HTTP_404_NOT_FOUND)
    )
    async def mandate_activity(  # pyright: ignore[reportUnusedFunction]
        rfq_id: UUID, principal: CurrentPrincipal, db: Db
    ) -> MandateActivity:
        try:
            return await mandates.get_activity(db, principal, rfq_id)
        except RfqError as exc:
            raise rfq_http_error(exc) from None

    @router.post(
        "/{rfq_id}/negotiation/mandate/pause",
        responses=error_responses(status.HTTP_404_NOT_FOUND),
    )
    async def pause_mandate(  # pyright: ignore[reportUnusedFunction]
        rfq_id: UUID, principal: CurrentPrincipal, db: Db
    ) -> MandateView:
        """Stop automatic sends now. Pending emails wait for a person."""
        try:
            return await mandates.set_paused(db, principal, rfq_id, paused=True)
        except RfqError as exc:
            raise rfq_http_error(exc) from None

    @router.post(
        "/{rfq_id}/negotiation/mandate/resume",
        responses=error_responses(status.HTTP_404_NOT_FOUND),
    )
    async def resume_mandate(  # pyright: ignore[reportUnusedFunction]
        rfq_id: UUID, principal: CurrentPrincipal, db: Db
    ) -> MandateView:
        try:
            return await mandates.set_paused(db, principal, rfq_id, paused=False)
        except RfqError as exc:
            raise rfq_http_error(exc) from None

    return router
