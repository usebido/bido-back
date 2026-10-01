"""Runner behavior against a scripted fake LLM. No network."""

import hashlib
import json
from collections.abc import Sequence
from typing import cast
from uuid import uuid4

import anthropic
import pytest
from pydantic import BaseModel, Field

from bido_back.agents.context import ToolContext
from bido_back.agents.definition import AgentDefinition
from bido_back.agents.events import (
    Artifact,
    EventAdapter,
    ReasoningStep,
    RunCompleted,
    TextDelta,
    ToolCalled,
    ToolResult,
)
from bido_back.agents.llm import (
    AnthropicLlmClient,
    AssistantMessage,
    LlmRequest,
    LlmResponse,
    ReasoningPart,
    StopReason,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    UserMessage,
)
from bido_back.agents.runner import Completed, Failed, NeedsApproval, run, run_to_completion
from bido_back.agents.tools import SUBMIT_OUTPUT_TOOL, Tool, ToolError

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


class FakeLlmClient:
    """Returns scripted responses in order and records every request."""

    def __init__(self, responses: Sequence[LlmResponse]) -> None:
        self._responses = list(responses)
        self.requests: list[LlmRequest] = []

    async def complete(self, request: LlmRequest) -> LlmResponse:
        self.requests.append(request)
        if not self._responses:
            raise AssertionError("FakeLlmClient ran out of scripted responses")
        return self._responses.pop(0)


def reply(
    *parts: TextPart | ReasoningPart | ToolCallPart, stop: StopReason | None = None
) -> LlmResponse:
    has_calls = any(isinstance(p, ToolCallPart) for p in parts)
    return LlmResponse(
        message=AssistantMessage(content=list(parts)),
        stop_reason=stop or ("tool_use" if has_calls else "end_turn"),
    )


def call(name: str, call_id: str, **args: object) -> ToolCallPart:
    return ToolCallPart(id=call_id, name=name, args=json.loads(json.dumps(args)))


lookup_calls: list[LookupIn] = []
send_calls: list[SendIn] = []


async def lookup_price(ctx: ToolContext, args: LookupIn) -> LookupOut:
    lookup_calls.append(args)
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
        model="claude-opus-5-5",
        output_model=Answer,
        tools=(LOOKUP, SEND),
        max_turns=max_turns,
    )


@pytest.fixture(autouse=True)
def _reset_calls() -> None:
    lookup_calls.clear()
    send_calls.clear()


def ctx() -> ToolContext:
    return ToolContext(
        tenant_id=uuid4(), user_id=uuid4(), run_id=uuid4(), roles=frozenset({"buyer"})
    )


TASK = Task(question="How much is SKU-1?")


# --- Tests -----------------------------------------------------------------


async def test_read_tool_runs_and_its_result_is_fed_back() -> None:
    llm = FakeLlmClient(
        [
            reply(
                ReasoningPart(text="I need the price.", signature="sig-1"),
                call("lookup_price", "c1", sku="SKU-1"),
            ),
            reply(call(SUBMIT_OUTPUT_TOOL, "c2", answer="12.50", confidence=0.9)),
        ]
    )

    result = await run_to_completion(agent(), TASK, ctx(), llm)

    assert isinstance(result, Completed)
    assert result.output == Answer(answer="12.50", confidence=0.9)
    assert lookup_calls == [LookupIn(sku="SKU-1")]

    second = llm.requests[1]
    assert isinstance(second.messages[1], AssistantMessage)
    # Reasoning is replayed verbatim.
    assert second.messages[1].content[0] == ReasoningPart(
        text="I need the price.", signature="sig-1"
    )
    tool_turn = second.messages[2]
    assert isinstance(tool_turn, UserMessage)
    assert tool_turn.content == [
        ToolResultPart(call_id="c1", content='{"sku":"SKU-1","price_cents":1250}')
    ]

    kinds = [type(e) for e in result.events]
    assert kinds == [ReasoningStep, ToolCalled, ToolResult, Artifact, RunCompleted]


