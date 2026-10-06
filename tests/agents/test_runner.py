"""Runner behavior on Agno, against a scripted OpenAI client. No network.

The model is the adapter's real Agno OpenAI Responses model; only its OpenAI
SDK client is replaced by a stub that returns scripted Responses API responses
and records every request. So these tests exercise Agno's loop, tool calls,
pause, structured output, and message formatting end to end.
"""

import hashlib
import json
import os
from collections.abc import Callable, Sequence
from typing import Any, cast
from uuid import UUID, uuid4

import openai
import pytest
from agno.agent import Agent
from agno.db.postgres import AsyncPostgresDb
from openai.types.responses import Response, ResponseReasoningItem
from pydantic import BaseModel, Field, JsonValue

from bido_back.agents import agno_adapter
from bido_back.agents.approvals import ApprovalProposal
from bido_back.agents.config import RunnerConfig
from bido_back.agents.context import ToolContext
from bido_back.agents.definition import AgentDefinition
from bido_back.agents.events import (
    ApprovalRequested,
    Artifact,
    EventAdapter,
    ReasoningStep,
    RunCompleted,
    TextDelta,
    ToolCalled,
    ToolResult,
)
from bido_back.agents.runner import Completed, Failed, NeedsApproval, run, run_to_completion
from bido_back.agents.tools import Tool, ToolError

# --- Scripted OpenAI client -------------------------------------------------

MODEL = "gpt-6-luna"

type Item = dict[str, Any]


def thinking(summary: str, encrypted: str = "enc") -> Item:
    """A reasoning item: its summary is shown, its encrypted content replayed."""
    return {
        "type": "reasoning",
        "id": f"rs_{uuid4().hex[:12]}",
        "summary": [{"type": "summary_text", "text": summary}],
        "encrypted_content": encrypted,
    }


def text(value: str) -> Item:
    return {
        "type": "message",
        "id": f"msg_{uuid4().hex[:12]}",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": value, "annotations": []}],
    }


def tool_use(item_id: str, name: str, **args: object) -> Item:
    """A function call. Agno keys calls by the item id (`fc_1`); the API pairs
    the call with its output by `call_id` (`call_1`)."""
    return {
        "type": "function_call",
        "id": item_id,
        "call_id": item_id.replace("fc_", "call_", 1),
        "name": name,
        "arguments": json.dumps(args),
        "status": "completed",
    }


def reply(*items: Item) -> Response:
    return Response.model_validate(
        {
            "id": f"resp_{uuid4().hex}",
            "object": "response",
            "created_at": 0,
            "model": MODEL,
            "status": "completed",
            "output": list(items),
            "parallel_tool_calls": True,
            "tool_choice": "auto",
            "tools": [],
            "usage": {
                "input_tokens": 10,
                "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
                "output_tokens": 5,
                "output_tokens_details": {"reasoning_tokens": 0},
                "total_tokens": 15,
            },
        }
    )


def final(*before: Item, **output: object) -> Response:
    return reply(*before, text(json.dumps(output)))


