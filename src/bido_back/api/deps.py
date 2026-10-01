"""FastAPI dependencies shared by the /v1 routers.

`get_principal` is the only way a route learns who is calling. Until Clerk
verification lands it refuses every request with 401, so no route that
depends on it can be reached unauthenticated; tests override it.

The database and the job queue are built lazily, on the first request that
needs them, so building the app (OpenAPI export, health checks) never
touches Postgres or AWS.
"""

from fastapi import HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.auth import Principal
from bido_back.db import create_engine, create_sessionmaker
from bido_back.settings import Settings
from bido_back.worker.jobs import JobQueue, SqsJobQueue


async def get_principal() -> Principal:
    # TODO(auth): verify the Clerk JWT (signature, issuer, audience, expiry),
    # map it to our user, and resolve the tenant from our memberships, never
    # from token claims alone.
    raise HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "authentication not implemented",
        headers={"WWW-Authenticate": "Bearer"},
    )


class ApiResources:
    """Process-wide clients for the api, created on first use."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._engine: AsyncEngine | None = None
        self._sessionmaker: async_sessionmaker[AsyncSession] | None = None
        self._jobs: JobQueue | None = None

    def sessionmaker(self) -> async_sessionmaker[AsyncSession]:
        if self._sessionmaker is None:
            self._engine = create_engine(self._settings)
            self._sessionmaker = create_sessionmaker(self._engine)
        return self._sessionmaker

    def job_queue(self) -> JobQueue | None:
        if self._jobs is None and self._settings.sqs_queue_url:
            self._jobs = SqsJobQueue(self._settings.sqs_queue_url, region=self._settings.aws_region)
        return self._jobs

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
