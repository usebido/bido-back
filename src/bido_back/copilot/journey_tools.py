"""Copilot tools for the buyer's journey: spreadsheet in, mandate out, outcome back.

- `import_spreadsheet` (draft): any workbook the buyer attached, every sheet;
  rules first, the sheet mapper for the rest, every value validated; asks
  before writing when unsure (`purchasing.workbook`).
- `add_supplier_contact` (draft): a supplier's contact email, from the chat.
- `explain_analysis_item` (read): one item of the analysis this conversation already
  ran, as code computed it (`copilot.analyses`); no model call, no re-planning.
- `refine_analysis` (draft): the buyer's edits to a recommendation (drop
  parts, set quantities, or re-run with another quantity, safety stock or date).
- `propose_negotiation_mandate` (draft): the single approval that starts the
  negotiation (`mandates.service`). Nothing is sent until a person approves it.
- `pause_negotiation` / `resume_negotiation` (draft): the kill switch.
- `close_negotiation` (draft): the buyer decides it is good enough now.
- `get_negotiation_outcome` (read): the best offer per line and what is not
  covered, so the copilot can say the buyer's network was insufficient and
  offer a partial order.
- `list_extra_offers` (read): offers suppliers made for things not requested.

None of them sends anything or awards anything.
"""

from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import TYPE_CHECKING, Annotated, Final, cast
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)
from sqlalchemy import func, select

from bido_back.agents.catalog.contracts import ExtraOfferDraft, ModelDecimal
from bido_back.agents.context import ToolContext
from bido_back.agents.tools import AnyTool, Tool, ToolError, ToolValidationError
from bido_back.auth import Principal
from bido_back.comparison.compute import DecisionOffer, ExcludedOffer, OfferAlternative
from bido_back.comparison.loader import load_comparison
from bido_back.comparison.policy import ComparisonPolicy, SupplierPerformance
from bido_back.copilot import analyses, proposals, queries
from bido_back.copilot.analyses import AnalysisItemOut
from bido_back.copilot.model_views import analysis_item_view, assessment_view
from bido_back.copilot.proposals import (
    PROPOSAL_ARG,
    AwaitingConfirmation,
    ProposalPreview,
    ProposedLine,
    ProposedSupplier,
)
from bido_back.db import tenant_session
from bido_back.db.models import Contact, Invite, Proposal, Supplier, SupplierContact
from bido_back.mandates import service as mandates
from bido_back.mandates.schemas import ProposeMandateRequest
from bido_back.negotiation import service as negotiation_service
from bido_back.negotiation.schemas import LineLimitsUpdate
from bido_back.outbound.domains import RecipientCheck
from bido_back.purchasing import service as purchasing_service
from bido_back.purchasing.schemas import AnalysisRequest, CurrencyTotalView
from bido_back.purchasing.sources.contracts import ScopeRequest, SourceAssessment
from bido_back.purchasing.sources.handoff import (
    AssessmentInvitedSupplier,
    AssessmentSkippedSupplier,
)
from bido_back.purchasing.sources.mapping import default_mapper
from bido_back.purchasing.sources.scope import as_request
from bido_back.purchasing.sources.service import (
    ScopeValidationError,
    SourceAccessError,
    analyze_uploads,
)
from bido_back.purchasing.sources.snapshots import (
    AssessmentRfqPlan,
    AssessmentRfqRequest,
    AssessmentSnapshotSummary,
    draft_from_snapshot,
    get_snapshot,
    list_snapshots,
    plan_from_snapshot,
    rfq_lines,
)
from bido_back.purchasing.workbook import WorkbookFileError, WorkbookImportReport, import_workbook
from bido_back.rfqs import suppliers as supplier_service
from bido_back.rfqs.errors import (
    MailboxNotConfigured,
    MailboxNotConnected,
    ReplyByRequired,
    RfqError,
)
from bido_back.rfqs.language import DEFAULT_LANGUAGE, email_language
from bido_back.rfqs.schemas import ContactIn, CreateSupplierRequest, EmailAddress
from bido_back.rfqs.service import preliminary_hold, preliminary_title

if TYPE_CHECKING:
    from bido_back.copilot.tools import CopilotToolDeps

MAX_EXTRA_OFFERS: Final = 20


def reply_by_explained(exc: ReplyByRequired) -> str:
    """A reply-by refusal with what the model should do next."""
    if exc.draft:
        return (
            f"{exc}. Ask the buyer by when suppliers must answer, set it with "
            "set_rfq_terms(response_due=...), then try again."
        )
    return (
        f"{exc}. The RFQ is no longer a draft: ask the buyer to set the reply-by date in "
        "its follow-up settings, then try again."
    )


def _explain(exc: RfqError) -> str:
    if isinstance(exc, ReplyByRequired):
        return reply_by_explained(exc)
    if isinstance(exc, MailboxNotConnected):
        return "the buyer has not connected a mailbox; emails are sent from it"
    if isinstance(exc, MailboxNotConfigured):
        return "sending from a mailbox is not set up on this server yet"
    return str(exc)


def _principal(ctx: ToolContext) -> Principal:
    if ctx.user_id is None:
        raise ToolError("the copilot needs a signed-in user")
    return Principal(tenant_id=ctx.tenant_id, user_id=ctx.user_id)


class RfqArg(BaseModel):
    rfq: str = Field(description="The RFQ's reference (e.g. RFQ-12) or id")


class ComparisonPolicyInput(ComparisonPolicy):
    minimum_on_time_rate: ModelDecimal | None = Field(default=None, ge=0, le=1)
    minimum_accepted_quantity_rate: ModelDecimal | None = Field(default=None, ge=0, le=1)


class SetComparisonPolicyIn(RfqArg):
    policy: ComparisonPolicyInput


class ImportSpreadsheetIn(BaseModel):
    upload_id: UUID = Field(description="The attachment id from attachment_ids")
    confirmed: bool = Field(
        default=False,
        description="True only after the buyer answered the questions of a previous call",
    )


class ScopeRequestInput(ScopeRequest):
    # A column value put where a field should be ("NecDep": true) is an error the model
    # reads, never silently dropped.
    model_config = ConfigDict(extra="forbid")

    # A plain number: the decimal string pattern has a lookahead strict schemas reject.
    safety_stock: ModelDecimal | None = Field(
        default=None,
        ge=0,
        lt=10**12,
        description="The buyer's explicit safety-stock policy ('sem estoque de segurança "
        "adicional' = 0); empty = the sources' own policy",
    )


