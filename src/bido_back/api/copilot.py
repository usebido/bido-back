"""`/v1/copilot`: the procurement copilot.

* `POST /v1/copilot/messages`: send a message, files uploaded through
  `POST /v1/onboarding/uploads`, or both (files alone mean "import and analyse
  them"; neither is a 422); the response is a Server-Sent Events
  stream (`text/event-stream`). Every event is `event: <type>` plus one JSON
  `data:` line with a `type` field:

  - `conversation` `{conversation_id, run_id}`: always first. Send the
    conversation id back with the next message to continue the conversation.
  - `reasoning_delta` `{step_id, text}`: the next piece of a thinking step,
    as the model writes it. Append pieces with the same `step_id` into one
    block.
  - `reasoning_step` `{step_id, text}`: a whole thinking step (a summary of
    the model's reasoning), sent once its pieces are complete. Clients that
    ignore `reasoning_delta` render only these.
  - `tool_called` `{call_id, tool, kind, args}` and `tool_result`
    `{call_id, tool, ok, output | error}`: a lookup or a draft action.
  - `artifact` `{kind, data}`: a card to render. Kinds: `rfq_draft`,
    `approvals` (emails waiting for approval), `comparison`, `rfq_template`.
  - `text_delta` `{text}`: the next piece of the reply, as the model writes it.
    Append them in order; text written before a tool call and after it is
    separated by a blank line.
  - `approval_requested` (reserved: the copilot has no act tool).
  - `answer` `{text}`: the whole reply (the deltas joined), after the last delta.
  - `run_failed` `{code, message}`: the run stopped without an answer.
  - `done` `{ok, conversation_id, actions}`: always last. `actions` lists the
    draft tools that ran (created RFQs, invites, prepared sends, template
    changes) with their output.

  Runner events also carry `run_id` and an increasing `seq`. Nothing the
  copilot does sends an email: preparing a send creates approvals that a
  person decides on in `/v1/approvals` (the buyer by default; a second
  person in four-eyes tenants).
* `GET /v1/copilot/conversations`: the caller's own conversations, most recent
  activity first (`limit`, and `before` = the previous page's `next_before`).
* `GET /v1/copilot/conversations/{id}`: the caller's own conversation so far.
"""

import json
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bido_back.agents.config import RunnerConfig
from bido_back.api.deps import (
    ApiResources,
    get_db,
    get_mailbox_senders,
    get_principal,
    get_rfq_agents,
    get_storage,
)
from bido_back.api.rfqs import error_responses
from bido_back.auth import Principal
from bido_back.copilot import service
from bido_back.copilot.contracts import (
    ConversationList,
    ConversationView,
    CopilotMessageRequest,
    StreamItem,
)
from bido_back.rfqs.agents import RfqAgents
from bido_back.rfqs.mailbox import MailboxSenderFactory
from bido_back.storage import BlobStorage

Db = Annotated[async_sessionmaker[AsyncSession], Depends(get_db)]
CurrentPrincipal = Annotated[Principal, Depends(get_principal)]


def get_copilot_runner_config(request: Request) -> RunnerConfig:
    """How the copilot's runs reach the model and Agno's storage; tests override it."""
    resources: object = request.app.state.resources
    if not isinstance(resources, ApiResources):
        raise RuntimeError("app.state.resources is not configured")
    return RunnerConfig.from_settings(resources.settings)


def get_copilot_deps(
    db: Db,
    storage: Annotated[BlobStorage, Depends(get_storage)],
    agents: Annotated[RfqAgents, Depends(get_rfq_agents)],
    mailboxes: Annotated[MailboxSenderFactory, Depends(get_mailbox_senders)],
    runner_config: Annotated[RunnerConfig, Depends(get_copilot_runner_config)],
) -> service.CopilotDeps:
    return service.CopilotDeps(
        db=db, storage=storage, agents=agents, mailboxes=mailboxes, runner_config=runner_config
    )


Deps = Annotated[service.CopilotDeps, Depends(get_copilot_deps)]


def _sse(item: StreamItem) -> str:
    data = item.model_dump(mode="json") if isinstance(item, BaseModel) else item
    kind = str(data.get("type", "message"))
    return f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"


async def _stream(items: AsyncIterator[StreamItem]) -> AsyncIterator[str]:
    async for item in items:
        yield _sse(item)


def copilot_router() -> APIRouter:
    router = APIRouter(prefix="/v1/copilot", tags=["copilot"])

    @router.post(
        "/messages",
        response_class=StreamingResponse,
        responses={
            **error_responses(status.HTTP_404_NOT_FOUND),
            200: {
                "description": "Server-Sent Events; see the module docs for the event types.",
                "content": {"text/event-stream": {"schema": {"type": "string"}}},
            },
        },
    )
    async def post_message(  # pyright: ignore[reportUnusedFunction]
        body: CopilotMessageRequest, principal: CurrentPrincipal, deps: Deps
    ) -> StreamingResponse:
        """Send the copilot a message; the reply streams back as Server-Sent Events."""
        try:
            prepared = await service.prepare_message(deps, principal, body)
        except service.AttachmentNotFound as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None
        return StreamingResponse(
            _stream(service.stream_message(deps, prepared)),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.get("/conversations")
    async def list_conversations(  # pyright: ignore[reportUnusedFunction]
        principal: CurrentPrincipal,
        db: Db,
        limit: Annotated[int, Query(ge=1, le=service.LIST_LIMIT_MAX)] = 20,
        before: Annotated[
            datetime | None, Query(description="The previous page's `next_before`")
        ] = None,
    ) -> ConversationList:
        """The caller's own conversations, most recent activity first."""
        return await service.list_conversations(db, principal, limit=limit, before=before)

    @router.get(
        "/conversations/{conversation_id}",
        responses=error_responses(status.HTTP_404_NOT_FOUND),
    )
    async def get_conversation(  # pyright: ignore[reportUnusedFunction]
        conversation_id: UUID, principal: CurrentPrincipal, deps: Deps
    ) -> ConversationView:
        """The caller's own conversation, oldest turn first."""
        found = await service.conversation(deps, principal, conversation_id)
        if found is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")
        return found

    return router
