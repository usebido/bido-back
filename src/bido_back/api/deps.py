"""FastAPI dependencies shared by the /v1 routers.

`get_principal` is the only way a route learns who is calling: it verifies
the Clerk session JWT (`Authorization: Bearer ...`), or, in local dev mode
only, the `X-Dev-User` header, and resolves the tenant and role from our
memberships (see `bido_back.auth`). Nothing about the tenant is taken from the
token; `X-Tenant-Id` only selects among the caller's own memberships.

The database, the job queue and the JWT verifier are built lazily, on the
first request that needs them, so building the app (OpenAPI export, health
checks) never touches Postgres, AWS or Clerk. Unsafe auth configuration (dev
mode outside `ENVIRONMENT=local`) is refused when the app is built.
"""

from typing import Annotated

from fastapi import Header, HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.auth import (
    AuthConfigError,
    AuthenticationFailed,
    ClerkJwtVerifier,
    NotAMember,
    Principal,
    TenantSelectionRequired,
    check_auth_settings,
    dev_mode_enabled,
    resolve_principal,
)
from bido_back.db import create_engine, create_sessionmaker
from bido_back.rfqs.agents import RfqAgents, default_rfq_agents
from bido_back.rfqs.mailbox import MailboxSenderFactory, default_mailbox_sender
from bido_back.settings import Settings
from bido_back.worker.jobs import JobQueue, SqsJobQueue

DEV_USER_HEADER = "X-Dev-User"

_bearer = HTTPBearer(auto_error=False, description="Clerk session token")


class ApiResources:
    """Process-wide clients for the api, created on first use."""

    def __init__(self, settings: Settings) -> None:
        # Fails app construction (startup) on unsafe auth configuration.
        check_auth_settings(settings)
        self._settings = settings
        self._engine: AsyncEngine | None = None
        self._sessionmaker: async_sessionmaker[AsyncSession] | None = None
        self._jobs: JobQueue | None = None
        self._verifier: ClerkJwtVerifier | None = None

    @property
    def settings(self) -> Settings:
        return self._settings

    def sessionmaker(self) -> async_sessionmaker[AsyncSession]:
        if self._sessionmaker is None:
            self._engine = create_engine(self._settings)
            self._sessionmaker = create_sessionmaker(self._engine)
        return self._sessionmaker

    def job_queue(self) -> JobQueue | None:
        if self._jobs is None and self._settings.sqs_queue_url:
            self._jobs = SqsJobQueue(self._settings.sqs_queue_url, region=self._settings.aws_region)
        return self._jobs

    def verifier(self) -> ClerkJwtVerifier | None:
        """The Clerk verifier, or None when Clerk is not configured (local/test)."""
        if self._verifier is None and self._settings.clerk_issuer and self._settings.clerk_jwks_url:
            self._verifier = ClerkJwtVerifier.from_settings(self._settings)
        return self._verifier

    async def aclose(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._sessionmaker = None


def _resources(request: Request) -> ApiResources:
    resources: object = request.app.state.resources
    if not isinstance(resources, ApiResources):
        raise RuntimeError("app.state.resources is not configured")
    return resources


def get_db(request: Request) -> async_sessionmaker[AsyncSession]:
    return _resources(request).sessionmaker()


def get_job_queue(request: Request) -> JobQueue:
    jobs = _resources(request).job_queue()
    if jobs is None:
        # Refused before anything is decided, so no approval is left without a job.
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "job queue not configured")
    return jobs


def get_rfq_agents() -> RfqAgents:
    """The RFQ agents (intake, email writer) over the real runner; tests override it."""
    return default_rfq_agents()


def get_mailbox_senders() -> MailboxSenderFactory:
    """How to get a buyer's mailbox sender (bido_back.mailbox); tests override it."""
    return default_mailbox_sender


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED, detail, headers={"WWW-Authenticate": "Bearer"}
    )


async def get_principal(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(_bearer)],
    x_tenant_id: Annotated[
        str | None,
        Header(
            alias="X-Tenant-Id",
            description="Required only for users with memberships in several tenants.",
        ),
    ] = None,
) -> Principal:
    resources = _resources(request)
    clerk_user_id: str | None = None

    # Read from the raw headers, not declared: dev mode is not part of the contract.
    dev_user = request.headers.get(DEV_USER_HEADER)
    if dev_user is not None:
        if not dev_mode_enabled(resources.settings):
            raise _unauthorized("dev authentication is disabled")
        clerk_user_id = dev_user.strip() or None
        if clerk_user_id is None:
            raise _unauthorized("empty X-Dev-User")
    else:
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise _unauthorized("missing bearer token")
        try:
            verifier = resources.verifier()
        except AuthConfigError:
            verifier = None
        if verifier is None:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "authentication is not configured"
            )
        try:
            clerk_user_id = await verifier.verify(credentials.credentials)
        except AuthenticationFailed as exc:
            raise _unauthorized(str(exc)) from None

    # The database is reached only after the credentials passed.
    try:
        return await resolve_principal(resources.sessionmaker(), clerk_user_id, x_tenant_id)
    except NotAMember as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from None
    except TenantSelectionRequired as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
