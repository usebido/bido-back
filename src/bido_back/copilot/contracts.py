"""The copilot agent's own input and output, and the request/response shapes of its API."""

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, JsonValue

MAX_MESSAGE_CHARS = 8_000
MAX_ATTACHMENTS = 5


# --- What the model sees and answers -------------------------------------------------


class CopilotInput(BaseModel):
    message: str = Field(description="What the buyer wrote")
    today: date
    attachments: list[str] = Field(
        default_factory=list[str], description="File names the buyer attached to this message"
    )
    attachment_ids: list[UUID] = Field(
        default_factory=list[UUID],
        description="The attachments' ids, in the same order (for import_spreadsheet)",
    )
    attachments_text: str = Field(
        default="", description="Text read from the attachments, with [file] markers"
    )
    earlier_attachment_ids: list[UUID] = Field(
        default_factory=list[UUID],
        description="Files attached in earlier messages of this conversation (still usable)",
    )


class CopilotReply(BaseModel):
    answer: str = Field(description="The reply to the buyer, plain text, in the buyer's language")


# --- HTTP ------------------------------------------------------------------------------------


class CopilotMessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    # Files uploaded first through POST /v1/onboarding/uploads (same storage).
    attachment_ids: list[UUID] = Field(default_factory=list[UUID], max_length=MAX_ATTACHMENTS)
    # Omit to start a new conversation; the first event returns its id.
    conversation_id: UUID | None = None


class CopilotAction(BaseModel):
    """A draft tool the copilot ran in this message (rfq created, invites, approvals...)."""

    call_id: str
    tool: str
    ok: bool
    output: dict[str, JsonValue] | None = None
    error: str | None = None


# Server-sent events of POST /v1/copilot/messages. Besides these three, the
# stream forwards the runner's own events unchanged (`bido_back.agents.events`):
# reasoning_delta, reasoning_step, tool_called, tool_result, artifact, text_delta,
# approval_requested and run_failed.


class ConversationEvent(BaseModel):
    type: Literal["conversation"] = "conversation"
    conversation_id: UUID
    run_id: UUID


class AnswerEvent(BaseModel):
    type: Literal["answer"] = "answer"
    run_id: UUID
    text: str


class DoneEvent(BaseModel):
    type: Literal["done"] = "done"
    run_id: UUID
    conversation_id: UUID
    ok: bool
    actions: list[CopilotAction]


class ConversationTurn(BaseModel):
    run_id: str
    created_at: datetime
    message: str | None
    answer: str | None
    status: str


class ConversationView(BaseModel):
    conversation_id: UUID
    turns: list[ConversationTurn]


class ConversationSummary(BaseModel):
    """One of the caller's conversations, for a history list."""

    conversation_id: UUID
    title: str = Field(description="The first message, truncated")
    created_at: datetime
    last_activity_at: datetime
    turns: int = Field(description="Messages sent in this conversation")


class ConversationList(BaseModel):
    """The caller's conversations, most recent activity first."""

    items: list[ConversationSummary]
    next_before: datetime | None = Field(
        default=None,
        description="Pass as `before` for the next page; null on the last page",
    )


StreamItem = ConversationEvent | AnswerEvent | DoneEvent | dict[str, Any]
