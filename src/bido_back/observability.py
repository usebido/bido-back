"""Tracing (OpenTelemetry) and error reporting (Sentry), both off when unset.

- Traces go out as OTLP/HTTP to `OTEL_EXPORTER_OTLP_ENDPOINT`. In AWS that is
  an ADOT collector sidecar that forwards to CloudWatch; locally it is unset
  and nothing is exported.
- Errors go to Sentry when `SENTRY_DSN` is set. Sentry does no tracing
  (`traces_sample_rate=0`): traces are OpenTelemetry's job.

What may leave the process: IDs (tenant_id, run_id, idempotency keys), the job
type, the model name, token counts, routes, status codes, durations, and SQL
statement text (bound parameters are never recorded). What may not: customer
content of any kind, meaning prompts, model output, documents, emails, request
and response bodies, query strings, cookies, and auth headers. The defaults
below enforce that where a library would otherwise capture content; the rest
is on the code that writes log lines and span attributes.

Call `init_observability` once per process, at startup: `create_app` for the
api, `main()` for the worker. Repeated calls are ignored. A misconfigured or
unreachable exporter never stops the process from starting.
"""

import logging
import weakref
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, cast
from urllib.parse import urlsplit, urlunsplit

from fastapi import FastAPI
from opentelemetry import metrics, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span
from sqlalchemy import Engine
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.logs import bound, safe_reason
from bido_back.settings import Settings

if TYPE_CHECKING:
    from sentry_sdk.types import Event, Hint

logger = logging.getLogger(__name__)

Service = Literal["api", "worker"]


@dataclass
class _State:
    tracer_provider: TracerProvider | None = None
    initialized: bool = False
    instrumented_engines: weakref.WeakSet[Engine] = field(
        default_factory=lambda: weakref.WeakSet[Engine]()
    )


_state = _State()


def init_observability(settings: Settings, *, service: Service) -> None:
    """Set up tracing and error reporting for this process. Idempotent."""
    if _state.initialized:
        return
    _state.initialized = True

    # Agno logs model output verbatim in some warnings (e.g. when parsing fails).
    # Keep only its errors, which carry no content.
    for name in _AGNO_LOGGERS:
        logging.getLogger(name).setLevel(logging.ERROR)

    if settings.otel_exporter_otlp_endpoint:
        try:
            _init_tracing(settings, service)
        except Exception as exc:
            # Never block startup on telemetry. The message names the error type
            # only: the endpoint may carry credentials.
            logger.warning("tracing disabled: setup failed (%s)", type(exc).__name__)

    if settings.sentry_dsn:
        try:
            _init_sentry(settings, service)
        except Exception as exc:
            logger.warning("error reporting disabled: setup failed (%s)", type(exc).__name__)


def instrument_fastapi(app: FastAPI, *, tracer_provider: TracerProvider | None = None) -> None:
    """Trace each request to `app`. A no-op when tracing is off.

    Spans carry the method, route template, status code and timing. Request and
    response bodies are never read, headers are not captured, and the query
    string is removed from URL attributes.
    """
    provider = tracer_provider or _state.tracer_provider
    if provider is None:
        return
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=provider,
        server_request_hook=_server_request_hook,
        # Load balancer health checks would drown everything else.
        excluded_urls="/health$",
        # Per-message ASGI receive/send spans add noise and nothing we use.
        exclude_spans=["receive", "send"],
    )


def instrument_engine(
    engine: AsyncEngine | Engine, *, tracer_provider: TracerProvider | None = None
) -> None:
    """Trace queries on `engine`. A no-op when tracing is off; idempotent.

    Spans carry the SQL statement text with placeholders; bound parameters
    (where tenant data lives) are never recorded, and sqlcommenter stays off.
    """
    provider = tracer_provider or _state.tracer_provider
    if provider is None:
        return
    sync_engine = engine.sync_engine if isinstance(engine, AsyncEngine) else engine
    if sync_engine in _state.instrumented_engines:
        return
    # The instrumentor is a process-wide singleton that instruments only the
    # first engine it is given, so engines are attached directly.
    # These two modules ship no type information.
    from opentelemetry.instrumentation.sqlalchemy import (  # pyright: ignore[reportMissingTypeStubs]
        __version__ as sa_version,
    )
    from opentelemetry.instrumentation.sqlalchemy.engine import (  # pyright: ignore[reportMissingTypeStubs]
        EngineTracer,
    )

    tracer = provider.get_tracer("opentelemetry.instrumentation.sqlalchemy", sa_version)
    connections_usage = metrics.get_meter(__name__).create_up_down_counter(
        name="db.client.connections.usage", unit="connections"
    )
    EngineTracer(tracer, sync_engine, connections_usage, False, {}, False)
    _state.instrumented_engines.add(sync_engine)


