"""One copilot message: load its attachments, run the agent, stream what happens.

A conversation is an Agno session keyed by tenant (by `agno_ids`), then by
user and conversation id here, so a conversation is only ever visible to the
person who started it, in the tenant they started it in. The model sees the
last `HISTORY_RUNS` turns of it when run storage (AGNO_DATABASE_URL) is set.

Events, in order (the SSE contract the API documents):

1. `conversation` - the conversation id (new or given) and this run's id.
2. The runner's own events, unchanged (`bido_back.agents.events`): reasoning
   steps, `tool_called` / `tool_result` per tool, `artifact` cards (a draft RFQ,
   prepared approvals, a comparison, the template), and `approval_requested`
   if an `act` tool was ever proposed (the copilot has none today).
3. `answer` - the reply text, when the run completed.
4. `done` - always last: whether it worked and the draft actions it ran
   (RFQs created, suppliers invited, sends prepared, template changes).

A failed run sends the runner's `run_failed`, then `done` with ok=false.
"""

import logging
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final
from uuid import UUID, uuid4

from pydantic import JsonValue, ValidationError
from sqlalchemy import select

from bido_back.agents.config import RunnerConfig
from bido_back.agents.context import ToolContext
from bido_back.agents.events import Event, RunCompleted, RunFailed, ToolCalled, ToolResult
from bido_back.approvals import PostgresApprovalSink
from bido_back.auth import Principal
from bido_back.copilot.agent import copilot_agent
from bido_back.copilot.contracts import (
    AnswerEvent,
    ConversationEvent,
    ConversationTurn,
    ConversationView,
    CopilotAction,
    CopilotInput,
    CopilotMessageRequest,
    CopilotReply,
    DoneEvent,
    StreamItem,
)
from bido_back.copilot.tools import CopilotToolDeps, Db, copilot_tools
from bido_back.db import tenant_session
from bido_back.db.models import TemplateUpload
from bido_back.documents import DocumentText, extract_text, render_documents
from bido_back.rfqs.agents import RfqAgents
from bido_back.rfqs.mailbox import MailboxSenderFactory
from bido_back.storage import BlobStorage

logger = logging.getLogger(__name__)

MAX_ATTACHMENT_CHARS: Final = 40_000


class AttachmentNotFound(Exception):
    """An attachment id that is not one of the caller's uploads."""


@dataclass(frozen=True, slots=True)
class CopilotDeps:
    db: Db
    storage: BlobStorage
    agents: RfqAgents
    mailboxes: MailboxSenderFactory
    runner_config: RunnerConfig


@dataclass(frozen=True, slots=True)
class PreparedMessage:
    principal: Principal
    conversation_id: UUID
    input: CopilotInput


def session_key(principal: Principal, conversation_id: UUID) -> str:
    """The Agno session for a conversation; `agno_ids` prefixes it with the tenant."""
    return f"copilot:{principal.user_id}:{conversation_id}"


async def prepare_message(
    deps: CopilotDeps, principal: Principal, request: CopilotMessageRequest
) -> PreparedMessage:
    """Everything that can fail before streaming starts (so it fails as a plain HTTP error)."""
    documents = await _attachments(deps, principal, request.attachment_ids)
    text = render_documents(documents) if documents else ""
    if len(text) > MAX_ATTACHMENT_CHARS:
        text = text[:MAX_ATTACHMENT_CHARS] + "\n[attachments truncated]"
    return PreparedMessage(
        principal=principal,
        conversation_id=request.conversation_id or uuid4(),
        input=CopilotInput(
            message=request.message,
            today=datetime.now(UTC).date(),
            attachments=[d.filename for d in documents],
            attachments_text=text,
        ),
    )


