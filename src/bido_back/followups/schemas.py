"""API shapes of the follow-up settings, the chase/clarification history, the
clarification ledger and the supplier questions."""

from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from bido_back.agents.catalog.contracts import FollowupField, FollowupQuestion

FollowupStatus = Literal["proposed", "sent", "canceled", "rejected"]
FollowupKindView = Literal["chase", "clarification", "reminder", "answer"]
ChaseState = Literal[
    "scheduled", "awaiting_approval", "paused", "done", "disabled", "not_applicable"
]
AskStatusView = Literal["proposed", "open", "answered", "unusable", "withdrawn", "resolved"]
QuestionStatusView = Literal["open", "answered", "dismissed"]
FollowupPhaseView = Literal[
    "needs_your_answer",
    "needs_your_decision",
    "email_pending",
    "waiting_for_supplier",
    "awaiting_offer",
    "will_ask",
    "nothing_to_ask",
    "not_applicable",
]
DecisionKindView = Literal["unusable_answer", "asked_before", "offer_held", "no_answer"]


class FollowupSettings(BaseModel):
    """The RFQ's chase schedule, in business days (weekends and supplier holidays skipped)."""

    enabled: bool
    first_after_business_days: int = Field(
        description="First chase this many business days after the RFQ email, and never "
        "before its response date"
    )
    every_business_days: int = Field(description="Then one chase every this many business days")
    max_chases: int
    response_due: date | None


class FollowupSettingsUpdate(BaseModel):
    """Only the fields sent are changed."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    first_after_business_days: int | None = Field(default=None, ge=0, le=30)
    every_business_days: int | None = Field(default=None, ge=1, le=30)
    max_chases: int | None = Field(default=None, ge=0, le=10)
    response_due: date | None = None


class FollowupMessageView(BaseModel):
    id: UUID
    # A reminder of an unanswered email shows as a chase, the answer to a supplier's
    # question as a clarification: `followup_kind` tells them apart.
    kind: Literal["chase", "clarification"]
    followup_kind: FollowupKindView = Field(
        description="The follow-up's own kind: chase, clarification, reminder (of an email "
        "the supplier has not answered) or answer (to the supplier's question)"
    )
    sequence: int
    status: FollowupStatus
    approval_id: UUID | None
    canceled_reason: str | None
    created_at: datetime
    sent_at: datetime | None
    # The date the email asks the supplier to answer by (clarifications, reminders).
    reply_by: date | None = None


class AskView(BaseModel):
    """One item a clarification asked (the ledger): each is asked at most once."""

    position: int | None = Field(description="RFQ line position; null for the whole offer")
    field: FollowupField
    question: FollowupQuestion
    need_by: date | None
    status: AskStatusView = Field(
        description="proposed (awaiting approval), open (asked), answered, unusable (still "
        "missing after the supplier's answer: your decision), withdrawn (never sent), "
        "resolved (you decided)"
    )
    asked_at: datetime | None
    reply_by: date | None
    settled_at: datetime | None


class OpenItemView(BaseModel):
    """Something the latest offer lacks that Bido has not asked yet."""

    position: int | None
    field: FollowupField
    question: FollowupQuestion
    need_by: date | None
    expected_delivery: date | None


class SupplierQuestionView(BaseModel):
    """A question the supplier asked the buyer. Automatic emails wait while one is open."""

    id: UUID
    question: str
    status: QuestionStatusView
    asked_at: datetime
    answer_followup_id: UUID | None
    decided_at: datetime | None


class WaitingView(BaseModel):
    """The email the supplier has not answered yet."""

    kind: Literal["clarification", "counter", "bafo"]
    sent_at: datetime
    reply_by: date | None
    reminded_at: datetime | None


class DecisionView(BaseModel):
    """Something only the buyer can settle for this supplier."""

    kind: DecisionKindView = Field(
        description="unusable_answer: asked once, the answer did not give it; asked_before: "
        "answered once, missing again; offer_held: the latest reply could not be applied "
        "(see reasons); no_answer: no answer even after the reminder"
    )
    position: int | None = None
    field: FollowupField | None = None
    reasons: list[str] = Field(default_factory=list[str])
    since: datetime | None = None


class InviteFollowups(BaseModel):
    invite_id: UUID
    supplier_name: str
    invite_status: str
    sent_at: datetime | None
    chase_state: ChaseState
    # When the next chase becomes due (`scheduled` or `paused` only).
    next_chase_at: datetime | None
    paused_until: datetime | None
    chases_used: int
    messages: list[FollowupMessageView]
    # Where this supplier stands: what Bido waits for, or what the buyer must do.
    phase: FollowupPhaseView = "not_applicable"
    waiting_for_supplier: WaitingView | None = None
    asks: list[AskView] = Field(default_factory=list[AskView])
    open_items: list[OpenItemView] = Field(default_factory=list[OpenItemView])
    late_deliveries: list[OpenItemView] = Field(
        default_factory=list[OpenItemView],
        description="Lines whose stated lead time delivers after the need-by date (a "
        "negotiation matter, not a clarification)",
    )
    questions: list[SupplierQuestionView] = Field(default_factory=list[SupplierQuestionView])
    needs_your_decision: list[DecisionView] = Field(default_factory=list[DecisionView])


class RfqFollowups(BaseModel):
    rfq_id: UUID
    settings: FollowupSettings
    invites: list[InviteFollowups]


# --- Answering a supplier's question ------------------------------------------------------------

AnswerText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=5000)]
Reason = Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)]


class AnswerQuestionsRequest(BaseModel):
    """The buyer's answer, sent to the supplier as written once a person approves it."""

    model_config = ConfigDict(extra="forbid")

    answer: AnswerText = Field(description="The email text, in the supplier's language")
    question_ids: list[UUID] = Field(
        default_factory=list[UUID],
        max_length=20,
        description="The questions it answers; empty: every open question of this supplier",
    )


class DismissQuestionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Reason | None = Field(
        default=None, description="Why no answer is needed (e.g. answered by phone)"
    )


class AnsweredQuestions(BaseModel):
    """The answer is recorded; its email waits for approval (`approval_id`)."""

    invite_id: UUID
    followup_id: UUID
    approval_id: UUID
    question_ids: list[UUID]
