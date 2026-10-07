"""API and act-tool models of the award, the purchase orders and the "not selected" notices.

Decimals travel as strings inside approval args: jsonb rewrites floats, and an
approval's args must hash the same after storage (see `approvals.sink`).
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from bido_back.awards.views import (
    AwardApprovalView,
    AwardLine,
    DecimalText,
    ThreadEmailApprovalView,
)
from bido_back.rfqs.schemas import EmailAddress

__all__ = ["AwardApprovalView", "AwardLine", "DecimalText", "ThreadEmailApprovalView"]

Subject = Annotated[str, StringConstraints(min_length=1, max_length=300, pattern=r"^[^\r\n]*$")]
PurchaseOrderStatus = Literal["draft", "pending_approval", "sent"]
AwardState = Literal["none", "pending_approval", "awarded"]


# --- Requests ------------------------------------------------------------------------


class AwardSelection(BaseModel):
    """Which supplier gets one RFQ line, at which quantity tier."""

    model_config = ConfigDict(extra="forbid")

    rfq_line_id: UUID
    tier: int = Field(default=0, ge=0, le=20)
    # The invite (supplier) whose latest proposal is taken. None = the recommendation.
    invite_id: UUID | None = None


class AwardRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Empty = every line that has a recommended offer, at tier 0, as recommended.
    lines: list[AwardSelection] = Field(default_factory=list[AwardSelection], max_length=200)
    # Required when any selection differs from the recommendation.
    reason: Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)] | None = None


# --- Approved args -------------------------------------------------------------------


class AwardRfqArgs(BaseModel):
    """What the approver approves: the award of one RFQ."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rfq_id: UUID
    rfq_ref: str
    requested_by: UUID
    lines: list[AwardLine] = Field(min_length=1, max_length=200)
    differs_from_recommendation: bool
    reason: str | None


class AwardRfqResult(BaseModel):
    rfq_id: UUID
    outcome: Literal["awarded", "already_awarded", "stale"]
    award_id: UUID | None = None
    purchase_order_ids: list[UUID] = Field(default_factory=list[UUID])


class SendThreadEmailArgs(BaseModel):
    """An email in an invite's thread: a purchase order or a "not selected" notice."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    invite_id: UUID
    # The buyer who sent the RFQ email: the email goes out from their mailbox.
    sender_user_id: UUID
    to: list[EmailAddress] = Field(min_length=1, max_length=10)
    cc: list[EmailAddress] = Field(default_factory=list[str], max_length=10)
    subject: Subject
    body: str = Field(min_length=1, max_length=50_000)


class SendPurchaseOrderArgs(SendThreadEmailArgs):
    purchase_order_id: UUID
    purchase_order_ref: str
    # sha256 of the PO's header and lines: the attachment is rendered from them
    # at send time, and must be what was approved.
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class SendAwardNoticeArgs(SendThreadEmailArgs):
    notice_id: UUID


class ThreadEmailResult(BaseModel):
    outcome: Literal["sent", "already_sent", "skipped"]
    message_id: str | None = None


# --- Views ---------------------------------------------------------------------------


class PurchaseOrderLineView(BaseModel):
    position: int
    rfq_line_id: UUID | None
    proposal_line_id: UUID | None
    description: str
    mpn: str | None
    quantity: Decimal
    unit: str
    unit_price: Decimal
    line_total: Decimal
    need_by: date | None
    lead_time_days: int | None


class PurchaseOrderView(BaseModel):
    id: UUID
    ref: str
    status: PurchaseOrderStatus
    rfq_id: UUID
    rfq_ref: str
    invite_id: UUID
    supplier_id: UUID
    supplier_name: str
    currency: str
    incoterm: str | None
    payment_terms: str | None
    total: Decimal
    created_at: datetime
    sent_at: datetime | None
    # The pending send approval, while one waits.
    send_approval_id: UUID | None = None
    lines: list[PurchaseOrderLineView]


class PurchaseOrderSummary(BaseModel):
    id: UUID
    ref: str
    status: PurchaseOrderStatus
    rfq_id: UUID
    rfq_ref: str
    supplier_name: str
    currency: str
    total: Decimal
    created_at: datetime
    sent_at: datetime | None


class AwardView(BaseModel):
    rfq_id: UUID
    rfq_ref: str
    state: AwardState
    # The pending award approval, or the one that was executed.
    approval_id: UUID | None = None
    award_id: UUID | None = None
    differs_from_recommendation: bool | None = None
    reason: str | None = None
    lines: list[AwardLine] = Field(default_factory=list[AwardLine])
    purchase_orders: list[PurchaseOrderSummary] = Field(default_factory=list[PurchaseOrderSummary])


class AwardRequested(BaseModel):
    rfq_id: UUID
    approval_id: UUID
    differs_from_recommendation: bool
    lines: list[AwardLine]


class ApprovalRequested(BaseModel):
    approval_id: UUID


class NoticeProposed(BaseModel):
    invite_id: UUID
    supplier_name: str
    approval_id: UUID


class NoticesRequested(BaseModel):
    rfq_id: UUID
    proposed: list[NoticeProposed]
    # Losing suppliers skipped: already noticed, no thread, or no contact email.
    skipped: list[UUID]
