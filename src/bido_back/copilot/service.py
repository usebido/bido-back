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
   The reply itself streams as `text_delta` events while the model writes it.
3. `answer` - the whole reply text, when the run completed (the deltas joined).
4. `done` - always last: whether it worked and the draft actions it ran
   (RFQs created, suppliers invited, sends prepared, template changes).

A failed run sends the runner's `run_failed`, then `done` with ok=false.
"""

import logging
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final
from uuid import UUID, uuid4

from pydantic import JsonValue, ValidationError
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from bido_back.agents.config import RunnerConfig
from bido_back.agents.context import ToolContext
from bido_back.agents.events import Event, RunCompleted, RunFailed, ToolCalled, ToolResult
from bido_back.approvals import PostgresApprovalSink
from bido_back.auth import Principal
from bido_back.copilot.agent import copilot_agent
from bido_back.copilot.contracts import (
    ATTACHMENTS_ONLY_MESSAGE,
    AnswerEvent,
    ConversationEvent,
    ConversationList,
    ConversationSummary,
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
from bido_back.db.models import CopilotConversation, TemplateUpload
from bido_back.documents import DocumentText, extract_text, render_documents
from bido_back.logs import annotate
from bido_back.purchasing.workbook import SheetMapper, default_sheet_mapper
from bido_back.rfqs.agents import RfqAgents
from bido_back.rfqs.mailbox import MailboxSenderFactory
from bido_back.storage import BlobStorage

logger = logging.getLogger(__name__)

MAX_ATTACHMENT_CHARS: Final = 40_000
# The buyer's own messages handed to the intake agent by create_rfq.
MAX_BUYER_MESSAGES: Final = 20
MAX_BUYER_MESSAGES_CHARS: Final = 16_000
TITLE_CHARS: Final = 80
LIST_LIMIT_MAX: Final = 100


class AttachmentNotFound(Exception):
    """An attachment id that is not one of the caller's uploads."""


@dataclass(frozen=True, slots=True)
class CopilotDeps:
    db: Db
    storage: BlobStorage
    agents: RfqAgents
    mailboxes: MailboxSenderFactory
    runner_config: RunnerConfig
    # Reads sheets the import rules cannot; None = the default agent.
    sheet_mapper: SheetMapper | None = None


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
    text = _attachments_text(documents)
    earlier: tuple[UUID, ...] = ()
    if request.conversation_id is not None:
        earlier = await earlier_attachment_ids(deps, principal, request.conversation_id)
    return PreparedMessage(
        principal=principal,
        conversation_id=request.conversation_id or uuid4(),
        input=CopilotInput(
            # The request requires text or files; files alone get a standing instruction.
            message=request.message if request.message.strip() else ATTACHMENTS_ONLY_MESSAGE,
            today=datetime.now(UTC).date(),
            attachments=[d.filename for d in documents],
            attachment_ids=list(dict.fromkeys(request.attachment_ids)),
            attachments_text=text,
            earlier_attachment_ids=[i for i in earlier if i not in request.attachment_ids],
        ),
    )


def _attachments_text(documents: Sequence[DocumentText]) -> str:
    text = render_documents(documents) if documents else ""
    if len(text) > MAX_ATTACHMENT_CHARS:
        text = text[:MAX_ATTACHMENT_CHARS] + "\n[attachments truncated]"
    return text


async def earlier_attachment_ids(
    deps: CopilotDeps, principal: Principal, conversation_id: UUID
) -> tuple[UUID, ...]:
    """Upload ids attached in the caller's earlier turns of a conversation, oldest first."""
    ids: list[UUID] = []
    for stored in await _earlier_inputs(deps, principal, conversation_id):
        ids.extend(stored.attachment_ids)
    return tuple(dict.fromkeys(ids))


async def buyer_messages(
    deps: CopilotDeps, principal: Principal, conversation_id: UUID, current: str
) -> list[str]:
    """What the buyer wrote in this conversation, oldest first, ending with `current`.

    For create_rfq: the intake agent takes part numbers and terms from the
    buyer's own words, not from the Copilot's summary of them. The standing
    instruction sent for files alone is not the buyer's words and is left out.
    Bounded to the latest messages that fit `MAX_BUYER_MESSAGES_CHARS`.
    """
    earlier = [s.message for s in await _earlier_inputs(deps, principal, conversation_id)]
    messages = [m for m in [*earlier, current] if m.strip() and m != ATTACHMENTS_ONLY_MESSAGE][
        -MAX_BUYER_MESSAGES:
    ]
    kept: list[str] = []
    total = 0
    for message in reversed(messages):
        total += len(message)
        if total > MAX_BUYER_MESSAGES_CHARS and kept:
            break
        kept.append(message[:MAX_BUYER_MESSAGES_CHARS])
    return list(reversed(kept))


