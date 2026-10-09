"""The negotiation writer, behind a seam tests replace.

`default_negotiation_writer()` runs `RFQ_NEGOTIATION_WRITER` through
`runner.run_to_completion`, importing the definitions (and so Agno) on first
use. The agent has no tools and decides no numbers: the strategy
(`bido_back.negotiation.strategy`) does, code renders the email
(`bido_back.supplier_email`), and the agent may only add an optional note to it.
What goes out is a proposal that a person approves.
"""

import logging
from collections.abc import Awaitable, Callable

from bido_back.agents.catalog.contracts import EmailNoteOutput, NegotiationEmailInput
from bido_back.agents.config import RunnerConfig
from bido_back.agents.context import ToolContext
from bido_back.rfqs.errors import AgentFailed

logger = logging.getLogger(__name__)

NegotiationWriter = Callable[[NegotiationEmailInput, ToolContext], Awaitable[EmailNoteOutput]]


def default_negotiation_writer(config: RunnerConfig | None = None) -> NegotiationWriter:
    async def write(task: NegotiationEmailInput, context: ToolContext) -> EmailNoteOutput:
        from bido_back.agents.catalog.definitions import RFQ_NEGOTIATION_WRITER
        from bido_back.agents.runner import Completed, Failed, run_to_completion

        result = await run_to_completion(RFQ_NEGOTIATION_WRITER, task, context, config=config)
        if isinstance(result, Completed):
            return result.output
        code = result.failure.code if isinstance(result, Failed) else "unexpected_approval"
        logger.error("agent rfq_negotiation_writer run %s failed: %s", context.run_id, code)
        raise AgentFailed(f"agent rfq_negotiation_writer failed ({code})")

    return write
