"""The supplier simulator's controls: `/v1/sandbox` (never mounted in prd).

For a sandbox tenant only (every endpoint answers 404 for a real company):
list and edit the simulated suppliers' personas, see what the tenant "sent"
and how the simulator answered, make a pending answer due now, and reset the
simulation. Nothing here can make a tenant a sandbox.
"""

from collections.abc import Callable
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from bido_back.api.deps import get_reply_schedulers
from bido_back.api.rfqs import CurrentPrincipal, Db, error_responses
from bido_back.simulator import service
from bido_back.simulator.personas import Persona
from bido_back.simulator.schemas import OutboundView, PersonaView, ResetResult
from bido_back.simulator.sender import ReplyScheduler

Schedulers = Annotated[Callable[[], ReplyScheduler], Depends(get_reply_schedulers)]


def _http(exc: service.SandboxError) -> HTTPException:
    if isinstance(exc, service.InvalidState):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))


def sandbox_router() -> APIRouter:
    router = APIRouter(prefix="/v1/sandbox", tags=["sandbox"])
    not_found = error_responses(status.HTTP_404_NOT_FOUND)

    @router.get("/personas", responses=not_found)
    async def list_personas(  # pyright: ignore[reportUnusedFunction]
        principal: CurrentPrincipal, db: Db
    ) -> list[PersonaView]:
        try:
            return await service.list_personas(db, principal)
        except service.SandboxError as exc:
            raise _http(exc) from None

    @router.put("/personas/{supplier_id}", responses=not_found)
    async def put_persona(  # pyright: ignore[reportUnusedFunction]
        supplier_id: UUID, body: Persona, principal: CurrentPrincipal, db: Db
    ) -> PersonaView:
        try:
            return await service.put_persona(db, principal, supplier_id, body)
        except service.SandboxError as exc:
            raise _http(exc) from None

    @router.get("/outbound", responses=not_found)
    async def list_outbound(  # pyright: ignore[reportUnusedFunction]
        principal: CurrentPrincipal, db: Db, invite_id: UUID | None = None
    ) -> list[OutboundView]:
        try:
            return await service.list_outbound(db, principal, invite_id)
        except service.SandboxError as exc:
            raise _http(exc) from None

    @router.post(
        "/outbound/{outbound_id}/reply-now",
        responses=error_responses(status.HTTP_404_NOT_FOUND, status.HTTP_409_CONFLICT),
    )
    async def reply_now(  # pyright: ignore[reportUnusedFunction]
        outbound_id: UUID, principal: CurrentPrincipal, db: Db, schedulers: Schedulers
    ) -> OutboundView:
        try:
            return await service.reply_now(db, principal, outbound_id, schedulers)
        except service.SandboxError as exc:
            raise _http(exc) from None

    @router.post("/reset", responses=not_found)
    async def reset(  # pyright: ignore[reportUnusedFunction]
        principal: CurrentPrincipal, db: Db
    ) -> ResetResult:
        try:
            return await service.reset(db, principal)
        except service.SandboxError as exc:
            raise _http(exc) from None

    return router
