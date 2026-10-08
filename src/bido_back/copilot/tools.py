"""The copilot's tools: typed functions over our services, bound to one request.

Every tool reads the tenant and the user from the run's `ToolContext`, never
from the model's arguments, and works in that tenant's RLS-scoped session.

- `read` tools answer questions: RFQs and their status, who responded, what is
  missing, proposals, the comparison, quoted price history, supplier
  suggestions, the RFQ template.
- `draft` tools change only drafts or the tenant's own settings: run a
  purchase analysis (stored, from the imported BOM, stock and purchase
  history), create an RFQ (draft, from text or from an analysis), invite
  suppliers (draft invites), update the RFQ template, prepare sends, and
  propose counter-offers. None of them sends anything: each records one
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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.agents.catalog.contracts import LineCategory, ModelDecimal
from bido_back.agents.context import ToolContext
from bido_back.agents.tools import AnyTool, Tool, ToolError
from bido_back.auth import Principal
from bido_back.awards import service as award_service
from bido_back.awards.schemas import AwardLine, AwardRequest, AwardSelection, PurchaseOrderSummary
from bido_back.comparison.loader import load_comparison
from bido_back.copilot import queries
from bido_back.copilot.journey_tools import journey_tools, reply_by_explained
from bido_back.db import tenant_session
from bido_back.db.models import Analysis, Invite, Rfq, RfqLine, RfqTemplate
from bido_back.negotiation import service as negotiation_service
from bido_back.negotiation.agents import NegotiationWriter, default_negotiation_writer
from bido_back.onboarding.standard import STANDARD_TEMPLATE
from bido_back.purchasing import service as purchasing_service
from bido_back.purchasing.schemas import AnalysisRequest, CurrencyTotalView, RfqFromAnalysisRequest
from bido_back.purchasing.workbook import SheetMapper
from bido_back.rfqs import service as rfq_service
from bido_back.rfqs.agents import RfqAgents
from bido_back.rfqs.errors import (
    Duplicate,
    MailboxNotConfigured,
    MailboxNotConnected,
    ReplyByRequired,
    RfqError,
)
from bido_back.rfqs.mailbox import MailboxSenderFactory
from bido_back.rfqs.schemas import AddLineRequest, RfqDetail, UpdateLineRequest, UpdateRfqRequest
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
    today: Callable[[], date] = lambda: datetime.now(UTC).date()


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


class ListRfqsIn(BaseModel):
    status: RfqStatus | None = Field(default=None, description="Only RFQs in this status")
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
    missing_fields: list[str]
    lines_needing_review: int


class WhatIsMissingOut(BaseModel):
    ref: str
    awaiting_suppliers: list[InviteItem]
    lines_without_quote: list[LineItem]
    proposal_gaps: list[SupplierGap]


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
    # Quoted prices: Bido does not record purchase orders yet.
    points: list[PricePointItem]


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


class CreateRfqIn(BaseModel):
    text: str = Field(
        min_length=1,
        max_length=8_000,
        description=(
            "What to quote: items, quantities, dates, Incoterm and delivery location. Copy the "
            "buyer's part numbers exactly as they wrote them; never turn them into MPNs"
        ),
    )
    pending_validation: str | None = Field(
        default=None,
        max_length=200,
        description=(
            "What the RFQ still waits for, when the buyer wants to move on before it "
            "(e.g. 'engineering validation of the BOM'). Marks the RFQ PRELIMINARY: it "
            "cannot be prepared for sending until the buyer confirms and the mark is removed "
            "with set_rfq_terms(title=...)"
        ),
    )
    include_attachments: bool = Field(
        default=True,
        description=(
            "Also read the files attached to this message (or, when it has none, "
            "the ones attached earlier in this conversation)"
        ),
    )


class CreateRfqOut(BaseModel):
    id: UUID
    ref: str
    title: str
    status: str
    lines: list[LineItem]
    questions: list[str]


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


class SetRfqTermsIn(BaseModel):
    rfq: str = Field(description="The RFQ's reference (e.g. RFQ-12) or id")
    incoterm: str | None = Field(
        default=None, description="For every line. The rule only: DAP, FCA, EXW..."
    )
    delivery_location: str | None = Field(default=None, description="For every line")
    need_by: date | None = Field(default=None, description="For every line")
    response_due: date | None = Field(default=None, description="When suppliers must answer")
    title: str | None = Field(default=None, min_length=1, max_length=500)


class EditedRfqOut(BaseModel):
    id: UUID
    ref: str
    title: str
    status: str
    response_due: date | None
    lines: list[LineItem]


def _request[M: BaseModel](model: type[M], fields: dict[str, object]) -> M:
    """The service's request from tool arguments; a refusal names the fields, not the values."""
    try:
        return model.model_validate(fields)
    except ValidationError as exc:
        where = ", ".join(".".join(str(p) for p in error["loc"]) for error in exc.errors())
        raise ToolError(f"invalid value for: {where}") from None


