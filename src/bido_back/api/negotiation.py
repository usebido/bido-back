"""The negotiation of an RFQ: `/v1/rfqs/{rfq_id}/negotiation`.

GET shows the settings (deadline, round cap, per-line limits), where each
supplier stands (its state in words: waiting for the supplier until when, held
for approval, accepted internally, needs your decision or answer, closed; the
rounds sent; how its latest offer compares to the limits and need-by dates;
every email decided) and, once closed, the outcome. POST
`/suppliers/{invite_id}/release-hold` negotiates an offer held for review as
it is (the buyer's audited decision). PUT saves the
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
from bido_back.comparison.policy import ComparisonPolicy
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
        "/{rfq_id}/negotiation/suppliers/{invite_id}/release-hold",
        responses=error_responses(status.HTTP_404_NOT_FOUND, status.HTTP_409_CONFLICT),
    )
    async def release_hold(  # pyright: ignore[reportUnusedFunction]
        rfq_id: UUID, invite_id: UUID, principal: CurrentPrincipal, db: Db
    ) -> RfqNegotiation:
        """Negotiate a supplier's current offer as it is, though it was held for review.

        The buyer's decision, audited. A newer offer from that supplier is held
        again if it needs to be; a withdrawn offer stays closed.
        """
        try:
            return await service.release_hold(db, principal, rfq_id, invite_id)
        except RfqError as exc:
            raise rfq_http_error(exc) from None

    @router.put(
        "/{rfq_id}/comparison/policy",
        responses=error_responses(status.HTTP_404_NOT_FOUND),
    )
    async def put_comparison_policy(  # pyright: ignore[reportUnusedFunction]
        rfq_id: UUID, body: ComparisonPolicy, principal: CurrentPrincipal, db: Db
    ) -> ComparisonPolicy:
        """Set buyer review priorities without opening negotiation or changing its bounds."""
        try:
            return await service.set_comparison_policy(db, principal, rfq_id, body)
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
