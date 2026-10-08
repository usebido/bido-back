"""`/v1/outbound/domains`: the recipient domains a tenant's emails may go to.

With the guard on (the default), Bido writes from the buyer's mailbox only to
reserved test domains or domains a person of the tenant confirmed here or in
an approval decision (`confirmed_domains`). `check` shows, for addresses, what
DNS says (a real company's domain, no mail, unknown) before confirming.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bido_back.api.deps import get_db, get_principal
from bido_back.api.rfqs import error_responses
from bido_back.auth import Principal
from bido_back.db import tenant_session
from bido_back.email.recipients import default_resolver
from bido_back.outbound.domains import (
    RecipientCheck,
    check_recipients,
    confirm_domains,
    confirmed_domains,
    requires_confirmation,
)

Db = Annotated[async_sessionmaker[AsyncSession], Depends(get_db)]
CurrentPrincipal = Annotated[Principal, Depends(get_principal)]


class OutboundDomains(BaseModel):
    # False only for tenants an operator configured without the guard.
    requires_confirmation: bool
    confirmed: list[str]


class ConfirmDomainsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Company domains, or exact addresses at free mailbox providers (gmail.com,
    # outlook.com, ...); a bare free-provider domain is ignored.
    domains: list[str] = Field(min_length=1, max_length=50)


class CheckRecipientsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    addresses: list[str] = Field(min_length=1, max_length=100)


def outbound_router() -> APIRouter:
    router = APIRouter(prefix="/v1/outbound", tags=["outbound"])

    async def _state(db: Db, principal: Principal) -> OutboundDomains:
        async with tenant_session(db, principal.tenant_id) as session:
            required = await requires_confirmation(session, principal.tenant_id)
            confirmed = await confirmed_domains(session, principal.tenant_id)
        return OutboundDomains(requires_confirmation=required, confirmed=sorted(confirmed))

    @router.get("/domains", responses=error_responses())
    async def list_domains(  # pyright: ignore[reportUnusedFunction]
        principal: CurrentPrincipal, db: Db
    ) -> OutboundDomains:
        """Whether the guard is on, and the domains confirmed so far."""
        return await _state(db, principal)

    @router.post("/domains", responses=error_responses())
    async def confirm(  # pyright: ignore[reportUnusedFunction]
        body: ConfirmDomainsRequest, principal: CurrentPrincipal, db: Db
    ) -> OutboundDomains:
        """Confirm recipient domains for the tenant (idempotent)."""
        async with tenant_session(db, principal.tenant_id) as session:
            await confirm_domains(session, principal.tenant_id, principal.user_id, body.domains)
        return await _state(db, principal)

    @router.post("/check", responses=error_responses())
    async def check(  # pyright: ignore[reportUnusedFunction]
        body: CheckRecipientsRequest, principal: CurrentPrincipal, db: Db
    ) -> list[RecipientCheck]:
        """What DNS says about each address, and whether an email would go out."""
        async with tenant_session(db, principal.tenant_id) as session:
            return await check_recipients(
                session, principal.tenant_id, body.addresses, default_resolver()
            )

    return router