def _edited(detail: RfqDetail) -> EditedRfqOut:
    return EditedRfqOut(
        id=detail.id,
        ref=detail.ref,
        title=detail.title,
        status=detail.status,
        response_due=detail.response_due,
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


class InviteResult(BaseModel):
    supplier_id: UUID
    ok: bool
    detail: str


class InviteSuppliersOut(BaseModel):
    ref: str
    results: list[InviteResult]


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


class NegotiationSupplierOut(BaseModel):
    supplier_name: str
    status: str
    rounds_used: int
    lines: list[NegotiationLineOut]


class NegotiationStatusOut(BaseModel):
    ref: str
    status: str
    deadline: datetime | None
    max_rounds: int
    base_currency: str
    suppliers: list[NegotiationSupplierOut]
    note: str


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
        default=None, description="Why, when the choice differs from the recommendation"
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


class PrepareAwardOut(BaseModel):
    ref: str
    approval_id: UUID
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


class RfqFromAnalysisIn(BaseModel):
    analysis_id: UUID = Field(description="The analysis_id returned by run_purchase_analysis")


class RfqFromAnalysisOut(BaseModel):
    id: UUID
    ref: str
    title: str
    status: str
    # The RFQ's currency: offers are compared, and limits set, in it.
    currency: str
    lines: list[LineItem]
    invited_suppliers: list[str]
    skipped_suppliers: list[str]
    # Positions without a target/maximum price (no history, or another currency
    # with no exchange rate): the buyer must give them before negotiating.
    lines_without_limits: list[int]


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


def copilot_tools(deps: CopilotToolDeps) -> tuple[AnyTool, ...]:
    db = deps.db

    async def list_rfqs(ctx: ToolContext, args: ListRfqsIn) -> ListRfqsOut:
        async with tenant_session(db, ctx.tenant_id) as session:
            rows = await queries.rfqs(session, args.status, args.limit)
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
            ]
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
        quoted = {
            line.rfq_line_id
            for item in proposals
            for line, _ in item.lines
            if line.unit_price is not None and line.rfq_line_id is not None
        }
        return WhatIsMissingOut(
            ref=rfq.ref,
            awaiting_suppliers=[i for i in invites if i.status in queries.AWAITING_STATUSES],
            lines_without_quote=[_line_item(line) for line in lines if line.id not in quoted],
            proposal_gaps=[
                SupplierGap(
                    supplier_name=item.supplier_name,
                    missing_fields=queries.missing_fields(item.proposal),
                    lines_needing_review=sum(1 for line, _ in item.lines if line.needs_review),
                )
                for item in proposals
                if queries.missing_fields(item.proposal)
                or any(line.needs_review for line, _ in item.lines)
            ],
        )

    async def list_proposals(ctx: ToolContext, args: RfqRef) -> ListProposalsOut:
        async with tenant_session(db, ctx.tenant_id) as session:
            rfq = await _rfq(session, args.rfq)
            proposals = await queries.latest_proposals(session, rfq.id)
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
                    missing_fields=queries.missing_fields(item.proposal),
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

    async def create_rfq(ctx: ToolContext, args: CreateRfqIn) -> CreateRfqOut:
        principal = _principal(ctx)
        text = args.text
        if args.include_attachments:
            attached = deps.attachments_text
            if not deps.attachment_ids and deps.earlier_attachments_text is not None:
                attached = await deps.earlier_attachments_text()
            if attached:
                text = f"{text}\n\n{attached}"
        own_words = await deps.buyer_messages() if deps.buyer_messages is not None else []
        try:
            created = await rfq_service.create_rfq_from_text(
                db, principal, text, deps.agents.intake, buyer_messages=own_words
            )
        except RfqError as exc:
            raise _service_error(exc) from None
        if args.pending_validation:
            title = rfq_service.preliminary_title(created.title, args.pending_validation)
            renamed = await rfq_service.update_rfq(
                db, principal, created.id, UpdateRfqRequest(title=title[:500])
            )
            created = created.model_copy(update={"title": renamed.title})
        return CreateRfqOut(
            id=created.id,
            ref=created.ref,
            title=created.title,
            status=created.status,
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
        changes = {
            field: value
            for field, value in args.model_dump(exclude={"rfq"}).items()
            if value is not None
        }
        if not changes:
            raise ToolError("nothing to change: pass the terms to set")
        patch = _request(UpdateRfqRequest, changes)
        try:
            return _edited(await rfq_service.update_rfq(db, _principal(ctx), rfq.id, patch))
        except RfqError as exc:
            raise _service_error(exc) from None

    async def invite_suppliers(ctx: ToolContext, args: InviteSuppliersIn) -> InviteSuppliersOut:
        principal = _principal(ctx)
        async with tenant_session(db, ctx.tenant_id) as session:
            rfq = await _rfq(session, args.rfq)
            contacts = await queries.first_contacts(session, list(args.supplier_ids))
        results: list[InviteResult] = []
        for supplier_id in dict.fromkeys(args.supplier_ids):
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
        return InviteSuppliersOut(ref=rfq.ref, results=results)

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
                        )
                        for s in line.suppliers[:MAX_ANALYSIS_SUPPLIERS]
                    ],
                    flags=[
                        AnalysisFlagItem(code=f.code, supplier=f.supplier, value=f.value)
                        for f in line.flags
                    ],
                )
                for line in view.lines
            ],
        )

    async def create_rfq_from_analysis(
        ctx: ToolContext, args: RfqFromAnalysisIn
    ) -> RfqFromAnalysisOut:
        principal = _principal(ctx)
        analysis_id = args.analysis_id
        try:
            created = await purchasing_service.create_rfq_from_analysis(
                db, principal, analysis_id, RfqFromAnalysisRequest()
            )
        except purchasing_service.NotFound:
            # Models sometimes copy an id wrongly: fall back to this buyer's most
            # recent analysis that has no RFQ yet, if there is one from today.
            latest = await _latest_open_analysis(db, principal)
            if latest is None or latest == analysis_id:
                raise ToolError(f"analysis {analysis_id} not found") from None
            try:
                created = await purchasing_service.create_rfq_from_analysis(
                    db, principal, latest, RfqFromAnalysisRequest()
                )
            except purchasing_service.PurchasingError as exc:
                raise ToolError(str(exc)) from None
        except purchasing_service.PurchasingError as exc:
            raise ToolError(str(exc)) from None
        rfq = created.rfq
        return RfqFromAnalysisOut(
            id=rfq.id,
            ref=rfq.ref,
            title=rfq.title,
            status=rfq.status,
            currency=rfq.base_currency,
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
        return NegotiationStatusOut(
            ref=rfq.ref,
            status=view.status,
            deadline=view.settings.deadline,
            max_rounds=view.settings.max_rounds,
            base_currency=view.settings.base_currency,
            suppliers=[
                NegotiationSupplierOut(
                    supplier_name=invite.supplier_name,
                    status=invite.status,
                    rounds_used=invite.rounds_used,
                    lines=[
                        NegotiationLineOut(
                            position=positions.get(line.rfq_line_id, 0),
                            decision=line.decision,
                            offered_unit_price=line.offered_unit_price_base,
                            ask_unit_price=line.ask_unit_price,
                            lead_time_days=line.lead_time_days,
                        )
                        for line in invite.lines
                    ],
                )
                for invite in view.invites
            ],
            note=notes[view.status],
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
                "Nothing was sent. Each counter-offer waits for approval by an approver "
                "other than the requester; it is sent only after that."
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

    async def prepare_award(ctx: ToolContext, args: PrepareAwardIn) -> PrepareAwardOut:
        principal = _principal(ctx)
        async with tenant_session(db, ctx.tenant_id) as session:
            rfq = await _rfq(session, args.rfq)
        selections: list[AwardSelection] = []
        if args.choices:
            comparison = await load_comparison(db, principal, rfq.id, deps.today())
            lines = {line.position: line for line in comparison.lines}
            for choice in args.choices:
                line = lines.get(choice.line_position)
                if line is None:
                    raise ToolError(f"{rfq.ref} has no line {choice.line_position}")
                wanted = choice.supplier.strip().casefold()
                matches = [
                    column
                    for column in comparison.suppliers
                    if column.supplier_name.casefold() == wanted
                ]
                if len(matches) != 1:
                    raise ToolError(
                        f"no single supplier named {choice.supplier!r} has a proposal on {rfq.ref}"
                    )
                selections.append(
                    AwardSelection(
                        rfq_line_id=line.id, tier=choice.tier, invite_id=matches[0].invite_id
                    )
                )
        try:
            requested = await award_service.request_award(
                db,
                principal,
                rfq.id,
                AwardRequest(lines=selections, reason=args.reason),
                deps.today(),
            )
        except RfqError as exc:
            raise _service_error(exc) from None
        return PrepareAwardOut(
            ref=rfq.ref,
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
            "List the company's RFQs, newest first.",
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
            "priced, and proposals with missing or unverified fields." + rfq_ref_hint,
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
            "Nothing is sent.",
            RfqFromAnalysisIn,
            RfqFromAnalysisOut,
            "draft",
            create_rfq_from_analysis,
            artifact_kind="rfq_draft",
        ),
        Tool(
            "create_rfq",
            "Create a DRAFT RFQ from a description of what to buy (and the attached "
            "files). Nothing is sent.",
            CreateRfqIn,
            CreateRfqOut,
            "draft",
            create_rfq,
            artifact_kind="rfq_draft",
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
            "need-by date; and the RFQ's response date or title." + _DRAFT_ONLY,
            SetRfqTermsIn,
            EditedRfqOut,
            "draft",
            set_rfq_terms,
            artifact_kind="rfq_draft",
        ),
        Tool(
            "invite_suppliers",
            "Add suppliers to a draft or open RFQ as DRAFT invites. Nothing is sent.",
            InviteSuppliersIn,
            InviteSuppliersOut,
            "draft",
            invite_suppliers,
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
            "Where an RFQ's negotiation stands: deadline, each supplier's rounds and how "
            "its latest offer compares to the buyer's targets." + rfq_ref_hint,
            RfqRef,
            NegotiationStatusOut,
            "read",
            get_negotiation_status,
        ),
        Tool(
            "propose_counteroffers",
            "Propose counter-offers (or best-and-final requests) to the suppliers whose "
            "latest offer is above target. The prices asked are decided by rules, not by "
            "you. Nothing is sent: each email waits for approval." + rfq_ref_hint,
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
    )
