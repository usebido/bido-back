"""Our thin agent runner: a small bounded loop over `LlmClient`.

One entry point, `run(agent, input, context, llm)`, yields OUR events and ends
with exactly one terminal event:

- `RunCompleted` - the model called `submit_output` with a valid output;
- `ApprovalRequested` - the model proposed an `act` tool call (not executed);
- `RunFailed` - anything else (max_turns, invalid output, refusal, LLM error).

The loop: call the model; run each requested `read`/`draft` tool (input
validated by the tool's model; failures go back to the model as error
results); stop at the first `act` call; stop when `submit_output` arrives.
The model is asked to finish through the `submit_output` tool whose schema is
the agent's output model. Tool choice is left on `auto` (current Claude models
reject forced tool choice), so if the model stops without calling it, the
runner reminds it once per turn until `max_turns` runs out.

The history is append-only: assistant messages (including reasoning) are kept
exactly as returned.
"""

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from pydantic import BaseModel, JsonValue, ValidationError

from bido_back.agents.context import ToolContext
from bido_back.agents.definition import AgentDefinition
from bido_back.agents.events import (
    ApprovalRequested,
    Artifact,
    Event,
    ReasoningStep,
    RunCompleted,
    RunFailed,
    TextDelta,
    ToolCalled,
    ToolResult,
    args_hash,
)
from bido_back.agents.llm import (
    AssistantMessage,
    LlmClient,
    LlmError,
    LlmRequest,
    Message,
    ReasoningPart,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    ToolSpec,
    UserMessage,
)
from bido_back.agents.tools import SUBMIT_OUTPUT_TOOL, AnyTool, ToolError, json_schema

logger = logging.getLogger(__name__)

SUBMIT_INSTRUCTION = (
    f"When you have the final result, call the `{SUBMIT_OUTPUT_TOOL}` tool with it. "
    "That call is the only way to finish; do not put the final result in plain text."
)
SUBMIT_REMINDER = f"Finish by calling the `{SUBMIT_OUTPUT_TOOL}` tool with your final result."


# --- Results ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Completed[OutT: BaseModel]:
    output: OutT
    events: list[Event] = field(default_factory=list[Event])


@dataclass(frozen=True, slots=True)
class NeedsApproval:
    approval: ApprovalRequested
    events: list[Event] = field(default_factory=list[Event])


@dataclass(frozen=True, slots=True)
class Failed:
    failure: RunFailed
    events: list[Event] = field(default_factory=list[Event])


type RunResult[OutT: BaseModel] = Completed[OutT] | NeedsApproval | Failed


# --- Loop ------------------------------------------------------------------


class _Emitter:
    def __init__(self, context: ToolContext) -> None:
        self._run_id = context.run_id
        self._seq = 0

    def __call__[
        E: (
            TextDelta,
            ReasoningStep,
            ToolCalled,
            ToolResult,
            Artifact,
            ApprovalRequested,
            RunCompleted,
            RunFailed,
        )
    ](self, event_type: type[E], **fields: object) -> E:
        event = event_type.model_validate({"run_id": self._run_id, "seq": self._seq, **fields})
        self._seq += 1
        return event


async def run[OutT: BaseModel](
    agent: AgentDefinition[OutT],
    input: BaseModel,
    context: ToolContext,
    llm: LlmClient,
) -> AsyncIterator[Event]:
    emit = _Emitter(context)
    try:
        async for event in _loop(agent, input, context, llm, emit):
            yield event
    except LlmError as exc:
        logger.warning("agent %s run %s: llm error %s", agent.name, context.run_id, exc.code)
        yield emit(RunFailed, code="llm_error", message=f"{exc.code}: {exc}")
    except Exception:
        logger.exception("agent %s run %s: internal error", agent.name, context.run_id)
        yield emit(RunFailed, code="internal_error", message="the run failed unexpectedly")


