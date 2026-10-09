"""The Agno adapter: the ONLY module that imports Agno (or the OpenAI model it wraps).

Agno runs the agent loop, calls the model, executes tools, and keeps sessions
and runs in Postgres. Everything that crosses this module's boundary is ours:
`AgentDefinition` and `Tool` in, `events.Event` out. Swapping Agno means
rewriting this file, not its callers.

How our rules map onto Agno:

- `read` / `draft` tools become Agno `Function`s whose entrypoint validates
  the model's arguments with the tool's input model, injects the run's
  `ToolContext` (the tenant comes from there, never from the model), and runs
  the handler. Handlers run one at a time per run, as before (Agno would run
  a batch of tool calls concurrently).
- `act` tools become Agno `Function`s with `external_execution=True`. Agno
  never calls the entrypoint of such a function: the run PAUSES with the
  proposed call (agno/models/base.py `arun_function_calls`; the continue path
  only appends a caller-supplied result, agno/agent/_tools.py
  `handle_external_execution_update`). The entrypoint we register refuses
  anyway, so even a misrouted call cannot execute. We turn the pause into an
  `ApprovalRequested` and stop. If the proposed arguments do not validate,
  the error is fed back as the call's result and the run continues, so a
  person is never asked to approve an invalid payload.
- The model is OpenAI's, through the Responses API, stateless: `store=False`,
  so OpenAI keeps no conversation state, and each turn replays the full
  context, including the encrypted reasoning items of earlier turns.
- The final answer uses Agno's `output_schema` (native strict structured
  output on OpenAI) and is validated again with our output model. Agents with
  `stream_text` run without an output schema instead: the model's text streams
  as `TextDelta` events, and the whole text fills the output model's one str
  field (validated the same way).
- Every Agno user_id / session_id is namespaced by tenant (`agno_ids`).
- Agno telemetry is off: `AGNO_TELEMETRY=false` is forced in the process
  environment (bido_back/agents/__init__.py, and again per run here, because
  Agno re-reads it on every run and it overrides the constructor flag:
  agno/agent/_init.py `set_telemetry`), and `telemetry=False` is passed to
  every Agno `Agent`.
- No Agno toolkits, knowledge, memory manager, MCP, skills or filesystem
  tools are enabled: the model sees exactly the agent definition's tools.
"""

import asyncio
import logging
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Final, Literal, cast

from agno.agent import Agent
from agno.db.base import SessionType
from agno.db.postgres import AsyncPostgresDb, create_async_postgres_engine
from agno.exceptions import redact_secrets
from agno.models.openai.responses import OpenAIResponses
from agno.run.agent import (
    ModelRequestCompletedEvent,
    ModelRequestStartedEvent,
    RunCancelledEvent,
    RunCompletedEvent,
    RunContentEvent,
    RunContinuedEvent,
    RunErrorEvent,
    RunOutput,
    RunOutputEvent,
    RunPausedEvent,
    RunStartedEvent,
    ToolCallCompletedEvent,
    ToolCallStartedEvent,
)
from agno.run.requirement import RunRequirement
from agno.session.agent import AgentSession
from agno.tools.function import Function, FunctionCall
from pydantic import BaseModel, JsonValue, ValidationError
from sqlalchemy.engine import URL, make_url

from bido_back.agents import tracing
from bido_back.agents.approvals import ApprovalProposal, ApprovalSink
from bido_back.agents.config import RunnerConfig
from bido_back.agents.context import ToolContext
from bido_back.agents.definition import AgentDefinition
from bido_back.agents.events import (
    ApprovalRequested,
    Artifact,
    Event,
    ReasoningDelta,
    ReasoningStep,
    RunCompleted,
    RunFailed,
    TextDelta,
    ToolCalled,
    ToolResult,
    args_hash,
)
from bido_back.agents.tools import AnyTool, ToolError
from bido_back.agents.usage import ModelUsage, UsageMeter
from bido_back.logs import safe_reason

logger = logging.getLogger(__name__)

