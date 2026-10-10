"""The copilot's tools: typed functions over our services, bound to one request.

Every tool reads the tenant and the user from the run's `ToolContext`, never
from the model's arguments, and works in that tenant's RLS-scoped session.

- `read` tools answer questions: RFQs and their status, who responded, what is
  missing, proposals, the comparison, quoted price history, supplier
  suggestions, the RFQ template, and the company's imported data: its
  suppliers with their contacts and what was bought from each
  (`list_suppliers`) and the prices it paid (`purchase_history`). That data
  belongs to the company, not to a conversation: these work in any chat.
- `draft` tools change only drafts or the tenant's own settings: run a
  purchase analysis (stored, from the imported BOM, stock and purchase
  history), create an RFQ (draft, from text or from an analysis), invite
  suppliers (draft invites), register a supplier's quote the buyer attached
  (stored as an emailed reply would be, `bido_back.quotes.upload`), update the
  RFQ template, prepare sends, and propose counter-offers. None of them sends
  anything: each records one
  pending approval per email (`rfqs.service.prepare_send`,
  `negotiation.service.evaluate_rfq`), which a person decides on (by default
  the buyer themself; a second person in four-eyes tenants); the worker sends
  only after that. The prices in a counter-offer are decided by the
  negotiation strategy, never by this model.
- `prepare_award` asks for approval to award an RFQ (`awards.service`); the
  award and its purchase orders exist only after a person approves it (the
  buyer by default; a second person in four-eyes tenants).

There is no `act` tool here on purpose: the only thing the copilot can make
happen outside Bido is an email, and that already goes through the approval
guard, with the email written by a vetted writer agent rather than by this
model.
"""

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Final, Literal, cast
from uuid import UUID

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.agents.catalog.contracts import LineCategory, ModelDecimal, RfqIntakeOutput
from bido_back.agents.context import ToolContext
from bido_back.agents.tools import AnyTool, Tool, ToolError
from bido_back.auth import Principal
from bido_back.awards import service as award_service
from bido_back.awards.schemas import AwardLine, AwardRequest, AwardSelection, PurchaseOrderSummary
from bido_back.comparison.compute import Recommendation, SupplierColumn
from bido_back.comparison.loader import load_comparison
from bido_back.comparison.policy import ComparisonPolicy
from bido_back.copilot import proposals, queries
from bido_back.copilot.followup_tools import (
    SupplierFollowupOut,
    followup_tools,
    missing_items,
    supplier_followups,
)
from bido_back.copilot.journey_tools import journey_tools, reply_by_explained
from bido_back.copilot.order_tools import order_tools
from bido_back.copilot.proposals import (
    PROPOSAL_ARG,
    AwaitingConfirmation,
    ProposalPreview,
    ProposedAwardLine,
    ProposedLine,
    ProposedSupplier,
    QuotedLinePreview,
)
from bido_back.db import tenant_session
from bido_back.db.models import Analysis, Contact, Invite, Rfq, RfqLine, RfqTemplate, Supplier
from bido_back.negotiation import service as negotiation_service
from bido_back.negotiation.agents import NegotiationWriter, default_negotiation_writer
from bido_back.negotiation.strategy import plain
from bido_back.onboarding.standard import STANDARD_TEMPLATE
from bido_back.purchasing import service as purchasing_service
from bido_back.purchasing.schemas import (
    AnalysisRequest,
    CalculationView,
    CurrencyTotalView,
    RfqFromAnalysisRequest,
)
from bido_back.purchasing.sources.mapping import Mapper
from bido_back.purchasing.workbook import SheetMapper
from bido_back.quotes import upload as quote_upload
from bido_back.quotes.state import restore_quote, store_quote
from bido_back.quotes.upload import QuoteExtractor, default_quote_extractor
from bido_back.quotes.validation import ValidatedQuote
from bido_back.rfqs import service as rfq_service
from bido_back.rfqs.agents import RfqAgents
from bido_back.rfqs.currency import CurrencyChange, known_currency
from bido_back.rfqs.errors import (
    Duplicate,
    MailboxNotConfigured,
    MailboxNotConnected,
    ReplyByRequired,
    RfqError,
)
from bido_back.rfqs.language import DEFAULT_LANGUAGE, email_language
from bido_back.rfqs.mailbox import MailboxSenderFactory
from bido_back.rfqs.schemas import (
    AddLineRequest,
    CurrencySource,
    RfqDetail,
    UpdateLineRequest,
    UpdateRfqRequest,
)
from bido_back.storage import BlobStorage
from bido_back.templates import service as template_service
from bido_back.templates.schemas import TemplateEdit
from bido_back.templates.service import RequiredFieldsRemoved

logger = logging.getLogger(__name__)

Db = AsyncEngine | async_sessionmaker[AsyncSession]

MAX_LIST: Final = 20
MAX_TEMPLATE_ITEMS: Final = 16
MAX_ANALYSIS_SUPPLIERS: Final = 4
RfqStatus = Literal["draft", "open", "closed", "cancelled"]


@dataclass(frozen=True, slots=True)
class CopilotToolDeps:
    db: Db
    agents: RfqAgents
    mailboxes: MailboxSenderFactory
    # Text of this message's attachments, for create_rfq. Empty without any.
    attachments_text: str = ""
    # Writes counter-offers for propose_counteroffers; None = the default agent.
    negotiation_writer: NegotiationWriter | None = None
    # This message's attachments (upload ids) and where their bytes live, for
    # import_spreadsheet; the sheet mapper reads sheets the rules cannot.
    attachment_ids: tuple[UUID, ...] = ()
    # The buyer sent files without writing anything: the turn reads them, and
    # import_spreadsheet refuses (storing data is asked for in the buyer's words).
    files_only: bool = False
    # Files attached in earlier turns of the same conversation, read from its
    # stored runs on the server (never from the request), so a confirmation
    # sent as text alone can still import the file it confirms.
    earlier_attachment_ids: tuple[UUID, ...] = ()
    # Reads the text of those earlier files, for create_rfq when this message
    # has none. Only called when needed; None = there is nothing to read.
    earlier_attachments_text: Callable[[], Awaitable[str]] | None = None
    # What the buyer wrote in this conversation, oldest first, for create_rfq: the
    # intake agent takes part numbers and terms from these, not from the summary.
    # Only called when needed; None = only the tool's text.
    buyer_messages: Callable[[], Awaitable[list[str]]] | None = None
    storage: BlobStorage | None = None
    sheet_mapper: SheetMapper | None = None
    source_mapper: Mapper | None = None
    # Reads a quote the buyer attached, for register_quote; None = the default agent,
    # the same `quote_extraction` the email reply path runs.
    quote_extractor: QuoteExtractor | None = None
    today: Callable[[], date] = lambda: datetime.now(UTC).date()
    # The conversation, for the gated tools: a proposal made in it can only be
    # confirmed in it, from a later run (`bido_back.copilot.proposals`).
    conversation_id: UUID | None = None


def _principal(ctx: ToolContext) -> Principal:
    if ctx.user_id is None:
        raise ToolError("the copilot needs a signed-in user")
    return Principal(tenant_id=ctx.tenant_id, user_id=ctx.user_id)


async def _rfq(session: AsyncSession, reference: str) -> Rfq:
    rfq = await queries.find_rfq(session, reference)
    if rfq is None:
        raise ToolError(f"no RFQ {reference!r}; use list_rfqs to find it")
    return rfq


def _service_error(exc: RfqError) -> ToolError:
    if isinstance(exc, ReplyByRequired):
        return ToolError(reply_by_explained(exc))
    if isinstance(exc, MailboxNotConnected):
        return ToolError("the buyer has not connected a mailbox; emails are sent from it")
    if isinstance(exc, MailboxNotConfigured):
        return ToolError("sending from a mailbox is not set up on this server yet")
    return ToolError(str(exc))


class RfqRef(BaseModel):
    rfq: str = Field(description="The RFQ's reference (e.g. RFQ-12) or id")


# --- list_rfqs ---------------------------------------------------------------------------


RFQ_STATUS_HINT: Final = (
    "The RFQ lifecycle: `draft` = being prepared, not sent to any supplier yet; "
    "`open` = sent to suppliers, quotes pending or under negotiation; "
    "`closed` = awarded or otherwise finished; `cancelled`. "
    "When the buyer says open, active, ongoing, in progress or pending, they mean "
    "everything not finished (draft AND open), so leave `status` empty and report drafts "
    "and sent RFQs separately; only filter by `open` when they clearly mean RFQs already "
    "sent to suppliers."
)


class ListRfqsIn(BaseModel):
    status: RfqStatus | None = Field(
        default=None, description="Only RFQs in this status. " + RFQ_STATUS_HINT
    )
    limit: int = Field(default=10, ge=1, le=MAX_LIST)


class RfqItem(BaseModel):
    id: UUID
    ref: str
    title: str
    status: str
    created_at: datetime
    response_due: date | None


class ListRfqsOut(BaseModel):
    rfqs: list[RfqItem]
    by_status: dict[str, int] = Field(
        description=(
            "How many RFQs the company has in each status, counted over ALL its RFQs "
            "(ignoring `status` and `limit`). An empty `rfqs` with drafts counted here "
            "means the filter excluded them, not that the buyer has nothing in progress."
        )
    )


# --- get_rfq_status / who_responded -------------------------------------------------------


class LineItem(BaseModel):
    position: int
    description: str
    # The buyer's own code: show it as the line's reference. `mpn` is the manufacturer's.
    part_number: str | None
    mpn: str | None
    quantities: list[Decimal]
    unit: str
    need_by: date | None
    incoterm: str | None
    delivery_location: str | None


class InviteItem(BaseModel):
    supplier_id: UUID
    supplier_name: str
    status: str
    sent_at: datetime | None
    responded_at: datetime | None


class RfqStatusOut(BaseModel):
    id: UUID
    ref: str
    title: str
    status: str
    response_due: date | None
    payment_terms: str | None
    quote_validity_days: int | None
    lines: list[LineItem]
    invites: list[InviteItem]
    invites_by_status: dict[str, int]


class WhoRespondedOut(BaseModel):
    ref: str
    responded: list[InviteItem]
    not_responded: list[InviteItem]


# --- what_is_missing ---------------------------------------------------------------------


class SupplierGap(BaseModel):
    supplier_name: str
    # What the offer lacks, by the rules Bido's clarification emails follow: asked and
    # waiting, or not asked yet (`followups.missing`), never the model's own list.
    missing_fields: list[str]
    lines_needing_review: int


class WhatIsMissingOut(BaseModel):
    ref: str
    awaiting_suppliers: list[InviteItem]
    lines_without_quote: list[LineItem]
    proposal_gaps: list[SupplierGap]
    # Every invited supplier: what Bido waits for, or what the buyer must do.
    suppliers: list[SupplierFollowupOut] = Field(default_factory=list[SupplierFollowupOut])


# --- list_proposals ----------------------------------------------------------------------


class ProposalLineItem(BaseModel):
    rfq_line_position: int | None
    offered_mpn: str | None
    is_alternative: bool
    min_quantity: Decimal | None
    unit_price: Decimal | None
    unit: str | None
    tooling_cost: Decimal | None
    moq: Decimal | None
    lead_time_days: int | None
    needs_review: bool


class ProposalItem(BaseModel):
    supplier_name: str
    version: int
    received_at: datetime
    currency: str | None
    valid_until: date | None
    incoterm: str | None
    payment_terms: str | None
    missing_fields: list[str]
    lines: list[ProposalLineItem]


class ListProposalsOut(BaseModel):
    ref: str
    proposals: list[ProposalItem]


# --- get_comparison ----------------------------------------------------------------------


class TierTotalItem(BaseModel):
    tier: int
    total: Decimal | None
    complete: bool


class ComparisonSupplier(BaseModel):
    supplier_name: str
    currency: str | None
    valid_until: date | None
    incoterm: str | None
    max_lead_time_days: int | None
    flags: list[str]
    low_confidence_fields: list[str]
    totals: list[TierTotalItem]


