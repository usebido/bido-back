"""The copilot agent's own input and output, and the request/response shapes of its API."""

from datetime import date, datetime
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, Field, JsonValue, model_validator

MAX_MESSAGE_CHARS = 8_000
# Files per message. ERP exports often come one file per transaction (MB52, ME2N,
# MD04, ...), so a single message can carry many; their text reaches the model
# capped at MAX_ATTACHMENT_CHARS in total (service.py), and imports read them by id.
MAX_ATTACHMENTS = 25
# What the copilot is asked when the buyer sends files without writing anything:
# import and analyse them. Portuguese, since the buyer's language is not stored
# and the copilot answers in the language of the message it reads.
ATTACHMENTS_ONLY_MESSAGE = "Importe e analise o(s) arquivo(s) anexado(s)."


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


class CopilotLimits(BaseModel):
    """What one Copilot message may carry, so the front never drifts from the server."""

    max_attachments: int = Field(description="Files per message")
    max_message_chars: int = Field(description="Characters in the message text")
    max_upload_bytes: int = Field(description="Size of one uploaded file, in bytes")
    accepted_extensions: list[str] = Field(description="File extensions accepted, lowercase")


class CopilotMessageRequest(BaseModel):
    # May be empty when files are attached: the buyer just dropped a spreadsheet.
    message: str = Field(default="", max_length=MAX_MESSAGE_CHARS)
    # Files uploaded first through POST /v1/onboarding/uploads (same storage).
    attachment_ids: list[UUID] = Field(default_factory=list[UUID], max_length=MAX_ATTACHMENTS)
    # Omit to start a new conversation; the first event returns its id.
    conversation_id: UUID | None = None

    @model_validator(mode="after")
    def _text_or_files(self) -> Self:
        if not self.message.strip() and not self.attachment_ids:
            raise ValueError("write a message or attach a file")
        return self


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


class TitleEvent(BaseModel):
    """The conversation's title changed: refined by the titler or set by a milestone.

    Sent before `done`, only when the new title is ready in time; a title that
    comes later is stored and shows up in the next conversation list.
    """

    type: Literal["title"] = "title"
    conversation_id: UUID
    title: str


class DoneEvent(BaseModel):
    type: Literal["done"] = "done"
    run_id: UUID
    conversation_id: UUID
    ok: bool
    actions: list[CopilotAction]


class ConversationTurn(BaseModel):
    run_id: str
    created_at: datetime
    # Empty when the buyer only sent files.
    message: str | None
    # The names of the files attached to this turn.
    attachments: list[str] = Field(default_factory=list[str])
    # Their upload ids, in the same order (for `/v1/uploads/{id}/preview` and `/download`).
    attachment_ids: list[UUID] = Field(default_factory=list[UUID])
    answer: str | None
    status: str


class ConversationView(BaseModel):
    conversation_id: UUID
    turns: list[ConversationTurn]


class ConversationSummary(BaseModel):
    """One of the caller's conversations, for a history list."""

    conversation_id: UUID
    title: str = Field(
        description=(
            "The first message, truncated, until the copilot names the conversation "
            "or the buyer renames it"
        )
    )
    title_source: Literal["fallback", "ai", "milestone", "user"] = Field(
        default="fallback",
        description=(
            "Where the title came from: `fallback` (the first message), `ai` (named by "
            "the copilot), `milestone` (an RFQ, analysis or award reference) or `user` "
            "(renamed by the buyer; never changed automatically after that)"
        ),
    )
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


class RenameConversationRequest(BaseModel):
    title: str = Field(min_length=1, max_length=120)

    @model_validator(mode="after")
    def _not_blank(self) -> Self:
        self.title = " ".join(self.title.split())
        if not self.title:
            raise ValueError("title must not be blank")
        return self


StreamItem = ConversationEvent | AnswerEvent | TitleEvent | DoneEvent | dict[str, Any]