AGNO_SCHEMA: Final = "agno"
# Agno injects the FunctionCall into an entrypoint parameter with this name
# (agno/tools/function.py `FunctionCall._build_entrypoint_args`).
_FC_PARAM: Final = "fc"

_INTERNAL_ERROR: Final = "internal error while running the tool"
_ACT_REFUSAL: Final = "error: this action needs a person's approval and was not executed"


def disable_agno_telemetry() -> None:
    """Force Agno's telemetry off for this process (see module docstring)."""
    os.environ["AGNO_TELEMETRY"] = "false"


# --- Tenant namespacing ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class AgnoIds:
    user_id: str
    session_id: str
    run_id: str


def agno_ids(context: ToolContext, session: str | None = None) -> AgnoIds:
    """The ids every Agno call runs under, all derived from the run's tenant.

    The tenant prefix is a fixed-length UUID followed by ':', so no value of
    `session` or `user_id` can produce another tenant's id. Without a session
    key the run is its own session.
    """
    if session is not None and not session:
        raise ValueError("session must be a non-empty string or None")
    tenant = str(context.tenant_id)
    user = str(context.user_id) if context.user_id is not None else "system"
    return AgnoIds(
        user_id=f"{tenant}:{user}",
        session_id=f"{tenant}:{session or context.run_id}",
        run_id=str(context.run_id),
    )


# --- Storage ---------------------------------------------------------------


def agno_db_url(url: str) -> URL:
    """Normalize a Postgres URL to the asyncpg driver, which AsyncPostgresDb needs.

    asyncpg is already a dependency. Agno's own default for a bare
    `postgresql://` URL is psycopg, which is not installed.
    """
    parsed = make_url(url)
    if parsed.drivername not in ("postgres", "postgresql", "postgresql+asyncpg"):
        raise ValueError(
            "agno_database_url must use postgresql or postgresql+asyncpg, "
            f"got {parsed.drivername!r}"
        )
    return parsed.set(drivername="postgresql+asyncpg")


@lru_cache(maxsize=4)
def _agno_db(url: str) -> AsyncPostgresDb:
    # One engine (one pool) per URL for the process. create_schema=False: the
    # `agno` schema is provisioned by us and the Agno role cannot CREATE SCHEMA.
    # Agno creates its own tables inside that schema on first use.
    engine = create_async_postgres_engine(agno_db_url(url))
    return AsyncPostgresDb(db_engine=engine, db_schema=AGNO_SCHEMA, create_schema=False)


@dataclass(frozen=True, slots=True)
class StoredRun:
    """One stored run of a session, as plain data: what went in, what came out."""

    run_id: str
    created_at: int
    status: str
    # The run's input as given to `run()` (our input model's JSON), if stored.
    input: str | None
    # The final output: a JSON-able mapping, a string, or None.
    output: dict[str, JsonValue] | str | None


async def load_session_runs(
    context: ToolContext, session: str, config: RunnerConfig
) -> list[StoredRun] | None:
    """The stored runs of `session` for the context's tenant AND user, oldest first.

    None when run storage is not configured or the session does not exist for
    this user. The ids are built exactly as for a run (`agno_ids`), and Agno
    filters the read by the namespaced user id too, so another tenant's or
    another user's session is never returned.
    """
    if not config.agno_database_url:
        return None
    ids = agno_ids(context, session)
    found = await _agno_db(config.agno_database_url).get_session(
        session_id=ids.session_id, session_type=SessionType.AGENT, user_id=ids.user_id
    )
    if not isinstance(found, AgentSession) or found.user_id != ids.user_id:
        return None
    runs: list[StoredRun] = []
    for run in found.runs or []:
        if not isinstance(run, RunOutput):
            continue
        content: object = run.content
        output: dict[str, JsonValue] | str | None
        if isinstance(content, BaseModel):
            output = content.model_dump(mode="json")
        elif isinstance(content, str):
            output = content
        elif isinstance(content, Mapping):
            output = dict(cast(Mapping[str, JsonValue], content))
        else:
            output = None
        raw_input = run.input.input_content_string() if run.input is not None else None
        runs.append(
            StoredRun(
                run_id=run.run_id or "",
                created_at=run.created_at,
                status=str(run.status.value if hasattr(run.status, "value") else run.status),
                input=raw_input or None,
                output=output,
            )
        )
    return sorted(runs, key=lambda r: r.created_at)


