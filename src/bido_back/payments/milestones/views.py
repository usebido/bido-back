"""What the API shows of milestone releases: to the buyer, to the approver and to the factory.

No imports from the awards or RFQ modules (no cycles): the approval queue imports these.
The factory's views carry no chain words, addresses, hashes or the buyer's other data.
"""

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

# Derived for display from the stored state, the payment and the earlier milestones.
MilestoneStatus = Literal[
    "locked",
    "awaiting_documents",
    "changes_requested",
    "in_review",
    "releasing",
    "released",
    "failed",
]
# What the factory sees: `processing` covers an approved release on its way; `released`:
# the buyer's money for it left the reservation, on its way to the factory's account (the
# payout to its bank is a separate step).
FactoryMilestoneStatus = Literal[
    "locked", "awaiting_documents", "changes_requested", "in_review", "processing", "released"
]
SubmissionState = Literal["draft", "submitted", "approved", "released", "rejected", "cancelled"]
DocumentSource = Literal["factory", "buyer"]


class DocumentRequirementView(BaseModel):
    """One kind of document a milestone asks for."""

    doc_type: str
    label: str
    label_zh: str = Field(description="The label on the factory's page (zh-CN)")
    required: bool
    accepts: list[str] = Field(description="Accepted file extensions, e.g. `pdf`, `jpg`")
    description: str


class MilestoneDocumentView(BaseModel):
    id: UUID
    doc_type: str
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    source: DocumentSource
    uploaded_at: datetime


class MilestoneSubmissionView(BaseModel):
    """One attempt at a milestone: the documents gathered, then the person's decision."""

    id: UUID
    attempt: int
    state: SubmissionState
    source: DocumentSource
    note: str | None
    evidence_hash: str | None = Field(
        description="sha256 binding the files to the escrow and the gate; set on submit"
    )
    approval_id: UUID | None
    rejection_code: str | None = Field(description="Why changes were asked (a code)")
    rejection_reason: str | None
    submitted_at: datetime | None
    decided_at: datetime | None
    documents: list[MilestoneDocumentView]


class PaymentMilestoneView(BaseModel):
    """One milestone of a PO payment's escrow, as the buyer sees it."""

    index: int = Field(description="Position in the escrow, also its approval gate")
    key: str | None = Field(description="`tool_start`, `t1`, `ppap`; null for a custom one")
    name: str = Field(description="The name on chain")
    label: str
    bps: int = Field(description="Share of the total in basis points")
    po_currency: str
    po_amount: Decimal = Field(description="This milestone's share of the PO total")
    charge_currency: str
    charge_amount: Decimal = Field(description="Its share of the card charge")
    token_amount: Decimal = Field(description="Tokens the release pays, in major units")
    status: MilestoneStatus
    requirements: list[DocumentRequirementView]
    checks: list[str] = Field(description="What the approver verifies before releasing it")
    submissions: list[MilestoneSubmissionView] = Field(description="Newest first")
    approval_id: UUID | None = Field(description="The release waiting for, or given, approval")
    evidence_hash: str | None
    release_signature: str | None
    release_explorer_url: str | None
    released_at: datetime | None
    failure_code: str | None


class FactoryLinkView(BaseModel):
    """The factory's link. The token is shown only here, once: Bido keeps its hash."""

    token: str
    url: str | None = Field(description="The factory page with the token; null when not set up")
    expires_at: datetime


class MilestoneSubmitted(BaseModel):
    """A milestone sent for approval: a person decides at POST /v1/approvals/{id}/decision."""

    approval_id: UUID
    milestone: PaymentMilestoneView


class MilestoneDocumentRefView(BaseModel):
    """A file of the submission being approved (the approval's arguments)."""

    document_id: UUID
    doc_type: str
    label: str
    filename: str
    size_bytes: int
    sha256: str


class MilestoneReleaseApprovalView(BaseModel):
    """What an approver sees for a `release_po_milestone` approval."""

    payment_id: UUID
    purchase_order_id: UUID
    purchase_order_ref: str
    supplier_name: str | None
    milestone_index: int
    milestone_key: str | None
    milestone_name: str
    label: str
    bps: int
    po_currency: str
    po_amount: Decimal
    charge_currency: str
    charge_amount: Decimal
    attempt: int
    submission_id: UUID
    source: DocumentSource
    note: str | None
    evidence_hash: str
    documents: list[MilestoneDocumentRefView]
    checks: list[str]
    simulated: bool = Field(description="The card charge was simulated: no money moved")


# --- The factory's page (no login) ------------------------------------------------------------


class FactoryDocumentView(BaseModel):
    id: UUID
    doc_type: str
    filename: str
    size_bytes: int
    uploaded_at: datetime


class FactoryRequirementView(BaseModel):
    doc_type: str
    label: str
    label_zh: str
    required: bool
    accepts: list[str]
    uploaded: list[FactoryDocumentView]


class FactoryMilestoneView(BaseModel):
    index: int
    key: str | None
    label: str
    label_zh: str
    bps: int
    amount: Decimal = Field(description="This milestone's share, in the order's currency")
    status: FactoryMilestoneStatus
    rejection_code: str | None = Field(description="Why the buyer asked for changes (a code)")
    rejection_reason: str | None = Field(description="What the buyer asked to change")
    requirements: list[FactoryRequirementView]
    submitted_at: datetime | None
    released_at: datetime | None


class FactoryOrderView(BaseModel):
    """The factory's view of its order: what is reserved for it and what to send."""

    purchase_order_ref: str
    buyer_name: str
    supplier_name: str | None
    currency: str
    total: Decimal
    reserved: bool = Field(description="The full amount is reserved for the factory")
    accepted_at: datetime | None = Field(
        description="When the factory confirmed the order; it uploads only after that"
    )
    milestones: list[FactoryMilestoneView]
    link_expires_at: datetime


class FactoryAcceptedView(BaseModel):
    accepted_at: datetime
