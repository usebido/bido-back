"""`/v1/mailbox`: connect the buyer's own Gmail / Microsoft 365 mailbox.

* `GET /v1/mailbox/connect/{provider}`: the provider's consent URL for the
  calling user (PKCE + a sealed state bound to tenant, user and provider).
* `GET /v1/mailbox/callback/{provider}`: where the provider redirects the
  browser. It carries no bearer token: the sealed `state` authenticates it
  (see `bido_back.mailbox.oauth`), and the user must still be a member of the
  tenant. The code is exchanged, the mailbox address resolved, and the tokens
  stored encrypted.
* `GET /v1/mailbox`: the calling user's connections (never tokens).
* `DELETE /v1/mailbox/{provider}`: erase the tokens and mark the connection
  revoked, then revoke at the provider (best effort).

Bido uses a connection only in the RFQ threads it started; see
`bido_back.mailbox.service`.
"""

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bido_back.api.deps import ApiResources, get_db, get_principal
from bido_back.api.errors import ErrorResponse
from bido_back.auth import Principal
from bido_back.mailbox.base import MailboxNotConfigured, Provider, ProviderError
from bido_back.mailbox.crypto import TokenCipher, build_token_cipher
from bido_back.mailbox.oauth import (
    InvalidOAuthState,
    MissingPermissions,
    OAuthExchangeFailed,
    OAuthState,
    StateSealer,
    account_email,
    authorization_url,
    exchange_code,
    new_code_verifier,
    oauth_client,
)
from bido_back.mailbox.service import (
    ConnectionView,
    disconnect_mailbox,
    list_connections,
    store_connection,
    user_is_member,
)
from bido_back.settings import Settings

_NO_STORE = {"Cache-Control": "no-store"}


class ConnectResponse(BaseModel):
    authorization_url: str


class MailboxConnectionResponse(BaseModel):
    provider: Literal["google", "microsoft"]
    email_address: str
    status: Literal["active", "revoked", "error"]
    scopes: list[str]
    created_at: datetime
    updated_at: datetime


class MailboxStatusResponse(BaseModel):
    connections: list[MailboxConnectionResponse]


def _response(view: ConnectionView) -> MailboxConnectionResponse:
    return MailboxConnectionResponse(
        provider=view.provider,
        email_address=view.email_address,
        status=view.status,
        scopes=list(view.scopes),
        created_at=view.created_at,
        updated_at=view.updated_at,
    )


def get_mailbox_settings(request: Request) -> Settings:
    resources: object = request.app.state.resources
    if not isinstance(resources, ApiResources):
        raise RuntimeError("app.state.resources is not configured")
    return resources.settings


async def get_mailbox_http() -> AsyncIterator[httpx.AsyncClient]:
    """A short-lived client for the OAuth calls of one request; tests override it."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        yield client


def get_token_cipher(
    settings: Annotated[Settings, Depends(get_mailbox_settings)],
) -> TokenCipher:
    try:
        return build_token_cipher(settings)
    except MailboxNotConfigured as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from None


def _unavailable(exc: MailboxNotConfigured) -> HTTPException:
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))


def _errors(*codes: int) -> dict[int | str, dict[str, object]]:
    return {code: {"model": ErrorResponse} for code in codes}


def mailbox_router() -> APIRouter:
    router = APIRouter(prefix="/v1/mailbox", tags=["mailbox"])

    @router.get(
        "/connect/{provider}",
        responses=_errors(status.HTTP_401_UNAUTHORIZED, status.HTTP_503_SERVICE_UNAVAILABLE),
    )
    async def connect(  # pyright: ignore[reportUnusedFunction]
        provider: Provider,
        principal: Annotated[Principal, Depends(get_principal)],
        settings: Annotated[Settings, Depends(get_mailbox_settings)],
        response: Response,
    ) -> ConnectResponse:
        try:
            client = oauth_client(settings, provider)
            sealer = StateSealer.from_settings(settings)
        except MailboxNotConfigured as exc:
            raise _unavailable(exc) from None
        verifier = new_code_verifier()
        state = sealer.seal(
            OAuthState(
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                provider=provider,
                code_verifier=verifier,
            )
        )
        response.headers.update(_NO_STORE)
        return ConnectResponse(
            authorization_url=authorization_url(client, state=state, code_verifier=verifier)
        )

    @router.get(
        "/callback/{provider}",
        responses=_errors(
            status.HTTP_400_BAD_REQUEST,
            status.HTTP_403_FORBIDDEN,
            status.HTTP_502_BAD_GATEWAY,
            status.HTTP_503_SERVICE_UNAVAILABLE,
        ),
    )
    async def callback(  # pyright: ignore[reportUnusedFunction]
        provider: Provider,
        settings: Annotated[Settings, Depends(get_mailbox_settings)],
        db: Annotated[async_sessionmaker[AsyncSession], Depends(get_db)],
        http: Annotated[httpx.AsyncClient, Depends(get_mailbox_http)],
        cipher: Annotated[TokenCipher, Depends(get_token_cipher)],
        response: Response,
        code: Annotated[str | None, Query(max_length=4096)] = None,
        state: Annotated[str | None, Query(max_length=2048)] = None,
        error: Annotated[str | None, Query(max_length=256)] = None,
    ) -> MailboxConnectionResponse:
        response.headers.update(_NO_STORE)
        if error is not None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "the mailbox connection was declined")
        if not code or not state:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "missing code or state")
        try:
            client = oauth_client(settings, provider)
            sealed = StateSealer.from_settings(settings).open(state, provider)
        except MailboxNotConfigured as exc:
            raise _unavailable(exc) from None
        except InvalidOAuthState as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None

        if not await user_is_member(db, sealed.tenant_id, sealed.user_id):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "not a member of this tenant")

        try:
            tokens = await exchange_code(
                http, client, code=code, code_verifier=sealed.code_verifier
            )
            address = await account_email(http, provider, tokens.access_token)
        except (OAuthExchangeFailed, MissingPermissions) as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
        except (ProviderError, httpx.HTTPError):
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY, f"{provider} could not be reached"
            ) from None

        view = await store_connection(
            db,
            tenant_id=sealed.tenant_id,
            user_id=sealed.user_id,
            provider=provider,
            email_address=address,
            tokens=tokens,
            cipher=cipher,
        )
        return _response(view)

    @router.get("", responses=_errors(status.HTTP_401_UNAUTHORIZED))
    async def mailbox_status(  # pyright: ignore[reportUnusedFunction]
        principal: Annotated[Principal, Depends(get_principal)],
        db: Annotated[async_sessionmaker[AsyncSession], Depends(get_db)],
    ) -> MailboxStatusResponse:
        views = await list_connections(db, principal.tenant_id, principal.user_id)
        return MailboxStatusResponse(connections=[_response(v) for v in views])

    @router.delete(
        "/{provider}",
        status_code=status.HTTP_204_NO_CONTENT,
        responses=_errors(status.HTTP_401_UNAUTHORIZED, status.HTTP_404_NOT_FOUND),
    )
    async def disconnect(  # pyright: ignore[reportUnusedFunction]
        provider: Provider,
        principal: Annotated[Principal, Depends(get_principal)],
        settings: Annotated[Settings, Depends(get_mailbox_settings)],
        db: Annotated[async_sessionmaker[AsyncSession], Depends(get_db)],
        http: Annotated[httpx.AsyncClient, Depends(get_mailbox_http)],
    ) -> None:
        found = await disconnect_mailbox(
            db, principal.tenant_id, principal.user_id, provider, settings=settings, http=http
        )
        if not found:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no mailbox connection")

    return router
