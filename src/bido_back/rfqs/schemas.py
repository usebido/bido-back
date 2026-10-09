"""Request and response shapes of the RFQ and supplier services (also the API's).

Responses carry only what the screens need. Personal data (contact names and
email addresses) appears only where a person must see it to act: the supplier
list (to pick a contact) and the approval queue (to review who gets the email).
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, JsonValue, StringConstraints

from bido_back.agents.catalog.contracts import LineCategory
from bido_back.awards.views import AwardApprovalView, ThreadEmailApprovalView
from bido_back.branding.schemas import LogoPlacement
from bido_back.fx import REFERENCE_CURRENCIES
from bido_back.outbound.domains import RecipientCheck

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
# The buyer's payment terms, verbatim ("28 DDL", "30 days net"); they go into the email.
PaymentTerms = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]
# How long a quote must stay valid, in days (the database checks the same range).
QuoteValidityDays = Annotated[int, Field(ge=1, le=365)]


UNKNOWN_CURRENCY = "not a currency Bido can convert: use an ISO 4217 code such as BRL, EUR or USD"


def _convertible(code: str) -> str:
    normalized = code.strip().upper()
    if normalized not in REFERENCE_CURRENCIES:
        raise ValueError(UNKNOWN_CURRENCY)
    return normalized


CurrencyCode = Annotated[
    str,
    AfterValidator(_convertible),
    Field(
        description="ISO 4217 code: EUR or a currency with an ECB reference rate (BRL, USD...)",
        examples=["BRL"],
    ),
]
# Where an RFQ's currency came from (`bido_back.rfqs.currency`): the buyer said it, the
# purchase analysis's lines were all bought in it, or most of the purchase history is.
CurrencySource = Literal["stated", "analysis", "purchase_history"]


# --- RFQs -------------------------------------------------------------------------


class CreateRfqRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=50_000, description="The buyer's request")
    currency: CurrencyCode | None = Field(
        default=None,
        description=(
            "The RFQ's currency, when the buyer said it. Without it the currency comes from "
            "the purchase history; when that does not tell, the request is refused (400) and "
            "the buyer has to say which currency"
        ),
    )


class RfqLineAssumption(BaseModel):
    """A reading or default the line's quantity rests on (from its procurement
    assessment): shown to the buyer and the approver, never sent to suppliers."""

    code: str
    message: str
    # What changes if the assumption is wrong.
    alternative: str | None = None
    # low: an inconsistency or ambiguity was settled by the most plausible reading.
    confidence: Literal["medium", "low"] = "medium"


LineConfidence = Literal["high", "medium", "low"]


class RfqLineReview(BaseModel):
    """One RFQ line whose quantity rests on assumptions, for the send approval."""

    position: int
    part_number: str | None
    description: str
    quantities: list[Decimal]
    unit: str
    confidence: LineConfidence
    assumptions: list[RfqLineAssumption]


class RfqLineView(BaseModel):
    id: UUID
    position: int
    description: str
    # The buyer's own code for the part (the main reference); `mpn` is the manufacturer's.
    part_number: str | None
    mpn: str | None
    manufacturer: str | None
    category: LineCategory
    quantities: list[Decimal]
    unit: str
    need_by: date | None
    incoterm: str | None
    delivery_location: str | None
    qualification: str | None
    source_evidence: dict[str, JsonValue] = Field(default_factory=dict[str, JsonValue])
    # From the procurement assessment the line was drafted from (None: not drafted from
    # one): how reliable its quantity is, and the assumptions it rests on.
    confidence: LineConfidence | None = None
    assumptions: list[RfqLineAssumption] = Field(default_factory=list[RfqLineAssumption])


class InviteView(BaseModel):
    id: UUID
    supplier_id: UUID
    supplier_name: str
    contact_id: UUID
    status: InviteStatus
    sent_at: datetime | None
    responded_at: datetime | None
    document_language: str | None = Field(
        default=None,
        description="ISO 639-1 of the email and PDF this supplier gets, as the send would "
        "render them now: the supplier's own language, else its country's, else English. "
        "Lets the on-screen preview match the document",
    )


# Why the RFQ's draft invites cannot be prepared for sending right now
# (`bido_back.rfqs.readiness`): no reply-by date, or one already in the past.
SendBlocker = Literal["reply_by_missing", "reply_by_past"]


class RfqHeader(BaseModel):
    id: UUID
    ref: str
    title: str
    status: RfqStatus
    response_due: date | None
    created_at: datetime
    send_blockers: list[SendBlocker] = Field(
        default_factory=list[SendBlocker],
        description="Why preparing a send would be refused now (reply_by_missing: no "
        "response_due; reply_by_past: response_due before today, UTC); empty when nothing "
        "blocks. Only draft and open RFQs can have blockers",
    )
    payment_terms: str | None = Field(
        default=None, description="The buyer's payment terms for suppliers, verbatim"
    )
    quote_validity_days: int | None = Field(
        default=None, description="How many days a quote must stay valid, at least"
    )
    logo_asset_id: UUID | None = Field(
        default=None,
        description="The company logo this RFQ's document shows; null = the company default",
    )
    footer_override: str | None = Field(
        default=None,
        description="Terms printed on this RFQ's document; null = the company default footer",
    )
    logo_placement: LogoPlacement | None = Field(
        default=None,
        description="Where this RFQ's logo sits in its box; null = the company default",
    )
    document_language: str | None = Field(
        default=None,
        description="ISO 639-1 the RFQ document is rendered in when no supplier decides "
        "otherwise (the default before any invite exists); each invite's "
        "document_language is the one its own PDF uses",
    )


RfqStage = Literal["draft", "bidding", "review", "awarded", "completed", "cancelled"]
RfqNextStep = Literal[
    "send RFQ",
    "approve",
    "waiting for suppliers",
    "review quotes",
    "close negotiation",
    "award",
    "send PO",
]


class RfqSummary(RfqHeader):
    """One row of the RFQ list: the header plus what the list screen shows."""

    stage: RfqStage = Field(
        default="draft",
        description="Where the RFQ is in the journey, derived from status, replies, "
        "negotiation and award",
    )
    next_step: RfqNextStep | None = Field(
        default=None, description="What the buyer should do next; null when nothing"
    )
    supplier_names: list[str] = Field(
        default_factory=list[str], description="Invited suppliers, in invite order"
    )
    invited_count: int = 0
    responses_count: int = Field(default=0, description="Invites that answered with a quote")
    best_total_base: Decimal | None = Field(
        default=None,
        description="Lowest complete first-tier total among the quotes, in base_currency",
    )
    base_currency: str | None = None
    need_by: date | None = Field(default=None, description="Earliest need-by among the lines")
    owner: str | None = Field(default=None, description="The buyer who created the RFQ")
    completed_at: datetime | None = Field(
        default=None,
        description="When every purchase order of the RFQ was closed or cancelled (at least "
        "one closed): the `completed` stage. Null until then",
    )


class RfqDetail(RfqHeader):
    base_currency: str
    lines: list[RfqLineView]
    invites: list[InviteView]


class RfqCreated(RfqDetail):
    questions: list[str] = Field(description="What the buyer should clarify before sending")
    currency_source: CurrencySource | None = Field(
        default=None, description="Where base_currency came from"
    )


class UpdateLineRequest(BaseModel):
    """Only the fields present are changed; `null` clears an optional field."""

    model_config = ConfigDict(extra="forbid")

    description: ShortText | None = None
    part_number: str | None = None
    mpn: str | None = None
    manufacturer: str | None = None
    category: LineCategory | None = None
    quantities: list[PositiveQuantity] | None = Field(default=None, min_length=1, max_length=20)
    unit: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)] | None = None
    need_by: date | None = None
    incoterm: str | None = None
    delivery_location: str | None = None
    qualification: str | None = None


class AddLineRequest(BaseModel):
    """A new line at the end of a draft RFQ."""

    model_config = ConfigDict(extra="forbid")

    description: ShortText
    part_number: str | None = None
    mpn: str | None = None
    manufacturer: str | None = None
    category: LineCategory = "catalog"
    quantities: list[PositiveQuantity] = Field(min_length=1, max_length=20)
    unit: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)] = "pcs"
    need_by: date | None = None
    incoterm: str | None = None
    delivery_location: str | None = None
    qualification: str | None = None


class UpdateRfqRequest(BaseModel):
    """RFQ-wide changes to a draft. `incoterm`, `delivery_location` and `need_by` are set
    on every line; only the fields present change, and `null` clears an optional one."""

    model_config = ConfigDict(extra="forbid")

    title: ShortText | None = None
    response_due: date | None = None
    payment_terms: PaymentTerms | None = None
    quote_validity_days: QuoteValidityDays | None = None
    incoterm: str | None = None
    delivery_location: str | None = None
    need_by: date | None = None
    # The RFQ's base currency; its negotiation limits are converted along with it.
    currency: CurrencyCode | None = None
    # The document's branding: a logo from the company library (`/v1/company/logos`)
    # and terms replacing the company's default footer; null = back to the default.
    logo_asset_id: UUID | None = None
    footer_override: (
        Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)] | None
    ) = None
    # Where the logo sits in its box on this RFQ's document; null = the company default.
    logo_placement: LogoPlacement | None = None


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
    # The same email as HTML (the item table as a real table), when the proposal
    # has one: the approver previews what the supplier's mail client shows.
    html_body: str | None = None
    # File name of the RFQ document PDF attached to this email, when there is
    # one (`GET /v1/rfqs/{rfq_id}/invites/{invite_id}/document.pdf` renders it).
    attachment_name: str | None = None
    # The RFQ's lines whose quantities rest on assumptions (low or medium confidence),
    # to highlight before approving. Internal: never part of the email.
    line_assumptions: list[RfqLineReview] = Field(default_factory=list[RfqLineReview])


class NegotiationEmailDraft(BaseModel):
    """The exact counter-offer or best-and-final email an approver approves."""

    action: Literal["counter", "bafo"]
    round: int
    invite_id: UUID
    rfq_id: UUID | None
    rfq_ref: str | None
    supplier_name: str | None
    sender_user_id: UUID
    to: list[str]
    cc: list[str]
    subject: str
    body: str


class MandateLineView(BaseModel):
    rfq_line_id: UUID
    position: int
    description: str
    target_unit_price: Decimal
    max_unit_price: Decimal
    max_lead_time_days: int | None


class MandateSupplierView(BaseModel):
    invite_id: UUID
    supplier_name: str
    to: list[str]


class MandateApprovalView(BaseModel):
    """What approving a negotiation mandate covers, to review before deciding."""

    rfq_id: UUID
    rfq_ref: str
    rfq_title: str | None
    currency: str
    deadline: datetime
    max_rounds: int
    max_messages_per_supplier_per_day: int
    initial_emails: int
    suppliers: list[MandateSupplierView]
    lines: list[MandateLineView]


class ApprovalQueueItem(BaseModel):
    id: UUID
    tool: str
    status: ApprovalStatus
    requested_by: UUID
    requested_by_me: bool
    # The address the requester's emails go out from (their active connected
    # mailbox), or None while they have none: sends then wait for a connection.
    sender_email: str | None = None
    created_at: datetime
    decided_at: datetime | None
    # Set for `send_rfq_email` and `send_followup_email`; other tools show only their name.
    rfq_email: RfqEmailDraft | None
    # Set for `send_negotiation_email`: a counter-offer or best-and-final request.
    negotiation_email: NegotiationEmailDraft | None = None
    # Set for `award_rfq`: the exact lines, suppliers and prices to award.
    award: AwardApprovalView | None = None
    # Set for `send_purchase_order` and `send_award_notice`: the exact email.
    thread_email: ThreadEmailApprovalView | None = None
    # For pending acts that send email (including a mandate's suppliers): each
    # recipient, whether its domain receives mail (a real company), whether it
    # is confirmed, and whether it would go out (outbound guard).
    recipient_checks: list[RecipientCheck] | None = None
    # Set for `approve_negotiation_mandate`: suppliers, addresses, per-line limits.
    mandate: MandateApprovalView | None = None


ActivityKind = Literal["agent", "email", "user", "system"]


class RfqActivityItem(BaseModel):
    """One event in an RFQ's activity feed: ids and a code-written summary, never content."""

    at: datetime
    kind: ActivityKind = Field(
        description="agent: Bido decided; email: something was sent or received; "
        "user: a person acted; system: bookkeeping"
    )
    action: str = Field(
        description="Stable code, e.g. rfq.created, rfq.line_updated, rfq.invite_added, "
        "rfq.email_sent, approval.approved, reply"
    )
    actor: str | None = Field(
        default=None, description="Who acted: a person's name, 'Bido', or the supplier"
    )
    supplier_name: str | None = None
    summary: str = Field(description="One short English sentence written by code")