class CheapestTier(BaseModel):
    tier: int
    supplier_name: str
    total: Decimal


class ComparisonOut(BaseModel):
    ref: str
    base_currency: str
    computed_on: date
    suppliers: list[ComparisonSupplier]
    cheapest_complete_by_tier: list[CheapestTier]
    awaiting: list[str]
    recommendations: list[Recommendation] = Field(default_factory=list[Recommendation])
    policy: ComparisonPolicy = Field(default_factory=ComparisonPolicy)
    readiness: str = "needs_review"
    covered_line_tiers: int = 0
    requested_line_tiers: int = 0


# --- price_history / suggest_suppliers ------------------------------------------------------


class PriceHistoryIn(BaseModel):
    mpn: str = Field(min_length=2, description="Manufacturer part number")
    limit: int = Field(default=10, ge=1, le=MAX_LIST)


class PricePointItem(BaseModel):
    rfq_ref: str
    supplier_name: str
    quoted_at: datetime
    offered_mpn: str | None
    min_quantity: Decimal | None
    unit_price: Decimal
    currency: str | None
    unit: str | None
    lead_time_days: int | None


class PriceHistoryOut(BaseModel):
    mpn: str
    # Quoted prices from RFQs; what was paid is purchase_history.
    points: list[PricePointItem]


# --- list_suppliers / purchase_history (the company's imported data) -----------------------


class ListSuppliersIn(BaseModel):
    supplier: str | None = Field(default=None, description="Part of the supplier's name")
    category: str | None = Field(
        default=None, description="Only suppliers the company bought this part category from"
    )
    part_number: str | None = Field(
        default=None, description="Only suppliers the company bought this part (or MPN) from"
    )
    limit: int = Field(default=MAX_LIST, ge=1, le=MAX_LIST)


class SupplierContactItem(BaseModel):
    name: str | None
    email: str


class PartBought(BaseModel):
    part_number: str
    description: str


class SupplierEntryItem(BaseModel):
    supplier_id: UUID
    name: str
    type: str
    country: str | None
    contacts: list[SupplierContactItem]
    # From the imported purchase history.
    purchase_orders: int
    last_ordered_at: date | None
    categories: list[str]
    # What was bought from it (at most 20), to tell its part families apart.
    parts: list[PartBought]
    # From RFQs sent through Bido.
    rfq_invites: int
    rfq_responses: int


class ListSuppliersOut(BaseModel):
    total: int
    suppliers: list[SupplierEntryItem]


class PurchaseHistoryIn(BaseModel):
    part_numbers: list[str] = Field(
        default_factory=list[str],
        max_length=MAX_LIST,
        description="Internal part numbers or MPNs; empty = the parts bought most recently",
    )
    supplier: str | None = Field(default=None, description="Part of a supplier's name")
    limit: int = Field(default=10, ge=1, le=MAX_LIST, description="Parts to return")


class PaidLineItem(BaseModel):
    supplier_name: str
    po_number: str
    ordered_at: date
    quantity: Decimal
    unit_price: Decimal
    currency: str


class PaidPartItem(BaseModel):
    part_number: str
    description: str
    mpn: str | None
    category: str | None
    unit: str
    last_paid: PaidLineItem
    # Every purchase of the part, newest first (at most 10).
    purchases: list[PaidLineItem]
    purchase_count: int


class PurchaseHistoryOut(BaseModel):
    # Part numbers asked for that the purchase history does not have.
    not_found: list[str]
    parts: list[PaidPartItem]


class SuggestSuppliersIn(BaseModel):
    mpn: str | None = Field(default=None, description="Part number to find past suppliers for")
    category: str | None = Field(default=None, description="Line category, e.g. catalog")
    limit: int = Field(default=5, ge=1, le=MAX_LIST)


class SupplierSuggestion(BaseModel):
    supplier_id: UUID
    name: str
    type: str
    country: str | None
    invited: int
    responded: int
    last_response: datetime | None


class SuggestSuppliersOut(BaseModel):
    # False: nothing matched the part or category, these are all suppliers.
    matched_history: bool
    suppliers: list[SupplierSuggestion]


# --- templates ---------------------------------------------------------------------------


class NoArgs(BaseModel):
    pass


class TemplateOut(BaseModel):
    exists: bool
    template_id: UUID | None = None
    language: str | None = None
    greeting: str | None = None
    columns: list[str] = Field(default_factory=list[str])
    footer: str | None = None
    fields: list[str] = Field(default_factory=list[str])


class UpdateTemplateIn(BaseModel):
    greeting: str | None = Field(default=None, max_length=500)
    columns: list[str] | None = Field(default=None, max_length=MAX_TEMPLATE_ITEMS)
    footer_fields: list[str] | None = Field(default=None, max_length=MAX_TEMPLATE_ITEMS)
    fields: list[str] | None = Field(default=None, max_length=MAX_TEMPLATE_ITEMS)


# --- create_rfq / invite_suppliers / prepare_send ------------------------------------------

_CURRENCY_ARG = (
    "ISO 4217 code (BRL, EUR, USD...), only when the buyer said which currency the RFQ is in "
    "('em reais' = BRL). Never guess it: left empty, Bido takes it from the buyer's data"
)
_CURRENCY_RESULT = (
    " The result has the RFQ's `currency` and `currency_source`: stated (the buyer said it), "
    "analysis (the parts were bought in it) or purchase_history (most of the company's "
    "purchases are in it). Always tell the buyer the currency and why, e.g. 'em BRL, a moeda "
    "das suas compras'; set_rfq_terms(currency=...) changes it while the RFQ is a draft. If "
    "the tool says the currency is unknown, ask the buyer which currency and call it again "
    "with `currency`."
)


class CreateRfqIn(BaseModel):
    text: str = Field(
        min_length=1,
        max_length=8_000,
        description=(
            "What to quote: items, quantities, dates, Incoterm and delivery location, in the "
            "buyer's language (the RFQ's title is written from it). Copy the buyer's part "
            "numbers exactly as they wrote them; never turn them into MPNs. When the buyer "
            "gives a manufacturer part number as such, say so ('MPN MT-120-ASM'): the "
            "preview's lines show it as `mpn`, apart from `part_number`"
        ),
    )
    pending_validation: str | None = Field(
        default=None,
        max_length=200,
        description=(
            "What the RFQ still waits for, when the buyer or the data says it waits for "
            "someone's sign-off (e.g. 'engineering validation of the drawing'). Marks the RFQ "
            "PRELIMINARY: it cannot be prepared for sending until the buyer confirms and the "
            "mark is removed with set_rfq_terms(title=...). For an RFQ from an assessment or "
            "an analysis, pass it to create_rfq_from_assessment / create_rfq_from_analysis "
            "instead: they keep the quantities and units"
        ),
    )
    include_attachments: bool = Field(
        default=True,
        description=(
            "Also read the files attached to this message (or, when it has none, "
            "the ones attached earlier in this conversation)"
        ),
    )
    currency: str | None = Field(default=None, description=_CURRENCY_ARG)
    proposal_id: UUID | None = Field(default=None, description=PROPOSAL_ARG)


class CreateRfqOut(AwaitingConfirmation):
    # Set once the RFQ exists (not while awaiting the buyer's confirmation).
    id: UUID | None = None
    ref: str | None = None
    title: str | None = None
    status: str | None = None
    # The RFQ's currency (offers are compared in it) and where it came from.
    currency: str | None = None
    currency_source: CurrencySource | None = None
    lines: list[LineItem] = Field(default_factory=list[LineItem])
    questions: list[str] = Field(default_factory=list[str])


# --- editing a draft RFQ ---------------------------------------------------------------

_DRAFT_ONLY = (
    " Only while the RFQ is a draft and no email has been prepared from it; after that the "
    "lines are frozen."
)
_LINE_FIELDS = Literal[
    "part_number",
    "mpn",
    "manufacturer",
    "need_by",
    "incoterm",
    "delivery_location",
    "qualification",
]


class UpdateRfqLineIn(BaseModel):
    rfq: str = Field(description="The RFQ's reference (e.g. RFQ-12) or id")
    position: int = Field(ge=1, description="The line's position, as shown")
    description: str | None = Field(default=None, min_length=1, max_length=500)
    part_number: str | None = Field(
        default=None, description="The buyer's own code for the part, exactly as written"
    )
    mpn: str | None = Field(default=None, description="A manufacturer part number, as given")
    manufacturer: str | None = None
    category: LineCategory | None = None
    quantities: list[ModelDecimal] | None = Field(
        default=None, min_length=1, max_length=20, description="All quantity tiers, replaced"
    )
    unit: str | None = Field(default=None, min_length=1)
    need_by: date | None = None
    incoterm: str | None = Field(default=None, description="The rule only: DAP, FCA, EXW...")
    delivery_location: str | None = None
    qualification: str | None = None
    clear: list[_LINE_FIELDS] = Field(
        default_factory=list[_LINE_FIELDS], description="Optional fields to empty"
    )


class AddRfqLineIn(BaseModel):
    rfq: str = Field(description="The RFQ's reference (e.g. RFQ-12) or id")
    description: str = Field(min_length=1, max_length=500)
    quantities: list[ModelDecimal] = Field(min_length=1, max_length=20)
    unit: str = Field(default="pcs", min_length=1)
    part_number: str | None = Field(
        default=None, description="The buyer's own code for the part, exactly as written"
    )
    mpn: str | None = Field(default=None, description="A manufacturer part number, as given")
    manufacturer: str | None = None
    category: LineCategory = "catalog"
    need_by: date | None = None
    incoterm: str | None = None
    delivery_location: str | None = None
    qualification: str | None = None


class RemoveRfqLineIn(BaseModel):
    rfq: str = Field(description="The RFQ's reference (e.g. RFQ-12) or id")
    position: int = Field(ge=1, description="The line's position, as shown")


_TERMS_FIELDS = Literal[
    "incoterm",
    "delivery_location",
    "need_by",
    "response_due",
    "payment_terms",
    "quote_validity_days",
]


class SetRfqTermsIn(BaseModel):
    rfq: str = Field(description="The RFQ's reference (e.g. RFQ-12) or id")
    currency: str | None = Field(
        default=None,
        description=(
            "The RFQ's currency, ISO 4217 (BRL, EUR, USD...), when the buyer asks to change it; "
            "negotiation limits already set are converted at the ECB reference rate"
        ),
    )
    incoterm: str | None = Field(
        default=None, description="For every line. The rule only: DAP, FCA, EXW..."
    )
    delivery_location: str | None = Field(default=None, description="For every line")
    need_by: date | None = Field(default=None, description="For every line")
    response_due: date | None = Field(default=None, description="When suppliers must answer")
    title: str | None = Field(
        default=None,
        min_length=1,
        max_length=500,
        description="The RFQ's title, in the buyer's language (the conversation's)",
    )
    payment_terms: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description=(
            "The buyer's payment terms for suppliers, exactly as they wrote them "
            "(e.g. '28 DDL', '30 days net'). Stated in the RFQ email"
        ),
    )
    quote_validity_days: int | None = Field(
        default=None,
        ge=1,
        le=365,
        description="How many days suppliers' quotes must stay valid, at least (1-365)",
    )
    clear: list[_TERMS_FIELDS] = Field(
        default_factory=list[_TERMS_FIELDS], description="Optional terms to empty"
    )


class EditedRfqOut(BaseModel):
    id: UUID
    ref: str
    title: str
    status: str
    response_due: date | None
    currency: str
    # What is stored now: report these, not what was asked for.
    payment_terms: str | None
    quote_validity_days: int | None
    lines: list[LineItem]
    # What a currency change did to the negotiation limits; None when nothing to tell.
    note: str | None = None