class SourcesIn(BaseModel):
    upload_ids: list[UUID] = Field(
        min_length=1,
        max_length=10,
        description="Source attachments in this conversation; include related sources together",
    )
    objective: str = Field(
        min_length=1,
        max_length=2000,
        description="The buyer's question and explicit clarifications; do not invent assumptions",
    )
    scope: ScopeRequestInput | None = Field(
        default=None,
        description="Always fill it with the planning boundaries the buyer stated, in their "
        "words: product (or product_code), area only for a whole area, location for one "
        "plant, plants for several ('todos os centros' = 'all', 'três centros' = 3, or the "
        "list they named), mrp_element when they name a value of the MRP element column "
        "(NecDep, NecInd...), as_of or horizon_end as dates, horizon_days for 'the next N "
        "days', safety_stock, production_lead_days. Leave unknown fields empty: code resolves "
        "them against the evidence (the reference date from the latest stock snapshot) and "
        "asks the rest. A value the data does not hold is a validation error that lists the "
        "valid values: correct it and call again. When saving, pass the same scope as the "
        "analysis plus the buyer's answers.",
    )


class AnalyzeSourcesIn(SourcesIn):
    recompute: bool = Field(
        default=False,
        description="True only when the buyer explicitly asks to recompute or refresh an "
        "analysis of this conversation with the same files and scope; otherwise its stored "
        "result is returned",
    )


_COUNTRY_ARG = (
    "The supplier's country, ISO 3166-1 alpha-2 (BR, DE, IT...). It decides the language of "
    "the RFQ document they receive. Pass it when the buyer said it or when it is obvious from "
    "the name or the email domain: 'Ltda', 'EIRELI', 'ME', '.com.br' are BR; 'GmbH', 'AG', "
    "'.de' are DE; 'S.r.l.', 'S.p.A.', '.it' are IT; 'S.L.', '.es' are ES; 'SARL', 'SAS', "
    "'.fr' are FR; 'S.A.' is Brazil only when the rest of the name is Portuguese. When it is "
    "not obvious (an English name, a .com address), leave it empty and ASK the buyer which "
    "country the supplier is in; never guess. Without it, documents go out in English."
)
_LANGUAGE_ARG = (
    "ISO 639-1 (pt, de, it, es, fr, en), only when the buyer said which language to write to "
    "this supplier in. Left empty, Bido uses the country's language."
)


class ExplainItemIn(BaseModel):
    material: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description="The item as the buyer named it: its code (e.g. 100000152) or its name",
    )
    line: int | None = Field(
        default=None,
        ge=1,
        le=100_000,
        description="The line's position in the analysis, when the buyer named a line number",
    )

    @model_validator(mode="after")
    def names_an_item(self) -> "ExplainItemIn":
        if self.material is None and self.line is None:
            raise ValueError("give the item's code or name (material) or its line number")
        return self


class AddContactIn(BaseModel):
    supplier: str = Field(min_length=1, max_length=300, description="The supplier's name")
    email: EmailAddress
    name: str | None = Field(default=None, max_length=200, description="The person's name")
    country: Annotated[str, StringConstraints(to_upper=True, pattern=r"^[A-Za-z]{2}$")] | None = (
        Field(default=None, description=_COUNTRY_ARG)
    )
    language: Annotated[str, StringConstraints(to_lower=True, pattern=r"^[A-Za-z]{2}$")] | None = (
        Field(default=None, description=_LANGUAGE_ARG)
    )


class AssessmentArg(BaseModel):
    assessment_id: UUID


class ListAssessmentsIn(BaseModel):
    search: str | None = Field(default=None, max_length=200)
    limit: int = Field(default=20, ge=1, le=50)


class ListAssessmentsOut(BaseModel):
    assessments: list[AssessmentSnapshotSummary]


class CreateAssessmentRfqIn(AssessmentRfqRequest):
    assessment_id: UUID
    proposal_id: UUID | None = Field(default=None, description=PROPOSAL_ARG)


class AssessmentRfqOut(AwaitingConfirmation):
    # Set once the RFQ exists (not while awaiting the buyer's confirmation).
    assessment_id: UUID | None = None
    rfq_id: UUID | None = None
    ref: str | None = None
    lines: int | None = None
    reused: bool | None = None
    title: str | None = None
    preliminary: bool | None = None
    # Preliminary only: assessed lines left out (no quantity or unit yet), by part number.
    skipped: list[str] = Field(default_factory=list[str])
    # The suppliers on the draft as DRAFT invites (nothing is sent), the assessed ones it
    # could not invite with why, and the lines without default limits.
    invited: list[AssessmentInvitedSupplier] = Field(
        default_factory=list[AssessmentInvitedSupplier]
    )
    skipped_suppliers: list[AssessmentSkippedSupplier] = Field(
        default_factory=list[AssessmentSkippedSupplier]
    )
    lines_without_limits: list[int] = Field(default_factory=list[int])


def _rfq_request(args: CreateAssessmentRfqIn) -> AssessmentRfqRequest:
    """The handoff request of a tool input (proposed, or stored and confirmed), losing
    nothing: copying fields one by one silently dropped the ones added later
    (pending_validation, once), and the request forbids unknown fields, so a field the
    handoff does not take fails instead of disappearing."""
    return AssessmentRfqRequest.model_validate(
        args.model_dump(exclude={"assessment_id", "proposal_id"})
    )


class AddContactOut(BaseModel):
    supplier: str
    supplier_created: bool
    email: str
    country: str | None = Field(
        default=None,
        description="The supplier's country on file: given, or inferred from name or email",
    )
    document_language: str = Field(
        description="The language of the RFQ document this supplier receives; tell the buyer"
    )
    question: str | None = Field(
        default=None,
        description="Set when the country is unknown: ask the buyer this, then call again "
        "with the same supplier and email plus `country` to fix it",
    )


class QuantityOverride(BaseModel):
    part_number: str
    quantity: ModelDecimal = Field(ge=0)


