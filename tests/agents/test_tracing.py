"""LLM usage, cost, and Langfuse traces, against the scripted OpenAI client. No network.

Every scripted reply reports 10 input and 5 output tokens (test_runner.reply).
"""

import json
import logging
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanContext
from pydantic import BaseModel

from bido_back.agents import tracing
from bido_back.agents.context import ToolContext
from bido_back.agents.definition import AgentDefinition
from bido_back.agents.runner import Completed, run_to_completion
from bido_back.agents.tools import Tool
from bido_back.agents.usage import ModelUsage, UsageMeter, estimate_cost
from bido_back.logs import bind
from bido_back.settings import Settings
from tests.agents.test_runner import (
    CONFIG,
    MODEL,
    Answer,
    Script,
    Task,
    agent,
    ctx,
    final,
    reply,
    script,
    tool_use,
)

__all__ = ["script"]

MARKER = "customer-content-91c2"


@pytest.fixture(autouse=True)
def _tracing_off() -> Iterator[None]:
    tracing.reset()
    yield
    tracing.reset()


def traced(*, capture: bool, environment: str = "dev") -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    tracing.configure(
        exporter, capture_content=capture, environment=environment, release="abc123", batch=False
    )
    return exporter


def all_values(spans: tuple[ReadableSpan, ...]) -> str:
    return "\n".join(
        f"{span.name} {key}={value}"
        for span in spans
        for key, value in (span.attributes or {}).items()
    )


def ctx_of(span: ReadableSpan) -> SpanContext:
    assert span.context is not None
    return span.context


def by_name(spans: tuple[ReadableSpan, ...], name: str) -> list[ReadableSpan]:
    return [s for s in spans if s.name == name]


# --- Cost --------------------------------------------------------------------


def test_cost_uses_cached_and_output_prices() -> None:
    usage = ModelUsage(input_tokens=1_000_000, cached_input_tokens=400_000, output_tokens=200_000)
    # luna: 600k uncached x 0.10 + 400k cached x 0.01 + 200k output x 0.50, per million.
    assert estimate_cost("gpt-6-luna", usage) == pytest.approx(0.06 + 0.004 + 0.10)
    assert estimate_cost("gpt-6.1-sol", usage) == pytest.approx(1.2 + 0.04 + 2.0)
    # A dated snapshot is priced as its model.
    assert estimate_cost("gpt-6-luna-2026-09-22", usage) == pytest.approx(0.164)


def test_unknown_model_has_no_cost_and_is_logged_once(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="bido_back.agents.usage"):
        assert estimate_cost("mystery-model", ModelUsage(input_tokens=10)) is None
        assert estimate_cost("mystery-model", ModelUsage(input_tokens=10)) is None
    assert len([r for r in caplog.records if "mystery-model" in r.getMessage()]) == 1

    meter = UsageMeter()
    meter.record("mystery-model", ModelUsage(input_tokens=10, output_tokens=2))
    fields = meter.log_fields()
    assert fields["cost_usd"] is None and fields["unpriced_calls"] == 1
    assert fields["input_tokens"] == 10


def test_usage_adds_up_to_enclosing_meters() -> None:
    outer = UsageMeter()
    inner = UsageMeter(outer)
    inner.record("gpt-6-luna", ModelUsage(input_tokens=100, output_tokens=10))
    outer.record("gpt-6-luna", ModelUsage(input_tokens=1, output_tokens=1))
    assert (inner.model_calls, inner.usage.input_tokens) == (1, 100)
    assert (outer.model_calls, outer.usage.input_tokens, outer.usage.output_tokens) == (2, 101, 11)
    assert outer.cost_usd == pytest.approx(inner.cost_usd + (0.1 + 0.5) / 1_000_000)


# --- Usage on the run log line -------------------------------------------------


