"""Request and response shapes of the RFQ and supplier services (also the API's).

Responses carry only what the screens need. Personal data (contact names and
email addresses) appears only where a person must see it to act: the supplier
list (to pick a contact) and the approval queue (to review who gets the email).
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from bido_back.agents.catalog.contracts import LineCategory

# Deliberately strict and single-line: these end up in email headers.
EmailAddress = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        max_length=254,
        pattern=r"^[^@\s<>,;:\"()\[\]\\]+@[^@\s<>,;:\"()\[\]\\]+\.[^@\s<>,;:\"()\[\]\\]+$",
    ),
]
SupplierType = Literal[
    "authorized_distributor", "distributor", "reseller", "manufacturer", "machining", "other"
]
RfqStatus = Literal["draft", "open", "closed", "cancelled"]
InviteStatus = Literal["draft", "pending_approval", "sent", "responded", "declined"]
PositiveQuantity = Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=4)]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


# --- RFQs -------------------------------------------------------------------------


class CreateRfqRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=50_000, description="The buyer's request")


class RfqLineView(BaseModel):
    id: UUID
    position: int
    description: str
    mpn: str | None
    manufacturer: str | None
    category: LineCategory
    quantities: list[Decimal]
    unit: str
    need_by: date | None
    incoterm: str | None
    delivery_location: str | None
    qualification: str | None


class InviteView(BaseModel):
    id: UUID
    supplier_id: UUID
    supplier_name: str
    contact_id: UUID
    status: InviteStatus
    sent_at: datetime | None
    responded_at: datetime | None


class RfqSummary(BaseModel):
    id: UUID
    ref: str
    title: str
    status: RfqStatus
    response_due: date | None
    created_at: datetime


class RfqDetail(RfqSummary):
    base_currency: str
    lines: list[RfqLineView]
    invites: list[InviteView]


class RfqCreated(RfqDetail):
    questions: list[str] = Field(description="What the buyer should clarify before sending")


class UpdateLineRequest(BaseModel):
    """Only the fields present are changed; `null` clears an optional field."""

    model_config = ConfigDict(extra="forbid")

    description: ShortText | None = None
    mpn: str | None = None
    manufacturer: str | None = None
    category: LineCategory | None = None
    quantities: list[PositiveQuantity] | None = Field(default=None, min_length=1, max_length=20)
    unit: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)] | None = None
    need_by: date | None = None
    incoterm: str | None = None
    delivery_location: str | None = None
    qualification: str | None = None


# --- Suppliers and contacts ---------------------------------------------------------


class ContactIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: ShortText | None = None
    email: EmailAddress
    phone: Annotated[str, StringConstraints(strip_whitespace=True, max_length=50)] | None = None


class ContactView(BaseModel):
    id: UUID
    name: str | None
    email: str | None


class CreateSupplierRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: ShortText
    type: SupplierType = "distributor"
    country: Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}$")] | None = None
    language: Annotated[str, StringConstraints(pattern=r"^[a-z]{2}$")] = "en"
    email_domain: (
        Annotated[str, StringConstraints(strip_whitespace=True, to_lower=True, max_length=253)]
        | None
    ) = None
    contacts: list[ContactIn] = Field(default_factory=list[ContactIn], max_length=20)


class SupplierView(BaseModel):
    id: UUID
    name: str
    type: SupplierType
    country: str | None
    language: str
    email_domain: str | None
    contacts: list[ContactView]


# --- Invites and sending ----------------------------------------------------------


class CreateInviteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    supplier_id: UUID
    contact_id: UUID


class PreparedInvite(BaseModel):
    invite_id: UUID
    approval_id: UUID


class PrepareSendResponse(BaseModel):
    rfq_id: UUID
    approvals: list[PreparedInvite] = Field(
        description="One pending approval per invite that was in draft"
    )


# --- The approval queue -------------------------------------------------------------

ApprovalStatus = Literal["pending", "approved", "rejected", "executed", "expired"]


EmailKind = Literal["rfq", "chase", "clarification"]


class RfqEmailDraft(BaseModel):
    """The exact email an approver approves (the stored, hashed arguments)."""

    # The RFQ email itself, a reminder (chase) or a clarification question.
    kind: EmailKind = "rfq"
    invite_id: UUID
    rfq_id: UUID | None
    rfq_ref: str | None
    supplier_name: str | None
    # The requesting buyer, whose connected mailbox sends it.
    sender_user_id: UUID
    to: list[str]
    cc: list[str]
    subject: str
    body: str


class ApprovalQueueItem(BaseModel):
    id: UUID
    tool: str
    status: ApprovalStatus
    requested_by: UUID
    requested_by_me: bool
    created_at: datetime
    decided_at: datetime | None
    # Set for `send_rfq_email` and `send_followup_email`; other tools show only their name.
    rfq_email: RfqEmailDraft | None
