"""LLM traces for Langfuse: one trace per top-level agent run, dev only.

A trace is a tree of OpenTelemetry spans exported as OTLP/HTTP to Langfuse
(`{LANGFUSE_HOST}/api/public/otel`, Basic auth with the public and secret key):

    agent run (copilot)                    langfuse.observation.type = agent
    ├── model request 1                    generation: model, tokens, cost, latency
    ├── tool call import_spreadsheet       tool: status, duration
    │   └── agent run (sheet_mapper)       a run started by a tool nests under it
    │       └── model request 1
    └── model request 2

The trace carries the session (the Copilot conversation id), the user (our
internal user id, never an email), the tenant, the request id the buyer sees as
"Referência", the job, the environment and the release, so a turn can be found
from any of them.

Content (the run's input and output, each model request's text, reasoning
summary and tool calls, each tool's arguments and result) is added only when
LANGFUSE_CAPTURE_CONTENT=true, and never in production: with ENVIRONMENT=prd
the setting is refused at startup and only metadata is exported. Without
content a span keeps names, timings, token counts, costs and statuses.

These spans use their own TracerProvider and exporter. They are never parented
to the request's HTTP or database spans (OTEL_EXPORTER_OTLP_ENDPOINT, see
bido_back/observability.py), and those never reach Langfuse. Export runs on a
background thread with a bounded queue; an unreachable or failing Langfuse
costs dropped spans and one log line, never a request.
"""

import base64
import json
import logging
import time
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import Span, Status, StatusCode, Tracer

from bido_back.agents.usage import ModelUsage, UsageMeter
from bido_back.logs import bound, safe_reason
from bido_back.settings import Settings

logger = logging.getLogger(__name__)

_PRODUCTION: Final = frozenset({"prd"})
# Per attribute. Langfuse shows the head; whole documents add nothing to a trace.
CONTENT_LIMIT: Final = 16_000
_TRACER_NAME: Final = "bido_back.agents"

type AttrValue = str | bool | int | float | Sequence[str]


@dataclass
class _State:
    provider: TracerProvider | None = None
    tracer: Tracer | None = None
    capture_content: bool = False
    environment: str = "local"
    release: str | None = None
    initialized: bool = False


_state = _State()


# --- Setup -----------------------------------------------------------------


def init_llm_tracing(settings: Settings, *, service: str) -> None:
    """Start exporting LLM traces when LANGFUSE_* are set. Idempotent; never raises."""
    if _state.initialized:
        return
    _state.initialized = True
    keys = (settings.langfuse_public_key, settings.langfuse_secret_key, settings.langfuse_host)
    if not any(keys):
        return
    if not all(keys):
        logger.warning("LLM tracing off: set LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY and HOST")
        return
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        public, secret, host = (str(k) for k in keys)
        auth = base64.b64encode(f"{public}:{secret}".encode()).decode()
        exporter = OTLPSpanExporter(
            endpoint=f"{host.rstrip('/')}/api/public/otel/v1/traces",
            headers={"Authorization": f"Basic {auth}"},
            timeout=10,
        )
        configure(
            exporter,
            capture_content=settings.langfuse_capture_content,
            environment=settings.environment,
            release=settings.git_sha,
            service=service,
        )
    except Exception as exc:
        # The message names the error type only: the settings hold the keys.
        logger.warning("LLM tracing off: setup failed (%s)", type(exc).__name__)
        return
    logger.info(
        "LLM tracing on",
        extra={"llm_trace_content": _state.capture_content, "llm_trace_host": host},
    )


def configure(
    exporter: SpanExporter,
    *,
    capture_content: bool,
    environment: str,
    release: str | None = None,
    service: str = "api",
    batch: bool = True,
) -> None:
    """Install `exporter` as the LLM trace sink (tests pass an in-memory one, unbatched)."""
    if capture_content and environment in _PRODUCTION:
        logger.error(
            "LANGFUSE_CAPTURE_CONTENT is refused in production; exporting LLM trace metadata only"
        )
        capture_content = False
    resource = Resource.create(
        {
            "service.name": f"bido-back-{service}",
            "deployment.environment": environment,
            **({"service.version": release} if release else {}),
        }
    )
    provider = TracerProvider(resource=resource)
    guarded = _GuardedExporter(exporter)
    processor: SpanProcessor = (
        BatchSpanProcessor(
            guarded,
            max_queue_size=2048,
            max_export_batch_size=256,
            schedule_delay_millis=2000,
            export_timeout_millis=10_000,
        )
        if batch
        else SimpleSpanProcessor(guarded)
    )
    provider.add_span_processor(processor)
    _state.provider = provider
    _state.tracer = provider.get_tracer(_TRACER_NAME)
    _state.capture_content = capture_content
    _state.environment = environment
    _state.release = release
    _state.initialized = True