async def test_prompt_and_tools_are_sent() -> None:
    llm = FakeLlmClient([reply(call(SUBMIT_OUTPUT_TOOL, "c1", answer="x", confidence=1))])

    await run_to_completion(agent(), TASK, ctx(), llm)

    request = llm.requests[0]
    assert request.system.startswith("You are a procurement assistant")
    assert SUBMIT_OUTPUT_TOOL in request.system
    assert [t.name for t in request.tools] == ["lookup_price", "send_payment", SUBMIT_OUTPUT_TOOL]
    assert request.tools[-1].input_schema["required"] == ["answer", "confidence"]
    first = request.messages[0]
    assert isinstance(first, UserMessage)
    assert first.content == [TextPart(text=TASK.model_dump_json())]


async def test_act_tool_is_never_executed_and_requests_approval() -> None:
    llm = FakeLlmClient(
        [reply(call("send_payment", "c1", supplier_id="sup_9", amount_cents=50_000))]
    )

    result = await run_to_completion(agent(), TASK, ctx(), llm)

    assert isinstance(result, NeedsApproval)
    assert send_calls == []
    assert len(llm.requests) == 1
    expected_args = {"supplier_id": "sup_9", "amount_cents": 50_000}
    canonical = json.dumps(expected_args, sort_keys=True, separators=(",", ":"))
    assert result.approval.tool == "send_payment"
    assert result.approval.args == expected_args
    assert result.approval.args_hash == hashlib.sha256(canonical.encode()).hexdigest()
    assert not any(isinstance(e, ToolCalled) for e in result.events)


async def test_act_tool_with_invalid_args_is_sent_back_not_proposed() -> None:
    llm = FakeLlmClient(
        [
            reply(call("send_payment", "c1", supplier_id="sup_9", amount_cents="lots")),
            reply(call(SUBMIT_OUTPUT_TOOL, "c2", answer="gave up", confidence=0)),
        ]
    )

    result = await run_to_completion(agent(), TASK, ctx(), llm)

    assert isinstance(result, Completed)
    tool_turn = llm.requests[1].messages[2]
    assert isinstance(tool_turn, UserMessage)
    error = tool_turn.content[0]
    assert isinstance(error, ToolResultPart)
    assert error.is_error
    assert "amount_cents" in error.content
    assert "lots" not in error.content  # input values are not echoed


async def test_max_turns_exceeded_fails() -> None:
    llm = FakeLlmClient([reply(call("lookup_price", f"c{i}", sku="SKU-1")) for i in range(3)])

    result = await run_to_completion(agent(max_turns=3), TASK, ctx(), llm)

    assert isinstance(result, Failed)
    assert result.failure.code == "max_turns_exceeded"
    assert len(llm.requests) == 3


async def test_text_only_turn_gets_a_reminder_and_counts_as_a_turn() -> None:
    llm = FakeLlmClient(
        [
            reply(TextPart(text="It costs 12.50.")),
            reply(call(SUBMIT_OUTPUT_TOOL, "c1", answer="12.50", confidence=0.8)),
        ]
    )

    result = await run_to_completion(agent(), TASK, ctx(), llm)

    assert isinstance(result, Completed)
    assert isinstance(result.events[0], TextDelta)
    reminder = llm.requests[1].messages[-1]
    assert isinstance(reminder, UserMessage)
    assert SUBMIT_OUTPUT_TOOL in str(reminder.content[0])


async def test_invalid_final_output_fails() -> None:
    llm = FakeLlmClient([reply(call(SUBMIT_OUTPUT_TOOL, "c1", answer="x", confidence=7))])

    result = await run_to_completion(agent(), TASK, ctx(), llm)

    assert isinstance(result, Failed)
    assert result.failure.code == "invalid_output"
    assert "confidence" in result.failure.message


