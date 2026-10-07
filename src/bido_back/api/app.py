"""FastAPI application factory.

Run locally:

    uv run uvicorn bido_back.api.app:create_app --factory --reload

or `uv run python -m bido_back.api`.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel

from bido_back.api.approvals import approvals_router
from bido_back.api.compliance import compliance_router
from bido_back.api.copilot import copilot_router
from bido_back.api.cors import add_cors
from bido_back.api.deps import ApiResources
from bido_back.api.exports import exports_router
from bido_back.api.followups import followups_router
from bido_back.api.mailbox import mailbox_router
from bido_back.api.me import me_router
from bido_back.api.onboarding import onboarding_router
from bido_back.api.proposals import proposals_router
from bido_back.api.rfqs import rfqs_router
from bido_back.api.search import search_router
from bido_back.api.suppliers import suppliers_router
from bido_back.api.templates import templates_router
from bido_back.observability import init_observability, instrument_fastapi
from bido_back.settings import Settings, get_settings

API_TITLE = "Bido API"
# The public contract version. Bump it deliberately when the API changes; it is
# part of openapi.json, which is checked in and diffed in review.
API_VERSION = "0.1.0"


class HealthResponse(BaseModel):
    status: Literal["ok"]
    environment: str


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

    app.include_router(me_router())
    app.include_router(rfqs_router())
    app.include_router(followups_router())
    app.include_router(suppliers_router())
    app.include_router(approvals_router())
    app.include_router(mailbox_router())
    app.include_router(onboarding_router())
    app.include_router(copilot_router())
    app.include_router(templates_router())
    app.include_router(compliance_router())
    app.include_router(proposals_router())
    app.include_router(search_router())
    app.include_router(exports_router())
    if resolved.environment != "prd":
        # The supplier simulator's controls; every endpoint 404s for a real tenant.
        from bido_back.api.sandbox import sandbox_router

        app.include_router(sandbox_router())
    if resolved.environment == "local":
        # Never mounted elsewhere (and every route refuses outside local too).
        from bido_back.api.dev import dev_router

        app.include_router(dev_router)
    instrument_fastapi(app)
    # Last, so it is the outermost middleware: preflights are answered before
    # tracing, routing or auth.
    add_cors(app, resolved)
    return app