def shutdown_llm_tracing(timeout_millis: int = 5000) -> None:
    """Flush pending spans and stop exporting. Called when the api or worker stops."""
    provider = _state.provider
    if provider is None:
        return
    try:
        provider.force_flush(timeout_millis)
        provider.shutdown()
    except Exception as exc:
        logger.warning("LLM tracing: flush on shutdown failed (%s)", type(exc).__name__)
    reset()


def reset() -> None:
    """Back to off (tests)."""
    _state.provider = None
    _state.tracer = None
    _state.capture_content = False
    _state.environment = "local"
    _state.release = None
    _state.initialized = False


def enabled() -> bool:
    return _state.tracer is not None


def captures_content() -> bool:
    return _state.tracer is not None and _state.capture_content


class _GuardedExporter(SpanExporter):
    """Never lets an export failure escape; logs the first one only."""

    def __init__(self, inner: SpanExporter) -> None:
        self._inner = inner
        self._reported = False

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        try:
            result = self._inner.export(spans)
        except Exception as exc:
            self._report(type(exc).__name__)
            return SpanExportResult.FAILURE
        if result is not SpanExportResult.SUCCESS:
            self._report("export rejected")
        return result

    def _report(self, reason: str) -> None:
        if not self._reported:
            self._reported = True
            logger.warning("LLM trace export failed (%s); further failures are not logged", reason)

    def shutdown(self) -> None:
        try:
            self._inner.shutdown()
        except Exception:
            logger.debug("LLM trace exporter shutdown failed", exc_info=True)

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        try:
            return self._inner.force_flush(timeout_millis)
        except Exception:
            return False


# --- Run scopes --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunScope:
    """Where a run's work is recorded: its usage meter, and the span nested work
    hangs from (the run's span, or a tool call's while that tool runs)."""

    meter: UsageMeter
    span: Span | None = None


_current: ContextVar[RunScope | None] = ContextVar("bido_llm_run_scope", default=None)


def current_scope() -> RunScope | None:
    """The scope of the tool call running now, if any: a run started here nests in it."""
    return _current.get()


@contextmanager
def _within(scope: RunScope) -> Generator[None]:
    token = _current.set(scope)
    try:
        yield
    finally:
        _current.reset(token)


# --- Spans -------------------------------------------------------------------


def clip(text: str) -> str:
    if len(text) <= CONTENT_LIMIT:
        return text
    return f"{text[:CONTENT_LIMIT]}… [truncated {len(text) - CONTENT_LIMIT} chars]"


def _json(value: object) -> str:
    return clip(value if isinstance(value, str) else json.dumps(value, default=str))


def _set(span: Span, attributes: Mapping[str, AttrValue | None]) -> None:
    span.set_attributes({k: v for k, v in attributes.items() if v is not None})


def _parent_context(parent: RunScope | None) -> otel_context.Context:
    # An empty context starts a new trace: never a child of an HTTP or DB span.
    if parent is not None and parent.span is not None:
        return trace.set_span_in_context(parent.span, otel_context.Context())
    return otel_context.Context()