async def test_run_log_line_has_tokens_and_cost_without_tracing(
    script: Script, caplog: pytest.LogCaptureFixture
) -> None:
    script(
        reply(tool_use("fc_1", "lookup_price", sku="SKU-1")),
        final(answer="12.50", confidence=0.9),
    )
    with caplog.at_level(logging.INFO, logger="bido_back.agents.runner"):
        result = await run_to_completion(agent(), Task(question="q"), ctx(), config=CONFIG)

    assert isinstance(result, Completed)
    assert not tracing.enabled()
    (line,) = [r for r in caplog.records if r.getMessage() == "agent run"]
    fields: dict[str, Any] = line.__dict__
    assert (fields["model_calls"], fields["input_tokens"], fields["output_tokens"]) == (2, 20, 10)
    assert fields["cost_usd"] == pytest.approx((20 * 0.10 + 10 * 0.50) / 1_000_000, abs=1e-6)


# --- Traces --------------------------------------------------------------------


async def test_trace_without_content_keeps_metadata_only(script: Script) -> None:
    exporter = traced(capture=False)
    script(
        reply(tool_use("fc_1", "lookup_price", sku=MARKER)),
        final(answer=MARKER, confidence=0.9),
    )
    context = ctx()
    conversation = str(uuid4())
    with bind(request_id="req-1", conversation_id=conversation):
        result = await run_to_completion(
            agent(), Task(question=f"what about {MARKER}?"), context, config=CONFIG
        )
    assert isinstance(result, Completed)

    spans = exporter.get_finished_spans()
    assert MARKER not in all_values(spans)
    (root,) = by_name(spans, "agent example")
    attrs = dict(root.attributes or {})
    assert root.parent is None
    assert attrs["langfuse.observation.type"] == "agent"
    assert attrs["langfuse.trace.name"] == "example"
    assert attrs["langfuse.session.id"] == conversation
    assert attrs["langfuse.user.id"] == str(context.user_id)
    assert attrs["langfuse.trace.metadata.tenant_id"] == str(context.tenant_id)
    assert attrs["langfuse.trace.metadata.request_id"] == "req-1"
    assert attrs["langfuse.environment"] == "dev"
    assert attrs["langfuse.release"] == "abc123"
    assert attrs["langfuse.observation.metadata.outcome"] == "completed"
    assert attrs["langfuse.observation.metadata.input_tokens"] == "20"
    # Usage is on the generations only, so Langfuse never counts it twice.
    assert "gen_ai.usage.input_tokens" not in attrs
    assert "langfuse.observation.input" not in attrs
    assert "langfuse.observation.output" not in attrs

    generations = by_name(spans, f"model {MODEL}")
    assert len(generations) == 2
    for generation in generations:
        g = dict(generation.attributes or {})
        assert g["langfuse.observation.type"] == "generation"
        assert g["gen_ai.usage.input_tokens"] == 10
        assert json.loads(str(g["langfuse.observation.usage_details"]))["output"] == 5
        assert json.loads(str(g["langfuse.observation.cost_details"]))["total"] > 0
        assert generation.parent is not None and generation.parent.span_id == ctx_of(root).span_id
        assert "langfuse.observation.output" not in g
    first = dict(generations[0].attributes or {})
    assert first["langfuse.observation.metadata.tool_calls"] == "lookup_price"

    (tool,) = by_name(spans, "tool lookup_price")
    t = dict(tool.attributes or {})
    assert (t["langfuse.observation.type"], t["langfuse.observation.metadata.status"]) == (
        "tool",
        "ok",
    )
    assert "langfuse.observation.input" not in t
    assert {ctx_of(s).trace_id for s in spans} == {ctx_of(root).trace_id}


