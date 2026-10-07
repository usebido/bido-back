"""What an agent is: a prompt file, a model, a bounded tool set, and an output type.

Agents are code-owned. The definition is data; `runner.run()` executes it.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from bido_back.agents.tools import AnyTool

# Repo-root `prompts/`. Resolved from the source tree, which is how the app is
# deployed (the image copies the repo). Tests and tools may pass another dir.
PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts"

Effort = Literal["low", "medium", "high", "xhigh", "max"]

_PROMPT_FILE = re.compile(r"^(?P<name>[a-z0-9_]+)\.v(?P<version>[1-9][0-9]*)\.md$")


@dataclass(frozen=True, slots=True)
class AgentDefinition[OutT: BaseModel]:
    name: str
    # File name under prompts/, `<name>.v<N>.md`. N must equal prompt_version.
    prompt_file: str
    prompt_version: int
    model: str
    # The run's final answer. The model is asked for it as structured output,
    # and the runner validates it again before reporting RunCompleted.
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
        names = [tool.name for tool in self.tools]
        if len(names) != len(set(names)):
            raise ValueError(f"duplicate tool names in agent {self.name!r}: {names}")

    def tool(self, name: str) -> AnyTool | None:
        for tool in self.tools:
            if tool.name == name:
                return tool
        return None

    def load_prompt(self, prompts_dir: Path | None = None) -> str:
        return _read_prompt((prompts_dir or PROMPTS_DIR) / self.prompt_file)


@lru_cache(maxsize=128)
def _read_prompt(path: Path) -> str:
    # Prompt files are immutable once versioned, so caching is safe.
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"prompt file is empty: {path}")
    return text
