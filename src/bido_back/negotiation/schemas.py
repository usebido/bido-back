"""API shapes of the negotiation: settings, per-supplier state, messages, outcome."""

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

NegotiationStatus = Literal["not_started", "open", "ready_to_close", "closed"]
InviteNegotiationStatus = Literal[
    "open", "countered", "bafo_requested", "accepted", "rejected", "closed"
]
NegotiationActionName = Literal["counter", "bafo"]
NegotiationMessageStatus = Literal["proposed", "sent", "canceled", "rejected"]
LineDecisionName = Literal["no_offer", "blocked", "accept", "above_max", "counter"]


class LineLimitsView(BaseModel):
    rfq_line_id: UUID
    position: int
    description: str
    mpn: str | None
    # In the RFQ's base currency. Suppliers are told the target, never the max.
    target_unit_price: Decimal | None
    max_unit_price: Decimal | None
    max_lead_time_days: int | None


class NegotiationSettings(BaseModel):
    deadline: datetime | None
    max_rounds: int
    early_close: bool
    base_currency: str
    lines: list[LineLimitsView]


class LineLimitsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rfq_line_id: UUID
    target_unit_price: Decimal | None = Field(default=None, gt=0)
    max_unit_price: Decimal | None = Field(default=None, gt=0)
    max_lead_time_days: int | None = Field(default=None, ge=0, le=3650)

    @model_validator(mode="after")
    def _target_within_max(self) -> "LineLimitsUpdate":
        if (
            self.target_unit_price is not None
            and self.max_unit_price is not None
            and self.target_unit_price > self.max_unit_price
        ):
            raise ValueError("target_unit_price must not exceed max_unit_price")
        return self


class NegotiationSettingsUpdate(BaseModel):
    """PUT: replaces the settings given. Lines not listed keep their limits; a line
    listed has all three limits replaced. Saving opens the negotiation."""

    model_config = ConfigDict(extra="forbid")

    deadline: datetime | None = None
    max_rounds: int = Field(default=3, ge=1, le=10)
    early_close: bool = True
    lines: list[LineLimitsUpdate] = Field(default_factory=list[LineLimitsUpdate], max_length=500)


class NegotiationMessageView(BaseModel):
    id: UUID
    round: int
    action: NegotiationActionName
    status: NegotiationMessageStatus
    approval_id: UUID | None
    proposal_id: UUID
    canceled_reason: str | None
    created_at: datetime
    sent_at: datetime | None


class LineDecisionView(BaseModel):
    rfq_line_id: UUID
    decision: LineDecisionName
    offered_unit_price_base: Decimal | None
    ask_unit_price: Decimal | None
    lead_time_days: int | None
    ask_lead_time_days: int | None


class InviteNegotiation(BaseModel):
    invite_id: UUID
    supplier_name: str
    invite_status: str
    status: InviteNegotiationStatus
    rounds_used: int
    latest_proposal_id: UUID | None
    latest_proposal_version: int | None
    # How the latest offer stands against the limits right now (recomputed).
    lines: list[LineDecisionView]
    messages: list[NegotiationMessageView]


class OutcomeLine(BaseModel):
    rfq_line_id: UUID
    tier: int
    quantity: Decimal
    invite_id: UUID | None
    supplier_name: str | None
    effective_unit_price_base: Decimal | None
    lead_time_days: int | None
    within_target: bool | None
    justification: str


class RfqNegotiation(BaseModel):
    rfq_id: UUID
    status: NegotiationStatus
    settings: NegotiationSettings
    ready_at: datetime | None
    closed_at: datetime | None
    invites: list[InviteNegotiation]
    # Set when the negotiation is closed: the best eligible offer per line and tier.
    outcome: list[OutcomeLine] | None