async def test_trace_with_content_has_inputs_and_outputs(script: Script) -> None:
    exporter = traced(capture=True)
    script(
        reply(tool_use("fc_1", "lookup_price", sku=MARKER)),
        final(answer=MARKER, confidence=0.9),
    )
    await run_to_completion(agent(), Task(question=f"what about {MARKER}?"), ctx(), config=CONFIG)

    spans = exporter.get_finished_spans()
    (root,) = by_name(spans, "agent example")
    attrs = dict(root.attributes or {})
    assert MARKER in str(attrs["langfuse.trace.input"])
    assert MARKER in str(attrs["langfuse.observation.output"])
    (tool,) = by_name(spans, "tool lookup_price")
    assert MARKER in str((tool.attributes or {})["langfuse.observation.input"])
    assert "1250" in str((tool.attributes or {})["langfuse.observation.output"])
    first, second = by_name(spans, f"model {MODEL}")
    assert MARKER in str((first.attributes or {})["langfuse.observation.output"])
    assert MARKER in str((second.attributes or {})["langfuse.observation.output"])


def test_production_refuses_content(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.ERROR, logger="bido_back.agents.tracing"):
        traced(capture=True, environment="prd")
    assert tracing.enabled() and not tracing.captures_content()
    assert any("refused in production" in r.getMessage() for r in caplog.records)


async def test_failed_run_is_an_error_span(script: Script) -> None:
    exporter = traced(capture=False)
    script(reply(tool_use("fc_1", "lookup_price", sku="missing")), final(answer="x"))
    await run_to_completion(agent(), Task(question="q"), ctx(), config=CONFIG)

    (tool,) = by_name(exporter.get_finished_spans(), "tool lookup_price")
    attrs = dict(tool.attributes or {})
    assert attrs["langfuse.observation.level"] == "ERROR"
    assert attrs["langfuse.observation.metadata.status"] == "error"
    (root,) = by_name(exporter.get_finished_spans(), "agent example")
    assert (root.attributes or {})["langfuse.observation.metadata.outcome"] == "failed"


# --- Nesting -----------------------------------------------------------------


class InnerOut(BaseModel):
    answer: str
    confidence: float


def inner_agent() -> AgentDefinition[Answer]:
    return AgentDefinition(
        name="inner",
        prompt_file="example.v1.md",
        prompt_version=1,
        model=MODEL,
        output_model=Answer,
        max_turns=2,
        effort="low",
    )


class AskIn(BaseModel):
    question: str


async def ask_inner(context: ToolContext, args: AskIn) -> InnerOut:
    inner_context = ToolContext(
        tenant_id=context.tenant_id, user_id=context.user_id, run_id=uuid4()
    )
    result = await run_to_completion(
        inner_agent(), Task(question=args.question), inner_context, config=CONFIG
    )
    assert isinstance(result, Completed)
    return InnerOut(answer=result.output.answer, confidence=result.output.confidence)


ASK = Tool(
    name="ask_inner",
    description="Ask another agent.",
    input_model=AskIn,
    output_model=InnerOut,
    kind="read",
    handler=ask_inner,
)


def outer_agent() -> AgentDefinition[Answer]:
    return AgentDefinition(
        name="outer",
        prompt_file="example.v1.md",
        prompt_version=1,
        model=MODEL,
        output_model=Answer,
        tools=(ASK,),
        max_turns=4,
        effort="low",
    )


async def test_a_run_started_by_a_tool_nests_under_it(
    script: Script, caplog: pytest.LogCaptureFixture
) -> None:
    exporter = traced(capture=False)
    script(
        reply(tool_use("fc_1", "ask_inner", question="q")),
        final(answer="inner", confidence=0.5),
        final(answer="outer", confidence=0.9),
    )
    meter = UsageMeter()
    from bido_back.agents.runner import run

    with caplog.at_level(logging.INFO, logger="bido_back.agents.runner"):
        async for _ in run(outer_agent(), Task(question="q"), ctx(), config=CONFIG, usage=meter):
            pass

    # The outer run's totals include the inner run's request...
    assert (meter.model_calls, meter.usage.input_tokens) == (3, 30)
    # ...but each run's log line counts its own requests, so lines add up.
    lines = {
        r.__dict__["agent"]: r.__dict__["input_tokens"]
        for r in caplog.records
        if r.getMessage() == "agent run"
    }
    assert lines == {"outer": 20, "inner": 10}
    spans = exporter.get_finished_spans()
    (outer,) = by_name(spans, "agent outer")
    (tool,) = by_name(spans, "tool ask_inner")
    (inner,) = by_name(spans, "agent inner")
    assert tool.parent is not None and tool.parent.span_id == ctx_of(outer).span_id
    assert inner.parent is not None and inner.parent.span_id == ctx_of(tool).span_id
    assert {ctx_of(s).trace_id for s in spans} == {ctx_of(outer).trace_id}
    # Trace-level attributes only on the root.
    assert "langfuse.trace.name" in (outer.attributes or {})
    assert "langfuse.trace.name" not in (inner.attributes or {})
    assert (outer.attributes or {})["langfuse.observation.metadata.input_tokens"] == "30"
    assert (inner.attributes or {})["langfuse.observation.metadata.input_tokens"] == "10"


