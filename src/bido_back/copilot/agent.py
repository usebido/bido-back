"""The copilot agent definition: a prompt, a model, this request's tools, a reply."""

from collections.abc import Sequence
from typing import Final

from bido_back.agents.catalog.definitions import DEFAULT_MODEL
from bido_back.agents.definition import AgentDefinition
from bido_back.agents.tools import AnyTool
from bido_back.copilot.contracts import CopilotReply

COPILOT_PROMPT_FILE: Final = "copilot.v26.md"
COPILOT_PROMPT_VERSION: Final = 26
# Earlier turns of the same conversation the model sees (needs Agno storage).
HISTORY_RUNS: Final = 10
# Model requests per message: a few lookups, maybe a draft action, the answer.
MAX_TURNS: Final = 12


def copilot_agent(
    tools: Sequence[AnyTool], model: str = DEFAULT_MODEL
) -> AgentDefinition[CopilotReply]:
    return AgentDefinition(
        name="copilot",
        prompt_file=COPILOT_PROMPT_FILE,
        prompt_version=COPILOT_PROMPT_VERSION,
        model=model,
        output_model=CopilotReply,
        tools=tuple(tools),
        max_turns=MAX_TURNS,
        effort="low",
        history_runs=HISTORY_RUNS,
        # The reply streams to the buyer token by token (text_delta events).
        stream_text=True,
    )