async def _earlier_inputs(
    deps: CopilotDeps, principal: Principal, conversation_id: UUID
) -> list[CopilotInput]:
    """The inputs of the caller's earlier turns of a conversation, oldest first.

    Read from the conversation's stored runs, which are scoped to the caller's
    tenant and user (`load_session_runs`); the request never names them.
    Empty without run storage or for a conversation the caller does not have.
    """
    from bido_back.agents.agno_adapter import load_session_runs

    context = ToolContext(tenant_id=principal.tenant_id, user_id=principal.user_id, run_id=uuid4())
    runs = await load_session_runs(
        context, session_key(principal, conversation_id), deps.runner_config
    )
    inputs: list[CopilotInput] = []
    for stored in runs or []:
        if not stored.input:
            continue
        try:
            inputs.append(CopilotInput.model_validate_json(stored.input))
        except ValidationError:
            continue
    return inputs


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

    earlier_ids = tuple(prepared.input.earlier_attachment_ids)

    async def earlier_text() -> str:
        try:
            documents = await _attachments(deps, principal, earlier_ids)
        except AttachmentNotFound:
            # A file was deleted since that turn; read none rather than a part.
            logger.warning(
                "copilot earlier attachments gone tenant=%s conversation=%s",
                principal.tenant_id,
                prepared.conversation_id,
            )
            return ""
        return _attachments_text(documents)

    async def own_words() -> list[str]:
        return await buyer_messages(
            deps, principal, prepared.conversation_id, prepared.input.message
        )

    tools = copilot_tools(
        CopilotToolDeps(
            db=deps.db,
            agents=deps.agents,
            mailboxes=deps.mailboxes,
            attachments_text=prepared.input.attachments_text,
            attachment_ids=tuple(prepared.input.attachment_ids),
            earlier_attachment_ids=earlier_ids,
            earlier_attachments_text=earlier_text if earlier_ids else None,
            buyer_messages=own_words,
            storage=deps.storage,
            sheet_mapper=deps.sheet_mapper or default_sheet_mapper(deps.runner_config),
        )
    )
    agent = copilot_agent(tools)
    # Every later log line of this request names the conversation and the run.
    annotate(conversation_id=prepared.conversation_id, run_id=context.run_id)
    turn = _TurnLog(prepared, context.run_id, agent.prompt_version, agent.model)
    yield ConversationEvent(conversation_id=prepared.conversation_id, run_id=context.run_id)
    await _record_turn(deps.db, principal, prepared)

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
    try:
        async for event in events:
            if isinstance(event, RunCompleted):
                try:
                    reply = CopilotReply.model_validate(event.output)
                except ValidationError:
                    turn.failed("invalid_output")
                    yield RunFailed(
                        run_id=event.run_id,
                        seq=event.seq,
                        code="invalid_output",
                        message="no answer",
                    ).model_dump(mode="json")
                    break
                ok = True
                yield AnswerEvent(run_id=context.run_id, text=reply.answer)
                continue
            if isinstance(event, ToolCalled):
                called[event.call_id] = event.tool
            elif isinstance(event, ToolResult):
                if not event.ok:
                    turn.tool_errors += 1
                if kinds.get(event.tool) == "draft":
                    actions.append(
                        CopilotAction(
                            call_id=event.call_id,
                            tool=event.tool,
                            ok=event.ok,
                            output=event.output,
                            error=event.error,
                        )
                    )
            elif isinstance(event, RunFailed):
                turn.failed(event.code)
            yield event.model_dump(mode="json")
        turn.finished("ok" if ok else "failed")
    except Exception:
        # Logged with its traceback by the request middleware.
        turn.finished("error")
        raise
    finally:
        turn.write(tools=sorted(set(called.values())), tool_calls=len(called), actions=len(actions))
    yield DoneEvent(
        run_id=context.run_id,
        conversation_id=prepared.conversation_id,
        ok=ok,
        actions=actions,
    )


