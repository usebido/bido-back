"""The copilot agent definition: a prompt, a model, this request's tools, a reply.

A turn that runs the purchase analysis is answered by `analysis_reply_writer`
on the stronger model instead: the Copilot stops right after the analysis tool
succeeds (its handoff) and the writer, given what the Copilot read of the tool
results, streams the reply. One writer per turn; the Copilot never rewrites it.
"""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from datetime import date
from typing import Any, Final

from pydantic import BaseModel, Field, JsonValue

from bido_back.agents.catalog.definitions import DEFAULT_MODEL
from bido_back.agents.definition import AgentDefinition, Effort, Handoff, HandoffCall
from bido_back.agents.tools import AnyTool
from bido_back.copilot.contracts import CopilotReply
from bido_back.copilot.model_views import cell_free_view

COPILOT_PROMPT_FILE: Final = "copilot.v34.md"
COPILOT_PROMPT_VERSION: Final = 34
# Earlier turns of the same conversation the model sees (needs Agno storage).
HISTORY_RUNS: Final = 10
# Model requests per message: a few lookups, maybe a draft action, the answer.
MAX_TURNS: Final = 12


# The tools whose result the analysis writer presents (when the Copilot has them).
ANALYSIS_TOOLS: Final = frozenset({"analyze_procurement_sources", "run_purchase_analysis"})
ANALYSIS_REPLY_PROMPT_FILE: Final = "analysis_reply_writer.v3.md"
ANALYSIS_REPLY_PROMPT_VERSION: Final = 3
ANALYSIS_REPLY_MODEL: Final = "gpt-6.1-sol"
# Live, 2026-10-09, Pedro's 8 SAP files and workbook 01 (k=2): low and medium gave the
# same answers on the review's points; low was ~10 s faster at the same cost.
ANALYSIS_REPLY_EFFORT: Final[Effort] = "low"


class AnalysisToolCall(BaseModel):
    tool: str
    args: dict[str, JsonValue]
    output: JsonValue = Field(description="What the Copilot read of the tool's output")


class AnalysisReplyInput(BaseModel):
    message: str = Field(description="What the buyer wrote this turn")
    buyer_messages: list[str] = Field(
        description="The buyer's own messages in this conversation, oldest first"
    )
    today: date
    attachments: list[str] = Field(description="File names attached this turn")
    tool_calls: list[AnalysisToolCall] = Field(
        description="Every tool the Copilot called this turn, in order, with its output"
    )


def analysis_reply_writer(
    model: str = ANALYSIS_REPLY_MODEL, effort: Effort = ANALYSIS_REPLY_EFFORT
) -> AgentDefinition[CopilotReply]:
    """Writes an analysis turn's reply. Its prompt is its own rules followed by the
    Copilot's, so the presentation rules have one home."""
    return AgentDefinition(
        name="analysis_reply_writer",
        prompt_file=ANALYSIS_REPLY_PROMPT_FILE,
        prompt_version=ANALYSIS_REPLY_PROMPT_VERSION,
        prompt_includes=(COPILOT_PROMPT_FILE,),
        model=model,
        output_model=CopilotReply,
        max_turns=1,
        effort=effort,
        stream_text=True,
    )


type WriterSetup = Callable[
    [Sequence[HandoffCall]], Awaitable[tuple[AgentDefinition[Any], BaseModel]]
]


def analysis_handoff(
    message: str,
    today: date,
    attachments: Sequence[str],
    buyer_messages: Callable[[], Awaitable[list[str]]],
    writer: AgentDefinition[CopilotReply] | None = None,
) -> Handoff:
    """The Copilot's handoff to the analysis writer, for one buyer message."""

    async def setup(calls: Sequence[HandoffCall]) -> tuple[AgentDefinition[Any], BaseModel]:
        return writer or analysis_reply_writer(), AnalysisReplyInput(
            message=message,
            buyer_messages=await buyer_messages(),
            today=today,
            attachments=list(attachments),
            tool_calls=[AnalysisToolCall(tool=c.tool, args=c.args, output=c.output) for c in calls],
        )

    return Handoff(tools=ANALYSIS_TOOLS, writer=setup)


def copilot_agent(
    tools: Sequence[AnyTool], model: str = DEFAULT_MODEL, handoff: Handoff | None = None
) -> AgentDefinition[CopilotReply]:
    if handoff is not None:
        # Only the analysis tools this request has (tests may give fewer).
        present = handoff.tools & {tool.name for tool in tools}
        handoff = Handoff(tools=present, writer=handoff.writer) if present else None
    return AgentDefinition(
        name="copilot",
        prompt_file=COPILOT_PROMPT_FILE,
        prompt_version=COPILOT_PROMPT_VERSION,
        model=model,
        output_model=CopilotReply,
        # Cell references are for code and the buyer's screen: the model reads every
        # tool's output without them, unless the tool bounds its output further.
        tools=tuple(
            tool if tool.model_view is not None else replace(tool, model_view=cell_free_view)
            for tool in tools
        ),
        max_turns=MAX_TURNS,
        effort="low",
        history_runs=HISTORY_RUNS,
        # The reply streams to the buyer token by token (text_delta events).
        stream_text=True,
        handoff=handoff,
    )
