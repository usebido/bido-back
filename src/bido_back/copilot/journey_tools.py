"""Copilot tools for the buyer's journey: spreadsheet in, mandate out, outcome back.

- `import_spreadsheet` (draft): any workbook the buyer attached, every sheet;
  rules first, the sheet mapper for the rest, every value validated; asks
  before writing when unsure (`purchasing.workbook`).
- `add_supplier_contact` (draft): a supplier's contact email, from the chat.
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
from typing import TYPE_CHECKING, Final, cast
from uuid import UUID

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, select

from bido_back.agents.catalog.contracts import ExtraOfferDraft, ModelDecimal
from bido_back.agents.context import ToolContext
from bido_back.agents.tools import AnyTool, Tool, ToolError
from bido_back.auth import Principal
from bido_back.comparison.loader import load_comparison
from bido_back.copilot import queries
from bido_back.db import tenant_session
from bido_back.db.models import Contact, Invite, Proposal, Supplier, SupplierContact
from bido_back.mandates import service as mandates
from bido_back.mandates.schemas import ProposeMandateRequest
from bido_back.negotiation import service as negotiation_service
from bido_back.outbound.domains import RecipientCheck
from bido_back.purchasing import service as purchasing_service
from bido_back.purchasing.schemas import AnalysisRequest
from bido_back.purchasing.workbook import WorkbookFileError, WorkbookImportReport, import_workbook
from bido_back.rfqs import suppliers as supplier_service
from bido_back.rfqs.errors import MailboxNotConfigured, MailboxNotConnected, RfqError
from bido_back.rfqs.schemas import ContactIn, CreateSupplierRequest, EmailAddress

if TYPE_CHECKING:
    from bido_back.copilot.tools import CopilotToolDeps

MAX_EXTRA_OFFERS: Final = 20


def _explain(exc: RfqError) -> str:
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


class ImportSpreadsheetIn(BaseModel):
    upload_id: UUID = Field(description="The attachment id from attachment_ids")
    confirmed: bool = Field(
        default=False,
        description="True only after the buyer answered the questions of a previous call",
    )


class AddContactIn(BaseModel):
    supplier: str = Field(min_length=1, max_length=300, description="The supplier's name")
    email: EmailAddress
    name: str | None = Field(default=None, max_length=200, description="The person's name")


class AddContactOut(BaseModel):
    supplier: str
    supplier_created: bool
    email: str


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
    lines: list[RefinedLine]


class ProposeMandateIn(BaseModel):
    rfq: str = Field(description="The RFQ's reference (e.g. RFQ-12) or id")
    deadline: date = Field(description="The last day of the negotiation, as the buyer said")
    max_rounds: int = Field(default=3, ge=1, le=10)


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


class OutcomeOut(BaseModel):
    rfq: str
    negotiation_status: str
    base_currency: str
    lines: list[OutcomeLineOut]
    uncovered_lines: int
    note: str


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


def journey_tools(deps: "CopilotToolDeps") -> tuple[AnyTool, ...]:  # noqa: C901
    db = deps.db

    async def import_spreadsheet(
        ctx: ToolContext, args: ImportSpreadsheetIn
    ) -> WorkbookImportReport:
        principal = _principal(ctx)
        if args.upload_id not in deps.attachment_ids:
            raise ToolError("that id is not one of this message's attachments")
        if deps.storage is None:
            raise ToolError("file storage is not available")
        try:
            return await import_workbook(
                db,
                deps.storage,
                principal,
                args.upload_id,
                mapper=deps.sheet_mapper,
                confirmed=args.confirmed,
            )
        except WorkbookFileError as exc:
            raise ToolError(str(exc)) from None

    async def add_supplier_contact(ctx: ToolContext, args: AddContactIn) -> AddContactOut:
        principal = _principal(ctx)
        async with tenant_session(db, ctx.tenant_id) as session:
            supplier = await session.scalar(
                select(Supplier).where(func.lower(Supplier.name) == args.supplier.strip().lower())
            )
            exists = None
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
            supplier_id = supplier.id if supplier else None
            name = supplier.name if supplier else args.supplier.strip()
        contact = ContactIn(email=args.email, name=args.name)
        if supplier_id is None:
            await supplier_service.create_supplier(
                db, principal, CreateSupplierRequest(name=name, contacts=[contact])
            )
            return AddContactOut(supplier=name, supplier_created=True, email=args.email)
        if exists is None:
            await supplier_service.add_contact(db, principal, supplier_id, contact)
        return AddContactOut(supplier=name, supplier_created=False, email=args.email)

    async def refine_analysis(ctx: ToolContext, args: RefineAnalysisIn) -> RefineAnalysisOut:
        principal = _principal(ctx)
        analysis_id = args.analysis_id
        try:
            if args.quantity or args.safety_stock_pct is not None or args.need_by:
                current = await purchasing_service.get_analysis(db, principal, analysis_id)
                rerun = await purchasing_service.create_analysis(
                    db,
                    principal,
                    AnalysisRequest(
                        product=current.product,
                        quantity=Decimal(args.quantity) if args.quantity else current.quantity,
                        need_by=args.need_by or current.need_by,
                        safety_stock_pct=Decimal(args.safety_stock_pct)
                        if args.safety_stock_pct is not None
                        else current.safety_stock_pct,
                        lookback_days=current.lookback_days,
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
            rfq_id, ref = rfq.id, rfq.ref
        if args.deadline < deps.today():
            raise ToolError("the deadline is in the past")
        try:
            proposed = await mandates.propose_mandate(
                db,
                principal,
                rfq_id,
                ProposeMandateRequest(
                    deadline=_deadline(args.deadline), max_rounds=args.max_rounds
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
            ),
            recipients=proposed.recipients,
            domains_to_confirm=sorted(
                {r.domain for r in proposed.recipients if not r.will_send and r.domain}
            ),
            mailbox_connected=proposed.mailbox_connected,
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
                )
            )
        uncovered = sum(1 for line in out if not line.covered)
        note = (
            "Every line has an offer that can be ordered."
            if not uncovered
            else (
                f"{uncovered} line(s) have no eligible offer from the buyer's suppliers: the "
                "buyer's network was not enough for them. Offer a partial order with the "
                "covered lines (prepare_award with those lines), or ask the buyer to add "
                "more suppliers or contacts."
            )
        )
        return OutcomeOut(
            rfq=ref,
            negotiation_status=status,
            base_currency=comparison.base_currency,
            lines=sorted(out, key=lambda line: line.position),
            uncovered_lines=uncovered,
            note=note,
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
            "supplier when new).",
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
            "from the analysis) until the deadline. Nothing is sent until it is approved."
            + ref_hint,
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
