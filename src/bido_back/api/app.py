"""FastAPI application factory.

Run locally:

    uv run uvicorn bido_back.api.app:create_app --factory --reload

or `uv run python -m bido_back.api`.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import APIRouter, FastAPI, HTTPException, status
from pydantic import BaseModel

from bido_back.api.approvals import approvals_router
from bido_back.api.deps import ApiResources
from bido_back.api.errors import ErrorResponse
from bido_back.observability import init_observability, instrument_fastapi
from bido_back.settings import Settings, get_settings

API_TITLE = "Bido API"
# The public contract version. Bump it deliberately when the API changes; it is
# part of openapi.json, which is checked in and diffed in review.
API_VERSION = "0.1.0"


class HealthResponse(BaseModel):
    status: Literal["ok"]
    environment: str


def _v1_router() -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["v1"])

    @router.get(
        "/me",
        # Documented as 501 until auth lands; the real response model comes with it.
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        response_model=None,
        responses={status.HTTP_501_NOT_IMPLEMENTED: {"model": ErrorResponse}},
    )
    async def get_me() -> None:
        # TODO(auth): verify the Clerk JWT (signature, issuer, audience, expiry),
        # resolve the user's tenant and roles from our database (never from
        # token claims alone), and return them. Every /v1 route will depend on
        # that resolution; no tenant, no request.
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "not implemented")

    return router


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or get_settings()
    # Off unless OTEL_EXPORTER_OTLP_ENDPOINT / SENTRY_DSN are set; once per process.
    init_observability(resolved, service="api")

    # Database and queue clients, created lazily on first use (see api/deps.py).
    resources = ApiResources(resolved)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        yield
        await resources.aclose()

    app = FastAPI(title=API_TITLE, version=API_VERSION, lifespan=lifespan)
    app.state.resources = resources

    @app.get("/health", tags=["ops"])
    async def health() -> HealthResponse:
        # Liveness only: no database or provider calls, so a dependency outage
        # does not get healthy tasks killed by the load balancer.
        return HealthResponse(status="ok", environment=resolved.environment)

    app.include_router(_v1_router())
    app.include_router(approvals_router())
    instrument_fastapi(app)
    return app
