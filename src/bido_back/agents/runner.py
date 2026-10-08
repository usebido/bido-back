"""The one entry point for running an agent: `run(agent, input, context)`.

It yields OUR events (`events.py`) and ends with exactly one terminal event:

- `RunCompleted`      - the final output validated against the agent's output model;
- `ApprovalRequested` - the model proposed an `act` tool call. It was NOT executed;
  the proposal went to the `ApprovalSink` first;
- `RunFailed`         - anything else (turn limit, invalid output, provider error...).

The agent loop itself (model calls, tool execution, sessions, run storage)
is Agno's, behind `agno_adapter.py`. Nothing here, and nothing a caller
sees, is an Agno or OpenAI type. Evals call `run()` too.
"""

import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from bido_back.agents import agno_adapter
from bido_back.agents.approvals import ApprovalSink, NoopApprovalSink
from bido_back.agents.config import RunnerConfig
from bido_back.agents.context import ToolContext
from bido_back.agents.definition import AgentDefinition
from bido_back.agents.events import (
    ApprovalRequested,
    Event,
    RunCompleted,
    RunFailed,
    ToolCalled,
    ToolResult,
)
from bido_back.logs import safe_reason
from bido_back.settings import get_settings

logger = logging.getLogger(__name__)

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


# --- Entry point -----------------------------------------------------------


async def run[OutT: BaseModel](
    agent: AgentDefinition[OutT],
    input: BaseModel,
    context: ToolContext,
    *,
    session: str | None = None,
    approvals: ApprovalSink | None = None,
    config: RunnerConfig | None = None,
) -> AsyncIterator[Event]:
    """Run `agent` on `input` as `context`; yield events, ending with one terminal event.

    `session` groups runs into one conversation (namespaced by tenant before it
    reaches storage); None makes the run its own session. `config` defaults to
    the process settings.
    """
    summary = _RunLog(agent, context)
    try:
        async for event in agno_adapter.stream_run(
            agent,
            input,
            context,
            session=session,
            approvals=approvals or NoopApprovalSink(),
            config=config or RunnerConfig.from_settings(get_settings()),
        ):
            summary.see(event)
            yield event
    finally:
        summary.write()


class _RunLog:
    """One log line per run: agent, prompt version, model, outcome, duration, tool counts.

    A run the caller stopped consuming (the buyer closed the stream) is logged
    as `aborted`. Failure messages are written by the runner (error codes,
    validation field paths), and are masked anyway.
    """

    def __init__(self, agent: AgentDefinition[Any], context: ToolContext) -> None:
        self._agent = agent
        self._context = context
        self._started = time.perf_counter()
        self._tools = 0
        self._tool_errors = 0
        self._terminal: Event | None = None

    def see(self, event: Event) -> None:
        if isinstance(event, ToolCalled):
            self._tools += 1
        elif isinstance(event, ToolResult) and not event.ok:
            self._tool_errors += 1
        elif isinstance(event, RunCompleted | ApprovalRequested | RunFailed):
            self._terminal = event

    def write(self) -> None:
        terminal = self._terminal
        fields: dict[str, object] = {
            "agent": self._agent.name,
            "prompt_version": self._agent.prompt_version,
            "model": self._agent.model,
            "run_id": str(self._context.run_id),
            "duration_ms": round((time.perf_counter() - self._started) * 1000, 1),
            "tool_calls": self._tools,
            "tool_errors": self._tool_errors,
        }
        level = logging.INFO
        if isinstance(terminal, RunCompleted):
            fields["outcome"] = "completed"
        elif isinstance(terminal, ApprovalRequested):
            fields["outcome"] = "approval_requested"
            fields["tool"] = terminal.tool
        elif isinstance(terminal, RunFailed):
            level = logging.WARNING
            fields["outcome"] = "failed"
            fields["code"] = terminal.code
            # Provider errors carry the provider's text after the error type.
            message = (
                terminal.message.split(":", 1)[0]
                if terminal.code == "llm_error"
                else terminal.message
            )
            fields["reason"] = safe_reason(message)
        else:
            fields["outcome"] = "aborted"
        logger.log(level, "agent run", extra=fields)


async def run_to_completion[OutT: BaseModel](
    agent: AgentDefinition[OutT],
    input: BaseModel,
    context: ToolContext,
    *,
    session: str | None = None,
    approvals: ApprovalSink | None = None,
    config: RunnerConfig | None = None,
) -> RunResult[OutT]:
    events: list[Event] = []
    async for event in run(
        agent, input, context, session=session, approvals=approvals, config=config
    ):
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