class RefineAnalysisIn(BaseModel):
    analysis_id: UUID
    quantity: int | None = Field(
        default=None, ge=1, le=1_000_000, description="Re-run for this many units"
    )
    safety_stock_pct: int | None = Field(default=None, ge=0, le=100)
    need_by: date | None = None
    planning_start: date | None = None
    planning_end: date | None = None
    location: str | None = Field(default=None, max_length=200)
    stock_as_of: date | None = None
    max_stock_age_days: int | None = Field(default=None, ge=0, le=1095)
    exclude_parts: list[str] = Field(
        default_factory=list[str], description="Part numbers the buyer does not want to buy now"
    )
    set_quantities: list[QuantityOverride] = Field(
        default_factory=list[QuantityOverride], description="Quantities the buyer decided"
    )


class RefinedLine(BaseModel):
    part_number: str
    description: str
    to_buy: Decimal
    target_price: Decimal | None
    max_price: Decimal | None


class RefineAnalysisOut(BaseModel):
    analysis_id: UUID
    items_to_buy: int
    currency: str | None
    total_at_target: Decimal | None
    # Spend per currency; never add amounts in different currencies together.
    totals: list[CurrencyTotalView]
    lines: list[RefinedLine]


_AS_GIVEN: Final = "In the RFQ's currency, exactly as the buyer gave it"


class LineLimitIn(BaseModel):
    position: int = Field(ge=1, description="The RFQ line's position")
    target_unit_price: ModelDecimal = Field(gt=0, description=_AS_GIVEN)
    max_unit_price: ModelDecimal = Field(gt=0, description=_AS_GIVEN)


class ProposeMandateIn(BaseModel):
    rfq: str = Field(description="The RFQ's reference (e.g. RFQ-12) or id")
    deadline: date = Field(description="The last day of the negotiation, as the buyer said")
    max_rounds: int = Field(
        default=2,
        ge=1,
        le=10,
        description="Negotiation emails per supplier: by default one request for a revised "
        "offer, then one best-and-final request",
    )
    limits: list[LineLimitIn] = Field(
        default_factory=list[LineLimitIn],
        max_length=200,
        description="Only limits the buyer stated: for lines without them "
        "(lines_without_limits), or the new limits they gave for a negotiation already "
        "running; other lines keep their current limits",
    )


class ProposeMandateOut(BaseModel):
    rfq: str
    approval_id: UUID
    suppliers: int
    lines: int
    initial_emails: int
    deadline: datetime
    note: str
    # Each address and its domain check (test, real_domain, no_mail, unknown).
    recipients: list[RecipientCheck]
    # Domains the buyer must confirm in the approval before anything is sent.
    domains_to_confirm: list[str]
    # False: approving is fine now, but nothing goes out until the buyer
    # connects their mailbox (Gmail or Outlook); the emails then send on their own.
    mailbox_connected: bool = True
    # True: the negotiation already runs under a mandate; this one replaces it once
    # approved (until then the current limits keep applying).
    replaces_current_mandate: bool = False


class MandateStateOut(BaseModel):
    rfq: str
    status: str
    note: str


class OutcomeLineOut(BaseModel):
    position: int
    description: str
    quantity: Decimal
    supplier: str | None
    unit_price: Decimal | None
    lead_time_days: int | None
    within_target: bool | None
    covered: bool
    why_not: list[str]
    justification: str = ""
    readiness: str = "needs_review"
    line_total: Decimal | None = None
    landed_total: Decimal | None = None
    evidence_gaps: list[str] = Field(default_factory=list[str])
    alternatives: list[OfferAlternative] = Field(default_factory=list[OfferAlternative])
    performance: SupplierPerformance = Field(default_factory=SupplierPerformance)
    evidence_message_ids: list[UUID] = Field(default_factory=list[UUID])
    part_number: str | None = None
    unit: str | None = None
    offer: DecisionOffer | None = None
    policy_reasons: list[str] = Field(default_factory=list[str])
    excluded: list[ExcludedOffer] = Field(default_factory=list[ExcludedOffer])


class OutcomeOut(BaseModel):
    rfq: str
    negotiation_status: str
    base_currency: str
    lines: list[OutcomeLineOut]
    uncovered_lines: int
    note: str
    readiness: str = "needs_review"
    policy: ComparisonPolicy = Field(default_factory=ComparisonPolicy)


class ExtraOfferOut(BaseModel):
    supplier: str
    description: str
    mpn: str | None
    quantity: str | None
    unit_price: str | None
    currency: str | None
    conditions: str | None


class ExtraOffersOut(BaseModel):
    rfq: str
    offers: list[ExtraOfferOut]
    note: str


def _deadline(day: date) -> datetime:
    """The end of the buyer's last day, in UTC."""
    return datetime.combine(day, time(23, 59), tzinfo=UTC)


def _conversation_upload(
    requested: UUID, current: tuple[UUID, ...], earlier: tuple[UUID, ...]
) -> UUID:
    """The file a call means: one attached to this message or an earlier turn of it.

    Models sometimes copy an id wrongly. With a single attachment in this
    message there is only one file it can mean; with none, the only file of
    the conversation. Anything else is refused.
    """
    if requested in current or requested in earlier:
        return requested
    if len(current) == 1:
        return current[0]
    if not current and len(set(earlier)) == 1:
        return earlier[0]
    raise ToolError("that id is not one of the files attached in this conversation")


def _assessment_preview(
    assessment: SourceAssessment, args: CreateAssessmentRfqIn, plan: AssessmentRfqPlan
) -> ProposalPreview:
    """What `create_rfq_from_assessment` would create from the saved assessment: its lines
    (`rfq_lines`) with their default limits, and the suppliers it would invite or skip
    (`plan_from_snapshot`). The same rules as `draft_from_snapshot`, which checks again
    when it runs."""
    lines, skipped = rfq_lines(assessment)
    limits = {limit.position: limit for limit in plan.limits}
    # Assumptions and problems never stop the draft: each line carries its own, shown
    # with it, and the send approval shows them again.
    issues: list[str] = []
    if not lines:
        issues.append("the assessment has no positive purchase quantities")
    issues.extend(plan.issues)
    return ProposalPreview(
        action="create_rfq_from_assessment",
        # Set when the assessment already has its draft: confirming returns it as it is.
        rfq=plan.ref,
        title=(
            preliminary_title(args.title, args.pending_validation)[:300]
            if args.pending_validation is not None
            else args.title
        ),
        currency=args.base_currency,
        lines=[
            ProposedLine(
                position=position,
                description=line.description or line.part_number,
                part_number=line.part_number,
                quantities=[line.calculated_to_buy],
                unit=line.unit or "",
                need_by=line.need_by,
                delivery_location=line.location,
                target_unit_price=found.target_unit_price if found else None,
                max_unit_price=found.max_unit_price if found else None,
                limits_basis=found.basis if found else None,
                confidence=line.confidence,
                assumptions=[a.message for a in line.assumptions],
            )
            for position, line in enumerate(lines, start=1)
            if line.calculated_to_buy is not None
            for found in [limits.get(position)]
        ],
        suppliers=[
            ProposedSupplier(
                name=supplier.name,
                email=supplier.email,
                part_numbers=supplier.part_numbers,
                new_supplier=supplier.supplier_created,
                new_contact=supplier.contact_created,
            )
            for supplier in plan.invited
        ],
        skipped=skipped,
        skipped_suppliers=plan.skipped_suppliers,
        lines_without_limits=plan.lines_without_limits,
        issues=issues,
    )