async def _attachments(
    deps: CopilotDeps, principal: Principal, ids: Sequence[UUID]
) -> list[DocumentText]:
    wanted = list(dict.fromkeys(ids))
    if not wanted:
        return []
    async with tenant_session(deps.db, principal.tenant_id) as session:
        rows = list(
            await session.scalars(
                select(TemplateUpload).where(
                    TemplateUpload.id.in_(wanted), TemplateUpload.user_id == principal.user_id
                )
            )
        )
    found = {row.id: row for row in rows}
    missing = [str(i) for i in wanted if i not in found]
    if missing:
        raise AttachmentNotFound(f"attachments not found: {', '.join(missing)}")
    documents: list[DocumentText] = []
    for upload_id in wanted:
        row = found[upload_id]
        data = await deps.storage.get(row.storage_key)
        documents.append(extract_text(row.filename, row.content_type, data))
    return documents


async def stream_message(deps: CopilotDeps, prepared: PreparedMessage) -> AsyncIterator[StreamItem]:
    from bido_back.agents.runner import run

    principal = prepared.principal
    context = ToolContext(tenant_id=principal.tenant_id, user_id=principal.user_id, run_id=uuid4())
    tools = copilot_tools(
        CopilotToolDeps(
            db=deps.db,
            agents=deps.agents,
            mailboxes=deps.mailboxes,
            attachments_text=prepared.input.attachments_text,
        )
    )
    agent = copilot_agent(tools)
    yield ConversationEvent(conversation_id=prepared.conversation_id, run_id=context.run_id)

    kinds = {tool.name: tool.kind for tool in tools}
    called: dict[str, str] = {}
    actions: list[CopilotAction] = []
    ok = False
    events: AsyncIterator[Event] = run(
        agent,
        prepared.input,
        context,
        session=session_key(principal, prepared.conversation_id),
        approvals=PostgresApprovalSink(deps.db),
        config=deps.runner_config,
    )
    async for event in events:
        if isinstance(event, RunCompleted):
            try:
                reply = CopilotReply.model_validate(event.output)
            except ValidationError:
                yield RunFailed(
                    run_id=event.run_id, seq=event.seq, code="invalid_output", message="no answer"
                ).model_dump(mode="json")
                break
            ok = True
            yield AnswerEvent(run_id=context.run_id, text=reply.answer)
            continue
        if isinstance(event, ToolCalled):
            called[event.call_id] = event.tool
        elif isinstance(event, ToolResult) and kinds.get(event.tool) == "draft":
            actions.append(
                CopilotAction(
                    call_id=event.call_id,
                    tool=event.tool,
                    ok=event.ok,
                    output=event.output,
                    error=event.error,
                )
            )
        yield event.model_dump(mode="json")

    logger.info(
        "copilot message tenant=%s run=%s ok=%s tools=%d actions=%d",
        principal.tenant_id,
        context.run_id,
        ok,
        len(called),
        len(actions),
    )
    yield DoneEvent(
        run_id=context.run_id,
        conversation_id=prepared.conversation_id,
        ok=ok,
        actions=actions,
    )


async def conversation(
    deps: CopilotDeps, principal: Principal, conversation_id: UUID
) -> ConversationView | None:
    """The caller's own conversation, oldest turn first; None if there is none."""
    from bido_back.agents.agno_adapter import load_session_runs

    context = ToolContext(tenant_id=principal.tenant_id, user_id=principal.user_id, run_id=uuid4())
    runs = await load_session_runs(
        context, session_key(principal, conversation_id), deps.runner_config
    )
    if runs is None:
        return None
    turns: list[ConversationTurn] = []
    for stored in runs:
        message: str | None = None
        if stored.input:
            try:
                message = CopilotInput.model_validate_json(stored.input).message
            except ValidationError:
                message = None
        turns.append(
            ConversationTurn(
                run_id=stored.run_id,
                created_at=datetime.fromtimestamp(stored.created_at, UTC),
                message=message,
                answer=_answer(stored.output),
                status=stored.status,
            )
        )
    return ConversationView(conversation_id=conversation_id, turns=turns)


def _answer(output: dict[str, JsonValue] | str | None) -> str | None:
    try:
        if isinstance(output, str):
            return CopilotReply.model_validate_json(output).answer
        if output is not None:
            return CopilotReply.model_validate(output).answer
    except ValidationError:
        return None
    return None