def redact_url_attributes(span: Span) -> None:
    """Drop query strings from a span's URL attributes (they can hold search
    terms, emails, or tokens). Called by the HTTP instrumentation hooks."""
    if not span.is_recording() or not isinstance(span, ReadableSpan):
        return
    attributes = span.attributes or {}
    for key in ("http.url", "url.full", "http.target"):
        value = attributes.get(key)
        if isinstance(value, str) and ("?" in value or "#" in value):
            span.set_attribute(key, _strip_query(value))
    if "url.query" in attributes:
        span.set_attribute("url.query", "[redacted]")


# --- Sentry event scrubbing ------------------------------------------------

# Request headers that identify a request without carrying credentials or content.
_SAFE_HEADERS = frozenset(
    {
        "accept",
        "content-length",
        "content-type",
        "host",
        "traceparent",
        "user-agent",
        "x-amzn-trace-id",
        "x-request-id",
    }
)
# `extra` keys that are not `*_id` / `*_tokens` but are still safe to send.
_SAFE_EXTRA_KEYS = frozenset({"job_type", "model"})
_CONTENT_BEARING_EXCEPTIONS = frozenset({"ValidationError", "RequestValidationError"})
_AGNO_LOGGERS = ("agno", "agno-team", "agno-workflow")
# Log context fields (bido_back.logs) copied onto Sentry events as tags.
_TAGGED_FIELDS = ("request_id", "tenant_id", "user_id", "conversation_id", "run_id", "job")


def scrub_sentry_event(event: "Event", hint: "Hint") -> "Event":
    """Sentry `before_send`: keep identifiers, drop anything that could be content.

    - request: only method, URL without its query string, and allowlisted
      headers. Body, cookies, query string and environment are dropped.
    - extra: only `*_id`, `*_tokens`, `job_type` and `model`, with scalar values.
    - user: only `id`.
    - exception messages and breadcrumb messages: quoted values and email
      addresses masked (`bido_back.logs.safe_reason`).
    - breadcrumbs: query strings and fragments removed from HTTP breadcrumbs;
      other breadcrumbs keep only identifier data.
    """
    data = cast(dict[str, object], event)

    request = data.get("request")
    if isinstance(request, Mapping):
        data["request"] = _scrub_request(cast(Mapping[str, object], request))

    extra = data.get("extra")
    if isinstance(extra, Mapping):
        data["extra"] = {
            key: value
            for key, value in cast(Mapping[str, object], extra).items()
            if _is_safe_extra(key, value)
        }

    user = data.get("user")
    if isinstance(user, Mapping):
        user_id = cast(Mapping[str, object], user).get("id")
        data["user"] = {"id": user_id} if user_id is not None else {}

    exception = data.get("exception")
    if isinstance(exception, Mapping):
        values = cast(Mapping[str, object], exception).get("values")
        if isinstance(values, list):
            for exc in cast(list[object], values):
                if isinstance(exc, dict):
                    _scrub_exception(cast(dict[str, object], exc))

    logentry = data.get("logentry")
    if isinstance(logentry, dict):
        # The formatted message and its params can carry content; keep the template.
        cast(dict[str, object], logentry).pop("params", None)
        cast(dict[str, object], logentry).pop("formatted", None)

    breadcrumbs = data.get("breadcrumbs")
    if isinstance(breadcrumbs, Mapping):
        values = cast(Mapping[str, object], breadcrumbs).get("values")
        if isinstance(values, list):
            for crumb in cast(list[object], values):
                if isinstance(crumb, dict):
                    _scrub_breadcrumb(cast(dict[str, object], crumb))
    return event


