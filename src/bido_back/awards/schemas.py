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
# The order after the send (`fulfilment.state`): `not_sent` while the send is
# a draft or waits for approval; then sent -> acknowledged -> partially_received
# -> received; `closed` (fully received, or short with a reason) and `cancelled`
# are terminal. `status` stays the send's own enum.
FulfilmentState = Literal[
    "not_sent",
    "sent",
    "acknowledged",
    "partially_received",
    "received",
    "closed",
    "cancelled",
]
OPEN_FULFILMENT_STATES: frozenset[str] = frozenset(
    {"sent", "acknowledged", "partially_received", "received"}
)
# A sent order a supplier reply can still change: such a reply is read as an answer to
# the order (`awards.responses`), not as a new offer.
RESPONDING_FULFILMENT_STATES: frozenset[str] = frozenset(
    {"sent", "acknowledged", "partially_received"}
)
AwardState = Literal["none", "pending_approval", "awarded"]
Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
ShortRef = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
Note = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
ReceiptQuantity = Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=4)]


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


class AcknowledgeRequest(BaseModel):
    """The supplier confirmed the order; the buyer records it."""

    model_config = ConfigDict(extra="forbid")

    acknowledged_on: date | None = Field(
        default=None, description="When the supplier confirmed; today when omitted"
    )
    supplier_order_ref: ShortRef | None = Field(
        default=None, description="The supplier's own order or confirmation number"
    )
    note: Note | None = None


class ReceiptLineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line_id: UUID = Field(description="The PO line (`lines[].id` of the purchase order)")
    quantity: ReceiptQuantity = Field(description="Received now, in the line's unit")


class ReceiptRequest(BaseModel):
    """One delivery: what arrived per line, when, and the invoice it came with."""

    model_config = ConfigDict(extra="forbid")

    received_on: date | None = Field(default=None, description="Delivery date; today if omitted")
    invoice_number: ShortRef | None = None
    invoice_date: date | None = None
    note: Note | None = None
    lines: list[ReceiptLineRequest] = Field(min_length=1, max_length=200)


class CloseRequest(BaseModel):
    """Close a fully received PO; or close it short, with a reason, recording the shortfall."""

    model_config = ConfigDict(extra="forbid")

    short: bool = Field(
        default=False,
        description="Close although not everything was received (the rest is given up)",
    )
    reason: Reason | None = Field(default=None, description="Required when closing short")


class ReviewRequest(BaseModel):
    """The buyer's decision on a supplier reply that changed or questioned the order."""

    model_config = ConfigDict(extra="forbid")
    accepted: bool = Field(
        description="True: the buyer accepts what the supplier wrote (e.g. the new date); "
        "false: they do not, and answer the supplier themselves"
    )
    note: Note | None = None


class CancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Reason


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


class PurchaseOrderFulfilment(BaseModel):
    """Where the order stands after the send, and who moved it there."""

    state: FulfilmentState
    open: bool = Field(description="Sent and neither closed nor cancelled: still expecting goods")
    ordered_quantity: Decimal = Field(description="Sum of the lines' ordered quantities")
    received_quantity: Decimal = Field(description="Sum of what every receipt brought so far")
    receipts_count: int = 0
    acknowledged_at: datetime | None = None
    acknowledged_by: UUID | None = None
    supplier_order_ref: str | None = None
    acknowledgement_note: str | None = None
    closed_at: datetime | None = None
    closed_by: UUID | None = None
    closed_short: bool = False
    close_reason: str | None = None
    cancelled_at: datetime | None = None
    cancelled_by: UUID | None = None
    cancel_reason: str | None = None


class ReceiptLineView(BaseModel):
    line_id: UUID
    position: int
    description: str
    unit: str
    quantity: Decimal


class ReceiptView(BaseModel):
    id: UUID
    purchase_order_id: UUID
    received_on: date
    invoice_number: str | None
    invoice_date: date | None
    note: str | None
    recorded_by: UUID
    created_at: datetime
    lines: list[ReceiptLineView]


class IssuedPurchaseOrderLineView(BaseModel):
    id: UUID = Field(description="Pass it as `line_id` in a receipt")
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
    received_quantity: Decimal = Field(default=Decimal(0), description="Received so far")
    remaining_quantity: Decimal = Field(
        default=Decimal(0), description="Ordered minus received; what is still expected"
    )


class SupplierResponseChange(BaseModel):
    position: int
    field: str = Field(description="delivery_date, unit_price or quantity")
    ordered: str | None = Field(description="What the order says")
    stated: str | None = Field(description="What the supplier wrote (a date or a number)")
    unverified: bool = Field(
        default=False, description="Not readable as a date or number, or not found in the email"
    )


class SupplierResponseView(BaseModel):
    """A supplier's reply to the order, as Bido read it (`awards.responses`)."""

    at: datetime
    message_id: UUID | None
    kind: str = Field(
        description="confirmation, confirmation_with_changes, rejection, question, "
        "delivery_update, other, or unread (Bido could not read it)"
    )
    needs_review: bool
    supplier_order_ref: str | None = None
    changes: list[SupplierResponseChange] = Field(default_factory=list[SupplierResponseChange])
    # Dates in the supplier's own words that match nothing on the order.
    other_dates: list[date] = Field(default_factory=list[date])
    summary: str | None = None


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
    fulfilment: PurchaseOrderFulfilment = Field(
        description="The order after the send; see `fulfilment.state`"
    )
    lines: list[IssuedPurchaseOrderLineView]
    needs_review: bool = Field(
        default=False,
        description="A supplier reply changes or questions the order: the buyer decides "
        "(acknowledge, cancel, or answer the supplier)",
    )
    supplier_responses: list[SupplierResponseView] = Field(
        default_factory=list[SupplierResponseView],
        description="The supplier's replies to the order, newest first",
    )
    # The cancellation email to the supplier: the approval while it waits or after it ran.
    cancellation_approval_id: UUID | None = None
    cancellation_sent: bool = False
    document_gaps: list[str] = Field(
        default_factory=list[str],
        description="What the PO document leaves blank (e.g. payment_terms, incoterm, "
        "line 2 delivery_date): fill it on the RFQ before sending, or send it as is",
    )


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
    fulfilment: PurchaseOrderFulfilment = Field(
        description="The order after the send; see `fulfilment.state`"
    )
    needs_review: bool = Field(
        default=False, description="A supplier reply to the order waits for the buyer"
    )


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
