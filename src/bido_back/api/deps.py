"""FastAPI dependencies shared by the /v1 routers.

`get_principal` is the only way a route learns who is calling: it verifies
the Clerk session JWT (`Authorization: Bearer ...`), or, in local dev mode
only, the `X-Dev-User` header, and resolves the tenant and role (see
`bido_back.auth` and `bido_back.orgs`). With a Clerk token the tenant is the
company linked to the token's active organization, created on first login
when allowed; `X-Tenant-Id` is ignored. In dev mode `X-Tenant-Id` only selects
among the caller's own memberships.

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
    OrganizationNotEnabled,
    OrganizationRequired,
    Principal,
    TenantSelectionRequired,
    VerifiedSession,
    check_auth_settings,
    dev_mode_enabled,
    resolve_principal,
)
from bido_back.awards.agents import NoticeWriter, default_notice_writer
from bido_back.db import create_engine, create_sessionmaker
from bido_back.logs import annotate
from bido_back.orgs import ClerkDirectory, OrgPolicy, clerk_directory_for, principal_for_org
from bido_back.rfqs.agents import RfqAgents, default_rfq_agents
from bido_back.rfqs.mailbox import MailboxSenderFactory, mailbox_senders_for
from bido_back.settings import Settings
from bido_back.storage import BlobStorage, build_storage
from bido_back.storage.direct import DirectUploads, LocalDirectUploads
from bido_back.storage.links import DownloadLinks, LocalDownloadLinks
from bido_back.storage.local import LocalFileStorage
from bido_back.storage.s3 import S3Storage
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
        self._storage: BlobStorage | None = None
        self.org_policy = OrgPolicy.from_settings(settings)
        self.clerk_directory: ClerkDirectory = clerk_directory_for(settings)

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
            from bido_back.intake.consumer import sqs_client

            # sqs_client honours SQS_ENDPOINT_URL (ElasticMQ locally).
            self._jobs = SqsJobQueue(
                self._settings.sqs_queue_url,
                region=self._settings.aws_region,
                sqs=sqs_client(self._settings),
            )
        return self._jobs

    def storage(self) -> BlobStorage:
        if self._storage is None:
            self._storage = build_storage(self._settings)
        return self._storage

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


def get_optional_job_queue(request: Request) -> JobQueue | None:
    """The job queue, or None where none is configured (best-effort follow-ups)."""
    return _resources(request).job_queue()


def get_storage(request: Request) -> BlobStorage:
    return _resources(request).storage()


def get_direct_uploads(request: Request) -> DirectUploads:
    """Signs direct-upload forms: S3 in AWS, `POST /dev/uploads` locally.

    503 when neither applies (local storage outside `ENVIRONMENT=local`).
    """
    resources = _resources(request)
    storage = resources.storage()
    if isinstance(storage, S3Storage):
        return storage
    if isinstance(storage, LocalFileStorage) and resources.settings.environment == "local":
        return LocalDirectUploads(storage, str(request.base_url))
    raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "direct uploads need S3 storage")


def get_download_links(request: Request) -> DownloadLinks:
    """Signs short-lived file links: S3 in AWS, `GET /dev/files` locally.

    503 when neither applies (local storage outside `ENVIRONMENT=local`).
    """
    resources = _resources(request)
    storage = resources.storage()
    if isinstance(storage, S3Storage):
        return storage
    if isinstance(storage, LocalFileStorage) and resources.settings.environment == "local":
        return LocalDownloadLinks(storage, str(request.base_url))
    raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "file links need S3 storage")


def get_rfq_agents() -> RfqAgents:
    """The RFQ agents (intake, email writer) over the real runner; tests override it."""
    return default_rfq_agents()


def get_notice_writer() -> NoticeWriter:
    """The "not selected" notice writer over the real runner; tests override it."""
    return default_notice_writer()


def get_mailbox_senders(request: Request) -> MailboxSenderFactory:
    """How to get a buyer's sender (`EMAIL_TRANSPORT`, see rfqs.mailbox); tests override it."""
    return mailbox_senders_for(_resources(request).settings)


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
            description=(
                "Dev mode only: picks the tenant for users with several memberships. "
                "Ignored with a Clerk token (the active organization decides)."
            ),
        ),
    ] = None,
) -> Principal:
    resources = _resources(request)
    clerk_user_id: str | None = None
    verified: VerifiedSession | None = None

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
            verified = await verifier.verify(credentials.credentials)
        except AuthenticationFailed as exc:
            raise _unauthorized(str(exc)) from None

    # The database is reached only after the credentials passed.
    try:
        if verified is not None:
            if verified.org is None:
                raise OrganizationRequired("select an organization")
            principal = await principal_for_org(
                resources.sessionmaker(), verified, resources.org_policy, resources.clerk_directory
            )
        elif clerk_user_id is None:
            raise _unauthorized("missing credentials")
        else:
            principal = await resolve_principal(
                resources.sessionmaker(), clerk_user_id, x_tenant_id
            )
    except (NotAMember, OrganizationRequired, OrganizationNotEnabled) as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from None
    except TenantSelectionRequired as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
    # Every later log line of this request names the tenant and user (ids only).
    annotate(tenant_id=principal.tenant_id, user_id=principal.user_id)
    return principal
