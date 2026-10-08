"""The events an agent run emits: OUR schema, rendered by the frontend.

These are the only thing a run exposes to the outside (API streams, logs,
persisted run history). They never contain provider SDK types, so swapping the
runner or the model provider does not change what the frontend sees.

Every event carries the `run_id` and a `seq` that starts at 0 and increases by
one per event within a run, so a client can order, dedupe, and resume.
"""

import hashlib
import json
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter

from bido_back.agents.tools import ToolKind


class _EventBase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    seq: int = Field(ge=0)


class TextDelta(_EventBase):
    """Model-written text meant for the user."""

    type: Literal["text_delta"] = "text_delta"
    text: str


class ReasoningDelta(_EventBase):
    """The next piece of a reasoning step while the model writes it.

    Deltas with the same `step_id` belong to one step; a `ReasoningStep` with
    that `step_id` and the whole text always follows. Clients that only render
    finished steps can ignore deltas.
    """

    type: Literal["reasoning_delta"] = "reasoning_delta"
    step_id: str
    text: str


class ReasoningStep(_EventBase):
    """A summarized reasoning step, shown as the agent's 'thinking'.

    Always one whole step (all of its deltas joined), never a fragment.
    """

    type: Literal["reasoning_step"] = "reasoning_step"
    text: str
    step_id: str | None = None


class ToolCalled(_EventBase):
    """A read or draft tool is about to run (rendered as a tool pill)."""

    type: Literal["tool_called"] = "tool_called"
    call_id: str
    tool: str
    kind: ToolKind
    args: dict[str, JsonValue]


class ToolResult(_EventBase):
    """The outcome of a ToolCalled. Exactly one of `output` / `error` is set."""

    type: Literal["tool_result"] = "tool_result"
    call_id: str
    tool: str
    ok: bool
    output: dict[str, JsonValue] | None = None
    error: str | None = None


class Artifact(_EventBase):
    """A structured object the frontend renders as a card (a draft, a table...)."""

    type: Literal["artifact"] = "artifact"
    kind: str
    data: dict[str, JsonValue]


class ApprovalRequested(_EventBase):
    """The model proposed an `act` tool call. It was NOT executed.

    `args_hash` is the sha256 of the canonical JSON of `args`. Whoever executes
    the approved action must recompute it and refuse on mismatch, so what a
    person approved is exactly what runs.
    """

    type: Literal["approval_requested"] = "approval_requested"
    call_id: str
    tool: str
    args: dict[str, JsonValue]
    args_hash: str


class RunCompleted(_EventBase):
    """Terminal: the run produced an output that validated against the agent's model."""

    type: Literal["run_completed"] = "run_completed"
    output: dict[str, JsonValue]


class RunFailed(_EventBase):
    """Terminal: the run stopped without a valid output or proposal."""

    type: Literal["run_failed"] = "run_failed"
    code: str
    message: str


Event = Annotated[
    TextDelta
    | ReasoningDelta
    | ReasoningStep
    | ToolCalled
    | ToolResult
    | Artifact
    | ApprovalRequested
    | RunCompleted
    | RunFailed,
    Field(discriminator="type"),
]

EventAdapter: TypeAdapter[Event] = TypeAdapter(Event)

TerminalEvent = ApprovalRequested | RunCompleted | RunFailed


def canonical_json(value: JsonValue) -> str:
    """Byte-stable JSON: sorted keys, no whitespace, UTF-8 kept as-is."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def args_hash(args: dict[str, JsonValue]) -> str:
    return hashlib.sha256(canonical_json(args).encode("utf-8")).hexdigest()