# --- Model -----------------------------------------------------------------


class _ReasoningResponses(OpenAIResponses):
    """OpenAIResponses for models that reason.

    Agno recognizes reasoning models by id prefix (o3, o4-mini, gpt-5) and
    otherwise drops the reasoning items between turns. Every model we run
    reasons, so this is always true: with `store=False` Agno then asks for the
    encrypted reasoning and replays it, which a reasoning model needs to
    continue after a tool call.
    """

    def _using_reasoning_model(self) -> bool:
        return True


def build_model(definition: AgentDefinition[Any], config: RunnerConfig) -> OpenAIResponses:
    """The Agno model for a definition. Tests replace this to script responses."""
    return _ReasoningResponses(
        id=definition.model,
        max_output_tokens=definition.max_tokens,
        reasoning_effort=definition.effort,
        reasoning_summary="auto" if definition.thinking else None,
        store=False,
        api_key=config.openai_api_key,
        timeout=config.model_timeout_s,
        max_retries=config.model_max_retries,
    )


# --- Tools -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Outcome:
    status: Literal["ok", "error", "invalid_args"]
    output: dict[str, JsonValue] | None = None
    error: str | None = None


@dataclass(slots=True)
class _RunState:
    # Outcome of each executed tool call, by provider call id.
    outcomes: dict[str, _Outcome] = field(default_factory=dict[str, _Outcome])
    # Handlers of one run execute one at a time.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Where usage and traces go (the runner's; a fresh meter when called directly).
    scope: tracing.RunScope = field(default_factory=lambda: tracing.RunScope(UsageMeter()))


def _to_function(tool: AnyTool, context: ToolContext, state: _RunState) -> Function:
    if _FC_PARAM in tool.input_model.model_fields:
        raise ValueError(f"tool {tool.name!r}: input field name {_FC_PARAM!r} is reserved")
    is_act = tool.kind == "act"
    return Function(
        name=tool.name,
        description=tool.description,
        parameters=dict(tool.input_schema()),
        entrypoint=_refusing_entrypoint(tool) if is_act else _entrypoint(tool, context, state),
        # Our schema is used as-is; our own validation is the gate.
        skip_entrypoint_processing=True,
        strict=False,
        external_execution=True if is_act else None,
        external_execution_silent=True if is_act else None,
    )


def _entrypoint(
    tool: AnyTool, context: ToolContext, state: _RunState
) -> Callable[..., Awaitable[str]]:
    async def entrypoint(fc: FunctionCall, **kwargs: object) -> str:
        call_id = fc.call_id or ""
        try:
            parsed = tool.parse_input(kwargs)
        except ValidationError as exc:
            state.outcomes[call_id] = _Outcome(status="invalid_args")
            reason = validation_errors(exc)
            _log_tool_call(tool, context, call_id, "invalid_args", 0.0, reason=reason)
            return f"error: invalid arguments: {reason}"

        async with state.lock:
            started = time.perf_counter()
            with tracing.tool_call(
                state.scope,
                tool=tool.name,
                kind=tool.kind,
                call_id=call_id,
                args=parsed.model_dump(mode="json"),
            ) as traced:
                try:
                    output = await tool.invoke(context, parsed)
                except ToolError as exc:
                    message = str(exc) or "tool error"
                    cause = exc.__cause__ or exc.__context__
                    error_class = type(cause).__name__ if cause is not None else "ToolError"
                    _log_tool_call(
                        tool,
                        context,
                        call_id,
                        "error",
                        started,
                        reason=message,
                        error_class=error_class,
                    )
                    traced.finished("error", error=message, error_class=error_class)
                except Exception as exc:
                    logger.exception(
                        "tool %s failed in run %s",
                        tool.name,
                        context.run_id,
                        extra={"tool": tool.name, "run_id": str(context.run_id)},
                    )
                    message = _INTERNAL_ERROR
                    _log_tool_call(
                        tool,
                        context,
                        call_id,
                        "internal_error",
                        started,
                        error_class=type(exc).__name__,
                    )
                    traced.finished("internal_error", error=message, error_class=type(exc).__name__)
                else:
                    _log_tool_call(tool, context, call_id, "ok", started)
                    dumped = output.model_dump(mode="json")
                    traced.finished("ok", output=dumped)
                    state.outcomes[call_id] = _Outcome(status="ok", output=dumped)
                    return tool.for_model(output)

        state.outcomes[call_id] = _Outcome(status="error", error=message)
        return f"error: {message}"

    return entrypoint


