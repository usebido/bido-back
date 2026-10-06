"""Observability: off when unset, and nothing customer-shaped leaves the process.

The process-wide setup (global tracer provider, library instrumentation,
Sentry) is exercised in a subprocess so it cannot leak into other tests.
"""

import json
import subprocess
import sys
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, cast

import httpx
import pytest
from fastapi import FastAPI
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import BaseModel
from sqlalchemy import create_engine, text

from bido_back.observability import (
    instrument_engine,
    instrument_fastapi,
    redact_url_attributes,
    scrub_sentry_event,
)

if TYPE_CHECKING:
    from sentry_sdk.types import Event

# Stands in for anything a customer sent: a prompt, a document, an email.
CONTENT = "customer-content-7f3a"


@pytest.fixture
def tracing() -> Iterator[tuple[TracerProvider, InMemorySpanExporter]]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield provider, exporter
    provider.shutdown()


def _all_values(span: ReadableSpan) -> list[str]:
    values = [str(v) for v in (span.attributes or {}).values()]
    for event in span.events:
        values.extend(str(v) for v in (event.attributes or {}).values())
    return values


# --- Off when unset ----------------------------------------------------------


def _run_isolated(script: str) -> str:
    """Run `script` in a fresh interpreter and return its last stdout line."""
    result = subprocess.run(  # noqa: S603 - fixed argv, our own interpreter
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip().splitlines()[-1]


def test_unset_settings_set_up_nothing() -> None:
    script = """
import sentry_sdk
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from bido_back import observability
from bido_back.api.app import create_app
from bido_back.settings import Settings

def forbidden(*args, **kwargs):
    raise AssertionError("observability must stay off when unset")

observability._init_tracing = forbidden
observability._init_sentry = forbidden
settings = Settings.model_validate(
    {"environment": "test", "database_url": "postgresql://app@localhost:5432/unused"}
)
app = create_app(settings)
observability.init_observability(settings, service="worker")
assert not isinstance(trace.get_tracer_provider(), TracerProvider)
assert not getattr(app, "_is_instrumented_by_opentelemetry", False)
assert not sentry_sdk.get_client().is_active()
print("ok")
"""
    assert _run_isolated(script) == "ok"


def test_setup_is_idempotent_and_survives_unreachable_exporters() -> None:
    # Port 9 (discard) on loopback: nothing listens, so any export would fail.
    script = """
import json
import sentry_sdk
from opentelemetry import trace
from bido_back.api.app import create_app
from bido_back.observability import init_observability
from bido_back.settings import Settings

settings = Settings.model_validate({
    "environment": "dev",
    "database_url": "postgresql://app@localhost:5432/unused",
    "otel_exporter_otlp_endpoint": "http://127.0.0.1:9",
    "sentry_dsn": "https://public@127.0.0.1:9/1",
})
init_observability(settings, service="worker")
first = trace.get_tracer_provider()
init_observability(settings, service="api")
app = create_app(settings)
assert trace.get_tracer_provider() is first
assert getattr(app, "_is_instrumented_by_opentelemetry", False)
assert sentry_sdk.get_client().is_active()
assert sentry_sdk.get_client().options["send_default_pii"] is False
assert sentry_sdk.get_client().options["traces_sample_rate"] == 0
print(json.dumps(dict(first.resource.attributes)))
"""
    resource = json.loads(_run_isolated(script))

    assert resource["service.name"] == "bido-back-worker"
    assert resource["deployment.environment"] == "dev"


# --- Traces carry no content -------------------------------------------------


class Note(BaseModel):
    text: str


async def test_request_spans_carry_no_body_query_or_headers(
    tracing: tuple[TracerProvider, InMemorySpanExporter],
) -> None:
    provider, exporter = tracing
    app = FastAPI()

    @app.post("/v1/notes/{note_id}")
    async def create_note(note_id: str, note: Note) -> Note:
        return note

    instrument_fastapi(app, tracer_provider=provider)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.post(
            f"/v1/notes/n-1?q={CONTENT}",
            json={"text": CONTENT},
            headers={"authorization": f"Bearer {CONTENT}", "cookie": f"session={CONTENT}"},
        )
    assert response.status_code == 200

    spans = exporter.get_finished_spans()
    assert [s.name for s in spans] == ["POST /v1/notes/{note_id}"]
    attributes = spans[0].attributes or {}
    assert attributes.get("http.route") == "/v1/notes/{note_id}"
    assert not any(CONTENT in value for value in _all_values(spans[0]))


def test_health_checks_are_not_traced(
    tracing: tuple[TracerProvider, InMemorySpanExporter],
) -> None:
    from fastapi.testclient import TestClient

    provider, exporter = tracing
    app = FastAPI()

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    instrument_fastapi(app, tracer_provider=provider)
    assert TestClient(app).get("/health").status_code == 200
    assert exporter.get_finished_spans() == ()


def test_query_spans_carry_the_statement_but_not_parameters(
    tracing: tuple[TracerProvider, InMemorySpanExporter],
) -> None:
    provider, exporter = tracing
    engine = create_engine("sqlite://")
    instrument_engine(engine, tracer_provider=provider)
    instrument_engine(engine, tracer_provider=provider)  # idempotent: still one span

    with engine.connect() as connection:
        connection.execute(text("SELECT :note AS note"), {"note": CONTENT})

    spans = [s for s in exporter.get_finished_spans() if s.name != "connect"]
    assert len(spans) == 1
    assert "SELECT" in str((spans[0].attributes or {}).get("db.statement"))
    assert not any(CONTENT in value for value in _all_values(spans[0]))
    engine.dispose()


def test_redact_url_attributes_drops_query_strings(
    tracing: tuple[TracerProvider, InMemorySpanExporter],
) -> None:
    provider, exporter = tracing
    tracer = provider.get_tracer(__name__)
    with tracer.start_as_current_span(
        "GET",
        attributes={
            "http.url": f"https://api.example.com/search?q={CONTENT}#frag",
            "url.full": f"https://api.example.com/search?q={CONTENT}",
            "url.query": f"q={CONTENT}",
            "http.method": "GET",
        },
    ) as span:
        redact_url_attributes(span)

    (finished,) = exporter.get_finished_spans()
    attributes = finished.attributes or {}
    assert attributes["http.url"] == "https://api.example.com/search"
    assert attributes["url.full"] == "https://api.example.com/search"
    assert attributes["url.query"] == "[redacted]"
    assert attributes["http.method"] == "GET"


# --- Sentry events carry no content ------------------------------------------


def test_sentry_scrubber_keeps_ids_and_drops_content() -> None:
    event: dict[str, object] = {
        "request": {
            "method": "POST",
            "url": f"https://api.bido.test/v1/runs?q={CONTENT}",
            "query_string": f"q={CONTENT}",
            "data": {"prompt": CONTENT},
            "cookies": {"session": CONTENT},
            "env": {"REMOTE_ADDR": "203.0.113.7"},
            "headers": {
                "Authorization": f"Bearer {CONTENT}",
                "Cookie": f"session={CONTENT}",
                "X-Api-Key": CONTENT,
                "User-Agent": "pytest",
                "Content-Type": "application/json",
                "X-Request-Id": "req-1",
            },
        },
        "extra": {
            "tenant_id": "6f1c0e9e-0000-4000-8000-000000000001",
            "run_id": "run-1",
            "job_type": "agent.run",
            "model": "gpt-6-luna",
            "input_tokens": 1200,
            "output_tokens": 300,
            "prompt": CONTENT,
            "email_body": CONTENT,
            "document": {"text": CONTENT},
            "attachment_id": {"nested": CONTENT},
        },
        "user": {"id": "user-1", "email": "buyer@example.com", "ip_address": "203.0.113.7"},
        "breadcrumbs": {
            "values": [
                {
                    "type": "http",
                    "data": {
                        "url": f"https://provider.test/lookup?email={CONTENT}",
                        "http.query": f"email={CONTENT}",
                        "http.fragment": CONTENT,
                        "method": "GET",
                        "status_code": 200,
                    },
                },
                {"type": "default", "message": "worker started"},
            ]
        },
    }

    serialized = json.dumps(scrub_sentry_event(cast("Event", event), {}))
    scrubbed: dict[str, Any] = json.loads(serialized)

    assert CONTENT not in serialized
    assert scrubbed["request"] == {
        "method": "POST",
        "url": "https://api.bido.test/v1/runs",
        "headers": {
            "User-Agent": "pytest",
            "Content-Type": "application/json",
            "X-Request-Id": "req-1",
        },
    }
    assert scrubbed["extra"] == {
        "tenant_id": "6f1c0e9e-0000-4000-8000-000000000001",
        "run_id": "run-1",
        "job_type": "agent.run",
        "model": "gpt-6-luna",
        "input_tokens": 1200,
        "output_tokens": 300,
    }
    assert scrubbed["user"] == {"id": "user-1"}
    crumbs = scrubbed["breadcrumbs"]["values"]
    assert crumbs[0]["data"] == {
        "url": "https://provider.test/lookup",
        "method": "GET",
        "status_code": 200,
    }
    assert crumbs[1] == {"type": "default", "message": "worker started"}


def test_scrubber_redacts_validation_error_values_and_log_params() -> None:
    from bido_back.observability import scrub_sentry_event

    event: dict[str, object] = {
        "exception": {
            "values": [
                {"type": "ValidationError", "value": "unit_price: input 'R$ 12,50 secret'"},
                {"type": "TimeoutError", "value": "timed out"},
            ]
        },
        "logentry": {
            "message": "extract failed for %s",
            "params": ["acme quote"],
            "formatted": "x",
        },
    }
    scrubbed = cast(dict[str, Any], scrub_sentry_event(cast(Any, event), cast(Any, {})))
    values = scrubbed["exception"]["values"]
    assert "secret" not in values[0]["value"]
    assert values[1]["value"] == "timed out"
    assert scrubbed["logentry"] == {"message": "extract failed for %s"}
