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

from bido_back.agents.tracing import init_llm_tracing, shutdown_llm_tracing
from bido_back.api.approvals import approvals_router
from bido_back.api.awards import awards_router
from bido_back.api.branding import branding_router
from bido_back.api.compliance import compliance_router
from bido_back.api.copilot import copilot_router
from bido_back.api.cors import add_cors
from bido_back.api.deps import ApiResources
from bido_back.api.exports import exports_router
from bido_back.api.files import files_router
from bido_back.api.followups import followups_router
from bido_back.api.mailbox import mailbox_router
from bido_back.api.me import me_router
from bido_back.api.negotiation import negotiation_router
from bido_back.api.onboarding import onboarding_router
from bido_back.api.outbound import outbound_router
from bido_back.api.payments import payments_router
from bido_back.api.procurement_assessments import procurement_assessments_router
from bido_back.api.proposals import proposals_router
from bido_back.api.purchasing import purchasing_router
from bido_back.api.request_context import add_request_context
from bido_back.api.rfqs import rfqs_router
from bido_back.api.search import search_router
from bido_back.api.suppliers import suppliers_router
from bido_back.api.templates import templates_router
from bido_back.api.uploads import uploads_router
from bido_back.logs import configure_logging
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
    # JSON lines on stdout (CloudWatch); once per process.
    configure_logging(resolved, service="api")
    # Off unless OTEL_EXPORTER_OTLP_ENDPOINT / SENTRY_DSN are set; once per process.
    init_observability(resolved, service="api")
    # LLM traces to Langfuse: off unless LANGFUSE_* are set; once per process.
    init_llm_tracing(resolved, service="api")

    # Database and queue clients, created lazily on first use (see api/deps.py).
    resources = ApiResources(resolved)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        yield
        await resources.aclose()
        shutdown_llm_tracing()

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
    app.include_router(negotiation_router())
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
    app.include_router(purchasing_router())
    app.include_router(procurement_assessments_router())
    app.include_router(awards_router())
    app.include_router(payments_router())
    app.include_router(outbound_router())
    app.include_router(uploads_router())
    app.include_router(files_router())
    app.include_router(branding_router())
    if resolved.environment == "local":
        # Never mounted elsewhere (and every route refuses outside local too).
        from bido_back.api.dev import dev_router

        app.include_router(dev_router)
    if resolved.environment in ("local", "dev"):
        # Debug data for demos; never mounted in production (and refuses there too).
        from bido_back.api.dev import debug_router

        app.include_router(debug_router)
    # Request ids, the access log and error bodies; inside tracing (so log
    # lines carry the trace id) and inside CORS (so a 500 still has its headers).
    add_request_context(app)
    instrument_fastapi(app)
    # Last, so it is the outermost middleware: preflights are answered before
    # tracing, routing or auth.
    add_cors(app, resolved)
    return app
