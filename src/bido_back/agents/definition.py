"""What an agent is: a prompt file, a model, a bounded tool set, and an output type.

Agents are code-owned. The definition is data; `runner.run()` executes it.
"""

import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, JsonValue

from bido_back.agents.tools import AnyTool

# Repo-root `prompts/`. Resolved from the source tree, which is how the app is
# deployed (the image copies the repo). Tests and tools may pass another dir.
PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts"

Effort = Literal["low", "medium", "high", "xhigh", "max"]

_PROMPT_FILE = re.compile(r"^(?P<name>[a-z0-9_]+)\.v(?P<version>[1-9][0-9]*)\.md$")


@dataclass(frozen=True, slots=True)
class HandoffCall:
    """A tool call of the run that handed its reply off: what was asked, and what the
    agent's model read of the output (the tool's model view)."""

    tool: str
    args: dict[str, JsonValue]
    output: JsonValue


@dataclass(frozen=True, slots=True)
class Handoff:
    """Tools whose successful call ends the agent's turn; another agent writes the reply.

    Once one of `tools` succeeds, the agent makes no further model request in
    that run: `writer` gets every successful call of the run (in order) and
    returns the writing agent and its input. That agent's streamed text is the
    run's answer, so a turn has exactly one writer. Only for `stream_text`
    agents; the writer must be one too.
    """

    tools: frozenset[str]
    writer: Callable[[Sequence[HandoffCall]], Awaitable["tuple[AgentDefinition[Any], BaseModel]"]]


@dataclass(frozen=True, slots=True)
class AgentDefinition[OutT: BaseModel]:
    name: str
    # File name under prompts/, `<name>.v<N>.md`. N must equal prompt_version.
    prompt_file: str
    prompt_version: int
    model: str
    # The run's final answer. The model is asked for it as structured output,
    # and the runner validates it again before reporting RunCompleted. With
    # `stream_text`, the model writes free text instead (streamed as TextDelta
    # events) and the runner puts the whole text in the output model's single
    # str field.
    output_model: type[OutT]
    tools: Sequence[AnyTool] = field(default_factory=tuple[AnyTool, ...])
    # Every model request counts as one turn, including the final-output turn.
    max_turns: int = 6
    max_tokens: int = 16_000
    # Ask the model for a summary of its reasoning (rendered as ReasoningStep).
    thinking: bool = True
    # The model's reasoning effort. None = the model's own default.
    effort: Effort | None = None
    # How many earlier runs of the same session the model sees. 0 = none.
    # Needs run storage (agno_database_url); ignored without it.
    history_runs: int = 0
    # Free-text final answer, streamed token by token, instead of structured
    # output. Only for conversational agents whose output model is one str
    # field (the copilot); agents whose output feeds code keep structured output.
    stream_text: bool = False
    # Other prompt files appended to this one's, in order (shared rules kept in
    # one versioned file). Each must look like `<name>.v<N>.md`.
    prompt_includes: tuple[str, ...] = ()
    # See Handoff. None: the agent always writes its own answer.
    handoff: Handoff | None = None

    def __post_init__(self) -> None:
        match = _PROMPT_FILE.match(self.prompt_file)
        if match is None:
            raise ValueError(f"prompt_file must look like 'name.v1.md': {self.prompt_file!r}")
        if int(match["version"]) != self.prompt_version:
            raise ValueError(
                f"prompt_file {self.prompt_file!r} does not match "
                f"prompt_version {self.prompt_version}"
            )
        if self.max_turns < 1:
            raise ValueError("max_turns must be >= 1")
        if self.history_runs < 0:
            raise ValueError("history_runs must be >= 0")
        if self.stream_text:
            self.text_field()  # validates the output model's shape
        names = [tool.name for tool in self.tools]
        if len(names) != len(set(names)):
            raise ValueError(f"duplicate tool names in agent {self.name!r}: {names}")
        for include in self.prompt_includes:
            if _PROMPT_FILE.match(include) is None:
                raise ValueError(f"prompt include must look like 'name.v1.md': {include!r}")
        if self.handoff is not None:
            if not self.stream_text:
                raise ValueError(f"agent {self.name!r}: a handoff needs stream_text")
            unknown = sorted(self.handoff.tools - set(names))
            if unknown:
                raise ValueError(f"agent {self.name!r}: handoff tools not in its tools: {unknown}")

    def text_field(self) -> str:
        """The output model's single str field, which a streamed text answer fills."""
        fields = self.output_model.model_fields
        if len(fields) != 1:
            raise ValueError(
                f"agent {self.name!r}: stream_text needs an output model with exactly one field"
            )
        name, info = next(iter(fields.items()))
        if info.annotation is not str:
            raise ValueError(f"agent {self.name!r}: stream_text needs a str output field")
        return name

    def tool(self, name: str) -> AnyTool | None:
        for tool in self.tools:
            if tool.name == name:
                return tool
        return None

    def load_prompt(self, prompts_dir: Path | None = None) -> str:
        base = prompts_dir or PROMPTS_DIR
        parts = [_read_prompt(base / name) for name in (self.prompt_file, *self.prompt_includes)]
        return "\n\n".join(parts)


@lru_cache(maxsize=128)
def _read_prompt(path: Path) -> str:
    # Prompt files are immutable once versioned, so caching is safe.
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"prompt file is empty: {path}")
    return text