def start_run_span(
    *,
    agent: str,
    prompt_version: int,
    model: str,
    run_id: str,
    tenant_id: str,
    user_id: str | None,
    parent: RunScope | None,
    input_json: str,
    session_id: str | None = None,
) -> Span | None:
    """The span of one agent run; the trace's root when nothing encloses the run."""
    tracer = _state.tracer
    if tracer is None:
        return None
    span = tracer.start_span(f"agent {agent}", context=_parent_context(parent))
    _set(
        span,
        {
            "langfuse.observation.type": "agent",
            "langfuse.observation.metadata.agent": agent,
            "langfuse.observation.metadata.prompt_version": str(prompt_version),
            "langfuse.observation.metadata.run_id": run_id,
            "langfuse.observation.prompt.name": agent,
            "langfuse.observation.prompt.version": prompt_version,
            "gen_ai.agent.name": agent,
            "gen_ai.request.model": model,
        },
    )
    if parent is None or parent.span is None:
        conversation = session_id or bound("conversation_id")
        request_id = bound("request_id")
        job = bound("job")
        _set(
            span,
            {
                "langfuse.trace.name": agent,
                "langfuse.session.id": conversation,
                "langfuse.user.id": user_id or "system",
                "langfuse.environment": _state.environment,
                "langfuse.release": _state.release,
                "langfuse.trace.tags": [t for t in (agent, job) if t],
                "langfuse.trace.metadata.tenant_id": tenant_id,
                "langfuse.trace.metadata.run_id": run_id,
                "langfuse.trace.metadata.request_id": request_id,
                "langfuse.trace.metadata.job": job,
                "langfuse.trace.metadata.prompt_version": str(prompt_version),
                "langfuse.trace.metadata.model": model,
            },
        )
        if _state.capture_content:
            span.set_attribute("langfuse.trace.input", clip(input_json))
    if _state.capture_content:
        span.set_attribute("langfuse.observation.input", clip(input_json))
    return span


def end_run_span(
    span: Span | None,
    *,
    is_root: bool,
    outcome: str,
    meter: UsageMeter,
    code: str | None = None,
    reason: str | None = None,
    output: object = None,
    approval_tool: str | None = None,
) -> None:
    if span is None:
        return
    usage = meter.usage
    _set(
        span,
        {
            "langfuse.observation.metadata.outcome": outcome,
            "langfuse.observation.metadata.code": code,
            "langfuse.observation.metadata.approval_tool": approval_tool,
            # Metadata, not usage: Langfuse sums usage and cost from the
            # generations, and these totals include nested runs' requests.
            "langfuse.observation.metadata.model_calls": str(meter.model_calls),
            "langfuse.observation.metadata.input_tokens": str(usage.input_tokens),
            "langfuse.observation.metadata.output_tokens": str(usage.output_tokens),
            "langfuse.observation.metadata.cost_usd": f"{meter.cost_usd:.6f}",
        },
    )
    if outcome == "failed" or outcome == "aborted":
        message = code if reason is None else f"{code}: {safe_reason(reason)}"
        _set(
            span,
            {"langfuse.observation.level": "ERROR" if outcome == "failed" else "WARNING"},
        )
        if message:
            span.set_attribute("langfuse.observation.status_message", message)
        span.set_status(Status(StatusCode.ERROR, message or outcome))
    if _state.capture_content and output is not None:
        rendered = _json(output)
        span.set_attribute("langfuse.observation.output", rendered)
        if is_root:
            span.set_attribute("langfuse.trace.output", rendered)
    span.end()


@dataclass(slots=True)
class _Generation:
    span: Span
    ended_ns: int | None = None
    text: list[str] = field(default_factory=list[str])
    reasoning: list[str] = field(default_factory=list[str])
    tool_calls: list[dict[str, object]] = field(default_factory=list[dict[str, object]])