# --- Setup -------------------------------------------------------------------


def settings(**langfuse: object) -> Settings:
    return Settings.model_validate(
        {"environment": "dev", "database_url": "postgresql://u@localhost/db", **langfuse}
    )


def test_off_when_unset() -> None:
    tracing.init_llm_tracing(settings(), service="api")
    assert not tracing.enabled()


def test_off_and_warned_when_incomplete(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="bido_back.agents.tracing"):
        tracing.init_llm_tracing(settings(langfuse_public_key="pk-lf-x"), service="api")
    assert not tracing.enabled()
    assert any("LLM tracing off" in r.getMessage() for r in caplog.records)


def test_on_with_keys_and_host_exports_to_langfuse_otel_endpoint() -> None:
    tracing.init_llm_tracing(
        settings(
            langfuse_public_key="pk-lf-x",
            langfuse_secret_key="sk-lf-y",  # noqa: S106 - not a real key
            langfuse_host="https://us.cloud.langfuse.com/",
        ),
        service="worker",
    )
    try:
        assert tracing.enabled() and not tracing.captures_content()
    finally:
        tracing.shutdown_llm_tracing(timeout_millis=100)
    assert not tracing.enabled()


def test_a_failing_exporter_never_raises(caplog: pytest.LogCaptureFixture) -> None:
    class Broken(InMemorySpanExporter):
        def export(self, spans: Any) -> Any:
            raise ConnectionError("down")

    tracing.configure(Broken(), capture_content=False, environment="dev", batch=False)
    with caplog.at_level(logging.WARNING, logger="bido_back.agents.tracing"):
        for _ in range(3):
            span = tracing.start_run_span(
                agent="a",
                prompt_version=1,
                model=MODEL,
                run_id="r",
                tenant_id="t",
                user_id=None,
                parent=None,
                input_json="",
            )
            tracing.end_run_span(span, is_root=True, outcome="completed", meter=UsageMeter())
    assert len([r for r in caplog.records if "export failed" in r.getMessage()]) == 1


async def test_an_approval_request_is_recorded_once(script: Script) -> None:
    from tests.agents.test_runner import RecordingSink

    exporter = traced(capture=False)
    script(reply(tool_use("fc_1", "send_payment", supplier_id="s-1", amount_cents=100)))
    result = await run_to_completion(
        agent(), Task(question="pay"), ctx(), approvals=RecordingSink(), config=CONFIG
    )
    assert type(result).__name__ == "NeedsApproval"

    spans = exporter.get_finished_spans()
    (root,) = by_name(spans, "agent example")
    attrs = dict(root.attributes or {})
    assert attrs["langfuse.observation.metadata.outcome"] == "approval_requested"
    assert attrs["langfuse.observation.metadata.approval_tool"] == "send_payment"
    (generation,) = by_name(spans, f"model {MODEL}")
    assert (generation.attributes or {})["langfuse.observation.metadata.tool_calls"] == (
        "send_payment"
    )
    # Never executed, so no tool span.
    assert by_name(spans, "tool send_payment") == []