class _StubResponses:
    def __init__(self, responses: Sequence[Response | Exception]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Response:
        # With an output schema Agno requests whole responses, never a stream.
        assert not kwargs.get("stream"), "streamed requests are not scripted"
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("the stub ran out of scripted responses")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class StubOpenAI:
    """Stands in for openai.AsyncOpenAI's Responses API."""

    def __init__(self, responses: Sequence[Response | Exception]) -> None:
        self.responses = _StubResponses(responses)

    def is_closed(self) -> bool:
        return False

    @property
    def calls(self) -> list[dict[str, Any]]:
        return self.responses.calls


type Script = Callable[..., StubOpenAI]


def install_stub(monkeypatch: pytest.MonkeyPatch, stub: StubOpenAI) -> None:
    """Make every model the adapter builds use `stub` as its client."""
    real = agno_adapter.build_model

    def build_model(definition: AgentDefinition[Any], config: RunnerConfig) -> Any:
        model = real(definition, config)
        model.async_client = cast(openai.AsyncOpenAI, stub)
        return model

    monkeypatch.setattr(agno_adapter, "build_model", build_model)


@pytest.fixture
def script(monkeypatch: pytest.MonkeyPatch) -> Script:
    """Install a scripted client into every model the adapter builds."""

    def install(*responses: Response | Exception) -> StubOpenAI:
        stub = StubOpenAI(responses)
        install_stub(monkeypatch, stub)
        return stub

    return install


# --- Fixtures --------------------------------------------------------------


class Task(BaseModel):
    question: str


class Answer(BaseModel):
    answer: str
    confidence: float = Field(ge=0, le=1)


class LookupIn(BaseModel):
    sku: str


class LookupOut(BaseModel):
    sku: str
    price_cents: int


class SendIn(BaseModel):
    supplier_id: str
    amount_cents: int


class SendOut(BaseModel):
    sent: bool


lookup_calls: list[tuple[UUID, LookupIn]] = []
send_calls: list[SendIn] = []


async def lookup_price(ctx: ToolContext, args: LookupIn) -> LookupOut:
    lookup_calls.append((ctx.tenant_id, args))
    if args.sku == "missing":
        raise ToolError("no such sku")
    if args.sku == "boom":
        raise RuntimeError("database password is hunter2")
    return LookupOut(sku=args.sku, price_cents=1250)


async def send_payment(ctx: ToolContext, args: SendIn) -> SendOut:
    send_calls.append(args)
    return SendOut(sent=True)


LOOKUP = Tool(
    name="lookup_price",
    description="Look up the current price of a SKU.",
    input_model=LookupIn,
    output_model=LookupOut,
    kind="read",
    handler=lookup_price,
    artifact_kind="price",
)
SEND = Tool(
    name="send_payment",
    description="Pay a supplier.",
    input_model=SendIn,
    output_model=SendOut,
    kind="act",
    handler=send_payment,
)


def agent(max_turns: int = 4) -> AgentDefinition[Answer]:
    return AgentDefinition(
        name="example",
        prompt_file="example.v1.md",
        prompt_version=1,
        model=MODEL,
        output_model=Answer,
        tools=(LOOKUP, SEND),
        max_turns=max_turns,
        effort="low",
    )


CONFIG = RunnerConfig(openai_api_key="test-not-a-real-key")


class RecordingSink:
    def __init__(self, fail: bool = False) -> None:
        self.proposals: list[ApprovalProposal] = []
        self._fail = fail

    async def request_approval(self, proposal: ApprovalProposal) -> None:
        if self._fail:
            raise RuntimeError("db down")
        self.proposals.append(proposal)


@pytest.fixture(autouse=True)
def _reset_calls() -> None:
    lookup_calls.clear()
    send_calls.clear()


def ctx(user_id: UUID | None = None) -> ToolContext:
    return ToolContext(
        tenant_id=uuid4(), user_id=user_id or uuid4(), run_id=uuid4(), roles=frozenset({"buyer"})
    )


TASK = Task(question="How much is SKU-1?")


def input_of(call: dict[str, Any]) -> list[Any]:
    """The request's input items: dict messages and calls, plus replayed reasoning items."""
    return cast(list[Any], call["input"])


def items_of(call: dict[str, Any], item_type: str) -> list[dict[str, Any]]:
    """The request's dict input items of one type."""
    items = [cast(dict[str, Any], i) for i in input_of(call) if isinstance(i, dict)]
    return [i for i in items if i.get("type") == item_type]


def tool_results(call: dict[str, Any]) -> list[dict[str, Any]]:
    """Every function_call_output item sent in a request."""
    return items_of(call, "function_call_output")


def result_text(block: dict[str, Any]) -> str:
    return cast(str, block["output"])


# --- Tests -----------------------------------------------------------------


async def test_read_tool_runs_and_its_result_is_fed_back(script: Script) -> None:
    stub = script(
        reply(
            thinking("I need the price.", "enc-1"), tool_use("fc_1", "lookup_price", sku="SKU-1")
        ),
        final(thinking("Got it."), answer="12.50", confidence=0.9),
    )
    context = ctx()

    result = await run_to_completion(agent(), TASK, context, config=CONFIG)

    assert isinstance(result, Completed)
    assert result.output == Answer(answer="12.50", confidence=0.9)
    # The tenant came from the context, not from the model.
    assert lookup_calls == [(context.tenant_id, LookupIn(sku="SKU-1"))]

    second = stub.calls[1]
    # Stateless: no chaining to a stored response; the context is replayed.
    assert "previous_response_id" not in second
    replayed = input_of(second)
    # The encrypted reasoning is replayed before the call it led to.
    [reasoning] = [i for i in replayed if isinstance(i, ResponseReasoningItem)]
    assert reasoning.encrypted_content == "enc-1"
    [call] = items_of(second, "function_call")
    assert (call["id"], call["call_id"], call["name"]) == ("fc_1", "call_1", "lookup_price")
    assert replayed.index(reasoning) < replayed.index(call)
    [fed_back] = tool_results(second)
    assert fed_back["call_id"] == "call_1"
    assert '{"sku":"SKU-1","price_cents":1250}' in result_text(fed_back)

    kinds = [type(e) for e in result.events]
    assert kinds == [ReasoningStep, ToolCalled, ToolResult, Artifact, ReasoningStep, RunCompleted]
    reasoning_steps = [e.text for e in result.events if isinstance(e, ReasoningStep)]
    assert reasoning_steps == ["I need the price.", "Got it."]
    called = result.events[1]
    assert isinstance(called, ToolCalled)
    assert (called.call_id, called.tool, called.kind) == ("fc_1", "lookup_price", "read")
    assert called.args == {"sku": "SKU-1"}
    artifact = result.events[3]
    assert isinstance(artifact, Artifact)
    assert artifact.kind == "price"
    assert artifact.data == {"sku": "SKU-1", "price_cents": 1250}


async def test_prompt_tools_and_model_options_are_sent(script: Script) -> None:
    stub = script(final(answer="x", confidence=1))

    await run_to_completion(agent(), TASK, ctx(), config=CONFIG)

    request = stub.calls[0]
    assert request["model"] == MODEL
    # Stateless: OpenAI stores nothing, so reasoning comes back encrypted for replay.
    assert request["store"] is False
    assert "reasoning.encrypted_content" in request["include"]
    assert request["reasoning"] == {"effort": "low", "summary": "auto"}
    assert request["max_output_tokens"] == 16_000
    tools = cast(list[dict[str, Any]], request["tools"])
    # Exactly our tools: no Agno built-ins (memory, knowledge, shell...).
    assert [t["name"] for t in tools] == ["lookup_price", "send_payment"]
    assert {t["type"] for t in tools} == {"function"}
    assert tools[1]["parameters"]["required"] == ["supplier_id", "amount_cents"]
    output_format = cast(dict[str, Any], request["text"])["format"]
    assert output_format["type"] == "json_schema"
    assert output_format["strict"] is True
    assert set(output_format["schema"]["properties"]) == {"answer", "confidence"}
    instructions, first = input_of(request)
    assert instructions["role"] == "developer"
    assert instructions["content"].startswith("You are a procurement assistant")
    assert first == {"role": "user", "content": TASK.model_dump_json()}


async def test_act_tool_is_never_executed_and_requests_approval(script: Script) -> None:
    stub = script(reply(tool_use("fc_1", "send_payment", supplier_id="sup_9", amount_cents=50_000)))
    sink = RecordingSink()
    context = ctx()

    result = await run_to_completion(
        agent(), TASK, context, session="rfq-42", approvals=sink, config=CONFIG
    )

    assert isinstance(result, NeedsApproval)
    assert send_calls == []
    assert len(stub.calls) == 1
    expected_args: dict[str, JsonValue] = {"supplier_id": "sup_9", "amount_cents": 50_000}
    canonical = json.dumps(expected_args, sort_keys=True, separators=(",", ":"))
    expected_hash = hashlib.sha256(canonical.encode()).hexdigest()
    assert result.approval.tool == "send_payment"
    assert result.approval.call_id == "fc_1"
    assert result.approval.args == expected_args
    assert result.approval.args_hash == expected_hash
    assert not any(isinstance(e, ToolCalled | ToolResult) for e in result.events)

    assert sink.proposals == [
        ApprovalProposal(
            tenant_id=context.tenant_id,
            run_id=context.run_id,
            session_id="rfq-42",
            tool_call_id="fc_1",
            tool="send_payment",
            args=expected_args,
            args_hash=expected_hash,
            requested_by=context.user_id,
        )
    ]


async def test_read_tools_in_the_same_turn_as_an_act_still_run(script: Script) -> None:
    script(
        reply(
            tool_use("fc_1", "lookup_price", sku="SKU-1"),
            tool_use("fc_2", "send_payment", supplier_id="sup_9", amount_cents=1250),
        )
    )

    result = await run_to_completion(agent(), TASK, ctx(), config=CONFIG)

    assert isinstance(result, NeedsApproval)
    assert result.approval.call_id == "fc_2"
    assert [args for _, args in lookup_calls] == [LookupIn(sku="SKU-1")]
    assert send_calls == []


async def test_act_tool_with_invalid_args_is_sent_back_not_proposed(script: Script) -> None:
    stub = script(
        reply(tool_use("fc_1", "send_payment", supplier_id="sup_9", amount_cents="lots")),
        final(answer="gave up", confidence=0),
    )
    sink = RecordingSink()

    result = await run_to_completion(agent(), TASK, ctx(), approvals=sink, config=CONFIG)

    assert isinstance(result, Completed)
    assert sink.proposals == []
    assert send_calls == []
    [error] = tool_results(stub.calls[1])
    assert error["call_id"] == "call_1"
    assert "amount_cents" in result_text(error)
    assert "lots" not in result_text(error)  # input values are not echoed


async def test_approval_that_cannot_be_recorded_fails_the_run(script: Script) -> None:
    script(reply(tool_use("fc_1", "send_payment", supplier_id="sup_9", amount_cents=1)))

    result = await run_to_completion(
        agent(), TASK, ctx(), approvals=RecordingSink(fail=True), config=CONFIG
    )

    assert isinstance(result, Failed)
    assert result.failure.code == "approval_not_recorded"
    assert not any(isinstance(e, ApprovalRequested) for e in result.events)


async def test_max_turns_exceeded_fails(script: Script) -> None:
    stub = script(*[reply(tool_use(f"fc_{i}", "lookup_price", sku="SKU-1")) for i in range(5)])

    result = await run_to_completion(agent(max_turns=3), TASK, ctx(), config=CONFIG)

    assert isinstance(result, Failed)
    assert result.failure.code == "max_turns_exceeded"
    assert len(stub.calls) == 3


async def test_invalid_final_output_fails(script: Script) -> None:
    script(final(answer="x", confidence=7))

    result = await run_to_completion(agent(), TASK, ctx(), config=CONFIG)

    assert isinstance(result, Failed)
    assert result.failure.code == "invalid_output"
    assert "confidence" in result.failure.message


async def test_text_without_output_fails(script: Script) -> None:
    script(reply(text("It costs 12.50.")))

    result = await run_to_completion(agent(), TASK, ctx(), config=CONFIG)

    assert isinstance(result, Failed)
    assert result.failure.code == "invalid_output"


async def test_tool_errors_become_error_results(script: Script) -> None:
    stub = script(
        reply(
            tool_use("fc_1", "lookup_price", sku="missing"),
            tool_use("fc_2", "lookup_price", sku="boom"),
            tool_use("fc_3", "no_such_tool"),
        ),
        final(answer="unknown", confidence=0),
    )

    result = await run_to_completion(agent(), TASK, ctx(), config=CONFIG)

    assert isinstance(result, Completed)
    results = [e for e in result.events if isinstance(e, ToolResult)]
    assert [(r.call_id, r.ok, r.error) for r in results] == [
        ("fc_1", False, "no such sku"),
        ("fc_2", False, "internal error while running the tool"),
    ]
    sent = tool_results(stub.calls[1])
    assert sorted(b["call_id"] for b in sent) == ["call_1", "call_2", "call_3"]
    assert "hunter2" not in json.dumps(sent)


async def test_provider_error_fails_the_run(script: Script) -> None:
    script(RuntimeError("upstream exploded"))

    result = await run_to_completion(agent(), TASK, ctx(), config=CONFIG)

    assert isinstance(result, Failed)
    assert result.failure.code == "llm_error"


async def test_events_have_increasing_seq_and_round_trip(script: Script) -> None:
    script(
        reply(thinking("thinking"), tool_use("fc_1", "lookup_price", sku="SKU-1")),
        final(answer="12.50", confidence=0.9),
    )
    context = ctx()

    events = [e async for e in run(agent(), TASK, context, config=CONFIG)]

    assert [e.seq for e in events] == list(range(len(events)))
    assert {e.run_id for e in events} == {context.run_id}
    assert isinstance(events[-1], RunCompleted)
    assert not any(isinstance(e, TextDelta) for e in events)
    for event in events:
        assert EventAdapter.validate_json(event.model_dump_json()) == event


# --- Tenant namespacing ----------------------------------------------------


def test_agno_ids_are_namespaced_by_tenant() -> None:
    person = ctx()
    ids = agno_adapter.agno_ids(person, "rfq-42")
    assert ids.user_id == f"{person.tenant_id}:{person.user_id}"
    assert ids.session_id == f"{person.tenant_id}:rfq-42"
    assert ids.run_id == str(person.run_id)

    background = ToolContext(tenant_id=uuid4(), user_id=None, run_id=uuid4())
    ids = agno_adapter.agno_ids(background)
    assert ids.user_id == f"{background.tenant_id}:system"
    assert ids.session_id == f"{background.tenant_id}:{background.run_id}"

    with pytest.raises(ValueError, match="session"):
        agno_adapter.agno_ids(person, "")


async def test_agno_runs_under_namespaced_ids(
    script: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    script(final(answer="x", confidence=1))
    seen: list[dict[str, Any]] = []
    real = agno_adapter.build_agent

    def build_agent(*args: Any, **kwargs: Any) -> Agent:
        built = real(*args, **kwargs)
        # Through Any: Agno annotates arun with bare List/Dict (partially unknown).
        arun = cast(Any, built).arun

        def spy(*a: Any, **kw: Any) -> Any:
            seen.append(kw)
            return arun(*a, **kw)

        monkeypatch.setattr(built, "arun", spy)
        seen.append({"agent.user_id": built.user_id, "agent.session_id": built.session_id})
        return built

    monkeypatch.setattr(agno_adapter, "build_agent", build_agent)
    context = ctx()

    await run_to_completion(agent(), TASK, context, session="rfq-42", config=CONFIG)

    user = f"{context.tenant_id}:{context.user_id}"
    session = f"{context.tenant_id}:rfq-42"
    assert seen[0] == {"agent.user_id": user, "agent.session_id": session}
    assert seen[1]["user_id"] == user
    assert seen[1]["session_id"] == session
    assert seen[1]["run_id"] == str(context.run_id)


# --- Telemetry -------------------------------------------------------------


async def test_agno_telemetry_is_disabled(script: Script, monkeypatch: pytest.MonkeyPatch) -> None:
    import agno.api.agent

    sent: list[object] = []

    async def record(*args: object, **kwargs: object) -> None:
        sent.append(kwargs)

    monkeypatch.setattr(agno.api.agent, "acreate_agent_run", record)
    # Importing the package forced it off.
    assert os.environ["AGNO_TELEMETRY"] == "false"
    # Even if something turns it back on, a run forces it off again.
    monkeypatch.setenv("AGNO_TELEMETRY", "true")
    script(final(answer="x", confidence=1))
    built: list[Agent] = []
    real = agno_adapter.build_agent

    def build_agent(*args: Any, **kwargs: Any) -> Agent:
        built.append(real(*args, **kwargs))
        return built[-1]

    monkeypatch.setattr(agno_adapter, "build_agent", build_agent)

    result = await run_to_completion(agent(), TASK, ctx(), config=CONFIG)

    assert isinstance(result, Completed)
    assert built[0].telemetry is False
    assert os.environ["AGNO_TELEMETRY"] == "false"
    assert sent == []


# --- Definition and storage config -----------------------------------------


def test_prompt_version_must_match_file() -> None:
    with pytest.raises(ValueError, match="prompt_version"):
        AgentDefinition(
            name="example",
            prompt_file="example.v1.md",
            prompt_version=2,
            model=MODEL,
            output_model=Answer,
        )


def test_tool_names_must_be_valid_for_providers() -> None:
    with pytest.raises(ValueError, match="invalid tool name"):
        Tool(
            name="look up",
            description="x",
            input_model=LookupIn,
            output_model=LookupOut,
            kind="read",
            handler=lookup_price,
        )


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://bido_agno:pw@localhost:5432/bido",
        "postgresql+asyncpg://bido_agno:pw@localhost:5432/bido",
    ],
)
def test_agno_db_url_uses_asyncpg(url: str) -> None:
    normalized = agno_adapter.agno_db_url(url)
    assert normalized.drivername == "postgresql+asyncpg"
    rendered = normalized.render_as_string(hide_password=False)
    assert rendered.endswith("://bido_agno:pw@localhost:5432/bido")


def test_agno_db_url_rejects_other_drivers() -> None:
    with pytest.raises(ValueError, match="asyncpg"):
        agno_adapter.agno_db_url("postgresql+psycopg://u:p@localhost/bido")


def test_no_storage_without_a_database_url() -> None:
    context = ctx()
    built = agno_adapter.build_agent(agent(), context, agno_adapter.agno_ids(context), CONFIG)
    assert built.db is None


def test_storage_is_agno_postgres_in_schema_agno() -> None:
    # Building the engine does not connect, so no database is needed here.
    config = RunnerConfig(
        openai_api_key="test-not-a-real-key",
        agno_database_url="postgresql+asyncpg://bido_agno:pw@localhost:54320/bido",
    )
    context = ctx()
    built = agno_adapter.build_agent(agent(), context, agno_adapter.agno_ids(context), config)
    assert isinstance(built.db, AsyncPostgresDb)
    assert built.db.db_schema == "agno"
    assert built.db.create_schema is False
    assert built.db.db_engine.url.drivername == "postgresql+asyncpg"