async def test_tool_errors_become_error_results() -> None:
    llm = FakeLlmClient(
        [
            reply(
                call("lookup_price", "c1", sku="missing"),
                call("lookup_price", "c2", sku="boom"),
                call("no_such_tool", "c3"),
            ),
            reply(call(SUBMIT_OUTPUT_TOOL, "c4", answer="unknown", confidence=0)),
        ]
    )

    result = await run_to_completion(agent(), TASK, ctx(), llm)

    assert isinstance(result, Completed)
    tool_results = [e for e in result.events if isinstance(e, ToolResult)]
    assert [(r.ok, r.error) for r in tool_results] == [
        (False, "no such sku"),
        (False, "internal error while running the tool"),
    ]
    tool_turn = llm.requests[1].messages[2]
    assert isinstance(tool_turn, UserMessage)
    contents = [p for p in tool_turn.content if isinstance(p, ToolResultPart)]
    assert [p.call_id for p in contents] == ["c1", "c2", "c3"]
    assert all(p.is_error for p in contents)
    assert "hunter2" not in str(contents)


async def test_refusal_fails() -> None:
    llm = FakeLlmClient([reply(stop="refusal")])

    result = await run_to_completion(agent(), TASK, ctx(), llm)

    assert isinstance(result, Failed)
    assert result.failure.code == "refused"


async def test_events_have_increasing_seq_and_round_trip() -> None:
    context = ctx()
    llm = FakeLlmClient(
        [
            reply(
                ReasoningPart(text="thinking", signature="s"),
                TextPart(text="Looking it up."),
                call("lookup_price", "c1", sku="SKU-1"),
            ),
            reply(call(SUBMIT_OUTPUT_TOOL, "c2", answer="12.50", confidence=0.9)),
        ]
    )

    events = [e async for e in run(agent(), TASK, context, llm)]

    assert [e.seq for e in events] == list(range(len(events)))
    assert {e.run_id for e in events} == {context.run_id}
    assert isinstance(events[-1], RunCompleted)
    for event in events:
        assert EventAdapter.validate_json(event.model_dump_json()) == event


def test_prompt_version_must_match_file() -> None:
    with pytest.raises(ValueError, match="prompt_version"):
        AgentDefinition(
            name="example",
            prompt_file="example.v1.md",
            prompt_version=2,
            model="claude-opus-5-5",
            output_model=Answer,
        )


# --- Anthropic adapter (no network: the SDK client is stubbed) -------------


class _StubMessages:
    def __init__(self, response: anthropic.types.Message) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    async def create(self, **kwargs: object) -> anthropic.types.Message:
        self.calls.append(kwargs)
        return self.response


class _StubAnthropic:
    def __init__(self, response: anthropic.types.Message) -> None:
        self.messages = _StubMessages(response)


async def test_anthropic_adapter_maps_both_directions() -> None:
    sdk_response = anthropic.types.Message.model_validate(
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-5-5",
            "content": [
                {"type": "thinking", "thinking": "check price", "signature": "sig"},
                {"type": "text", "text": "Looking."},
                {"type": "tool_use", "id": "tu_1", "name": "lookup_price", "input": {"sku": "A"}},
            ],
            "stop_reason": "tool_use",
            "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
    )
    stub = _StubAnthropic(sdk_response)
    client = AnthropicLlmClient(cast(anthropic.AsyncAnthropic, stub))
    history = [
        UserMessage(content=[TextPart(text="hi")]),
        AssistantMessage(
            content=[
                ReasoningPart(text="r", signature="s0"),
                ToolCallPart(id="tu_0", name="lookup_price", args={"sku": "Z"}),
            ]
        ),
        UserMessage(content=[ToolResultPart(call_id="tu_0", content="oops", is_error=True)]),
    ]

    response = await client.complete(
        LlmRequest(model="claude-opus-5-5", system="sys", messages=history, effort="low")
    )

    assert response.stop_reason == "tool_use"
    assert response.message.content == [
        ReasoningPart(text="check price", signature="sig"),
        TextPart(text="Looking."),
        ToolCallPart(id="tu_1", name="lookup_price", args={"sku": "A"}),
    ]
    assert response.usage.input_tokens == 10

    sent = stub.messages.calls[0]
    assert sent["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert sent["output_config"] == {"effort": "low"}
    assert sent["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "r", "signature": "s0"},
                {"type": "tool_use", "id": "tu_0", "name": "lookup_price", "input": {"sku": "Z"}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "tu_0", "content": "oops", "is_error": True}
            ],
        },
    ]
