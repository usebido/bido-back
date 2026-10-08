"""Shapes of a negotiation mandate: the approved payload and the API views."""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from bido_back.negotiation.schemas import LineLimitsUpdate
from bido_back.outbound.domains import RecipientCheck
from bido_back.rfqs.schemas import EmailAddress

MandateStatus = Literal["active", "paused", "ended"]
MandateSendOutcome = Literal["auto_approved", "held"]

_Hash = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class MandateLine(BaseModel):
    """One line's limits, in the RFQ's base currency. Suppliers may be told the
    target; the maximum never leaves the server."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rfq_line_id: UUID
    position: int = Field(ge=1)
    description: str = Field(max_length=500)
    target_unit_price: Decimal = Field(gt=0)
    max_unit_price: Decimal = Field(gt=0)
    max_lead_time_days: int | None = Field(default=None, ge=0, le=3650)

    @model_validator(mode="after")
    def _target_within_max(self) -> "MandateLine":
        if self.target_unit_price > self.max_unit_price:
            raise ValueError("target_unit_price must not exceed max_unit_price")
        return self


class MandateSupplier(BaseModel):
    """A supplier Bido may write to on the buyer's behalf, and the only addresses."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    invite_id: UUID
    supplier_name: str = Field(max_length=300)
    to: list[EmailAddress] = Field(min_length=1, max_length=10)


class MandateInitialEmail(BaseModel):
    """An initial RFQ email approved as part of the mandate, exactly (by hash)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    approval_id: UUID
    invite_id: UUID
    args_hash: _Hash


class ApproveMandateArgs(BaseModel):
    """Exactly what the approver sees and approves, once, for one RFQ."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rfq_id: UUID
    rfq_ref: str = Field(max_length=50)
    # The buyer: every email goes out from their connected mailbox.
    sender_user_id: UUID
    currency: str = Field(min_length=3, max_length=3)
    deadline: datetime
    max_rounds: int = Field(ge=1, le=10)
    max_messages_per_supplier_per_day: int = Field(ge=1, le=5)
    # What suppliers may be told: our target price, and that better offers exist
    # (never names, prices or terms of other suppliers, never the maximum).
    disclose_target_price: bool
    disclose_competing_offers: bool
    suppliers: list[MandateSupplier] = Field(min_length=1, max_length=50)
    lines: list[MandateLine] = Field(min_length=1, max_length=500)
    initial_emails: list[MandateInitialEmail] = Field(
        default_factory=list[MandateInitialEmail], max_length=50
    )


class ProposeMandateRequest(BaseModel):
    """POST /v1/rfqs/{id}/negotiation/mandate: build the mandate for approval."""

    model_config = ConfigDict(extra="forbid")

    deadline: datetime
    max_rounds: int = Field(default=3, ge=1, le=10)
    max_messages_per_supplier_per_day: int = Field(default=2, ge=1, le=5)
    # Lines not listed keep the limits already on the RFQ (e.g. from the analysis).
    lines: list[LineLimitsUpdate] = Field(default_factory=list[LineLimitsUpdate], max_length=500)


class ProposedMandate(BaseModel):
    rfq_id: UUID
    approval_id: UUID
    suppliers: int
    lines: int
    initial_emails: int
    deadline: datetime
    # Every address the mandate writes to, with whether its domain receives
    # mail (a real company) and whether it is confirmed: approving needs the
    # unconfirmed non-test domains confirmed (outbound guard).
    recipients: list[RecipientCheck] = Field(default_factory=list[RecipientCheck])


class MandateView(BaseModel):
    id: UUID
    rfq_id: UUID
    status: MandateStatus
    approval_id: UUID
    approved_by: UUID
    deadline: datetime
    max_rounds: int
    max_messages_per_supplier_per_day: int
    suppliers: list[str]
    created_at: datetime
    paused_at: datetime | None
    ended_at: datetime | None


class MandateActivityItem(BaseModel):
    approval_id: UUID
    tool: str
    outcome: MandateSendOutcome
    # Why it was held for a person; null when it went out automatically.
    reason: str | None
    invite_id: UUID | None
    supplier_name: str | None
    approval_status: str
    subject: str | None
    created_at: datetime


class MandateActivity(BaseModel):
    rfq_id: UUID
    mandate: MandateView | None
    items: list[MandateActivityItem]
