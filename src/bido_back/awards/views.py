"""Award models the approval queue shows. No imports from the RFQ modules (no cycles)."""

from datetime import date
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StringConstraints

DecimalText = Annotated[str, StringConstraints(pattern=r"^-?[0-9]+(\.[0-9]+)?$", max_length=40)]


class AwardLine(BaseModel):
    """One awarded line, exactly as the approver saw it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rfq_line_id: UUID
    position: int
    description: str
    mpn: str | None
    tier: int
    quantity: DecimalText
    unit: str
    need_by: date | None
    invite_id: UUID
    supplier_id: UUID
    supplier_name: str
    proposal_id: UUID
    proposal_version: int
    proposal_line_id: UUID
    unit_price: DecimalText
    currency: str
    lead_time_days: int | None
    recommended_invite_id: UUID | None


class AwardApprovalView(BaseModel):
    """What an approver sees for an `award_rfq` approval."""

    rfq_id: UUID
    rfq_ref: str
    differs_from_recommendation: bool
    reason: str | None
    lines: list[AwardLine]


class ThreadEmailApprovalView(BaseModel):
    """What an approver sees for a purchase-order or "not selected" email."""

    kind: Literal["purchase_order", "award_notice"]
    invite_id: UUID
    rfq_id: UUID | None
    rfq_ref: str | None
    supplier_name: str | None
    purchase_order_id: UUID | None = None
    purchase_order_ref: str | None = None
    sender_user_id: UUID
    to: list[str]
    cc: list[str]
    subject: str
    body: str