def _log_tool_call(
    tool: AnyTool,
    context: ToolContext,
    call_id: str,
    status: Literal["ok", "error", "invalid_args", "internal_error"],
    started: float,
    *,
    reason: str | None = None,
    error_class: str | None = None,
) -> None:
    """One line per tool call: name, outcome, duration; for errors a masked reason."""
    fields: dict[str, object] = {
        "tool": tool.name,
        "tool_kind": tool.kind,
        "run_id": str(context.run_id),
        "call_id": call_id,
        "status": status,
        "duration_ms": round((time.perf_counter() - started) * 1000, 1) if started else 0.0,
    }
    if reason is not None:
        fields["reason"] = safe_reason(reason)
    if error_class is not None:
        fields["error_class"] = error_class
    logger.log(logging.INFO if status == "ok" else logging.WARNING, "tool call", extra=fields)


def _refusing_entrypoint(tool: AnyTool) -> Callable[..., Awaitable[str]]:
    # Agno never calls an external_execution function. If it ever does, refuse.
    async def entrypoint(fc: FunctionCall, **kwargs: object) -> str:
        logger.error("act tool %s was routed to execution; refused", tool.name)
        return _ACT_REFUSAL

    return entrypoint


def build_agent(
    definition: AgentDefinition[Any],
    context: ToolContext,
    ids: AgnoIds,
    config: RunnerConfig,
    state: _RunState | None = None,
) -> Agent:
    """A fresh Agno Agent for one run. Never shared: Agno agents hold per-run state."""
    disable_agno_telemetry()
    state = state or _RunState()
    # Only an agent that reads its earlier runs keeps them. A stateless agent's input
    # (spreadsheet rows, a supplier's email) is not copied into Agno's tables, which
    # have no row-level security.
    stored = config.agno_database_url if definition.history_runs > 0 else None
    db = _agno_db(stored) if stored else None
    history = db is not None
    return Agent(
        model=build_model(definition, config),
        name=definition.name,
        id=f"{definition.name}.v{definition.prompt_version}",
        user_id=ids.user_id,
        session_id=ids.session_id,
        db=db,
        instructions=definition.load_prompt(),
        # Prompts are literal text: no `{var}` substitution from session state.
        resolve_in_context=False,
        tools=[_to_function(tool, context, state) for tool in definition.tools],
        # A free-text agent streams its answer; Agno never streams with a schema.
        output_schema=None if definition.stream_text else definition.output_model,
        add_history_to_context=history,
        num_history_runs=definition.history_runs if history else None,
        metadata={
            "tenant_id": str(context.tenant_id),
            "agent": definition.name,
            "prompt_version": definition.prompt_version,
        },
        telemetry=False,
        # Explicitly off, even where off is Agno's default today.
        enable_agentic_memory=False,
        update_memory_on_run=False,
        search_knowledge=False,
        search_past_sessions=False,
        read_chat_history=False,
        read_tool_call_history=False,
        enable_agentic_state=False,
        followups=False,
        markdown=False,
        store_events=False,
        debug_mode=False,
    )


# --- Running ---------------------------------------------------------------