def _contact_out(
    name: str, created: bool, email: str, country: str | None, language: str | None
) -> AddContactOut:
    question = None
    if country is None and language in (None, DEFAULT_LANGUAGE):
        question = (
            f"Which country is {name} in? Its country or language is not on file, so its RFQ "
            "documents would be in English."
        )
    return AddContactOut(
        supplier=name,
        supplier_created=created,
        email=email,
        country=country,
        document_language=email_language(language, country),
        question=question,
    )


def journey_tools(deps: "CopilotToolDeps") -> tuple[AnyTool, ...]:  # noqa: C901
    db = deps.db

    # Uploads this message's import asked about: the buyer has not answered yet.
    asked: set[UUID] = set()

    async def import_spreadsheet(
        ctx: ToolContext, args: ImportSpreadsheetIn
    ) -> WorkbookImportReport:
        principal = _principal(ctx)
        upload_id = _conversation_upload(
            args.upload_id, deps.attachment_ids, deps.earlier_attachment_ids
        )
        if deps.storage is None:
            raise ToolError("file storage is not available")
        # Only the buyer confirms, after seeing the questions: never in the message
        # that attached the file, nor after this message's import asked.
        confirmed = (
            args.confirmed and upload_id not in deps.attachment_ids and upload_id not in asked
        )
        try:
            report = await import_workbook(
                db,
                deps.storage,
                principal,
                upload_id,
                mapper=deps.sheet_mapper,
                confirmed=confirmed,
            )
        except WorkbookFileError as exc:
            raise ToolError(str(exc)) from None
        if report.status == "needs_confirmation":
            asked.add(upload_id)
        return report

    def _sources_principal(ctx: ToolContext, args: SourcesIn) -> Principal:
        principal = _principal(ctx)
        allowed = set(deps.attachment_ids) | set(deps.earlier_attachment_ids)
        if not set(args.upload_ids) <= allowed:
            raise ToolError("that id is not one of the files attached in this conversation")
        if deps.storage is None:
            raise ToolError("file storage is not available")
        return principal

    async def analyze_procurement_sources(
        ctx: ToolContext, args: AnalyzeSourcesIn
    ) -> SourceAssessment:
        principal = _sources_principal(ctx, args)
        conversation = deps.conversation_id
        if conversation is None:
            return await _analyze(ctx, principal, args, persist=False)
        # The conversation's analyses: the same inputs are never recomputed, and a
        # narrower call never replaces the analysis the buyer is asking about.
        request = as_request(args.scope)
        key = analyses.input_key(args.upload_ids, request)
        stored = await analyses.recent(db, principal, conversation)
        same = None
        if not args.recompute:
            same = await analyses.find(db, principal, conversation, key) or next(
                (s for s in stored if analyses.same_inputs(args.upload_ids, request, s)), None
            )
        if same is not None:
            await analyses.touch(db, principal, same.id)
            return same.assessment
        latest = stored[0] if stored else None
        narrowed = latest is not None and analyses.narrower(args.upload_ids, request, latest)
        item = analyses.requested_item(request)
        if latest is not None and narrowed and item is not None:
            found = analyses.matching_lines(latest.assessment, item)
            if found:
                raise ToolError(
                    f"not recomputed: {item!r} is line {found[0] + 1} of the analysis this "
                    "conversation already ran on these files; a narrower run would not "
                    "see the rest of its plan. Call explain_analysis_item with material="
                    f"{item!r} and answer from its record. Run this tool again only when the "
                    "buyer changes the files, the horizon or the plants, or asks to recompute."
                )
        assessment = await _analyze(ctx, principal, args, persist=False)
        if not narrowed:
            await analyses.store(
                db,
                principal,
                conversation_id=conversation,
                run_id=ctx.run_id,
                key=key,
                upload_ids=args.upload_ids,
                request=request,
                assessment=assessment,
            )
        return assessment

    async def _analyze(
        ctx: ToolContext, principal: Principal, args: SourcesIn, *, persist: bool
    ) -> SourceAssessment:
        assert deps.storage is not None
        try:
            return await analyze_uploads(
                db,
                deps.storage,
                principal,
                args.upload_ids,
                args.objective,
                ctx,
                deps.source_mapper or default_mapper(),
                persist=persist,
                scope=args.scope,
            )
        except SourceAccessError as exc:
            raise ToolError(str(exc)) from None
        except ScopeValidationError as exc:
            raise ToolValidationError(
                str(exc),
                code=exc.code,
                field=f"scope.{exc.field}",
                value=exc.value,
                valid_values=exc.valid_values,
            ) from None

    async def save_procurement_assessment(ctx: ToolContext, args: SourcesIn) -> SourceAssessment:
        return await _analyze(ctx, _sources_principal(ctx, args), args, persist=True)

    async def explain_analysis_item(ctx: ToolContext, args: ExplainItemIn) -> AnalysisItemOut:
        principal = _principal(ctx)
        stored = (
            await analyses.latest(db, principal, deps.conversation_id)
            if deps.conversation_id is not None
            else None
        )
        return analyses.explain_item(stored, material=args.material, position=args.line)

    async def list_procurement_assessments(
        ctx: ToolContext, args: ListAssessmentsIn
    ) -> ListAssessmentsOut:
        return ListAssessmentsOut(
            assessments=await list_snapshots(
                db, _principal(ctx), search=args.search, limit=args.limit
            )
        )

    async def get_procurement_assessment(ctx: ToolContext, args: AssessmentArg) -> SourceAssessment:
        try:
            return await get_snapshot(db, _principal(ctx), args.assessment_id)
        except purchasing_service.PurchasingError as exc:
            raise ToolError(str(exc)) from None

    async def set_comparison_policy(
        ctx: ToolContext, args: SetComparisonPolicyIn
    ) -> ComparisonPolicy:
        principal = _principal(ctx)
        async with tenant_session(db, ctx.tenant_id) as session:
            rfq = await queries.find_rfq(session, args.rfq)
            if rfq is None:
                raise ToolError("RFQ not found")
            rfq_id = rfq.id
        try:
            return await negotiation_service.set_comparison_policy(
                db, principal, rfq_id, ComparisonPolicy.model_validate(args.policy.model_dump())
            )
        except RfqError as exc:
            raise ToolError(str(exc)) from None

    async def create_rfq_from_assessment(
        ctx: ToolContext, args: CreateAssessmentRfqIn
    ) -> AssessmentRfqOut:
        principal = _principal(ctx)
        if deps.conversation_id is None:
            raise ToolError("this action needs a conversation to ask the buyer in")
        if args.proposal_id is None:
            try:
                assessment, plan = await plan_from_snapshot(
                    db, principal, args.assessment_id, _rfq_request(args)
                )
            except purchasing_service.PurchasingError as exc:
                raise ToolError(str(exc)) from None
            preview = _assessment_preview(assessment, args, plan)
            proposal_id = await proposals.propose(
                db,
                principal,
                conversation_id=deps.conversation_id,
                run_id=ctx.run_id,
                action="create_rfq_from_assessment",
                args=args.model_dump(mode="json", exclude={"proposal_id"}),
                preview={"shown": preview.model_dump(mode="json")},
            )
            return AssessmentRfqOut(
                awaiting_confirmation=True, proposal_id=proposal_id, preview=preview
            )
        try:
            claimed = await proposals.claim(
                db,
                principal,
                conversation_id=deps.conversation_id,
                run_id=ctx.run_id,
                action="create_rfq_from_assessment",
                proposal_id=args.proposal_id,
            )
        except proposals.ProposalRefused as exc:
            raise ToolError(str(exc)) from None
        stored = CreateAssessmentRfqIn.model_validate(claimed.args)
        try:
            result = await draft_from_snapshot(
                db, principal, stored.assessment_id, _rfq_request(stored)
            )
        except purchasing_service.PurchasingError as exc:
            await proposals.release(db, principal, args.proposal_id)
            raise ToolError(str(exc)) from None
        return AssessmentRfqOut.model_validate(
            {**result.model_dump(), "proposal_id": args.proposal_id}
        )

    async def add_supplier_contact(ctx: ToolContext, args: AddContactIn) -> AddContactOut:
        principal = _principal(ctx)
        async with tenant_session(db, ctx.tenant_id) as session:
            supplier = await session.scalar(
                select(Supplier).where(func.lower(Supplier.name) == args.supplier.strip().lower())
            )
            exists = country = language = None
            if supplier is not None:
                exists = await session.scalar(
                    select(Contact.id)
                    .join(SupplierContact, SupplierContact.contact_id == Contact.id)
                    .where(
                        SupplierContact.supplier_id == supplier.id,
                        func.lower(Contact.email) == args.email.lower(),
                        Contact.pseudonymized_at.is_(None),
                    )
                )
                # The buyer answered which country (or language) an existing supplier is
                # in: that is the fix for a supplier saved without one.
                if args.country is not None:
                    supplier.country = args.country
                if args.language is not None:
                    supplier.language = args.language
                country, language = supplier.country, supplier.language
            supplier_id = supplier.id if supplier else None
            name = supplier.name if supplier else args.supplier.strip()
        contact = ContactIn(email=args.email, name=args.name)
        if supplier_id is None:
            view = await supplier_service.create_supplier(
                db,
                principal,
                CreateSupplierRequest(
                    name=name,
                    country=args.country,
                    language=args.language or DEFAULT_LANGUAGE,
                    contacts=[contact],
                ),
            )
            return _contact_out(name, True, args.email, view.country, view.language)
        if exists is None:
            await supplier_service.add_contact(db, principal, supplier_id, contact)
        return _contact_out(name, False, args.email, country, language)

    async def refine_analysis(ctx: ToolContext, args: RefineAnalysisIn) -> RefineAnalysisOut:
        principal = _principal(ctx)
        analysis_id = args.analysis_id
        try:
            if any(
                value is not None
                for value in (
                    args.quantity,
                    args.safety_stock_pct,
                    args.need_by,
                    args.planning_start,
                    args.planning_end,
                    args.location,
                    args.stock_as_of,
                    args.max_stock_age_days,
                )
            ):
                current = await purchasing_service.get_analysis(db, principal, analysis_id)
                from_plan = current.demand_source == "production_plan" and args.quantity is None
                rerun = await purchasing_service.create_analysis(
                    db,
                    principal,
                    AnalysisRequest(
                        product=current.product,
                        quantity=(
                            Decimal(args.quantity)
                            if args.quantity
                            else (None if from_plan else current.quantity)
                        ),
                        need_by=args.need_by if from_plan else args.need_by or current.need_by,
                        safety_stock_pct=Decimal(args.safety_stock_pct)
                        if args.safety_stock_pct is not None
                        else current.safety_stock_pct,
                        lookback_days=current.lookback_days,
                        planning_start=args.planning_start or current.planning_start,
                        planning_end=(
                            args.planning_end
                            or (args.need_by if from_plan else None)
                            or current.planning_end
                        ),
                        location=args.location or current.location,
                        stock_as_of=args.stock_as_of or current.stock_as_of,
                        max_stock_age_days=(
                            args.max_stock_age_days
                            if args.max_stock_age_days is not None
                            else current.max_stock_age_days
                        ),
                    ),
                    today=deps.today(),
                )
                analysis_id = rerun.id
            view = await purchasing_service.adjust_analysis(
                db,
                principal,
                analysis_id,
                exclude_parts=args.exclude_parts,
                quantities={o.part_number: o.quantity for o in args.set_quantities},
            )
        except purchasing_service.PurchasingError as exc:
            raise ToolError(str(exc)) from None
        return RefineAnalysisOut(
            analysis_id=view.id,
            items_to_buy=view.summary.items_to_buy,
            currency=view.summary.currency,
            total_at_target=view.summary.total_at_target,
            totals=view.summary.totals,
            lines=[
                RefinedLine(
                    part_number=line.part_number,
                    description=line.description,
                    to_buy=line.to_buy,
                    target_price=line.target_price,
                    max_price=line.max_price,
                )
                for line in view.lines
                if line.to_buy > 0
            ],
        )

    async def propose_negotiation_mandate(
        ctx: ToolContext, args: ProposeMandateIn
    ) -> ProposeMandateOut:
        principal = _principal(ctx)
        async with tenant_session(db, ctx.tenant_id) as session:
            rfq = await queries.find_rfq(session, args.rfq)
            if rfq is None:
                raise ToolError(f"no RFQ {args.rfq!r}; use list_rfqs to find it")
            if (hold := preliminary_hold(rfq)) is not None:
                raise ToolError(hold)
            rfq_id, ref = rfq.id, rfq.ref
            line_ids = {line.position: line.id for line in await queries.lines(session, rfq_id)}
        if args.deadline < deps.today():
            raise ToolError("the deadline is in the past")
        limits: list[LineLimitsUpdate] = []
        for given in args.limits:
            if given.position not in line_ids:
                raise ToolError(f"{ref} has no line {given.position}")
            if given.target_unit_price > given.max_unit_price:
                raise ToolError(f"line {given.position}: the target is above the maximum")
            limits.append(
                LineLimitsUpdate(
                    rfq_line_id=line_ids[given.position],
                    target_unit_price=given.target_unit_price,
                    max_unit_price=given.max_unit_price,
                )
            )
        try:
            proposed = await mandates.propose_mandate(
                db,
                principal,
                rfq_id,
                ProposeMandateRequest(
                    deadline=_deadline(args.deadline), max_rounds=args.max_rounds, lines=limits
                ),
                writer=deps.agents.email_writer,
                mailboxes=deps.mailboxes,
            )
        except RfqError as exc:
            raise ToolError(_explain(exc)) from None
        return ProposeMandateOut(
            rfq=ref,
            approval_id=proposed.approval_id,
            suppliers=proposed.suppliers,
            lines=proposed.lines,
            initial_emails=proposed.initial_emails,
            deadline=proposed.deadline,
            note=(
                "Nothing was sent. One approval covers the initial emails and the "
                "negotiation's limits; once approved, follow-ups and counter-offers within "
                "those limits go out automatically until the deadline or until closed. "
                "Emails go only to test domains or domains the buyer confirms in that "
                "approval: list every recipient and warn about domains of real companies."
                + (
                    ""
                    if proposed.mailbox_connected
                    else " The buyer has not connected a mailbox yet: the approval can be given "
                    "now, but sending starts only once they connect Gmail or Outlook "
                    "(onboarding); tell them so."
                )
                + (
                    " This replaces the negotiation's current mandate once approved: until "
                    "then the current limits keep applying; on approval, emails prepared "
                    "under the old limits are withdrawn and decided again with the new ones."
                    if proposed.replaces_mandate_id is not None
                    else ""
                )
            ),
            recipients=proposed.recipients,
            domains_to_confirm=sorted(
                {r.domain for r in proposed.recipients if not r.will_send and r.domain}
            ),
            mailbox_connected=proposed.mailbox_connected,
            replaces_current_mandate=proposed.replaces_mandate_id is not None,
        )

    async def _set_paused(ctx: ToolContext, args: RfqArg, *, paused: bool) -> MandateStateOut:
        principal = _principal(ctx)
        async with tenant_session(db, ctx.tenant_id) as session:
            rfq = await queries.find_rfq(session, args.rfq)
            if rfq is None:
                raise ToolError(f"no RFQ {args.rfq!r}")
            rfq_id, ref = rfq.id, rfq.ref
        try:
            view = await mandates.set_paused(db, principal, rfq_id, paused=paused)
        except RfqError as exc:
            raise ToolError(str(exc)) from None
        return MandateStateOut(
            rfq=ref,
            status=view.status,
            note="Automatic emails are stopped." if paused else "Automatic emails resumed.",
        )

    async def pause_negotiation(ctx: ToolContext, args: RfqArg) -> MandateStateOut:
        return await _set_paused(ctx, args, paused=True)

    async def resume_negotiation(ctx: ToolContext, args: RfqArg) -> MandateStateOut:
        return await _set_paused(ctx, args, paused=False)

    async def _outcome(ctx: ToolContext, rfq_ref: str) -> OutcomeOut:
        principal = _principal(ctx)
        async with tenant_session(db, ctx.tenant_id) as session:
            rfq = await queries.find_rfq(session, rfq_ref)
            if rfq is None:
                raise ToolError(f"no RFQ {rfq_ref!r}")
            rfq_id, ref, status = rfq.id, rfq.ref, rfq.negotiation_status
            lines = {line.id: line for line in await queries.lines(session, rfq.id)}
        try:
            comparison = await load_comparison(db, principal, rfq_id, deps.today())
        except RfqError as exc:
            raise ToolError(str(exc)) from None
        out: list[OutcomeLineOut] = []
        for rec in comparison.recommendations:
            if rec.tier != 0:
                continue
            line = lines.get(rec.rfq_line_id)
            target = line.target_unit_price if line else None
            price = rec.effective_unit_price_base
            out.append(
                OutcomeLineOut(
                    position=line.position if line else 0,
                    description=line.description if line else "",
                    quantity=rec.quantity,
                    supplier=rec.supplier_name,
                    unit_price=price,
                    lead_time_days=rec.lead_time_days,
                    within_target=(price <= target) if price is not None and target else None,
                    covered=rec.invite_id is not None,
                    why_not=[str(r) for r in rec.reasons] if rec.invite_id is None else [],
                    justification=rec.justification,
                    readiness=rec.readiness,
                    line_total=rec.line_total_base,
                    landed_total=rec.landed_total_base,
                    evidence_gaps=rec.evidence_gaps,
                    alternatives=rec.alternatives,
                    performance=rec.performance,
                    evidence_message_ids=rec.evidence_message_ids,
                    part_number=line.part_number if line else None,
                    unit=line.unit if line else None,
                    offer=rec.offer,
                    policy_reasons=rec.policy_reasons,
                    excluded=rec.excluded,
                )
            )
        uncovered = sum(1 for line in out if not line.covered)
        note = (
            "Every line has a candidate offer for buyer review. Check readiness and evidence gaps."
            if not uncovered
            else (
                f"{uncovered} line(s) have no eligible offer from the buyer's suppliers: the "
                "buyer can review the covered lines, request missing evidence, or add "
                "more suppliers or contacts. No order is created by this workflow."
            )
        )
        return OutcomeOut(
            rfq=ref,
            negotiation_status=status,
            base_currency=comparison.base_currency,
            lines=sorted(out, key=lambda line: line.position),
            uncovered_lines=uncovered,
            note=note,
            readiness=comparison.readiness,
            policy=comparison.policy,
        )

    async def close_negotiation(ctx: ToolContext, args: RfqArg) -> OutcomeOut:
        principal = _principal(ctx)
        async with tenant_session(db, ctx.tenant_id) as session:
            rfq = await queries.find_rfq(session, args.rfq)
            if rfq is None:
                raise ToolError(f"no RFQ {args.rfq!r}")
            rfq_id = rfq.id
        try:
            await negotiation_service.close(db, principal, rfq_id)
        except RfqError as exc:
            raise ToolError(str(exc)) from None
        return await _outcome(ctx, args.rfq)

    async def get_negotiation_outcome(ctx: ToolContext, args: RfqArg) -> OutcomeOut:
        return await _outcome(ctx, args.rfq)

    async def list_extra_offers(ctx: ToolContext, args: RfqArg) -> ExtraOffersOut:
        _principal(ctx)
        async with tenant_session(db, ctx.tenant_id) as session:
            rfq = await queries.find_rfq(session, args.rfq)
            if rfq is None:
                raise ToolError(f"no RFQ {args.rfq!r}")
            latest = (
                select(Proposal.invite_id, func.max(Proposal.version).label("version"))
                .join(Invite, Invite.id == Proposal.invite_id)
                .where(Invite.rfq_id == rfq.id)
                .group_by(Proposal.invite_id)
                .subquery()
            )
            rows = await session.execute(
                select(Proposal, Supplier.name)
                .join(
                    latest,
                    (latest.c.invite_id == Proposal.invite_id)
                    & (latest.c.version == Proposal.version),
                )
                .join(Invite, Invite.id == Proposal.invite_id)
                .join(Supplier, Supplier.id == Invite.supplier_id)
            )
            offers: list[ExtraOfferOut] = []
            for proposal, supplier_name in rows:
                offers.extend(_offers(proposal.fields.get("extra_offers"), supplier_name))
            ref = rfq.ref
        return ExtraOffersOut(
            rfq=ref,
            offers=offers[:MAX_EXTRA_OFFERS],
            note=(
                "These were not requested and are not in the comparison. Mention the relevant "
                "ones briefly, with an honest reason they could help; never pressure."
            ),
        )

    ref_hint = " Pass the RFQ reference (RFQ-12) or id."
    return (
        Tool(
            "set_comparison_policy",
            "Save the buyer's declared price/reliability priorities and evidence thresholds "
            "for this RFQ. Do not invent preferences. Does not start a negotiation or send email.",
            SetComparisonPolicyIn,
            ComparisonPolicy,
            "draft",
            set_comparison_policy,
        ),
        Tool(
            "save_procurement_assessment",
            "Analyze the conversation's procurement sources and save an immutable evidence "
            "snapshot. Returns assessment_id for later retrieval and draft RFQ creation. "
            "Use when the buyer asks to save the analysis or proceed to an RFQ. Pass the same "
            "upload_ids, objective and scope as the analyze_procurement_sources call the buyer "
            "saw (their words plus any answers, not the resolved values), so the saved snapshot "
            "resolves the same scope. Nothing is sent.",
            SourcesIn,
            SourceAssessment,
            "draft",
            save_procurement_assessment,
            model_view=assessment_view,
        ),
        Tool(
            "get_procurement_assessment",
            "Retrieve this buyer's saved source assessment by its assessment_id.",
            AssessmentArg,
            SourceAssessment,
            "read",
            get_procurement_assessment,
            model_view=assessment_view,
        ),
        Tool(
            "list_procurement_assessments",
            "Find the buyer's saved source assessments across conversations, optionally by "
            "objective text. Retrieve a selected id with get_procurement_assessment.",
            ListAssessmentsIn,
            ListAssessmentsOut,
            "read",
            list_procurement_assessments,
        ),
        Tool(
            "create_rfq_from_assessment",
            "Create one draft RFQ from validated saved source quantities, units, dates and "
            "evidence. Blocks unresolved assessments unless pending_validation makes it "
            "PRELIMINARY. Requires the buyer's RFQ currency. It also invites each line's "
            "assessed suppliers (suggested first) as DRAFT invites, creating a supplier only "
            "from the files' company name and email, and gives each line a default "
            "target/maximum from the internal estimate; skipped_suppliers and "
            "lines_without_limits say what is missing. Nothing is sent or approved: the buyer "
            "approves suppliers and limits in the negotiation mandate. Two calls: the first "
            "(no proposal_id) does nothing and answers a preview with a proposal_id; show it "
            "to the buyer and ask. Call again with that proposal_id only after they confirm "
            "in their own later message.",
            CreateAssessmentRfqIn,
            AssessmentRfqOut,
            "draft",
            create_rfq_from_assessment,
        ),
        Tool(
            "analyze_procurement_sources",
            "Read every row of the attached procurement sources and cross-reference them: "
            "production orders and BOMs, stock snapshots, open orders, receipts, purchase "
            "history, suppliers and direct material demand. Always pass `scope` with the "
            "buyer's planning boundaries as they said them (product words or code, plant, "
            "horizon as dates or days, safety stock) and leave unknowns empty: in this same "
            "call code finds the exact product code and plant, takes the reference date from "
            "the latest stock snapshot when none was given, and derives production/BOM demand "
            "netted against stock and timely deliveries. `scope` in the result is the resolved "
            "scope; `scope_resolution` lists the assumptions to disclose, the questions "
            "(with options) to ask when a boundary cannot be settled without guessing, and "
            "where each value came from (buyer, evidence or default). Returns source-reported "
            "quantities separately from validated calculations, with evidence and specific "
            "issues. No BOM required for direct demand. Read-only: nothing imported or sent. "
            'Examples of scope: "Motor MTR-100 na Planta A, da exportação até 14 dias depois, '
            'sem estoque de segurança adicional" -> {"product": "Motor MTR-100", "location": '
            '"Planta A", "horizon_days": 14, "safety_stock": 0}; "MTR-100 at Plant A from '
            '2026-10-08 to 2026-10-22" -> {"product_code": "MTR-100", "location": "Plant A", '
            '"as_of": "2026-10-08", "horizon_end": "2026-10-22"}; "tudo da área de Manutenção '
            'até 20/10/2026, a montagem leva 2 dias" -> {"area": "Manutenção", "horizon_end": '
            '"2026-10-20", "production_lead_days": 2}; "analisa o motor" -> {"product": '
            '"motor"}. A large result is cut for you: `lines` are read in full (lines to '
            "buy first), `more_lines` are one-row summaries of the others and `lines_left_out` "
            "counts the lines not shown; cell references are only counted (`evidence_cells`). "
            "Never present the lines shown as all of them. `found` lists the plants, MRP "
            "elements, products and need dates the sources hold. Every evaluated line has a "
            "one-line reason: `timing_alerts[].reason` for alerts, `alerts_excluded[]` for "
            "the lines that are not. A question about an item or a conclusion of an analysis "
            "already run in this conversation is answered with explain_analysis_item, never by "
            "calling this again: call it again only when the buyer changes the files, the "
            "horizon or the plants, or asks to recompute. The same files and an equivalent "
            "scope return the stored result unchanged; a narrower call (an item's code as "
            "product_code) is not run when that item is in the stored analysis.",
            AnalyzeSourcesIn,
            SourceAssessment,
            "read",
            analyze_procurement_sources,
            model_view=assessment_view,
        ),
        Tool(
            "explain_analysis_item",
            "Explain one item (material code or name) or line of the analysis this "
            "conversation already ran, from its stored result as code computed it: demand by "
            "date, stock and safety stock, open orders and when they arrive, the netting and "
            "quantity to buy, the timing status with its shortage and stockout dates, the "
            "chosen supplier and why (lead time, arrival, the source it switched from), "
            "expedites, assumptions, and its timing alert with `reason` or why it is not an "
            "alert. Use it for every follow-up about an item or conclusion of that analysis "
            "('e o item 100000152, por quê?', 'por que comprar X?', 'quando chega o pedido?'). "
            "Nothing is recomputed. Says so when the conversation has no analysis yet or the "
            "item is not in it.",
            ExplainItemIn,
            AnalysisItemOut,
            "read",
            explain_analysis_item,
            model_view=analysis_item_view,
        ),
        Tool(
            "import_spreadsheet",
            "Import a spreadsheet the buyer attached (any layout, every sheet): parts, BOM, "
            "stock, purchase history, production plan, supplier contacts. If it returns "
            "needs_confirmation, ask the buyer its questions and call again with "
            "confirmed=true only after they answer.",
            ImportSpreadsheetIn,
            WorkbookImportReport,
            "draft",
            import_spreadsheet,
        ),
        Tool(
            "add_supplier_contact",
            "Save a supplier's contact email the buyer gave in the chat (creates the "
            "supplier when new), with its country when known: it decides the language of "
            "the RFQ document the supplier receives. Called again with `country` for an "
            "existing supplier, it records the country the buyer answered.",
            AddContactIn,
            AddContactOut,
            "draft",
            add_supplier_contact,
        ),
        Tool(
            "refine_analysis",
            "Change a purchase recommendation before its RFQ exists: drop parts, set "
            "quantities, or re-run it for another quantity, safety stock or date. Returns "
            "the analysis_id to use from now on.",
            RefineAnalysisIn,
            RefineAnalysisOut,
            "draft",
            refine_analysis,
        ),
        Tool(
            "propose_negotiation_mandate",
            "When the buyer says to go ahead and negotiate: record ONE approval that covers "
            "the initial RFQ emails and the negotiation's limits (targets and maximums "
            "from the analysis) until the deadline. Nothing is sent until it is approved. "
            "Needs the RFQ's reply-by date (response_due, today or later) when initial "
            "emails are still to be written: if it is missing or past, ask the buyer for it "
            "and set it with set_rfq_terms first. To change the limits, deadline or rounds "
            "of a negotiation already running under a mandate, call it again with the new "
            "values: it proposes a replacement that takes over once approved." + ref_hint,
            ProposeMandateIn,
            ProposeMandateOut,
            "draft",
            propose_negotiation_mandate,
            artifact_kind="approvals",
        ),
        Tool(
            "pause_negotiation",
            "Stop the automatic emails of an RFQ's negotiation now." + ref_hint,
            RfqArg,
            MandateStateOut,
            "draft",
            pause_negotiation,
        ),
        Tool(
            "resume_negotiation",
            "Resume the automatic emails of a paused negotiation." + ref_hint,
            RfqArg,
            MandateStateOut,
            "draft",
            resume_negotiation,
        ),
        Tool(
            "close_negotiation",
            "Close the negotiation now, when the buyer says it is good enough; returns the "
            "best offer per line and what is not covered. Nothing is awarded." + ref_hint,
            RfqArg,
            OutcomeOut,
            "draft",
            close_negotiation,
        ),
        Tool(
            "get_negotiation_outcome",
            "The current best eligible offer per line, and which lines no supplier "
            "covers." + ref_hint,
            RfqArg,
            OutcomeOut,
            "read",
            get_negotiation_outcome,
        ),
        Tool(
            "list_extra_offers",
            "Offers suppliers made for things the RFQ did not ask for (volume discounts, "
            "other items, bundles)." + ref_hint,
            RfqArg,
            ExtraOffersOut,
            "read",
            list_extra_offers,
        ),
    )


def _offers(raw: object, supplier: str) -> list[ExtraOfferOut]:
    if not isinstance(raw, list):
        return []
    out: list[ExtraOfferOut] = []
    for entry in cast(list[object], raw):
        try:
            draft = ExtraOfferDraft.model_validate(entry)
        except ValidationError:
            continue
        out.append(
            ExtraOfferOut(
                supplier=supplier,
                description=draft.description[:300],
                mpn=draft.mpn,
                quantity=draft.quantity,
                unit_price=draft.unit_price,
                currency=draft.currency,
                conditions=draft.conditions[:300] if draft.conditions else None,
            )
        )
    return out