class _TurnLog:
    """A line when a Copilot turn starts and one when it ends (ids and counts only).

    The end line says how it ended: `ok`, `failed` (with the run's error code),
    `error` on an exception, or `aborted` when the buyer closed the stream
    before the end.
    """

    def __init__(
        self, prepared: PreparedMessage, run_id: UUID, prompt_version: int, model: str
    ) -> None:
        self._started = time.perf_counter()
        self._outcome = "aborted"
        self._code: str | None = None
        self.tool_errors = 0
        self._fields: dict[str, object] = {
            # Also bound to the request's other lines; repeated here so the turn
            # lines name it wherever they are written (evals, scripts).
            "conversation_id": str(prepared.conversation_id),
            "run_id": str(run_id),
            "prompt_version": prompt_version,
            "model": model,
            "attachments": len(prepared.input.attachment_ids),
            "earlier_attachments": len(prepared.input.earlier_attachment_ids),
            "files_only": _files_only(prepared.input),
        }
        logger.info("copilot turn started", extra=self._fields)

    def failed(self, code: str) -> None:
        self._code = code

    def finished(self, outcome: str) -> None:
        self._outcome = outcome

    def write(self, *, tools: list[str], tool_calls: int, actions: int) -> None:
        fields = {
            **self._fields,
            "outcome": self._outcome,
            "duration_ms": round((time.perf_counter() - self._started) * 1000, 1),
            "tool_calls": tool_calls,
            "tool_errors": self.tool_errors,
            "tools": tools,
            "actions": actions,
        }
        if self._code is not None:
            fields["code"] = self._code
        level = logging.INFO if self._outcome == "ok" else logging.WARNING
        logger.log(level, "copilot turn finished", extra=fields)


def conversation_title(input: CopilotInput) -> str:
    """The first message, whitespace collapsed and truncated; the file names for files only."""
    text = ", ".join(input.attachments) if _files_only(input) else " ".join(input.message.split())
    if not text:
        return "(attachments)" if input.attachment_ids else "(empty)"
    if len(text) <= TITLE_CHARS:
        return text
    return text[: TITLE_CHARS - 1].rstrip() + "…"


def _files_only(input: CopilotInput) -> bool:
    """The buyer sent files without text, so the message is the standing instruction."""
    return bool(input.attachment_ids) and input.message == ATTACHMENTS_ONLY_MESSAGE


async def _record_turn(db: Db, principal: Principal, prepared: PreparedMessage) -> None:
    """Upsert the caller's conversation row: the title on the first turn, then counts.

    Listing is secondary to answering, so a failure here is logged (ids only)
    and the message goes on.
    """
    statement = (
        insert(CopilotConversation)
        .values(
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            id=prepared.conversation_id,
            title=conversation_title(prepared.input),
            turns=1,
        )
        .on_conflict_do_update(
            index_elements=["tenant_id", "user_id", "id"],
            set_={
                "turns": CopilotConversation.turns + 1,
                "last_activity_at": func.now(),
            },
        )
    )
    try:
        async with tenant_session(db, principal.tenant_id) as session:
            await session.execute(statement)
    except SQLAlchemyError:
        logger.warning(
            "copilot conversation not recorded tenant=%s conversation=%s",
            principal.tenant_id,
            prepared.conversation_id,
        )


async def list_conversations(
    db: Db, principal: Principal, *, limit: int, before: datetime | None = None
) -> ConversationList:
    """The caller's own conversations in their tenant, most recent activity first."""
    limit = max(1, min(limit, LIST_LIMIT_MAX))
    query = (
        select(CopilotConversation)
        .where(CopilotConversation.user_id == principal.user_id)
        .order_by(CopilotConversation.last_activity_at.desc(), CopilotConversation.id.desc())
        .limit(limit + 1)
    )
    if before is not None:
        query = query.where(CopilotConversation.last_activity_at < before)
    async with tenant_session(db, principal.tenant_id) as session:
        rows = list((await session.scalars(query)).all())
    page = rows[:limit]
    return ConversationList(
        items=[
            ConversationSummary(
                conversation_id=row.id,
                title=row.title,
                created_at=row.created_at,
                last_activity_at=row.last_activity_at,
                turns=row.turns,
            )
            for row in page
        ],
        next_before=page[-1].last_activity_at if len(rows) > limit else None,
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
        attachments: list[str] = []
        if stored.input:
            try:
                parsed = CopilotInput.model_validate_json(stored.input)
            except ValidationError:
                parsed = None
            if parsed is not None:
                attachments = parsed.attachments
                # Show what the buyer wrote, not the instruction sent for files alone.
                message = "" if _files_only(parsed) else parsed.message
        turns.append(
            ConversationTurn(
                run_id=stored.run_id,
                created_at=datetime.fromtimestamp(stored.created_at, UTC),
                message=message,
                attachments=attachments,
                answer=_answer(stored.output),
                status=stored.status,
            )
        )
    return ConversationView(conversation_id=conversation_id, turns=turns)


def _answer(output: dict[str, JsonValue] | str | None) -> str | None:
    try:
        if isinstance(output, str):
            # Streamed replies are stored as the text itself; older turns as JSON.
            if not output.lstrip().startswith("{"):
                return output or None
            return CopilotReply.model_validate_json(output).answer
        if output is not None:
            return CopilotReply.model_validate(output).answer
    except ValidationError:
        return None
    return None
