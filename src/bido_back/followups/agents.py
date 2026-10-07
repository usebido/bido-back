"""The follow-up writer, behind a seam tests replace.

`default_followup_writer()` runs `RFQ_FOLLOWUP_WRITER` through
`runner.run_to_completion`, importing the definitions (and so Agno) on first
use. The agent has no tools: what it writes becomes a proposal that a person
approves, recorded by code, never by the model.
"""

import logging
from collections.abc import Awaitable, Callable

from bido_back.agents.catalog.contracts import FollowupEmailInput, FollowupEmailOutput
from bido_back.agents.config import RunnerConfig
from bido_back.agents.context import ToolContext
from bido_back.rfqs.errors import AgentFailed

logger = logging.getLogger(__name__)

FollowupWriter = Callable[[FollowupEmailInput, ToolContext], Awaitable[FollowupEmailOutput]]


def default_followup_writer(config: RunnerConfig | None = None) -> FollowupWriter:
    async def write(task: FollowupEmailInput, context: ToolContext) -> FollowupEmailOutput:
        from bido_back.agents.catalog.definitions import RFQ_FOLLOWUP_WRITER
        from bido_back.agents.runner import Completed, Failed, run_to_completion

        result = await run_to_completion(RFQ_FOLLOWUP_WRITER, task, context, config=config)
        if isinstance(result, Completed):
            return result.output
        code = result.failure.code if isinstance(result, Failed) else "unexpected_approval"
        logger.error("agent rfq_followup_writer run %s failed: %s", context.run_id, code)
        raise AgentFailed(f"agent rfq_followup_writer failed ({code})")

    return write
