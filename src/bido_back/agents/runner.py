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

from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from pydantic import BaseModel

from bido_back.agents import agno_adapter
from bido_back.agents.approvals import ApprovalSink, NoopApprovalSink
from bido_back.agents.config import RunnerConfig
from bido_back.agents.context import ToolContext
from bido_back.agents.definition import AgentDefinition
from bido_back.agents.events import ApprovalRequested, Event, RunCompleted, RunFailed
from bido_back.settings import get_settings

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
    async for event in agno_adapter.stream_run(
        agent,
        input,
        context,
        session=session,
        approvals=approvals or NoopApprovalSink(),
        config=config or RunnerConfig.from_settings(get_settings()),
    ):
        yield event


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
