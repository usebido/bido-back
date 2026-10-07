"""API shapes of the follow-up settings and the chase/clarification history."""

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

FollowupStatus = Literal["proposed", "sent", "canceled", "rejected"]
ChaseState = Literal[
    "scheduled", "awaiting_approval", "paused", "done", "disabled", "not_applicable"
]


class FollowupSettings(BaseModel):
    """The RFQ's chase schedule, in business days (weekends and supplier holidays skipped)."""

    enabled: bool
    first_after_business_days: int = Field(
        description="First chase this many business days after the RFQ email"
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
    kind: Literal["chase", "clarification"]
    sequence: int
    status: FollowupStatus
    approval_id: UUID | None
    canceled_reason: str | None
    created_at: datetime
    sent_at: datetime | None


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


class RfqFollowups(BaseModel):
    rfq_id: UUID
    settings: FollowupSettings
    invites: list[InviteFollowups]