def _request[M: BaseModel](model: type[M], fields: dict[str, object]) -> M:
    """The service's request from tool arguments; a refusal names the fields, not the values."""
    try:
        return model.model_validate(fields)
    except ValidationError as exc:
        where = ", ".join(".".join(str(p) for p in error["loc"]) for error in exc.errors())
        raise ToolError(f"invalid value for: {where}") from None


def _currency_note(change: CurrencyChange | None) -> str | None:
    if change is None or not change.converted_lines:
        return None
    positions = ", ".join(str(p) for p in change.converted_lines)
    when = f" of {change.rate_date.isoformat()}" if change.rate_date else ""
    return (
        f"the negotiation limits of line(s) {positions} were converted from {change.old} to "
        f"{change.new} at the ECB reference rate{when}; tell the buyer"
    )


def _edited(detail: RfqDetail, change: CurrencyChange | None = None) -> EditedRfqOut:
    return EditedRfqOut(
        id=detail.id,
        ref=detail.ref,
        title=detail.title,
        status=detail.status,
        response_due=detail.response_due,
        currency=detail.base_currency,
        note=_currency_note(change),
        payment_terms=detail.payment_terms,
        quote_validity_days=detail.quote_validity_days,
        lines=[
            LineItem(
                position=line.position,
                description=line.description,
                part_number=line.part_number,
                mpn=line.mpn,
                quantities=list(line.quantities),
                unit=line.unit,
                need_by=line.need_by,
                incoterm=line.incoterm,
                delivery_location=line.delivery_location,
            )
            for line in detail.lines
        ],
    )


class InviteSuppliersIn(BaseModel):
    rfq: str = Field(description="The RFQ's reference (e.g. RFQ-12) or id")
    supplier_ids: list[UUID] = Field(min_length=1, max_length=10)
    proposal_id: UUID | None = Field(default=None, description=PROPOSAL_ARG)


class InviteResult(BaseModel):
    supplier_id: UUID
    ok: bool
    detail: str


class InviteSuppliersOut(AwaitingConfirmation):
    ref: str
    results: list[InviteResult] = Field(default_factory=list[InviteResult])


class RegisterQuoteIn(BaseModel):
    rfq: str = Field(description="The RFQ's reference (e.g. RFQ-12) or id")
    supplier: str = Field(
        description=(
            "The invited supplier the quote comes from: its name as get_rfq_status shows it, "
            "or its supplier id or invite id"
        )
    )
    upload_ids: list[UUID] = Field(
        min_length=1,
        max_length=quote_upload.MAX_FILES,
        description=(
            "The attached file(s) holding the quote: ids from attachment_ids or "
            "earlier_attachment_ids"
        ),
    )
    proposal_id: UUID | None = Field(default=None, description=PROPOSAL_ARG)


class RegisterQuoteOut(AwaitingConfirmation):
    ref: str
    supplier_name: str
    version: int | None = Field(
        default=None, description="The stored proposal's version, once registered"
    )
    needs_review: bool = False
    issues: list[str] = Field(
        default_factory=list[str],
        description="What a person must check: missing values, lines the RFQ does not have, "
        "values the file does not back. The quote is stored with them marked for review",
    )


class PreparedApproval(BaseModel):
    invite_id: UUID
    approval_id: UUID


class PrepareSendOut(BaseModel):
    ref: str
    approvals: list[PreparedApproval]
    note: str


class NegotiationLineOut(BaseModel):
    position: int
    decision: str
    offered_unit_price: Decimal | None
    ask_unit_price: Decimal | None
    lead_time_days: int | None
    # price: in_target, ask_met, in_range, above_max, no_target; delivery: on_time,
    # no_need_by, unknown, late, still_late, need_by_unreachable.
    price: str | None = None
    delivery: str | None = None
    # The date Bido asks the supplier to deliver by, the date needed, and the supplier's
    # expected delivery if ordered today.
    deliver_by: date | None = None
    required_by: date | None = None
    expected_delivery: date | None = None
    reasons: list[str] = Field(default_factory=list[str])


class NegotiationEmailOut(BaseModel):
    """A negotiation email Bido decided for the supplier, in its exact state."""

    kind: str  # revised_offer, delivery_date, bafo, reminder
    round: int
    state: str  # decided, proposed, sent, canceled, rejected, held
    reply_by: date | None = None
    cutoff_at: datetime | None = None
    held_reason: str | None = None


class NegotiationSupplierOut(BaseModel):
    supplier_name: str
    # open, countered / bafo_requested (only once that email was sent), accepted,
    # rejected, closed.
    status: str
    rounds_used: int
    lines: list[NegotiationLineOut]
    # Where the supplier stands, in one sentence written by code: waiting for the
    # supplier (until when), waiting (daily limit, mailbox), held for your approval,
    # accepted internally (nothing to ask), needs your decision (why), needs your
    # answer (a supplier question), closed.
    state: str = ""
    state_text: str = ""
    reason: str = ""
    waiting_until: datetime | None = None
    next_action: str = ""
    warnings: list[str] = Field(default_factory=list[str])
    emails: list[NegotiationEmailOut] = Field(default_factory=list[NegotiationEmailOut])


class NegotiationStatusOut(BaseModel):
    ref: str
    status: str
    deadline: datetime | None
    max_rounds: int
    base_currency: str
    suppliers: list[NegotiationSupplierOut]
    note: str
    # The one best-and-final cut-off for every supplier, once the final round started.
    cutoff_at: datetime | None = None
    warnings: list[str] = Field(default_factory=list[str])


class ProposeCounteroffersOut(BaseModel):
    ref: str
    proposed: int
    approval_ids: list[UUID]
    note: str


# --- Award and purchase orders -------------------------------------------------------------


class AwardChoice(BaseModel):
    line_position: int = Field(description="The RFQ line's position (1, 2, ...)")
    supplier: str = Field(description="The supplier's name, as in get_comparison")
    tier: int = Field(default=0, description="Quantity tier index; 0 = the first quantity")


class PrepareAwardIn(BaseModel):
    rfq: str = Field(description="The RFQ's reference (e.g. RFQ-12) or id")
    choices: list[AwardChoice] = Field(
        default_factory=list[AwardChoice],
        description="Only when the buyer picks suppliers; empty = the recommendation",
    )
    reason: str | None = Field(
        default=None,
        description=(
            "Why, when the choice differs from the recommendation or breaks the approved "
            "limits: the buyer's words"
        ),
    )
    proposal_id: UUID | None = Field(
        default=None,
        description="From the preview: only after the buyer confirmed it in a later message",
    )


class AwardedLineItem(BaseModel):
    position: int
    description: str
    supplier_name: str
    quantity: Decimal
    unit: str
    unit_price: Decimal
    currency: str


class PoItem(BaseModel):
    ref: str
    rfq_ref: str
    status: str
    supplier_name: str
    total: Decimal
    currency: str


class AwardStatusOut(BaseModel):
    ref: str
    state: Literal["none", "pending_approval", "awarded"]
    differs_from_recommendation: bool | None
    reason: str | None
    lines: list[AwardedLineItem]
    purchase_orders: list[PoItem]


class PrepareAwardOut(AwaitingConfirmation):
    ref: str
    # Set once confirmed: the award waiting in the approvals queue.
    approval_id: UUID | None = None
    differs_from_recommendation: bool
    lines: list[AwardedLineItem]
    note: str


class ListOpenPosIn(BaseModel):
    include_sent: bool = Field(default=False, description="Also list POs already sent")


class ListOpenPosOut(BaseModel):
    purchase_orders: list[PoItem]


# --- Handlers ------------------------------------------------------------------------------


async def _latest_open_analysis(db: Db, principal: Principal) -> UUID | None:
    """The buyer's newest analysis from the last 24 hours that has no RFQ yet."""
    since = datetime.now(UTC) - timedelta(hours=24)
    async with tenant_session(db, principal.tenant_id) as session:
        return await session.scalar(
            select(Analysis.id)
            .where(
                Analysis.created_by == principal.user_id,
                Analysis.rfq_id.is_(None),
                Analysis.created_at >= since,
            )
            .order_by(Analysis.created_at.desc())
            .limit(1)
        )


# --- run_purchase_analysis / create_rfq_from_analysis ---------------------------------------


class PurchaseAnalysisIn(BaseModel):
    product: str = Field(
        min_length=1,
        max_length=200,
        description="The product to build, as named in the BOM (e.g. 'Motor EV-120' or 'motor')",
    )
    quantity: int | None = Field(
        default=None,
        ge=1,
        le=1_000_000,
        description="Units to build; omit to use the production plan from today on",
    )
    safety_stock_pct: int = Field(default=0, ge=0, le=100, description="Extra % to buy")
    need_by: date | None = None
    planning_start: date | None = None
    planning_end: date | None = None
    location: str | None = Field(default=None, max_length=200)
    stock_as_of: date | None = None
    max_stock_age_days: int | None = Field(default=None, ge=0, le=1095)


class AnalysisSupplierItem(BaseModel):
    supplier_id: UUID
    name: str
    type: str
    last_price: Decimal
    last_ordered_at: date
    avg_price: Decimal
    on_time_rate: Decimal | None
    quality_rate: Decimal | None
    suggested: bool
    delivery_observations: int = 0
    quality_observations: int = 0
    last_received_at: date | None = None


class AnalysisFlagItem(BaseModel):
    code: str
    supplier: str | None = None
    value: Decimal | None = None


class AnalysisLineItem(BaseModel):
    part_number: str
    description: str
    mpn: str | None
    unit: str
    required: Decimal
    on_hand: Decimal
    on_order: Decimal
    to_buy: Decimal
    need_by: date | None
    currency: str | None
    last_price: Decimal | None
    avg_price: Decimal | None
    trend_pct: Decimal | None
    target_price: Decimal | None
    max_price: Decimal | None
    suppliers: list[AnalysisSupplierItem]
    flags: list[AnalysisFlagItem]
    calculation: CalculationView = Field(default_factory=CalculationView)


class PurchaseAnalysisOut(BaseModel):
    analysis_id: UUID
    product: str
    quantity: Decimal
    need_by: date | None
    items: int
    items_to_buy: int
    # Set when every part to buy is in one currency; otherwise use `totals`.
    currency: str | None
    total_at_target: Decimal | None
    total_at_last_price: Decimal | None
    # Spend per currency; never add amounts in different currencies together.
    totals: list[CurrencyTotalView]
    lines: list[AnalysisLineItem]
    planning_start: date | None = None
    planning_end: date | None = None
    location: str | None = None
    stock_as_of: date | None = None
    max_stock_age_days: int | None = None
    demand_source: str = "explicit_quantity"


class RfqFromAnalysisIn(BaseModel):
    analysis_id: UUID = Field(description="The analysis_id returned by run_purchase_analysis")
    currency: str | None = Field(default=None, description=_CURRENCY_ARG)
    pending_validation: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description=(
            "Only when the buyer or the data says the RFQ waits for someone's sign-off: what "
            "it waits for. Marks the RFQ PRELIMINARY: it cannot be prepared for sending until "
            "the mark is removed with set_rfq_terms(title=...)"
        ),
    )
    proposal_id: UUID | None = Field(default=None, description=PROPOSAL_ARG)


class RfqFromAnalysisOut(AwaitingConfirmation):
    # Set once the RFQ exists (not while awaiting the buyer's confirmation).
    id: UUID | None = None
    ref: str | None = None
    title: str | None = None
    status: str | None = None
    # The RFQ's currency: offers are compared, and limits set, in it.
    currency: str | None = None
    currency_source: CurrencySource | None = None
    lines: list[LineItem] = Field(default_factory=list[LineItem])
    invited_suppliers: list[str] = Field(default_factory=list[str])
    skipped_suppliers: list[str] = Field(default_factory=list[str])
    # Positions without a target/maximum price (no history, or another currency
    # with no exchange rate): the buyer must give them before negotiating.
    lines_without_limits: list[int] = Field(default_factory=list[int])