class GenerationRecorder:
    """Model requests of one run: usage on the meter always, spans when tracing is on.

    The adapter reports what Agno streams: a request starts, its text and
    reasoning arrive, it completes with usage, and the tool calls it asked for
    start right after. A request's span is closed when the next one starts or
    the run segment ends, with the end time of its completion.
    """

    def __init__(self, scope: RunScope) -> None:
        self._scope = scope
        self._open: _Generation | None = None
        self._index = 0

    def started(self, model: str) -> None:
        self.close()
        tracer = _state.tracer
        if tracer is None or self._scope.span is None:
            return
        self._index += 1
        span = tracer.start_span(f"model {model}", context=_parent_context(self._scope))
        _set(
            span,
            {
                "langfuse.observation.type": "generation",
                "langfuse.observation.model.name": model,
                "gen_ai.request.model": model,
                "gen_ai.system": "openai",
                "langfuse.observation.metadata.request": str(self._index),
            },
        )
        self._open = _Generation(span)

    def content(self, *, text: str | None = None, reasoning: str | None = None) -> None:
        if self._open is None or not _state.capture_content:
            return
        if text:
            self._open.text.append(text)
        if reasoning:
            self._open.reasoning.append(reasoning)

    def final_answer(self, text: str | None) -> None:
        """The run's answer, for the last request, unless its text already streamed."""
        if self._open is not None and _state.capture_content and text and not self._open.text:
            self._open.text.append(text)

    def tool_call(self, name: str, args: object) -> None:
        if self._open is None:
            return
        entry: dict[str, object] = {"name": name}
        if _state.capture_content:
            entry["arguments"] = args
        self._open.tool_calls.append(entry)

    def completed(
        self, model: str, usage: ModelUsage, *, time_to_first_token_s: float | None = None
    ) -> None:
        cost = self._scope.meter.record(model, usage)
        generation = self._open
        if generation is None:
            return
        generation.ended_ns = time.time_ns()
        uncached = usage.input_tokens - min(usage.cached_input_tokens, usage.input_tokens)
        _set(
            generation.span,
            {
                "gen_ai.usage.input_tokens": usage.input_tokens,
                "gen_ai.usage.output_tokens": usage.output_tokens,
                "langfuse.observation.usage_details": json.dumps(
                    {
                        "input": uncached,
                        "input_cached_tokens": usage.cached_input_tokens,
                        "output": usage.output_tokens,
                        "output_reasoning_tokens": usage.reasoning_tokens,
                        "total": usage.input_tokens + usage.output_tokens,
                    }
                ),
                "langfuse.observation.cost_details": (
                    json.dumps({"total": round(cost, 8)}) if cost is not None else None
                ),
            },
        )
        start = getattr(generation.span, "start_time", None)
        if time_to_first_token_s is not None and isinstance(start, int):
            # When the first token arrived: Langfuse shows it as time to first token.
            first = datetime.fromtimestamp(start / 1e9 + time_to_first_token_s, UTC)
            generation.span.set_attribute(
                "langfuse.observation.completion_start_time", first.isoformat()
            )

    def close(self) -> None:
        generation, self._open = self._open, None
        if generation is None:
            return
        span = generation.span
        names = [str(call["name"]) for call in generation.tool_calls]
        if names:
            span.set_attribute("langfuse.observation.metadata.tool_calls", ",".join(names))
        if _state.capture_content:
            output: dict[str, object] = {}
            if generation.text:
                output["text"] = "".join(generation.text)
            if generation.reasoning:
                output["reasoning"] = "".join(generation.reasoning)
            if generation.tool_calls:
                output["tool_calls"] = generation.tool_calls
            if output:
                span.set_attribute("langfuse.observation.output", _json(output))
        if generation.ended_ns is None:
            # No completion (the run stopped mid-request): a warning, not a cost.
            span.set_attribute("langfuse.observation.level", "WARNING")
            span.end()
        else:
            span.end(end_time=generation.ended_ns)


class ToolCall:
    """The span of one executed tool call (see `tool_call`)."""

    def __init__(self, span: Span | None) -> None:
        self._span = span

    def finished(
        self,
        status: str,
        *,
        output: object = None,
        error: str | None = None,
        error_class: str | None = None,
    ) -> None:
        span = self._span
        if span is None:
            return
        _set(
            span,
            {
                "langfuse.observation.metadata.status": status,
                "langfuse.observation.metadata.error_class": error_class,
            },
        )
        if status != "ok":
            span.set_attribute("langfuse.observation.level", "ERROR")
            span.set_attribute("langfuse.observation.status_message", safe_reason(error or status))
            span.set_status(Status(StatusCode.ERROR, status))
        if _state.capture_content:
            if output is not None:
                span.set_attribute("langfuse.observation.output", _json(output))
            elif error is not None:
                span.set_attribute("langfuse.observation.output", clip(error))


@contextmanager
def tool_call(
    scope: RunScope, *, tool: str, kind: str, call_id: str, args: object
) -> Generator[ToolCall]:
    """Trace one tool execution; runs started inside nest under it."""
    tracer = _state.tracer
    span: Span | None = None
    if tracer is not None and scope.span is not None:
        span = tracer.start_span(f"tool {tool}", context=_parent_context(scope))
        _set(
            span,
            {
                "langfuse.observation.type": "tool",
                "gen_ai.tool.name": tool,
                "gen_ai.tool.call.id": call_id,
                "langfuse.observation.metadata.tool_kind": kind,
            },
        )
        if _state.capture_content:
            span.set_attribute("langfuse.observation.input", _json(args))
    handle = ToolCall(span)
    try:
        with _within(RunScope(meter=scope.meter, span=span or scope.span)):
            yield handle
    finally:
        if span is not None:
            span.end()