def _scrub_exception(exc: dict[str, object]) -> None:
    # Validation errors embed the offending input values in their message.
    if exc.get("type") in _CONTENT_BEARING_EXCEPTIONS:
        exc["value"] = "<redacted: may contain input values>"
        return
    # Other messages are ours or a library's; mask quoted values and addresses.
    value = exc.get("value")
    if isinstance(value, str):
        exc["value"] = safe_reason(value, 1000)


def _scrub_request(request: Mapping[str, object]) -> dict[str, object]:
    scrubbed: dict[str, object] = {}
    method = request.get("method")
    if isinstance(method, str):
        scrubbed["method"] = method
    url = request.get("url")
    if isinstance(url, str):
        scrubbed["url"] = _strip_query(url)
    headers = request.get("headers")
    if isinstance(headers, Mapping):
        scrubbed["headers"] = {
            name: value
            for name, value in cast(Mapping[str, object], headers).items()
            if name.lower() in _SAFE_HEADERS
        }
    return scrubbed


def _is_safe_extra(key: str, value: object) -> bool:
    if not isinstance(value, str | int | float | bool) and value is not None:
        return False
    return key.endswith(("_id", "_tokens")) or key in _SAFE_EXTRA_KEYS


def _scrub_breadcrumb(crumb: dict[str, object]) -> None:
    message = crumb.get("message")
    if isinstance(message, str):
        crumb["message"] = safe_reason(message, 1000)
    data = crumb.get("data")
    if not isinstance(data, dict):
        return
    fields = cast(dict[str, object], data)
    if crumb.get("type") != "http":
        # Log breadcrumbs carry the record's `extra`: keep the identifiers only.
        for key in [k for k, v in fields.items() if not _is_safe_extra(k, v)]:
            del fields[key]
        return
    fields.pop("http.query", None)
    fields.pop("http.fragment", None)
    url = fields.get("url")
    if isinstance(url, str):
        fields["url"] = _strip_query(url)


def _strip_query(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


# --- Setup -----------------------------------------------------------------


def _init_tracing(settings: Settings, service: Service) -> None:
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.botocore import (  # pyright: ignore[reportMissingTypeStubs]
        BotocoreInstrumentor,
    )
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    endpoint = cast(str, settings.otel_exporter_otlp_endpoint)
    resource = Resource.create(
        {
            "service.name": f"{settings.otel_service_name}-{service}",
            "deployment.environment": settings.environment,
        }
    )
    provider = TracerProvider(resource=resource)
    # OTEL_EXPORTER_OTLP_ENDPOINT is the collector's base URL; the exporter
    # constructor wants the full traces URL. Export happens on a background
    # thread, so an unreachable collector costs dropped spans, not startup.
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces"))
    )
    trace.set_tracer_provider(provider)
    _state.tracer_provider = provider

    # Outbound HTTP (OpenAI, providers) and AWS calls (SQS, S3). Headers and
    # bodies are not captured; Bedrock message capture stays at its default (off).
    HTTPXClientInstrumentor().instrument(
        tracer_provider=provider,
        request_hook=_httpx_request_hook,
        async_request_hook=_httpx_async_request_hook,
    )
    BotocoreInstrumentor().instrument(tracer_provider=provider)


def _init_sentry(settings: Settings, service: Service) -> None:
    import sentry_sdk

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.environment,
        release=settings.git_sha or None,
        send_default_pii=False,
        max_request_body_size="never",
        include_local_variables=False,
        before_send=_before_send,
        traces_sample_rate=0,
    )
    sentry_sdk.set_tag("service", service)


def _before_send(event: "Event", hint: "Hint") -> "Event":
    """Scrub, then tag the event with the request or job it came from (ids only)."""
    scrubbed = scrub_sentry_event(event, hint)
    tags = cast(dict[str, object], scrubbed).setdefault("tags", {})
    if isinstance(tags, dict):
        for name in _TAGGED_FIELDS:
            value = bound(name)
            if value is not None:
                cast(dict[str, object], tags)[name] = value
    return scrubbed


def _server_request_hook(span: Span, scope: dict[str, Any]) -> None:
    redact_url_attributes(span)


def _httpx_request_hook(span: Span, request: object) -> None:
    redact_url_attributes(span)


async def _httpx_async_request_hook(span: Span, request: object) -> None:
    redact_url_attributes(span)