async def _loop[OutT: BaseModel](
    agent: AgentDefinition[OutT],
    input: BaseModel,
    context: ToolContext,
    llm: LlmClient,
    emit: _Emitter,
) -> AsyncIterator[Event]:
    system = f"{agent.load_prompt()}\n\n{SUBMIT_INSTRUCTION}"
    tool_specs = [
        *(
            ToolSpec(name=t.name, description=t.description, input_schema=t.input_schema())
            for t in agent.tools
        ),
        ToolSpec(
            name=SUBMIT_OUTPUT_TOOL,
            description="Submit the final result of this task. Call it exactly once, last.",
            input_schema=json_schema(agent.output_model),
        ),
    ]
    messages: list[Message] = [UserMessage(content=[TextPart(text=input.model_dump_json())])]

    for _turn in range(agent.max_turns):
        response = await llm.complete(
            LlmRequest(
                model=agent.model,
                system=system,
                messages=messages,
                tools=tool_specs,
                max_tokens=agent.max_tokens,
                thinking=agent.thinking,
                effort=agent.effort,
            )
        )
        messages.append(response.message)
        for event in _content_events(response.message, emit):
            yield event

        if response.stop_reason == "refusal":
            yield emit(RunFailed, code="refused", message="the model declined this request")
            return
        if response.stop_reason == "max_tokens":
            yield emit(RunFailed, code="max_tokens", message="the model output was truncated")
            return

        calls = response.tool_calls
        if not calls:
            messages.append(UserMessage(content=[TextPart(text=SUBMIT_REMINDER)]))
            continue

        results: list[TextPart | ToolResultPart] = []
        for call in calls:
            if call.name == SUBMIT_OUTPUT_TOOL:
                yield _finish(agent, call, emit)
                return

            tool = agent.tool(call.name)
            if tool is None:
                results.append(_error_result(call, f"unknown tool {call.name!r}"))
                continue

            try:
                parsed = tool.parse_input(call.args)
            except ValidationError as exc:
                results.append(_error_result(call, f"invalid arguments: {_errors(exc)}"))
                continue
            normalized: dict[str, JsonValue] = parsed.model_dump(mode="json")

            if tool.kind == "act":
                # Never executed here. The run ends with this proposal.
                yield emit(
                    ApprovalRequested,
                    call_id=call.id,
                    tool=tool.name,
                    args=normalized,
                    args_hash=args_hash(normalized),
                )
                return

            yield emit(ToolCalled, call_id=call.id, tool=tool.name, kind=tool.kind, args=normalized)
            async for event in _execute(tool, parsed, call, context, emit, results):
                yield event

        messages.append(UserMessage(content=results))

    yield emit(
        RunFailed,
        code="max_turns_exceeded",
        message=f"no final output after {agent.max_turns} turns",
    )


def _content_events(message: AssistantMessage, emit: _Emitter) -> list[Event]:
    events: list[Event] = []
    for part in message.content:
        if isinstance(part, ReasoningPart) and part.text:
            events.append(emit(ReasoningStep, text=part.text))
        elif isinstance(part, TextPart) and part.text:
            events.append(emit(TextDelta, text=part.text))
    return events


async def _execute(
    tool: AnyTool,
    parsed: BaseModel,
    call: ToolCallPart,
    context: ToolContext,
    emit: _Emitter,
    results: list[TextPart | ToolResultPart],
) -> AsyncIterator[Event]:
    try:
        output: BaseModel = await tool.invoke(context, parsed)
    except ToolError as exc:
        message = str(exc) or "tool error"
    except Exception:
        logger.exception("tool %s failed in run %s", tool.name, context.run_id)
        message = "internal error while running the tool"
    else:
        data: dict[str, JsonValue] = output.model_dump(mode="json")
        results.append(ToolResultPart(call_id=call.id, content=output.model_dump_json()))
        yield emit(ToolResult, call_id=call.id, tool=tool.name, ok=True, output=data)
        if tool.artifact_kind is not None:
            yield emit(Artifact, kind=tool.artifact_kind, data=data)
        return

    results.append(_error_result(call, message))
    yield emit(ToolResult, call_id=call.id, tool=tool.name, ok=False, error=message)


def _finish[OutT: BaseModel](
    agent: AgentDefinition[OutT], call: ToolCallPart, emit: _Emitter
) -> Event:
    try:
        output = agent.output_model.model_validate(call.args)
    except ValidationError as exc:
        return emit(RunFailed, code="invalid_output", message=_errors(exc))
    return emit(RunCompleted, output=output.model_dump(mode="json"))


def _error_result(call: ToolCallPart, message: str) -> ToolResultPart:
    return ToolResultPart(call_id=call.id, content=f"error: {message}", is_error=True)


def _errors(exc: ValidationError) -> str:
    # Field locations and messages only; never echo input values back.
    return "; ".join(
        f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
        for err in exc.errors(include_input=False, include_url=False)
    )


# --- Convenience -----------------------------------------------------------


async def run_to_completion[OutT: BaseModel](
    agent: AgentDefinition[OutT],
    input: BaseModel,
    context: ToolContext,
    llm: LlmClient,
) -> RunResult[OutT]:
    events: list[Event] = []
    async for event in run(agent, input, context, llm):
        events.append(event)
        if isinstance(event, RunCompleted):
            return Completed(agent.output_model.model_validate(event.output), events)
        if isinstance(event, ApprovalRequested):
            return NeedsApproval(event, events)
        if isinstance(event, RunFailed):
            return Failed(event, events)
    # run() always ends with a terminal event; this is a defensive fallback.
    failure = RunFailed(
        run_id=context.run_id, seq=len(events), code="internal_error", message="no terminal event"
    )
    return Failed(failure, [*events, failure])