class _Emitter:
    def __init__(self, context: ToolContext) -> None:
        self._run_id = context.run_id
        self._seq = 0

    def __call__[
        E: (
            TextDelta,
            ReasoningDelta,
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


class _Translator:
    """Maps one Agno event stream (a run, then any continuations) to our events."""

    def __init__(self, definition: AgentDefinition[Any], state: _RunState, emit: _Emitter) -> None:
        self._definition = definition
        self._state = state
        self._emit = emit
        # Each model request's usage (always) and span (when LLM tracing is on).
        self._generations = tracing.GenerationRecorder(state.scope)
        self._turns = 0
        self._announced: set[str] = set()
        # Free text written to the user so far (stream_text agents), and whether
        # a tool call interrupted it (the next text starts a new paragraph).
        self._text: list[str] = []
        self._text_interrupted = False
        # Reasoning already emitted in the current run segment (see _on_completed).
        self._segment_reasoning = ""
        # The reasoning step being streamed: its id and the deltas so far. The
        # provider streams a summary in tiny pieces; they are one step, closed
        # (emitted whole as ReasoningStep) when anything else happens.
        self._steps = 0
        self._step_id: str | None = None
        self._step_text: list[str] = []
        # Set by consume(): how the segment ended.
        self.completed = False
        self.final_content: object = None
        self.paused = False
        self.output: RunOutput | None = None
        self.failure: tuple[str, str] | None = None

    async def consume(
        self, stream: AsyncIterator[RunOutputEvent | RunOutput]
    ) -> AsyncIterator[Event]:
        self.paused = False
        self.output = None
        try:
            async for item in stream:
                if isinstance(item, RunOutput):
                    self.output = item
                    continue
                self._record(item)
                for event in self._translate(item):
                    yield event
                if self.failure is not None:
                    return
            for event in self._close_step():
                yield event
        finally:
            self._generations.close()
            # Leaving early (turn limit) must stop Agno before its next request.
            aclose = getattr(stream, "aclose", None)
            if aclose is not None:
                await aclose()

    def _record(self, item: RunOutputEvent) -> None:
        generations = self._generations
        if isinstance(item, ModelRequestStartedEvent):
            generations.started(item.model or self._definition.model)
        elif isinstance(item, ModelRequestCompletedEvent):
            generations.completed(
                item.model or self._definition.model,
                ModelUsage(
                    input_tokens=item.input_tokens or 0,
                    cached_input_tokens=item.cache_read_tokens or 0,
                    output_tokens=item.output_tokens or 0,
                    reasoning_tokens=item.reasoning_tokens or 0,
                ),
                time_to_first_token_s=item.time_to_first_token,
            )
        elif isinstance(item, RunContentEvent):
            generations.content(text=_as_text(item.content), reasoning=item.reasoning_content)
        elif isinstance(item, RunCompletedEvent):
            # With structured output the answer only arrives here, already parsed.
            generations.final_answer(_as_text(item.content))
        elif isinstance(item, ToolCallStartedEvent) and item.tool is not None:
            generations.tool_call(item.tool.tool_name or "", item.tool.tool_args or {})
        elif isinstance(item, RunPausedEvent):
            # The act calls the model proposed: they pause the run instead of starting.
            for execution in item.tools or []:
                generations.tool_call(execution.tool_name or "", execution.tool_args or {})

    def _translate(self, item: RunOutputEvent) -> list[Event]:
        emit = self._emit
        events: list[Event] = []
        has_text = (
            isinstance(item, RunContentEvent)
            and item.content_type == "str"
            and isinstance(item.content, str)
            and bool(item.content)
        )
        # The reasoning step being streamed ends when text, a tool call, a new
        # model request or the end of the run comes; other provider events
        # (empty deltas, bookkeeping) don't split it.
        if has_text or isinstance(
            item,
            ToolCallStartedEvent
            | ModelRequestStartedEvent
            | RunStartedEvent
            | RunContinuedEvent
            | RunPausedEvent
            | RunErrorEvent
            | RunCancelledEvent,
        ):
            events.extend(self._close_step())
        if isinstance(item, RunStartedEvent | RunContinuedEvent):
            self._segment_reasoning = ""
        elif isinstance(item, ModelRequestStartedEvent):
            self._turns += 1
            if self._turns > self._definition.max_turns:
                self.failure = (
                    "max_turns_exceeded",
                    f"no final output after {self._definition.max_turns} turns",
                )
        elif isinstance(item, RunContentEvent):
            if item.reasoning_content:
                self._segment_reasoning += item.reasoning_content
                events.extend(self._reasoning(item.reasoning_content))
                if has_text:
                    # Reasoning and text in one event: the reasoning is a step of its own.
                    events.extend(self._close_step())
            if has_text:
                assert isinstance(item.content, str)
                chunk = item.content
                if self._text_interrupted and self._text:
                    chunk = "\n\n" + chunk.lstrip("\n")
                self._text_interrupted = False
                self._text.append(chunk)
                events.append(emit(TextDelta, text=chunk))
        elif isinstance(item, ToolCallStartedEvent):
            self._text_interrupted = True
            events.extend(self._on_tool_started(item))
        elif isinstance(item, ToolCallCompletedEvent):
            events.extend(self._on_tool_completed(item))
        elif isinstance(item, RunPausedEvent):
            self.paused = True
        elif isinstance(item, RunCompletedEvent):
            events.extend(self._on_completed(item))
        elif isinstance(item, RunErrorEvent):
            self.failure = _failure_from_error(item)
        elif isinstance(item, RunCancelledEvent):
            self.failure = ("cancelled", "the run was cancelled")
        return events

    def _reasoning(self, piece: str) -> list[Event]:
        if self._step_id is None:
            self._steps += 1
            self._step_id = f"r{self._steps}"
        self._step_text.append(piece)
        return [self._emit(ReasoningDelta, step_id=self._step_id, text=piece)]

    def _close_step(self) -> list[Event]:
        if self._step_id is None:
            return []
        step_id, text = self._step_id, "".join(self._step_text)
        self._step_id, self._step_text = None, []
        if not text.strip():
            return []
        return [self._emit(ReasoningStep, step_id=step_id, text=text)]

    @property
    def text(self) -> str:
        """Everything streamed as TextDelta, in order (the free-text answer)."""
        return "".join(self._text)

    def _on_tool_started(self, item: ToolCallStartedEvent) -> list[Event]:
        execution = item.tool
        if execution is None or execution.tool_call_id is None:
            return []
        tool = self._definition.tool(execution.tool_name or "")
        if tool is None or tool.kind == "act":
            return []
        try:
            parsed = tool.parse_input(execution.tool_args or {})
        except ValidationError:
            # The entrypoint reports it to the model; nothing runs, nothing to show.
            return []
        self._announced.add(execution.tool_call_id)
        return [
            self._emit(
                ToolCalled,
                call_id=execution.tool_call_id,
                tool=tool.name,
                kind=tool.kind,
                args=parsed.model_dump(mode="json"),
            )
        ]

    def _on_tool_completed(self, item: ToolCallCompletedEvent) -> list[Event]:
        execution = item.tool
        if execution is None or execution.tool_call_id not in self._announced:
            return []
        call_id = execution.tool_call_id
        tool = self._definition.tool(execution.tool_name or "")
        outcome = self._state.outcomes.get(call_id)
        if tool is None or outcome is None or outcome.status == "invalid_args":
            return []
        if outcome.status == "error":
            return [
                self._emit(
                    ToolResult, call_id=call_id, tool=tool.name, ok=False, error=outcome.error
                )
            ]
        events: list[Event] = [
            self._emit(ToolResult, call_id=call_id, tool=tool.name, ok=True, output=outcome.output)
        ]
        if tool.artifact_kind is not None and (
            tool.artifact_if is None or tool.artifact_if(outcome.output or {})
        ):
            events.append(self._emit(Artifact, kind=tool.artifact_kind, data=outcome.output))
        return events

    def _on_completed(self, item: RunCompletedEvent) -> list[Event]:
        events: list[Event] = self._close_step()
        # With structured output Agno does not put the final turn's reasoning on
        # a content event; it only accumulates it on the run. Emit what is left.
        full = item.reasoning_content or ""
        if full.startswith(self._segment_reasoning) and len(full) > len(self._segment_reasoning):
            events.extend(self._reasoning(full[len(self._segment_reasoning) :]))
            events.extend(self._close_step())
            self._segment_reasoning = full
        self.completed = True
        self.final_content = item.content
        return events


def _as_text(content: object) -> str | None:
    if isinstance(content, str):
        return content
    if isinstance(content, BaseModel):
        return content.model_dump_json()
    return None


def _failure_from_error(item: RunErrorEvent) -> tuple[str, str]:
    error_type = item.error_type or "error"
    logger.warning("agno run %s failed: %s", item.run_id, error_type)
    if error_type.startswith("model_"):
        detail = redact_secrets(item.content or "")[:300]
        return "llm_error", f"{error_type}: {detail}" if detail else error_type
    return "internal_error", "the run failed unexpectedly"


async def stream_run(
    definition: AgentDefinition[Any],
    input: BaseModel,
    context: ToolContext,
    *,
    session: str | None,
    approvals: ApprovalSink,
    config: RunnerConfig,
    scope: tracing.RunScope | None = None,
) -> AsyncIterator[Event]:
    """Run `definition` on Agno, yielding our events; always ends with one terminal event.

    `scope` receives the run's model usage and parents its trace spans (the
    runner's; a fresh one when omitted).
    """
    emit = _Emitter(context)
    state = _RunState(scope=scope) if scope is not None else _RunState()
    try:
        async for event in _drive(
            definition, input, context, session, approvals, config, emit, state
        ):
            yield event
    except Exception:
        logger.exception("agent %s run %s: internal error", definition.name, context.run_id)
        yield emit(RunFailed, code="internal_error", message="the run failed unexpectedly")


async def _drive(
    definition: AgentDefinition[Any],
    input: BaseModel,
    context: ToolContext,
    session: str | None,
    approvals: ApprovalSink,
    config: RunnerConfig,
    emit: _Emitter,
    state: _RunState,
) -> AsyncIterator[Event]:
    ids = agno_ids(context, session)
    agent = build_agent(definition, context, ids, config, state)
    translator = _Translator(definition, state, emit)

    # Agno annotates `input` with bare List/Dict, which strict pyright reports
    # as partially unknown. Our argument is a str and the result is typed here.
    stream: AsyncIterator[RunOutputEvent | RunOutput] = agent.arun(  # pyright: ignore[reportUnknownMemberType]
        input.model_dump_json(),
        stream=True,
        stream_events=True,
        yield_run_output=True,
        user_id=ids.user_id,
        session_id=ids.session_id,
        run_id=ids.run_id,
    )
    while True:
        async for event in translator.consume(stream):
            yield event

        if translator.failure is not None:
            code, message = translator.failure
            yield emit(RunFailed, code=code, message=message)
            return

        if translator.paused and translator.output is not None:
            output = translator.output
            try:
                proposal = _first_valid_proposal(definition, context, session, output)
            except _UnexpectedPause as exc:
                logger.error("run %s paused unexpectedly: %s", context.run_id, exc)
                yield emit(RunFailed, code="internal_error", message="the run paused unexpectedly")
                return
            if proposal is not None:
                yield await _request_approval(approvals, proposal, emit)
                return
            # Every proposed act call was invalid: the errors are now the calls'
            # results. Let the model see them and continue.
            stream = agent.acontinue_run(
                run_response=output,
                requirements=output.requirements,
                stream=True,
                stream_events=True,
                yield_run_output=True,
                user_id=ids.user_id,
                session_id=ids.session_id,
            )
            continue

        if translator.completed:
            if definition.stream_text:
                yield _finish_text(definition, translator, emit)
            else:
                yield _finish(definition, translator.final_content, emit)
            return

        yield emit(RunFailed, code="internal_error", message="the run ended without a result")
        return


class _UnexpectedPause(Exception):
    """Agno paused on something other than one of our act tools."""


def _first_valid_proposal(
    definition: AgentDefinition[Any],
    context: ToolContext,
    session: str | None,
    output: RunOutput,
) -> ApprovalProposal | None:
    """The first valid act proposal of a paused run.

    None when every proposal was invalid; each one's validation error is then
    set as its result, ready for the run to continue.
    """
    invalid: list[tuple[RunRequirement, str]] = []
    for requirement in output.active_requirements:
        execution = requirement.tool_execution
        name = execution.tool_name if execution is not None else None
        tool = definition.tool(name or "")
        if execution is None or tool is None or tool.kind != "act":
            # We only ever pause on our own act tools. Anything else: fail closed.
            raise _UnexpectedPause(f"tool {name!r}")
        try:
            parsed = tool.parse_input(execution.tool_args or {})
        except ValidationError as exc:
            invalid.append((requirement, f"error: invalid arguments: {validation_errors(exc)}"))
            continue
        args: dict[str, JsonValue] = parsed.model_dump(mode="json")
        return ApprovalProposal(
            tenant_id=context.tenant_id,
            run_id=context.run_id,
            session_id=session or str(context.run_id),
            tool_call_id=execution.tool_call_id or "",
            tool=tool.name,
            args=args,
            args_hash=args_hash(args),
            requested_by=context.user_id,
        )
    if not invalid:
        raise _UnexpectedPause("no pending act call")
    for requirement, message in invalid:
        requirement.set_external_execution_result(message)
        if requirement.tool_execution is not None:
            requirement.tool_execution.tool_call_error = True
    return None


async def _request_approval(
    approvals: ApprovalSink, proposal: ApprovalProposal, emit: _Emitter
) -> Event:
    try:
        await approvals.request_approval(proposal)
    except Exception:
        logger.exception("approval sink failed for run %s", proposal.run_id)
        return emit(
            RunFailed, code="approval_not_recorded", message="the approval request was not recorded"
        )
    return emit(
        ApprovalRequested,
        call_id=proposal.tool_call_id,
        tool=proposal.tool,
        args=proposal.args,
        args_hash=proposal.args_hash,
    )


def _finish(definition: AgentDefinition[Any], content: object, emit: _Emitter) -> Event:
    try:
        output = _parse_output(definition.output_model, content)
    except ValidationError as exc:
        return emit(RunFailed, code="invalid_output", message=validation_errors(exc))
    except ValueError as exc:
        return emit(RunFailed, code="invalid_output", message=str(exc))
    return emit(RunCompleted, output=output.model_dump(mode="json"))


def _finish_text(
    definition: AgentDefinition[Any], translator: _Translator, emit: _Emitter
) -> Event:
    # The streamed deltas are what the user saw; the final content is only a
    # fallback for a provider that sent the text in one piece.
    text = translator.text
    if not text.strip() and isinstance(translator.final_content, str):
        text = translator.final_content
    if not text.strip():
        return emit(RunFailed, code="invalid_output", message="the model returned no answer")
    try:
        output = definition.output_model.model_validate({definition.text_field(): text})
    except ValidationError as exc:
        return emit(RunFailed, code="invalid_output", message=validation_errors(exc))
    return emit(RunCompleted, output=output.model_dump(mode="json"))


def _parse_output(model: type[BaseModel], content: object) -> BaseModel:
    # Agno hands back a parsed model when the output validated, else the raw
    # text. Either way, validate with our model from plain JSON data.
    if isinstance(content, BaseModel):
        return model.model_validate(content.model_dump(mode="json"))
    if isinstance(content, str) and content.strip():
        return model.model_validate_json(content)
    if isinstance(content, Mapping):
        return model.model_validate(content)
    raise ValueError("the model returned no final output")


def validation_errors(exc: ValidationError) -> str:
    # Field locations and messages only; never echo input values back.
    return "; ".join(
        f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
        for err in exc.errors(include_input=False, include_url=False)
    )
