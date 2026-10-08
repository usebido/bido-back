"""`/v1/mailbox`: connect the buyer's own Gmail / Microsoft 365 mailbox.

* `GET /v1/mailbox/connect/{provider}`: the provider's consent URL for the
  calling user (PKCE + a sealed state bound to tenant, user and provider).
* `GET /v1/mailbox/callback/{provider}`: where the provider redirects the
  browser. It finishes nothing: it carries no bearer token, so it only
  forwards `code` and `state` (or the provider's `error`) to the frontend's
  `MAILBOX_CONNECT_RETURN_URL`, in the URL fragment so they stay out of
  server and CDN logs.
* `POST /v1/mailbox/complete`: the frontend, as the signed-in user, finishes
  the link. The sealed state must open (else 400) and name the same tenant and
  user as the caller (else 403); only then is the code exchanged, the mailbox
  address resolved, and the tokens stored encrypted. Requiring the signed-in
  user closes login CSRF on the link: a victim who follows an attacker's
  callback URL cannot have the attacker's mailbox (or theirs) attached to the
  wrong account, because the caller must be the user who started the flow.
* `GET /v1/mailbox`: the calling user's connections (never tokens).
* `DELETE /v1/mailbox/{provider}`: erase the tokens and mark the connection
  revoked, then revoke at the provider (best effort).

Bido uses a connection only in the RFQ threads it started; see
`bido_back.mailbox.service`.
"""

import logging
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import urlencode
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bido_back.api.deps import ApiResources, get_db, get_optional_job_queue, get_principal
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
from bido_back.mandates.service import auto_approve_pending
from bido_back.settings import Settings
from bido_back.worker.jobs import JobQueue

logger = logging.getLogger(__name__)

_NO_STORE = {"Cache-Control": "no-store"}


class ConnectResponse(BaseModel):
    authorization_url: str


class CompleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Provider
    code: str = Field(min_length=1, max_length=4096)
    state: str = Field(min_length=1, max_length=2048)


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
        status_code=status.HTTP_303_SEE_OTHER,
        response_class=RedirectResponse,
        responses={status.HTTP_303_SEE_OTHER: {"description": "Back to the frontend"}},
    )
    async def callback(  # pyright: ignore[reportUnusedFunction]
        provider: Provider,
        settings: Annotated[Settings, Depends(get_mailbox_settings)],
        code: Annotated[str | None, Query(max_length=4096)] = None,
        state: Annotated[str | None, Query(max_length=2048)] = None,
        error: Annotated[str | None, Query(max_length=256)] = None,
    ) -> RedirectResponse:
        params: dict[str, str] = {"provider": provider}
        if error is not None or not code or not state:
            # The provider's own error text is not forwarded.
            params["error"] = "declined" if error is not None else "missing_code"
        else:
            params["code"] = code
            params["state"] = state
        target = f"{settings.mailbox_connect_return_url}#{urlencode(params)}"
        return RedirectResponse(target, status.HTTP_303_SEE_OTHER, headers=_NO_STORE)

    @router.post(
        "/complete",
        responses=_errors(
            status.HTTP_400_BAD_REQUEST,
            status.HTTP_401_UNAUTHORIZED,
            status.HTTP_403_FORBIDDEN,
            status.HTTP_502_BAD_GATEWAY,
            status.HTTP_503_SERVICE_UNAVAILABLE,
        ),
    )
    async def complete(  # pyright: ignore[reportUnusedFunction]
        body: CompleteRequest,
        principal: Annotated[Principal, Depends(get_principal)],
        settings: Annotated[Settings, Depends(get_mailbox_settings)],
        db: Annotated[async_sessionmaker[AsyncSession], Depends(get_db)],
        http: Annotated[httpx.AsyncClient, Depends(get_mailbox_http)],
        cipher: Annotated[TokenCipher, Depends(get_token_cipher)],
        jobs: Annotated[JobQueue | None, Depends(get_optional_job_queue)],
        response: Response,
    ) -> MailboxConnectionResponse:
        response.headers.update(_NO_STORE)
        provider = body.provider
        try:
            client = oauth_client(settings, provider)
            sealed = StateSealer.from_settings(settings).open(body.state, provider)
        except MailboxNotConfigured as exc:
            raise _unavailable(exc) from None
        except InvalidOAuthState as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None

        if sealed.tenant_id != principal.tenant_id or sealed.user_id != principal.user_id:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "this mailbox connection was started by another user"
            )
        if not await user_is_member(db, sealed.tenant_id, sealed.user_id):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "not a member of this tenant")

        try:
            tokens = await exchange_code(
                http, client, code=body.code, code_verifier=sealed.code_verifier
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
        await _release_waiting_mandate_sends(db, sealed.tenant_id, jobs)
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


async def _release_waiting_mandate_sends(
    db: async_sessionmaker[AsyncSession], tenant_id: UUID, jobs: JobQueue | None
) -> None:
    """A mailbox was just connected: approved mandates' waiting emails can go out now.

    Best effort: the mailbox sync (every couple of minutes) releases them too.
    """
    if jobs is None:
        return
    try:
        await auto_approve_pending(db, tenant_id, jobs=jobs)
    except Exception:
        logger.exception("mandate release after mailbox connect failed tenant=%s", tenant_id)