def _line_item(line: RfqLine) -> LineItem:
    return LineItem(
        position=line.position,
        description=line.description,
        part_number=line.part_number,
        mpn=line.mpn,
        quantities=rfq_service.quantities_from_json(line.quantities),
        unit=line.unit,
        need_by=line.need_by,
        incoterm=line.incoterm,
        delivery_location=line.delivery_location,
    )


def _invite_items(rows: list[tuple[Invite, str]]) -> list[InviteItem]:
    items: list[InviteItem] = []
    for invite, name in rows:
        items.append(
            InviteItem(
                supplier_id=invite.supplier_id,
                supplier_name=name,
                status=invite.status,
                sent_at=invite.sent_at,
                responded_at=invite.responded_at,
            )
        )
    return items


def _template_out(row: RfqTemplate | None) -> TemplateOut:
    if row is None:
        return TemplateOut(exists=False)
    return TemplateOut(
        exists=True,
        template_id=row.id,
        language=row.language,
        greeting=row.greeting,
        columns=[str(c) for c in row.columns],
        footer=row.footer,
        fields=[str(f) for f in row.fields],
    )


def _award_supplier(columns: Sequence[SupplierColumn], name: str) -> SupplierColumn | None:
    """The comparison's supplier the buyer named: an exact name, else the only one whose
    name contains it ("Roltec" for "ROLTEC DISTRIBUIDORA DE ROLAMENTOS LTDA")."""
    wanted = " ".join(name.split()).casefold()
    if not wanted:
        return None
    exact = [c for c in columns if c.supplier_name.casefold() == wanted]
    if len(exact) == 1:
        return exact[0]
    partial = [c for c in columns if wanted in c.supplier_name.casefold()]
    return partial[0] if len(partial) == 1 else None


def _awarded_items(lines: list[AwardLine]) -> list[AwardedLineItem]:
    return [
        AwardedLineItem(
            position=line.position,
            description=line.description,
            supplier_name=line.supplier_name,
            quantity=Decimal(line.quantity),
            unit=line.unit,
            unit_price=Decimal(line.unit_price),
            currency=line.currency,
        )
        for line in lines
    ]


def _po_item(po: PurchaseOrderSummary) -> PoItem:
    return PoItem(
        ref=po.ref,
        rfq_ref=po.rfq_ref,
        status=po.status,
        supplier_name=po.supplier_name,
        total=po.total,
        currency=po.currency,
    )


def _clean(items: list[str]) -> list[str]:
    return [item.strip()[:120] for item in items if item.strip()][:MAX_TEMPLATE_ITEMS]


_CONFIRM_FIRST: Final = (
    " Two calls: the first (no proposal_id) does nothing and answers a preview with a "
    "proposal_id; show it to the buyer and ask. Call again with that proposal_id only after "
    "they confirm in their own later message."
)


def _analysis_request(args: RfqFromAnalysisIn) -> RfqFromAnalysisRequest:
    try:
        currency = known_currency(args.currency) if args.currency else None
    except RfqError as exc:
        raise _service_error(exc) from None
    return RfqFromAnalysisRequest(currency=currency, pending_validation=args.pending_validation)


def _invite_matching(rows: list[tuple[Invite, str]], wanted: str) -> tuple[Invite, str]:
    """The RFQ's invite `wanted` names: by invite id, supplier id, or (part of) the name."""
    key = wanted.strip()
    try:
        as_id: UUID | None = UUID(key)
    except ValueError:
        as_id = None
    if as_id is not None:
        for invite, name in rows:
            if as_id in (invite.id, invite.supplier_id):
                return invite, name
        raise ToolError(f"no invited supplier {key} on this RFQ; use get_rfq_status")
    folded = key.casefold()
    exact = [(i, n) for i, n in rows if n.casefold() == folded]
    partial = exact or [(i, n) for i, n in rows if folded and folded in n.casefold()]
    if len(partial) == 1:
        return partial[0]
    if not partial:
        raise ToolError(f"no invited supplier named {key!r} on this RFQ; use get_rfq_status")
    names = ", ".join(n for _, n in partial)
    raise ToolError(f"several invited suppliers match {key!r} ({names}); say which")


def _conversation_uploads(
    requested: list[UUID], current: tuple[UUID, ...], earlier: tuple[UUID, ...]
) -> list[UUID]:
    """The files a call means, each attached in this message or an earlier turn of it.

    Models sometimes copy an id wrongly: one requested file with exactly one
    file in this message means that file.
    """
    allowed = set(current) | set(earlier)
    wanted = list(dict.fromkeys(requested))
    if all(u in allowed for u in wanted):
        return wanted
    if len(wanted) == 1 and len(current) == 1:
        return [current[0]]
    raise ToolError("that id is not one of the files attached in this conversation")


def _quote_preview(extracted: quote_upload.ExtractedUpload) -> ProposalPreview:
    """What register_quote would store: the supplier, the terms and every quoted line."""
    target, quote = extracted.target, extracted.validated
    currency = quote.parsed("currency")
    quoted: list[QuotedLinePreview] = []
    issues = [
        *quote.issues,
        *(f"{name}:{issue}" for name, f in quote.header.items() for issue in f.issues),
        *(f"missing:{f}" for f in quote.output.missing_fields),
    ]
    for line in quote.lines:
        position = line.draft.rfq_line_position
        line_issues = [
            *line.issues,
            *(f"{name}:{issue}" for name, f in line.fields.items() for issue in f.issues),
        ]
        quoted.append(
            QuotedLinePreview(
                rfq_line_position=position,
                description=target.line_descriptions.get(position) if position else None,
                offered_mpn=_text(line.parsed("offered_mpn")),
                is_alternative=line.is_alternative,
                quantity=_amount(line.parsed("min_quantity")),
                unit=_text(line.parsed("unit")),
                unit_price=_amount(line.parsed("unit_price")),
                currency=_text(currency),
                lead_time_days=_days(line.parsed("lead_time_days")),
                issues=line_issues,
            )
        )
        issues.extend(f"line {position if position else '?'}: {issue}" for issue in line_issues)
    return ProposalPreview(
        action="register_quote",
        rfq=target.rfq_ref,
        title=target.rfq_title,
        currency=_text(currency),
        suppliers=[ProposedSupplier(name=target.supplier_name, email=target.contact_email)],
        files=[f.filename for f in extracted.files],
        payment_terms=_text(quote.parsed("payment_terms")),
        valid_until=_day(quote.parsed("valid_until")),
        incoterm=_text(quote.parsed("incoterm")),
        quoted=quoted,
        issues=list(dict.fromkeys(issues)),
    )


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _amount(value: object) -> Decimal | None:
    return value if isinstance(value, Decimal) else None


