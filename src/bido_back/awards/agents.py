"""The "not selected" notice writer, behind a seam tests replace.

`default_notice_writer()` runs `AWARD_NOTICE_WRITER` through
`runner.run_to_completion`, importing the definitions (and so Agno) on first
use. The agent has no tools: code renders the notice (`bido_back.supplier_email`)
and the agent may only add an optional note to it. The email becomes a proposal a
person approves, recorded by code, never by the model.
"""

import logging
from collections.abc import Awaitable, Callable

from bido_back.agents.catalog.contracts import AwardNoticeInput, EmailNoteOutput
from bido_back.agents.config import RunnerConfig
from bido_back.agents.context import ToolContext
from bido_back.rfqs.errors import AgentFailed

logger = logging.getLogger(__name__)

NoticeWriter = Callable[[AwardNoticeInput, ToolContext], Awaitable[EmailNoteOutput]]


def default_notice_writer(config: RunnerConfig | None = None) -> NoticeWriter:
    async def write(task: AwardNoticeInput, context: ToolContext) -> EmailNoteOutput:
        from bido_back.agents.catalog.definitions import AWARD_NOTICE_WRITER
        from bido_back.agents.runner import Completed, Failed, run_to_completion

        result = await run_to_completion(AWARD_NOTICE_WRITER, task, context, config=config)
        if isinstance(result, Completed):
            return result.output
        code = result.failure.code if isinstance(result, Failed) else "unexpected_approval"
        logger.error("agent award_notice_writer run %s failed: %s", context.run_id, code)
        raise AgentFailed(f"agent award_notice_writer failed ({code})")

    return write
