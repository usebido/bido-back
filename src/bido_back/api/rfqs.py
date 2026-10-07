"""`/v1/rfqs`: RFQs from text, their lines and invites, preparing sends, the comparison.

Every route is tenant-scoped through `get_principal` and the services'
`tenant_session`. Preparing a send records pending approvals only; nothing
is sent until an approver approves (`/v1/approvals/{id}/decision`) and the
worker executes the approved `send_rfq_email`.
"""

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bido_back.api.deps import get_db, get_mailbox_senders, get_principal, get_rfq_agents
from bido_back.api.errors import ErrorResponse
from bido_back.auth import Principal
from bido_back.comparison.compute import Comparison
from bido_back.comparison.loader import load_comparison
from bido_back.compliance import service as compliance_service
from bido_back.rfqs import service
from bido_back.rfqs.agents import RfqAgents
from bido_back.rfqs.errors import (
    AgentFailed,
    Duplicate,
    InvalidState,
    MailboxNotConfigured,
    MailboxNotConnected,
    NotFound,
    RfqError,
)
from bido_back.rfqs.mailbox import MailboxSenderFactory
from bido_back.rfqs.schemas import (
    CreateInviteRequest,
    CreateRfqRequest,
    InviteView,
    PrepareSendResponse,
    RfqCreated,
    RfqDetail,
    RfqLineView,
    RfqSummary,
    UpdateLineRequest,
)
from bido_back.templates.schemas import RfqEmailPreview

Db = Annotated[async_sessionmaker[AsyncSession], Depends(get_db)]
CurrentPrincipal = Annotated[Principal, Depends(get_principal)]

_AUTH_ERRORS = (
    status.HTTP_400_BAD_REQUEST,
    status.HTTP_401_UNAUTHORIZED,
    status.HTTP_403_FORBIDDEN,
)


def error_responses(*codes: int) -> dict[int | str, dict[str, object]]:
    return {code: {"model": ErrorResponse} for code in (*_AUTH_ERRORS, *codes)}


def rfq_http_error(exc: RfqError) -> HTTPException:
    """Map a service refusal to an HTTP error. Messages carry ids only."""
    if isinstance(exc, NotFound):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    if isinstance(exc, MailboxNotConnected):
        return HTTPException(
            status.HTTP_409_CONFLICT,
            "connect your mailbox before sending RFQs: they are sent from it",
        )
    if isinstance(exc, MailboxNotConfigured):
        return HTTPException(
            status.HTTP_409_CONFLICT,
            "sending from a mailbox is not set up on this server yet",
        )
    if isinstance(exc, Duplicate | InvalidState):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    if isinstance(exc, AgentFailed):
        return HTTPException(status.HTTP_502_BAD_GATEWAY, "the agent could not complete")
    return HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "rfq operation failed")


def rfqs_router() -> APIRouter:
    router = APIRouter(prefix="/v1/rfqs", tags=["rfqs"])

    @router.post(
        "",
        status_code=status.HTTP_201_CREATED,
        responses=error_responses(status.HTTP_502_BAD_GATEWAY),
    )
    async def create_rfq(  # pyright: ignore[reportUnusedFunction]
        body: CreateRfqRequest,
        principal: CurrentPrincipal,
        db: Db,
        agents: Annotated[RfqAgents, Depends(get_rfq_agents)],
    ) -> RfqCreated:
        try:
            return await service.create_rfq_from_text(db, principal, body.text, agents.intake)
        except RfqError as exc:
            raise rfq_http_error(exc) from None

    @router.get("", responses=error_responses())
    async def list_rfqs(  # pyright: ignore[reportUnusedFunction]
        principal: CurrentPrincipal, db: Db
    ) -> list[RfqSummary]:
        return await service.list_rfqs(db, principal)

    @router.get("/{rfq_id}", responses=error_responses(status.HTTP_404_NOT_FOUND))
    async def get_rfq(  # pyright: ignore[reportUnusedFunction]
        rfq_id: UUID, principal: CurrentPrincipal, db: Db
    ) -> RfqDetail:
        try:
            return await service.get_rfq(db, principal, rfq_id)
        except RfqError as exc:
            raise rfq_http_error(exc) from None

    @router.patch(
        "/{rfq_id}/lines/{line_id}",
        responses=error_responses(status.HTTP_404_NOT_FOUND, status.HTTP_409_CONFLICT),
    )
    async def update_line(  # pyright: ignore[reportUnusedFunction]
        rfq_id: UUID,
        line_id: UUID,
        body: UpdateLineRequest,
        principal: CurrentPrincipal,
        db: Db,
    ) -> RfqLineView:
        try:
            return await service.update_line(db, principal, rfq_id, line_id, body)
        except RfqError as exc:
            raise rfq_http_error(exc) from None

    @router.post(
        "/{rfq_id}/invites",
        status_code=status.HTTP_201_CREATED,
        responses=error_responses(status.HTTP_404_NOT_FOUND, status.HTTP_409_CONFLICT),
    )
    async def add_invite(  # pyright: ignore[reportUnusedFunction]
        rfq_id: UUID, body: CreateInviteRequest, principal: CurrentPrincipal, db: Db
    ) -> InviteView:
        try:
            return await service.add_invite(
                db, principal, rfq_id, body.supplier_id, body.contact_id
            )
        except RfqError as exc:
            raise rfq_http_error(exc) from None

    @router.get(
        "/{rfq_id}/invites/{invite_id}/preview",
        responses=error_responses(
            status.HTTP_404_NOT_FOUND, status.HTTP_409_CONFLICT, status.HTTP_502_BAD_GATEWAY
        ),
    )
    async def preview_email(  # pyright: ignore[reportUnusedFunction]
        rfq_id: UUID,
        invite_id: UUID,
        principal: CurrentPrincipal,
        db: Db,
        agents: Annotated[RfqAgents, Depends(get_rfq_agents)],
    ) -> RfqEmailPreview:
        """The email this invite would get, rendered from the RFQ's template. Sends nothing."""
        try:
            return await service.preview_email(
                db, principal, rfq_id, invite_id, agents.email_writer
            )
        except RfqError as exc:
            raise rfq_http_error(exc) from None

    @router.post(
        "/{rfq_id}/send",
        responses=error_responses(
            status.HTTP_404_NOT_FOUND, status.HTTP_409_CONFLICT, status.HTTP_502_BAD_GATEWAY
        ),
    )
    async def prepare_send(  # pyright: ignore[reportUnusedFunction]
        rfq_id: UUID,
        principal: CurrentPrincipal,
        db: Db,
        agents: Annotated[RfqAgents, Depends(get_rfq_agents)],
        mailboxes: Annotated[MailboxSenderFactory, Depends(get_mailbox_senders)],
    ) -> PrepareSendResponse:
        """Render each invite's email and queue it for approval. Sends nothing."""
        try:
            return await service.prepare_send(db, principal, rfq_id, agents.email_writer, mailboxes)
        except RfqError as exc:
            raise rfq_http_error(exc) from None

    @router.get("/{rfq_id}/comparison", responses=error_responses(status.HTTP_404_NOT_FOUND))
    async def comparison(  # pyright: ignore[reportUnusedFunction]
        rfq_id: UUID, principal: CurrentPrincipal, db: Db
    ) -> Comparison:
        today = datetime.now(UTC).date()
        try:
            result = await load_comparison(db, principal, rfq_id, today)
        except RfqError as exc:
            raise rfq_http_error(exc) from None
        result.compliance = await compliance_service.rfq_summary(db, principal, rfq_id, today)
        return result

    return router