def _days(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _day(value: object) -> date | None:
    return value if isinstance(value, date) else None


def copilot_tools(deps: CopilotToolDeps) -> tuple[AnyTool, ...]:
    db = deps.db

    async def list_rfqs(ctx: ToolContext, args: ListRfqsIn) -> ListRfqsOut:
        async with tenant_session(db, ctx.tenant_id) as session:
            rows = await queries.rfqs(session, args.status, args.limit)
            by_status = await queries.rfq_counts(session)
        return ListRfqsOut(
            rfqs=[
                RfqItem(
                    id=r.id,
                    ref=r.ref,
                    title=r.title,
                    status=r.status,
                    created_at=r.created_at,
                    response_due=r.response_due,
                )
                for r in rows
            ],
            by_status=by_status,
        )

    async def get_rfq_status(ctx: ToolContext, args: RfqRef) -> RfqStatusOut:
        async with tenant_session(db, ctx.tenant_id) as session:
            rfq = await _rfq(session, args.rfq)
            lines = await queries.lines(session, rfq.id)
            invites = _invite_items(await queries.invites(session, rfq.id))
        return RfqStatusOut(
            id=rfq.id,
            ref=rfq.ref,
            title=rfq.title,
            status=rfq.status,
            response_due=rfq.response_due,
            payment_terms=rfq.payment_terms,
            quote_validity_days=rfq.quote_validity_days,
            lines=[_line_item(line) for line in lines],
            invites=invites,
            invites_by_status=queries.count_by([i.status for i in invites]),
        )

    async def who_responded(ctx: ToolContext, args: RfqRef) -> WhoRespondedOut:
        async with tenant_session(db, ctx.tenant_id) as session:
            rfq = await _rfq(session, args.rfq)
            invites = _invite_items(await queries.invites(session, rfq.id))
        return WhoRespondedOut(
            ref=rfq.ref,
            responded=[i for i in invites if i.status == "responded"],
            not_responded=[i for i in invites if i.status != "responded"],
        )

    async def what_is_missing(ctx: ToolContext, args: RfqRef) -> WhatIsMissingOut:
        async with tenant_session(db, ctx.tenant_id) as session:
            rfq = await _rfq(session, args.rfq)
            lines = await queries.lines(session, rfq.id)
            invites = _invite_items(await queries.invites(session, rfq.id))
            proposals = await queries.latest_proposals(session, rfq.id)
            followups = await supplier_followups(session, rfq.id, deps.today())
        quoted = {
            line.rfq_line_id
            for item in proposals
            for line, _ in item.lines
            if line.unit_price is not None and line.rfq_line_id is not None
        }
        missing = {out.supplier_name: missing_items(out) for out in followups}
        return WhatIsMissingOut(
            ref=rfq.ref,
            awaiting_suppliers=[i for i in invites if i.status in queries.AWAITING_STATUSES],
            lines_without_quote=[_line_item(line) for line in lines if line.id not in quoted],
            proposal_gaps=[
                SupplierGap(
                    supplier_name=item.supplier_name,
                    missing_fields=missing.get(item.supplier_name, []),
                    lines_needing_review=sum(1 for line, _ in item.lines if line.needs_review),
                )
                for item in proposals
                if missing.get(item.supplier_name)
                or any(line.needs_review for line, _ in item.lines)
            ],
            suppliers=followups,
        )

    async def list_proposals(ctx: ToolContext, args: RfqRef) -> ListProposalsOut:
        async with tenant_session(db, ctx.tenant_id) as session:
            rfq = await _rfq(session, args.rfq)
            proposals = await queries.latest_proposals(session, rfq.id)
            followups = await supplier_followups(session, rfq.id, deps.today())
        missing = {out.supplier_name: missing_items(out) for out in followups}
        return ListProposalsOut(
            ref=rfq.ref,
            proposals=[
                ProposalItem(
                    supplier_name=item.supplier_name,
                    version=item.proposal.version,
                    received_at=item.proposal.created_at,
                    currency=item.proposal.currency,
                    valid_until=item.proposal.valid_until,
                    incoterm=item.proposal.incoterm,
                    payment_terms=item.proposal.payment_terms,
                    missing_fields=missing.get(item.supplier_name, []),
                    lines=[
                        ProposalLineItem(
                            rfq_line_position=position,
                            offered_mpn=line.offered_mpn,
                            is_alternative=line.is_alternative,
                            min_quantity=line.min_quantity,
                            unit_price=line.unit_price,
                            unit=line.unit,
                            tooling_cost=line.tooling_cost,
                            moq=line.moq,
                            lead_time_days=line.lead_time_days,
                            needs_review=line.needs_review,
                        )
                        for line, position in item.lines
                    ],
                )
                for item in proposals
            ],
        )

    async def get_comparison(ctx: ToolContext, args: RfqRef) -> ComparisonOut:
        principal = _principal(ctx)
        async with tenant_session(db, ctx.tenant_id) as session:
            rfq = await _rfq(session, args.rfq)
        try:
            comparison = await load_comparison(db, principal, rfq.id, deps.today())
        except RfqError as exc:
            raise _service_error(exc) from None
        cheapest: dict[int, CheapestTier] = {}
        for column in comparison.suppliers:
            for total in column.totals:
                if not total.complete or total.total_base is None:
                    continue
                best = cheapest.get(total.tier)
                if best is None or total.total_base < best.total:
                    cheapest[total.tier] = CheapestTier(
                        tier=total.tier, supplier_name=column.supplier_name, total=total.total_base
                    )
        return ComparisonOut(
            ref=rfq.ref,
            base_currency=comparison.base_currency,
            computed_on=comparison.computed_on,
            suppliers=[
                ComparisonSupplier(
                    supplier_name=column.supplier_name,
                    currency=column.currency,
                    valid_until=column.valid_until,
                    incoterm=column.incoterm,
                    max_lead_time_days=column.max_lead_time_days,
                    flags=[str(f) for f in column.flags],
                    low_confidence_fields=column.low_confidence_fields,
                    totals=[
                        TierTotalItem(tier=t.tier, total=t.total_base, complete=t.complete)
                        for t in column.totals
                    ],
                )
                for column in comparison.suppliers
            ],
            cheapest_complete_by_tier=[cheapest[tier] for tier in sorted(cheapest)],
            awaiting=[a.supplier_name for a in comparison.awaiting],
            recommendations=comparison.recommendations,
            policy=comparison.policy,
            readiness=comparison.readiness,
            covered_line_tiers=comparison.covered_line_tiers,
            requested_line_tiers=comparison.requested_line_tiers,
        )

    async def price_history(ctx: ToolContext, args: PriceHistoryIn) -> PriceHistoryOut:
        async with tenant_session(db, ctx.tenant_id) as session:
            points = await queries.price_history(session, args.mpn, args.limit)
        return PriceHistoryOut(
            mpn=args.mpn,
            points=[
                PricePointItem(
                    rfq_ref=p.rfq_ref,
                    supplier_name=p.supplier_name,
                    quoted_at=p.quoted_at,
                    offered_mpn=p.offered_mpn,
                    min_quantity=p.min_quantity,
                    unit_price=p.unit_price,
                    currency=p.currency,
                    unit=p.unit,
                    lead_time_days=p.lead_time_days,
                )
                for p in points
            ],
        )

    async def list_suppliers(ctx: ToolContext, args: ListSuppliersIn) -> ListSuppliersOut:
        async with tenant_session(db, ctx.tenant_id) as session:
            entries = await queries.supplier_directory(
                session, name=args.supplier, category=args.category, part_number=args.part_number
            )
        return ListSuppliersOut(
            total=len(entries),
            suppliers=[
                SupplierEntryItem(
                    supplier_id=e.supplier.id,
                    name=e.supplier.name,
                    type=e.supplier.type,
                    country=e.supplier.country,
                    contacts=[SupplierContactItem(name=n, email=m) for n, m in e.contacts],
                    purchase_orders=e.purchase_orders,
                    last_ordered_at=e.last_ordered_at,
                    categories=sorted(e.categories),
                    parts=[
                        PartBought(part_number=n, description=d)
                        for n, d in sorted(e.parts.items())[:MAX_LIST]
                    ],
                    rfq_invites=e.invited,
                    rfq_responses=e.responded,
                )
                for e in entries[: args.limit]
            ],
        )

    async def purchase_history(ctx: ToolContext, args: PurchaseHistoryIn) -> PurchaseHistoryOut:
        async with tenant_session(db, ctx.tenant_id) as session:
            found = await queries.purchase_history(
                session,
                part_numbers=args.part_numbers,
                supplier=args.supplier,
                limit=max(args.limit, len(args.part_numbers)),
            )
        known = {
            queries.normalize_mpn(code)
            for p in found
            for code in (p.item.part_number, p.item.mpn)
            if code
        }

        def item(line: queries.PaidLine) -> PaidLineItem:
            return PaidLineItem(
                supplier_name=line.supplier_name,
                po_number=line.po_number,
                ordered_at=line.ordered_at,
                quantity=line.quantity,
                unit_price=line.unit_price,
                currency=line.currency,
            )

        return PurchaseHistoryOut(
            not_found=[p for p in args.part_numbers if queries.normalize_mpn(p) not in known],
            parts=[
                PaidPartItem(
                    part_number=p.item.part_number,
                    description=p.item.description,
                    mpn=p.item.mpn,
                    category=p.item.category,
                    unit=p.item.unit,
                    last_paid=item(p.lines[0]),
                    purchases=[item(line) for line in p.lines[:10]],
                    purchase_count=len(p.lines),
                )
                for p in found
            ],
        )

    async def suggest_suppliers(ctx: ToolContext, args: SuggestSuppliersIn) -> SuggestSuppliersOut:
        async with tenant_session(db, ctx.tenant_id) as session:
            records, matched = await queries.supplier_records(
                session, mpn=args.mpn, category=args.category
            )
        return SuggestSuppliersOut(
            matched_history=matched,
            suppliers=[
                SupplierSuggestion(
                    supplier_id=r.supplier.id,
                    name=r.supplier.name,
                    type=r.supplier.type,
                    country=r.supplier.country,
                    invited=r.invited,
                    responded=r.responded,
                    last_response=r.last_response,
                )
                for r in records[: args.limit]
            ],
        )

    async def get_rfq_template(ctx: ToolContext, args: NoArgs) -> TemplateOut:
        async with tenant_session(db, ctx.tenant_id) as session:
            return _template_out(await queries.default_template(session))

    async def update_rfq_template(ctx: ToolContext, args: UpdateTemplateIn) -> TemplateOut:
        principal = _principal(ctx)
        if args.model_dump(exclude_none=True) == {}:
            raise ToolError("nothing to change: give a greeting, columns, footer_fields or fields")
        async with tenant_session(db, ctx.tenant_id) as session:
            row = await queries.default_template(session)
            if row is None:
                # Start from Bido's standard template; the change below is its version 2.
                row = RfqTemplate(
                    tenant_id=principal.tenant_id,
                    created_by=principal.user_id,
                    source="none",
                    status="ready",
                    language=STANDARD_TEMPLATE.language,
                    greeting=STANDARD_TEMPLATE.greeting,
                    columns=list(STANDARD_TEMPLATE.columns),
                    footer=STANDARD_TEMPLATE.footer,
                    fields=list(STANDARD_TEMPLATE.fields),
                    source_refs={},
                    is_default=True,
                )
                session.add(row)
                await session.flush()
            template_id = row.id
            current = template_service.row_content(row)
        greeting = (args.greeting or "").strip() or current.greeting
        edit = TemplateEdit(
            language=current.language,
            greeting=greeting,
            columns=_clean(args.columns) if args.columns is not None else current.columns,
            footer_fields=(
                _clean(args.footer_fields)
                if args.footer_fields is not None
                else current.footer_fields
            ),
            fields=_clean(args.fields) if args.fields is not None else current.fields,
        )
        # Versions are immutable: the edit is saved as the template's next version.
        try:
            await template_service.edit_template(db, principal, template_id, edit)
        except RequiredFieldsRemoved as exc:
            raise ToolError(str(exc)) from exc
        async with tenant_session(db, ctx.tenant_id) as session:
            updated = await queries.default_template(session)
        logger.info("copilot updated template tenant=%s template=%s", ctx.tenant_id, template_id)
        return _template_out(updated)

    async def _propose(
        ctx: ToolContext, action: str, args: BaseModel, preview: ProposalPreview, **stored: object
    ) -> UUID:
        """Store what `action` would do (its args, the preview, anything it needs to run
        exactly that); nothing else happens until the buyer confirms."""
        if deps.conversation_id is None:
            raise ToolError("this action needs a conversation to ask the buyer in")
        return await proposals.propose(
            db,
            _principal(ctx),
            conversation_id=deps.conversation_id,
            run_id=ctx.run_id,
            action=action,
            args=args.model_dump(mode="json", exclude={"proposal_id"}),
            preview={
                "shown": preview.model_dump(mode="json"),
                **stored,
            },
        )

    async def _claim(ctx: ToolContext, action: str, proposal_id: UUID) -> proposals.Claimed:
        if deps.conversation_id is None:
            raise ToolError("this action needs a conversation to ask the buyer in")
        try:
            return await proposals.claim(
                db,
                _principal(ctx),
                conversation_id=deps.conversation_id,
                run_id=ctx.run_id,
                action=action,
                proposal_id=proposal_id,
            )
        except proposals.ProposalRefused as exc:
            raise ToolError(str(exc)) from None

    async def create_rfq(ctx: ToolContext, args: CreateRfqIn) -> CreateRfqOut:
        principal = _principal(ctx)
        if args.proposal_id is not None:
            return await _confirm_create_rfq(ctx, args.proposal_id)
        text = args.text
        if args.include_attachments:
            attached = deps.attachments_text
            if not deps.attachment_ids and deps.earlier_attachments_text is not None:
                attached = await deps.earlier_attachments_text()
            if attached:
                text = f"{text}\n\n{attached}"
        own_words = await deps.buyer_messages() if deps.buyer_messages is not None else []
        try:
            drafted = await rfq_service.draft_rfq_from_text(
                db,
                principal,
                text,
                deps.agents.intake,
                buyer_messages=own_words,
                currency=args.currency,
            )
        except RfqError as exc:
            raise _service_error(exc) from None
        title = drafted.draft.title
        if args.pending_validation:
            title = rfq_service.preliminary_title(title, args.pending_validation)[:500]
        preview = ProposalPreview(
            action="create_rfq",
            title=title,
            currency=drafted.currency,
            lines=[
                ProposedLine(
                    position=position,
                    description=line.description,
                    part_number=line.part_number,
                    mpn=line.mpn,
                    quantities=list(line.quantities),
                    unit=line.unit,
                    need_by=line.need_by,
                    incoterm=line.incoterm,
                    delivery_location=line.delivery_location,
                )
                for position, line in enumerate(drafted.draft.lines, start=1)
            ],
            questions=list(drafted.draft.questions),
        )
        # The intake draft itself is stored, so the confirmation creates this very
        # RFQ instead of asking the agent again (it could answer differently).
        proposal_id = await _propose(
            ctx,
            "create_rfq",
            args,
            preview,
            draft={
                "source_text": drafted.source_text,
                "draft": drafted.draft.model_dump(mode="json"),
                "currency": drafted.currency,
                "currency_source": drafted.currency_source,
                "title": title,
            },
        )
        return CreateRfqOut(awaiting_confirmation=True, proposal_id=proposal_id, preview=preview)

    async def _confirm_create_rfq(ctx: ToolContext, proposal_id: UUID) -> CreateRfqOut:
        principal = _principal(ctx)
        claimed = await _claim(ctx, "create_rfq", proposal_id)
        stored = cast(dict[str, Any], claimed.preview["draft"])
        draft = RfqIntakeOutput.model_validate(stored["draft"])
        drafted = rfq_service.TextDraft(
            source_text=str(stored["source_text"]),
            draft=draft.model_copy(update={"title": str(stored["title"])}),
            currency=str(stored["currency"]),
            currency_source=cast(CurrencySource, stored["currency_source"]),
        )
        try:
            created = await rfq_service.store_text_draft(db, principal, drafted, origin="copilot")
        except RfqError as exc:
            await proposals.release(db, principal, proposal_id)
            raise _service_error(exc) from None
        return CreateRfqOut(
            proposal_id=proposal_id,
            id=created.id,
            ref=created.ref,
            title=created.title,
            status=created.status,
            currency=created.base_currency,
            currency_source=created.currency_source,
            lines=[
                LineItem(
                    position=line.position,
                    description=line.description,
                    part_number=line.part_number,
                    mpn=line.mpn,
                    quantities=list(line.quantities),
                    unit=line.unit,
                    need_by=line.need_by,
                    incoterm=line.incoterm,
                    delivery_location=line.delivery_location,
                )
                for line in created.lines
            ],
            questions=list(created.questions),
        )

    async def _draft_line(ctx: ToolContext, reference: str, position: int) -> tuple[Rfq, RfqLine]:
        async with tenant_session(db, ctx.tenant_id) as session:
            rfq = await _rfq(session, reference)
            line = await session.scalar(
                select(RfqLine).where(RfqLine.rfq_id == rfq.id, RfqLine.position == position)
            )
        if line is None:
            raise ToolError(f"{rfq.ref} has no line {position}")
        return rfq, line

    async def update_rfq_line(ctx: ToolContext, args: UpdateRfqLineIn) -> EditedRfqOut:
        rfq, line = await _draft_line(ctx, args.rfq, args.position)
        changes: dict[str, object] = {
            field: value
            for field, value in args.model_dump(exclude={"rfq", "position", "clear"}).items()
            if value is not None
        }
        changes.update({field: None for field in args.clear})
        if not changes:
            raise ToolError("nothing to change: pass the fields to set or to clear")
        patch = _request(UpdateLineRequest, changes)
        try:
            await rfq_service.update_line(db, _principal(ctx), rfq.id, line.id, patch)
            return _edited(await rfq_service.get_rfq(db, _principal(ctx), rfq.id))
        except RfqError as exc:
            raise _service_error(exc) from None

    async def add_rfq_line(ctx: ToolContext, args: AddRfqLineIn) -> EditedRfqOut:
        async with tenant_session(db, ctx.tenant_id) as session:
            rfq = await _rfq(session, args.rfq)
        request = _request(AddLineRequest, args.model_dump(exclude={"rfq"}))
        try:
            return _edited(await rfq_service.add_line(db, _principal(ctx), rfq.id, request))
        except RfqError as exc:
            raise _service_error(exc) from None

    async def remove_rfq_line(ctx: ToolContext, args: RemoveRfqLineIn) -> EditedRfqOut:
        rfq, line = await _draft_line(ctx, args.rfq, args.position)
        try:
            return _edited(await rfq_service.remove_line(db, _principal(ctx), rfq.id, line.id))
        except RfqError as exc:
            raise _service_error(exc) from None

    async def set_rfq_terms(ctx: ToolContext, args: SetRfqTermsIn) -> EditedRfqOut:
        async with tenant_session(db, ctx.tenant_id) as session:
            rfq = await _rfq(session, args.rfq)
        changes: dict[str, object] = {
            field: value
            for field, value in args.model_dump(exclude={"rfq", "clear"}).items()
            if value is not None
        }
        changes.update({field: None for field in args.clear})
        if not changes:
            raise ToolError("nothing to change: pass the terms to set or to clear")
        if args.currency is not None:
            try:
                changes["currency"] = known_currency(args.currency)
            except RfqError as exc:
                raise _service_error(exc) from None
        patch = _request(UpdateRfqRequest, changes)
        try:
            edited = await rfq_service.edit_rfq(
                db, _principal(ctx), rfq.id, patch, today=deps.today()
            )
        except RfqError as exc:
            raise _service_error(exc) from None
        return _edited(edited.detail, edited.currency_change)

    async def invite_suppliers(ctx: ToolContext, args: InviteSuppliersIn) -> InviteSuppliersOut:
        principal = _principal(ctx)
        if args.proposal_id is None:
            async with tenant_session(db, ctx.tenant_id) as session:
                rfq = await _rfq(session, args.rfq)
                contacts = await queries.first_contacts(session, list(args.supplier_ids))
                wanted = list(dict.fromkeys(args.supplier_ids))
                rows = {
                    row.id: row
                    for row in await session.execute(
                        select(
                            Supplier.id, Supplier.name, Supplier.country, Supplier.language
                        ).where(Supplier.id.in_(wanted))
                    )
                }
                names = {supplier_id: row.name for supplier_id, row in rows.items()}
                emails = {
                    row.id: row.email
                    for row in await session.execute(
                        select(Contact.id, Contact.email).where(
                            Contact.id.in_(list(contacts.values()))
                        )
                    )
                }
            missing = [str(s) for s in wanted if s not in names]
            if missing:
                raise ToolError(f"unknown supplier ids: {', '.join(missing)}; use list_suppliers")
            # A supplier with no country and the default language would get an English
            # document: that is the buyer's call, not a guess (a Brazilian supplier saved
            # from an English name and a .com address).
            unknown_locale = [
                rows[s].name
                for s in wanted
                if rows[s].country is None and rows[s].language in (None, DEFAULT_LANGUAGE)
            ]
            preview = ProposalPreview(
                action="invite_suppliers",
                rfq=rfq.ref,
                title=rfq.title,
                suppliers=[
                    ProposedSupplier(
                        name=names[s],
                        email=emails.get(contacts[s]) if s in contacts else None,
                        document_language=email_language(rows[s].language, rows[s].country),
                    )
                    for s in wanted
                ],
                questions=[
                    f"{name}: no country or language on file, so its RFQ document would be in "
                    "English. Which country is it in? (add_supplier_contact with `country` "
                    "records the answer.)"
                    for name in unknown_locale
                ],
            )
            proposal_id = await _propose(
                ctx, "invite_suppliers", args.model_copy(update={"rfq": str(rfq.id)}), preview
            )
            return InviteSuppliersOut(
                ref=rfq.ref, awaiting_confirmation=True, proposal_id=proposal_id, preview=preview
            )
        claimed = await _claim(ctx, "invite_suppliers", args.proposal_id)
        stored = InviteSuppliersIn.model_validate(claimed.args)
        async with tenant_session(db, ctx.tenant_id) as session:
            rfq = await _rfq(session, stored.rfq)
            contacts = await queries.first_contacts(session, list(stored.supplier_ids))
        results: list[InviteResult] = []
        for supplier_id in dict.fromkeys(stored.supplier_ids):
            contact_id = contacts.get(supplier_id)
            if contact_id is None:
                results.append(
                    InviteResult(
                        supplier_id=supplier_id, ok=False, detail="no contact with an email"
                    )
                )
                continue
            try:
                await rfq_service.add_invite(db, principal, rfq.id, supplier_id, contact_id)
            except Duplicate:
                results.append(InviteResult(supplier_id=supplier_id, ok=True, detail="already"))
                continue
            except RfqError as exc:
                results.append(InviteResult(supplier_id=supplier_id, ok=False, detail=str(exc)))
                continue
            results.append(InviteResult(supplier_id=supplier_id, ok=True, detail="invited"))
        return InviteSuppliersOut(ref=rfq.ref, results=results, proposal_id=args.proposal_id)

    async def register_quote(ctx: ToolContext, args: RegisterQuoteIn) -> RegisterQuoteOut:
        principal = _principal(ctx)
        if args.proposal_id is not None:
            return await _confirm_register_quote(ctx, args.proposal_id)
        if deps.storage is None:
            raise ToolError("the copilot cannot read files on this server")
        upload_ids = _conversation_uploads(
            args.upload_ids, deps.attachment_ids, deps.earlier_attachment_ids
        )
        async with tenant_session(db, ctx.tenant_id) as session:
            rfq = await _rfq(session, args.rfq)
            invite, supplier_name = _invite_matching(
                await queries.invites(session, rfq.id), args.supplier
            )
        try:
            extracted = await quote_upload.extract_uploaded_quote(
                db,
                deps.storage,
                principal,
                rfq_id=rfq.id,
                invite_id=invite.id,
                upload_ids=upload_ids,
                extractor=deps.quote_extractor or default_quote_extractor(),
                context=ctx,
                today=deps.today(),
            )
        except quote_upload.QuoteUploadError as exc:
            raise ToolError(str(exc)) from None
        preview = _quote_preview(extracted)
        # The validated quote itself is stored: the confirmation registers exactly
        # what the buyer saw instead of reading the file again (it could differ).
        proposal_id = await _propose(
            ctx,
            "register_quote",
            args.model_copy(
                update={
                    "rfq": str(rfq.id),
                    "supplier": str(invite.id),
                    "upload_ids": [f.upload_id for f in extracted.files],
                }
            ),
            preview,
            quote={"validated": store_quote(extracted.validated), "run_id": str(extracted.run_id)},
        )
        return RegisterQuoteOut(
            ref=rfq.ref,
            supplier_name=supplier_name,
            awaiting_confirmation=True,
            proposal_id=proposal_id,
            preview=preview,
            issues=preview.issues,
        )

    async def _confirm_register_quote(ctx: ToolContext, proposal_id: UUID) -> RegisterQuoteOut:
        principal = _principal(ctx)
        if deps.storage is None:
            raise ToolError("the copilot cannot read files on this server")
        claimed = await _claim(ctx, "register_quote", proposal_id)
        stored = RegisterQuoteIn.model_validate(claimed.args)
        quote = cast(dict[str, Any], claimed.preview["quote"])
        validated: ValidatedQuote = restore_quote(quote["validated"])
        try:
            registered = await quote_upload.register_uploaded_quote(
                db,
                deps.storage,
                principal,
                rfq_id=UUID(stored.rfq),
                invite_id=UUID(stored.supplier),
                upload_ids=stored.upload_ids,
                validated=validated,
                run_id=UUID(str(quote["run_id"])),
            )
        except quote_upload.QuoteUploadError as exc:
            await proposals.release(db, principal, proposal_id)
            raise ToolError(str(exc)) from None
        shown = ProposalPreview.model_validate(claimed.preview["shown"])
        return RegisterQuoteOut(
            ref=registered.rfq_ref,
            supplier_name=registered.supplier_name,
            proposal_id=proposal_id,
            version=registered.version,
            needs_review=registered.needs_review,
            issues=shown.issues,
        )

    async def prepare_send(ctx: ToolContext, args: RfqRef) -> PrepareSendOut:
        principal = _principal(ctx)
        async with tenant_session(db, ctx.tenant_id) as session:
            rfq = await _rfq(session, args.rfq)
        if (hold := rfq_service.preliminary_hold(rfq)) is not None:
            raise ToolError(hold)
        try:
            prepared = await rfq_service.prepare_send(
                db, principal, rfq.id, deps.agents.email_writer, deps.mailboxes, today=deps.today()
            )
        except RfqError as exc:
            raise _service_error(exc) from None
        return PrepareSendOut(
            ref=rfq.ref,
            approvals=[
                PreparedApproval(invite_id=p.invite_id, approval_id=p.approval_id)
                for p in prepared.approvals
            ],
            note=(
                "Nothing was sent. Each email waits for approval by an approver other "
                "than the requester; it is sent only after that."
            ),
        )

    async def run_purchase_analysis(
        ctx: ToolContext, args: PurchaseAnalysisIn
    ) -> PurchaseAnalysisOut:
        principal = _principal(ctx)
        try:
            view = await purchasing_service.create_analysis(
                db,
                principal,
                AnalysisRequest(
                    product=args.product,
                    quantity=Decimal(args.quantity) if args.quantity is not None else None,
                    safety_stock_pct=Decimal(args.safety_stock_pct),
                    need_by=args.need_by,
                    planning_start=args.planning_start,
                    planning_end=args.planning_end,
                    location=args.location,
                    stock_as_of=args.stock_as_of,
                    max_stock_age_days=args.max_stock_age_days,
                ),
                today=deps.today(),
            )
        except purchasing_service.PurchasingError as exc:
            raise ToolError(str(exc)) from None
        return PurchaseAnalysisOut(
            analysis_id=view.id,
            product=view.product,
            quantity=view.quantity,
            need_by=view.need_by,
            items=view.summary.items,
            items_to_buy=view.summary.items_to_buy,
            currency=view.summary.currency,
            total_at_target=view.summary.total_at_target,
            total_at_last_price=view.summary.total_at_last_price,
            planning_start=view.planning_start,
            planning_end=view.planning_end,
            location=view.location,
            stock_as_of=view.stock_as_of,
            max_stock_age_days=view.max_stock_age_days,
            demand_source=view.demand_source,
            totals=view.summary.totals,
            lines=[
                AnalysisLineItem(
                    part_number=line.part_number,
                    description=line.description,
                    mpn=line.mpn,
                    unit=line.unit,
                    required=line.required,
                    on_hand=line.on_hand,
                    on_order=line.on_order,
                    to_buy=line.to_buy,
                    need_by=line.need_by,
                    currency=line.currency,
                    last_price=line.last_price,
                    avg_price=line.avg_price,
                    trend_pct=line.trend_pct,
                    target_price=line.target_price,
                    max_price=line.max_price,
                    suppliers=[
                        AnalysisSupplierItem(
                            supplier_id=s.supplier_id,
                            name=s.name,
                            type=s.type,
                            last_price=s.last_price,
                            last_ordered_at=s.last_ordered_at,
                            avg_price=s.avg_price,
                            on_time_rate=s.on_time_rate,
                            quality_rate=s.quality_rate,
                            suggested=s.suggested,
                            delivery_observations=s.delivery_observations,
                            quality_observations=s.quality_observations,
                            last_received_at=s.last_received_at,
                        )
                        for s in line.suppliers[:MAX_ANALYSIS_SUPPLIERS]
                    ],
                    flags=[
                        AnalysisFlagItem(code=f.code, supplier=f.supplier, value=f.value)
                        for f in line.flags
                    ],
                    calculation=line.calculation,
                )
                for line in view.lines
            ],
        )

    async def create_rfq_from_analysis(
        ctx: ToolContext, args: RfqFromAnalysisIn
    ) -> RfqFromAnalysisOut:
        principal = _principal(ctx)
        if args.proposal_id is None:
            return await _propose_rfq_from_analysis(ctx, args)
        claimed = await _claim(ctx, "create_rfq_from_analysis", args.proposal_id)
        stored = RfqFromAnalysisIn.model_validate(claimed.args)
        try:
            created = await purchasing_service.create_rfq_from_analysis(
                db, principal, stored.analysis_id, _analysis_request(stored)
            )
        except (purchasing_service.PurchasingError, RfqError) as exc:
            await proposals.release(db, principal, args.proposal_id)
            raise ToolError(str(exc)) from None
        rfq = created.rfq
        return RfqFromAnalysisOut(
            proposal_id=args.proposal_id,
            id=rfq.id,
            ref=rfq.ref,
            title=rfq.title,
            status=rfq.status,
            currency=rfq.base_currency,
            currency_source=created.currency_source,
            lines=[
                LineItem(
                    position=line.position,
                    description=line.description,
                    part_number=line.part_number,
                    mpn=line.mpn,
                    quantities=list(line.quantities),
                    unit=line.unit,
                    need_by=line.need_by,
                    incoterm=line.incoterm,
                    delivery_location=line.delivery_location,
                )
                for line in rfq.lines
            ],
            invited_suppliers=[s.name for s in created.invited],
            skipped_suppliers=[f"{s.name} ({s.reason})" for s in created.skipped],
            lines_without_limits=created.lines_without_limits,
        )

    async def _propose_rfq_from_analysis(
        ctx: ToolContext, args: RfqFromAnalysisIn
    ) -> RfqFromAnalysisOut:
        principal = _principal(ctx)
        request = _analysis_request(args)
        analysis_id = args.analysis_id
        try:
            plan = await purchasing_service.plan_rfq_from_analysis(
                db, principal, analysis_id, request
            )
        except purchasing_service.NotFound:
            # Models sometimes copy an id wrongly: fall back to this buyer's most
            # recent analysis that has no RFQ yet, if there is one from today.
            latest = await _latest_open_analysis(db, principal)
            if latest is None or latest == analysis_id:
                raise ToolError(f"analysis {analysis_id} not found") from None
            try:
                plan = await purchasing_service.plan_rfq_from_analysis(
                    db, principal, latest, request
                )
            except purchasing_service.PurchasingError as exc:
                raise ToolError(str(exc)) from None
        except purchasing_service.PurchasingError as exc:
            raise ToolError(str(exc)) from None
        preview = ProposalPreview(
            action="create_rfq_from_analysis",
            title=plan.title,
            currency=plan.currency,
            lines=[
                ProposedLine(
                    position=position,
                    description=line.description,
                    part_number=line.part_number,
                    quantities=[line.quantity],
                    unit=line.unit,
                    need_by=line.need_by,
                )
                for position, line in enumerate(plan.lines, start=1)
            ],
            suppliers=[ProposedSupplier(name=s.name, email=s.email) for s in plan.suppliers],
        )
        # The analysis that was previewed is the one confirmed, fallback included.
        proposal_id = await _propose(
            ctx,
            "create_rfq_from_analysis",
            args.model_copy(update={"analysis_id": plan.analysis_id}),
            preview,
        )
        return RfqFromAnalysisOut(
            awaiting_confirmation=True, proposal_id=proposal_id, preview=preview
        )

    async def get_negotiation_status(ctx: ToolContext, args: RfqRef) -> NegotiationStatusOut:
        principal = _principal(ctx)
        async with tenant_session(db, ctx.tenant_id) as session:
            rfq = await _rfq(session, args.rfq)
            positions = {line.id: line.position for line in await queries.lines(session, rfq.id)}
        try:
            view = await negotiation_service.get_negotiation(db, principal, rfq.id)
        except RfqError as exc:
            raise _service_error(exc) from None
        notes = {
            "not_started": "No negotiation yet: the buyer sets a deadline and line targets first.",
            "open": "Negotiating.",
            "ready_to_close": "Every line has an offer within target: it can be closed now.",
            "closed": "Closed: the outcome is the best eligible offer per line.",
        }
        suppliers: list[NegotiationSupplierOut] = []
        for invite in view.invites:
            plans = {plan.rfq_line_id: plan for plan in invite.line_plans}
            lines: list[NegotiationLineOut] = []
            for line in invite.lines:
                plan = plans.get(line.rfq_line_id)
                lines.append(
                    NegotiationLineOut(
                        position=positions.get(line.rfq_line_id, 0),
                        decision=line.decision,
                        offered_unit_price=plain(line.offered_unit_price_base)
                        if line.offered_unit_price_base is not None
                        else None,
                        ask_unit_price=line.ask_unit_price,
                        lead_time_days=line.lead_time_days,
                        price=plan.price if plan else None,
                        delivery=plan.delivery if plan else None,
                        deliver_by=plan.deliver_by if plan else None,
                        required_by=plan.required_by if plan else None,
                        expected_delivery=plan.expected_delivery if plan else None,
                        reasons=plan.reasons if plan else [],
                    )
                )
            state = invite.state
            suppliers.append(
                NegotiationSupplierOut(
                    supplier_name=invite.supplier_name,
                    status=invite.status,
                    rounds_used=invite.rounds_used,
                    lines=lines,
                    state=state.phase if state else "",
                    state_text=state.label if state else "",
                    reason=state.reason if state else "",
                    waiting_until=state.until if state else None,
                    next_action=state.next_action if state else "",
                    warnings=state.warnings if state else [],
                    emails=[
                        NegotiationEmailOut(
                            kind=m.kind or m.action,
                            round=m.round,
                            state=m.state,
                            reply_by=m.reply_by,
                            cutoff_at=m.cutoff_at,
                            held_reason=m.held_reason,
                        )
                        for m in invite.messages
                        if m.state != "canceled"
                    ],
                )
            )
        note = notes[view.status]
        if view.status in ("open", "ready_to_close") and suppliers:
            counts: dict[str, int] = {}
            for supplier in suppliers:
                counts[supplier.state] = counts.get(supplier.state, 0) + 1
            words = {
                "waiting_for_supplier": "waiting for the supplier",
                "waiting": "waiting (an email is queued)",
                "held_for_approval": "held for your approval",
                "accepted_internally": "accepted internally, nothing to ask",
                "needs_decision": "needs your decision",
                "needs_answer": "needs your answer",
                "closed": "closed",
                "not_started": "not started",
            }
            parts = [f"{n} {words.get(k, k)}" for k, n in counts.items() if k]
            note += " Per supplier: " + "; ".join(parts) + ". Each supplier's state_text says "
            note += "why and until when; tell the buyer what needs them."
        return NegotiationStatusOut(
            ref=rfq.ref,
            status=view.status,
            deadline=view.settings.deadline,
            max_rounds=view.settings.max_rounds,
            base_currency=view.settings.base_currency,
            suppliers=suppliers,
            note=note,
            cutoff_at=view.cutoff_at,
            warnings=[
                w.text if not w.supplier_name else f"{w.supplier_name}: {w.text}"
                for w in view.warnings
            ],
        )

    async def propose_counteroffers(ctx: ToolContext, args: RfqRef) -> ProposeCounteroffersOut:
        _principal(ctx)
        async with tenant_session(db, ctx.tenant_id) as session:
            rfq = await _rfq(session, args.rfq)
            status = rfq.negotiation_status
        if status not in ("open", "ready_to_close"):
            raise ToolError(
                f"{rfq.ref}: the negotiation is {status.replace('_', ' ')}; it must be open"
            )
        result = await negotiation_service.evaluate_rfq(
            db,
            ctx.tenant_id,
            rfq.id,
            writer=deps.negotiation_writer or default_negotiation_writer(),
            force=True,
        )
        return ProposeCounteroffersOut(
            ref=rfq.ref,
            proposed=result.proposed,
            approval_ids=list(result.approval_ids),
            note=(
                "Nothing was sent. Each new email waits for approval (by the buyer, or by a "
                "second person where the company requires it), unless an approved negotiation "
                "mandate covers it. A supplier offer gets one email at most: one already "
                "decided, waiting, sent, held or rejected is never repeated, so 0 proposed "
                "can be right; get_negotiation_status says where each supplier stands."
            ),
        )

    async def get_award_status(ctx: ToolContext, args: RfqRef) -> AwardStatusOut:
        principal = _principal(ctx)
        async with tenant_session(db, ctx.tenant_id) as session:
            rfq = await _rfq(session, args.rfq)
        view = await award_service.get_award(db, principal, rfq.id)
        return AwardStatusOut(
            ref=view.rfq_ref,
            state=view.state,
            differs_from_recommendation=view.differs_from_recommendation,
            reason=view.reason,
            lines=_awarded_items(view.lines),
            purchase_orders=[_po_item(po) for po in view.purchase_orders],
        )

    async def list_open_pos(ctx: ToolContext, args: ListOpenPosIn) -> ListOpenPosOut:
        principal = _principal(ctx)
        orders = await award_service.list_purchase_orders(db, principal)
        return ListOpenPosOut(
            purchase_orders=[
                _po_item(po) for po in orders if args.include_sent or po.status != "sent"
            ][:MAX_LIST]
        )

    async def _award_request(principal: Principal, rfq: Rfq, args: PrepareAwardIn) -> AwardRequest:
        selections: list[AwardSelection] = []
        if args.choices:
            comparison = await load_comparison(db, principal, rfq.id, deps.today())
            lines = {line.position: line for line in comparison.lines}
            for choice in args.choices:
                line = lines.get(choice.line_position)
                if line is None:
                    raise ToolError(f"{rfq.ref} has no line {choice.line_position}")
                column = _award_supplier(comparison.suppliers, choice.supplier)
                if column is None:
                    raise ToolError(
                        f"no single supplier named {choice.supplier!r} has a proposal on {rfq.ref}"
                    )
                selections.append(
                    AwardSelection(
                        rfq_line_id=line.id, tier=choice.tier, invite_id=column.invite_id
                    )
                )
        return AwardRequest(lines=selections, reason=args.reason)

    async def prepare_award(ctx: ToolContext, args: PrepareAwardIn) -> PrepareAwardOut:
        principal = _principal(ctx)
        action = "prepare_award"
        if args.proposal_id is None:
            async with tenant_session(db, ctx.tenant_id) as session:
                rfq = await _rfq(session, args.rfq)
            request = await _award_request(principal, rfq, args)
            try:
                checked = await award_service.check_award(
                    db, principal, rfq.id, request, deps.today()
                )
            except RfqError as exc:
                raise _service_error(exc) from None
            items = _awarded_items(checked.args.lines)
            preview = ProposalPreview(
                action=action,
                rfq=rfq.ref,
                award=[ProposedAwardLine.model_validate(i.model_dump()) for i in items],
                differs_from_recommendation=checked.args.differs_from_recommendation,
                reason=checked.args.reason,
            )
            proposal_id = await _propose(
                ctx,
                action,
                args,
                preview,
                rfq_id=str(rfq.id),
                request=request.model_dump(mode="json"),
            )
            return PrepareAwardOut(
                awaiting_confirmation=True,
                proposal_id=proposal_id,
                preview=preview,
                ref=rfq.ref,
                differs_from_recommendation=checked.args.differs_from_recommendation,
                lines=items,
                note=(
                    "Nothing was asked yet. Confirming sends the award to the approvals "
                    "queue; nothing is awarded until it is approved."
                ),
            )
        claimed = await _claim(ctx, action, args.proposal_id)
        stored = AwardRequest.model_validate(claimed.preview["request"])
        rfq_id = UUID(str(claimed.preview["rfq_id"]))
        try:
            requested = await award_service.request_award(
                db, principal, rfq_id, stored, deps.today()
            )
        except RfqError as exc:
            await proposals.release(db, principal, args.proposal_id)
            raise _service_error(exc) from None
        return PrepareAwardOut(
            proposal_id=args.proposal_id,
            ref=str(claimed.preview["shown"]["rfq"]),
            approval_id=requested.approval_id,
            differs_from_recommendation=requested.differs_from_recommendation,
            lines=_awarded_items(requested.lines),
            note=(
                "Nothing was awarded yet. The award must be approved first (by the buyer, "
                "or a second person if the company requires one); then the purchase "
                "orders are created as drafts."
            ),
        )

    rfq_ref_hint = " Takes the RFQ's reference (RFQ-12) or id."
    return (
        Tool(
            "list_rfqs",
            "List the company's RFQs, newest first, with a count per status over all of "
            "them. " + RFQ_STATUS_HINT,
            ListRfqsIn,
            ListRfqsOut,
            "read",
            list_rfqs,
        ),
        Tool(
            "get_rfq_status",
            "An RFQ's lines and its invited suppliers with their status." + rfq_ref_hint,
            RfqRef,
            RfqStatusOut,
            "read",
            get_rfq_status,
        ),
        Tool(
            "who_responded",
            "Which invited suppliers responded to an RFQ and which did not." + rfq_ref_hint,
            RfqRef,
            WhoRespondedOut,
            "read",
            who_responded,
        ),
        Tool(
            "what_is_missing",
            "What an RFQ still needs: suppliers who have not answered, lines nobody "
            "priced, proposals with missing or unverified fields, and per supplier where it "
            "stands: what Bido asked and waits for, the supplier's questions waiting for the "
            "buyer's answer, and what needs the buyer's decision." + rfq_ref_hint,
            RfqRef,
            WhatIsMissingOut,
            "read",
            what_is_missing,
        ),
        Tool(
            "list_proposals",
            "The latest proposal of each supplier for an RFQ, line by line." + rfq_ref_hint,
            RfqRef,
            ListProposalsOut,
            "read",
            list_proposals,
        ),
        Tool(
            "get_comparison",
            "Side-by-side comparison of an RFQ's proposals in the base currency, with "
            "totals per quantity tier and the cheapest complete offer." + rfq_ref_hint,
            RfqRef,
            ComparisonOut,
            "read",
            get_comparison,
            artifact_kind="comparison",
        ),
        Tool(
            "price_history",
            "Unit prices suppliers QUOTED for a part number in past RFQs, newest first. "
            "These are quotes, not purchase prices.",
            PriceHistoryIn,
            PriceHistoryOut,
            "read",
            price_history,
        ),
        Tool(
            "list_suppliers",
            "The company's suppliers, stored from its imported data and RFQs: contacts "
            "(emails), what it bought from each (part categories, part numbers, purchase "
            "orders, last order date) and RFQ invites. Filter by name, category or part "
            "number. Works in any conversation, with no file attached.",
            ListSuppliersIn,
            ListSuppliersOut,
            "read",
            list_suppliers,
        ),
        Tool(
            "purchase_history",
            "What the company PAID for parts, from its imported purchase orders: the last "
            "price paid (supplier, date, currency) and earlier purchases. Give part numbers "
            "or MPNs, or none for the parts bought most recently. Needs no BOM and no file "
            "attached.",
            PurchaseHistoryIn,
            PurchaseHistoryOut,
            "read",
            purchase_history,
        ),
        Tool(
            "suggest_suppliers",
            "Suppliers to invite, ranked by how often they were invited and responded "
            "for the same part number or category.",
            SuggestSuppliersIn,
            SuggestSuppliersOut,
            "read",
            suggest_suppliers,
        ),
        Tool(
            "get_rfq_template",
            "The company's default RFQ template (greeting, item columns, fields asked for).",
            NoArgs,
            TemplateOut,
            "read",
            get_rfq_template,
        ),
        Tool(
            "update_rfq_template",
            "Change the company's default RFQ template. Only the given parts change.",
            UpdateTemplateIn,
            TemplateOut,
            "draft",
            update_rfq_template,
            artifact_kind="rfq_template",
        ),
        Tool(
            "run_purchase_analysis",
            "Analyse what to buy to build a product: per part, how much is needed (BOM x "
            "units - stock - open orders), how much to buy, the suppliers who sold it with "
            "their last and average PURCHASE prices, on-time and quality rates, and a target "
            "and maximum price. Saves the analysis (no email, no RFQ).",
            PurchaseAnalysisIn,
            PurchaseAnalysisOut,
            "draft",
            run_purchase_analysis,
            artifact_kind="purchase_analysis",
        ),
        Tool(
            "create_rfq_from_analysis",
            "Create a DRAFT RFQ from a purchase analysis: the parts to buy with their "
            "quantities and need-by dates, and the suggested suppliers as DRAFT invites. "
            "Nothing is sent." + _CURRENCY_RESULT + _CONFIRM_FIRST,
            RfqFromAnalysisIn,
            RfqFromAnalysisOut,
            "draft",
            create_rfq_from_analysis,
            artifact_kind="rfq_draft",
            artifact_if=proposals.created,
        ),
        Tool(
            "create_rfq",
            "Create a DRAFT RFQ from a description of what to buy (and the attached "
            "files). Nothing is sent." + _CURRENCY_RESULT + _CONFIRM_FIRST,
            CreateRfqIn,
            CreateRfqOut,
            "draft",
            create_rfq,
            artifact_kind="rfq_draft",
            artifact_if=proposals.created,
        ),
        Tool(
            "update_rfq_line",
            "Change one line of a draft RFQ (by its position): description, quantities, unit, "
            "the buyer's part number, MPN, manufacturer, need-by date, Incoterm, delivery "
            "location, qualification. Only the fields passed change; `clear` empties optional "
            "ones." + _DRAFT_ONLY,
            UpdateRfqLineIn,
            EditedRfqOut,
            "draft",
            update_rfq_line,
            artifact_kind="rfq_draft",
        ),
        Tool(
            "add_rfq_line",
            "Add a line at the end of a draft RFQ." + _DRAFT_ONLY,
            AddRfqLineIn,
            EditedRfqOut,
            "draft",
            add_rfq_line,
            artifact_kind="rfq_draft",
        ),
        Tool(
            "remove_rfq_line",
            "Remove a line (by its position) from a draft RFQ; the lines after it move up. "
            "The last line cannot be removed." + _DRAFT_ONLY,
            RemoveRfqLineIn,
            EditedRfqOut,
            "draft",
            remove_rfq_line,
            artifact_kind="rfq_draft",
        ),
        Tool(
            "set_rfq_terms",
            "Set a draft RFQ's terms for every line at once: Incoterm, delivery location, "
            "need-by date; and the RFQ's response date, title, currency (any negotiation "
            "limits are converted with it; tell the buyer the `note`), payment terms (the "
            "buyer's words, e.g. '28 DDL') and minimum quote validity in days. The RFQ email "
            "states the payment terms and validity to suppliers. Only the fields passed change; "
            "`clear` empties optional ones. The result is what is stored: report those "
            "values, and never claim a term the result does not show." + _DRAFT_ONLY,
            SetRfqTermsIn,
            EditedRfqOut,
            "draft",
            set_rfq_terms,
            artifact_kind="rfq_draft",
        ),
        Tool(
            "invite_suppliers",
            "Add suppliers to a draft or open RFQ as DRAFT invites. Nothing is sent."
            + _CONFIRM_FIRST,
            InviteSuppliersIn,
            InviteSuppliersOut,
            "draft",
            invite_suppliers,
        ),
        Tool(
            "register_quote",
            "Register a supplier's quote the buyer attached in the chat (a PDF or sheet the "
            "supplier sent them outside Bido) on the RFQ, for that invited supplier. The file "
            "is read like an emailed reply and the quote stored the same way: it shows in the "
            "comparison and the Quotes tab, marked as uploaded by the buyer. The preview "
            "lists the supplier, the terms (currency, payment terms, validity, Incoterm), "
            "each quoted line (price, quantity, lead time in calendar days) and `issues` to "
            "check: values the file does not back, missing terms, and what differs from "
            "the RFQ (quantity_mismatch, incoterm_mismatch, delivery_location_mismatch, "
            "currency_mismatch). Show all of it, every issue by name; never say there are "
            "none when the list is not empty. Nothing is sent." + _CONFIRM_FIRST,
            RegisterQuoteIn,
            RegisterQuoteOut,
            "draft",
            register_quote,
        ),
        Tool(
            "prepare_send",
            "Write the RFQ email for every draft invite and ask for approval. Nothing is "
            "sent: the buyer (or, if the company requires it, a second person) must approve "
            "each email first. The RFQ needs a reply-by date (response_due, today or "
            "later): if it is missing or past, ask the buyer by when suppliers must answer "
            "and set it with set_rfq_terms before calling this." + rfq_ref_hint,
            RfqRef,
            PrepareSendOut,
            "draft",
            prepare_send,
            artifact_kind="approvals",
        ),
        Tool(
            "get_negotiation_status",
            "Where an RFQ's negotiation stands: deadline, the best-and-final cut-off, and per "
            "supplier its state in one sentence (waiting for the supplier until when, held "
            "for approval, accepted internally with nothing to ask, needs the buyer's "
            "decision or answer, closed), the emails decided or sent, and how its latest "
            "offer compares to the targets and need-by dates." + rfq_ref_hint,
            RfqRef,
            NegotiationStatusOut,
            "read",
            get_negotiation_status,
        ),
        Tool(
            "propose_counteroffers",
            "Plan the negotiation now: a revised-offer, delivery-date or best-and-final "
            "request to the suppliers that need one. The prices and dates asked are decided "
            "by rules, not by you, and an offer never gets a second email. Nothing is sent: "
            "each email waits for approval." + rfq_ref_hint,
            RfqRef,
            ProposeCounteroffersOut,
            "draft",
            propose_counteroffers,
            artifact_kind="approvals",
        ),
        Tool(
            "get_award_status",
            "Whether an RFQ is awarded or waiting for award approval, the awarded "
            "lines and the purchase orders." + rfq_ref_hint,
            RfqRef,
            AwardStatusOut,
            "read",
            get_award_status,
        ),
        Tool(
            "list_open_pos",
            "Purchase orders not sent yet (drafts and those waiting for approval).",
            ListOpenPosIn,
            ListOpenPosOut,
            "read",
            list_open_pos,
        ),
        Tool(
            "prepare_award",
            "Ask for approval to award an RFQ: the recommendation, or the suppliers the "
            "buyer chose (then a reason is required). Nothing is awarded until the "
            "award is approved." + rfq_ref_hint,
            PrepareAwardIn,
            PrepareAwardOut,
            "draft",
            prepare_award,
            artifact_kind="approvals",
        ),
        *journey_tools(deps),
        *order_tools(deps),
        *followup_tools(deps),
    )
