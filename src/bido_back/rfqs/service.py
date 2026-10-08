"""RFQ services: create from text, read, edit lines, invite suppliers, prepare sends.

Every function takes the caller's `Principal` and works in one
`tenant_session` for that tenant (forced RLS). Agent runs (seconds) happen
OUTSIDE any transaction; their results are written afterwards.

Preparing a send never sends. It first checks that the RFQ has a reply-by
date of today or later when there is an invite to prepare
(`bido_back.rfqs.readiness`, `ReplyByRequired` otherwise), then that the
caller (the buyer) has a connected mailbox, since that is where the emails will go out from
(`MailboxNotConnected` otherwise). It pins the RFQ to the template version
its emails use (`bido_back.templates.service.resolve_for_rfq`, recorded at
the first send). Then, for each invite still in `draft`, it renders the email
from that template in the supplier's language (`bido_back.templates.render`;
the email writer agent only translates the buyer's own template text when the
languages differ), checks it (`rfq_email_problems`), makes sure the subject
carries the `[RFQ-<n>]` marker, records an `ApprovalProposal` for the act
tool `send_rfq_email` through `PostgresApprovalSink` (requested by the
caller), and moves the invite to `pending_approval`. The proposal's run id
and tool-call id are derived from the invite, so preparing the same invite
twice (a retry, a concurrent click) records one approval: the second proposal
hits the sink's uniqueness rule and the first one stands.
"""

import importlib
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Final, cast
from uuid import UUID, uuid4

from pydantic import JsonValue
from sqlalchemy import Integer, func, select, text
from sqlalchemy import cast as sql_cast
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.agents.approvals import ApprovalProposal
from bido_back.agents.catalog.contracts import (
    LineCategory,
    RfqEmailInput,
    RfqEmailLine,
    RfqEmailOutput,
    RfqIntakeInput,
)
from bido_back.agents.catalog.definitions import rfq_email_problems
from bido_back.agents.context import ToolContext
from bido_back.agents.events import args_hash
from bido_back.approvals import ApprovalConflict, PostgresApprovalSink
from bido_back.auth import Principal
from bido_back.db import tenant_session
from bido_back.db.models import (
    Approval,
    Contact,
    Invite,
    Rfq,
    RfqLine,
    Supplier,
    SupplierContact,
    Tenant,
    User,
)
from bido_back.email.recipients import MxResolver, default_resolver
from bido_back.followups.send import SEND_FOLLOWUP_EMAIL, SendFollowupEmailArgs
from bido_back.negotiation.send import SEND_NEGOTIATION_EMAIL, SendNegotiationEmailArgs
from bido_back.onboarding.standard import TemplateContent
from bido_back.outbound.domains import RecipientCheck, check_recipients, recipients_in_args
from bido_back.rfqs.agents import EmailWriterAgent, IntakeAgent
from bido_back.rfqs.currency import CurrencyChange, change_currency, resolve_currency
from bido_back.rfqs.errors import Duplicate, InvalidState, NotFound, ReplyByRequired
from bido_back.rfqs.mailbox import MailboxSenderFactory
from bido_back.rfqs.readiness import reply_by_refusal, send_blockers, utc_today
from bido_back.rfqs.schemas import (
    AddLineRequest,
    ApprovalQueueItem,
    ApprovalStatus,
    EmailKind,
    InviteStatus,
    InviteView,
    MandateApprovalView,
    MandateLineView,
    MandateSupplierView,
    NegotiationEmailDraft,
    PreparedInvite,
    PrepareSendResponse,
    RfqCreated,
    RfqDetail,
    RfqEmailDraft,
    RfqLineView,
    RfqStatus,
    UpdateLineRequest,
    UpdateRfqRequest,
)
from bido_back.rfqs.send import (
    SEND_RFQ_EMAIL,
    SendRfqEmailArgs,
    send_proposal_ids,
    subject_marker,
)
from bido_back.templates.localize import localize
from bido_back.templates.render import RenderedRfq, RenderInput, RenderLine, render_rfq
from bido_back.templates.schemas import RfqEmailPreview
from bido_back.templates.service import (
    ResolvedTemplate,
    record_template,
    required_views,
    resolve_for_rfq,
)

logger = logging.getLogger(__name__)

Db = AsyncEngine | async_sessionmaker[AsyncSession]

REPLY_TOKENS_MODULE: Final = "bido_back.email.addresses"
_REF_PATTERN: Final = r"^RFQ-([0-9]+)$"
_EDITABLE_INVITES: Final = frozenset({"draft"})
_QUEUE_LIMIT: Final = 200


# --- Helpers -----------------------------------------------------------------------


def quantity_json(quantity: Decimal) -> int | float:
    """A quantity as a JSON number: an int when integral (the common case)."""
    if quantity == quantity.to_integral_value():
        return int(quantity)
    return float(quantity)


def quantities_from_json(raw: Sequence[object]) -> list[Decimal]:
    return [Decimal(str(value)) for value in raw]


def _new_reply_token() -> str:
    module = importlib.import_module(REPLY_TOKENS_MODULE)
    factory: object = getattr(module, "new_reply_token", None)
    if not callable(factory):
        raise RuntimeError(f"{REPLY_TOKENS_MODULE}.new_reply_token is missing")
    token: object = cast(Callable[[], object], factory)()
    if not isinstance(token, str) or not token:
        raise RuntimeError("new_reply_token did not return a token")
    return token


def _line_view(line: RfqLine) -> RfqLineView:
    return RfqLineView(
        id=line.id,
        position=line.position,
        description=line.description,
        part_number=line.part_number,
        mpn=line.mpn,
        manufacturer=line.manufacturer,
        category=cast(LineCategory, line.category),
        quantities=quantities_from_json(line.quantities),
        unit=line.unit,
        need_by=line.need_by,
        incoterm=line.incoterm,
        delivery_location=line.delivery_location,
        qualification=line.qualification,
    )


def _invite_view(invite: Invite, supplier_name: str) -> InviteView:
    return InviteView(
        id=invite.id,
        supplier_id=invite.supplier_id,
        supplier_name=supplier_name,
        contact_id=invite.contact_id,
        status=cast(InviteStatus, invite.status),
        sent_at=invite.sent_at,
        responded_at=invite.responded_at,
    )


def _summary_fields(rfq: Rfq) -> dict[str, object]:
    return {
        "id": rfq.id,
        "ref": rfq.ref,
        "title": rfq.title,
        "status": cast(RfqStatus, rfq.status),
        "response_due": rfq.response_due,
        "created_at": rfq.created_at,
        "send_blockers": send_blockers(rfq.status, rfq.response_due, utc_today()),
        "payment_terms": rfq.payment_terms,
        "quote_validity_days": rfq.quote_validity_days,
    }


async def _rfq(session: AsyncSession, rfq_id: UUID, *, lock: bool = False) -> Rfq:
    query = select(Rfq).where(Rfq.id == rfq_id)
    rfq = await session.scalar(query.with_for_update() if lock else query)
    if rfq is None:
        raise NotFound(f"rfq {rfq_id} not found")
    return rfq


async def _lines(session: AsyncSession, rfq_id: UUID) -> list[RfqLine]:
    return list(
        await session.scalars(
            select(RfqLine).where(RfqLine.rfq_id == rfq_id).order_by(RfqLine.position)
        )
    )


async def _detail(session: AsyncSession, rfq: Rfq) -> RfqDetail:
    lines = await _lines(session, rfq.id)
    invites = await session.execute(
        select(Invite, Supplier.name)
        .join(Supplier, Supplier.id == Invite.supplier_id)
        .where(Invite.rfq_id == rfq.id)
        .order_by(Invite.created_at, Invite.id)
    )
    return RfqDetail.model_validate(
        {
            **_summary_fields(rfq),
            "base_currency": rfq.base_currency,
            "lines": [_line_view(line) for line in lines],
            "invites": [_invite_view(invite, name) for invite, name in invites],
        }
    )


async def _next_ref(session: AsyncSession, tenant_id: UUID) -> str:
    # Serialize ref allocation per tenant for the rest of this transaction.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"rfq_ref:{tenant_id}"},
    )
    highest = await session.scalar(
        select(func.max(sql_cast(func.substring(Rfq.ref, _REF_PATTERN), Integer)))
    )
    return f"RFQ-{(highest or 0) + 1}"


# --- RFQs ----------------------------------------------------------------------------


async def create_rfq_from_text(
    db: Db,
    principal: Principal,
    source_text: str,
    intake: IntakeAgent,
    *,
    buyer_messages: Sequence[str] = (),
    currency: str | None = None,
) -> RfqCreated:
    """Structure the buyer's text with the intake agent and store it as a draft RFQ.

    From the Copilot, `source_text` is its summary of what to quote and
    `buyer_messages` the buyer's own words, which win on codes and terms.
    `currency` is the one the buyer stated; without it the purchase history
    decides, and when it cannot, `CurrencyRequired` is raised before the intake
    agent runs (`bido_back.rfqs.currency`).
    """
    async with tenant_session(db, principal.tenant_id) as session:
        resolved = await resolve_currency(session, stated=currency)
    context = ToolContext(tenant_id=principal.tenant_id, user_id=principal.user_id, run_id=uuid4())
    draft = await intake(
        RfqIntakeInput(
            text=source_text, base_currency=resolved.code, buyer_messages=list(buyer_messages)
        ),
        context,
    )

    async with tenant_session(db, principal.tenant_id) as session:
        rfq = Rfq(
            tenant_id=principal.tenant_id,
            ref=await _next_ref(session, principal.tenant_id),
            title=draft.title,
            status="draft",
            base_currency=resolved.code,
            response_due=draft.response_due,
            source_text=source_text,
            created_by=principal.user_id,
        )
        session.add(rfq)
        await session.flush()
        for position, line in enumerate(draft.lines, start=1):
            session.add(
                RfqLine(
                    tenant_id=principal.tenant_id,
                    rfq_id=rfq.id,
                    position=position,
                    description=line.description,
                    part_number=line.part_number,
                    mpn=line.mpn,
                    manufacturer=line.manufacturer,
                    category=line.category,
                    quantities=[quantity_json(q) for q in line.quantities],
                    unit=line.unit,
                    need_by=line.need_by,
                    incoterm=line.incoterm,
                    delivery_location=line.delivery_location,
                    qualification=line.qualification,
                )
            )
        await session.flush()
        await session.refresh(rfq)
        detail = await _detail(session, rfq)
    logger.info(
        "rfq created tenant=%s rfq=%s run=%s currency_source=%s",
        principal.tenant_id,
        rfq.id,
        context.run_id,
        resolved.source,
    )
    return RfqCreated.model_validate(
        {**detail.model_dump(), "questions": draft.questions, "currency_source": resolved.source}
    )


@dataclass(frozen=True, slots=True)
class NewRfqLine:
    """A line for `create_draft_rfq`, when the lines come from code (an analysis)."""

    description: str
    quantity: Decimal
    unit: str = "pcs"
    part_number: str | None = None
    mpn: str | None = None
    manufacturer: str | None = None
    category: LineCategory = "catalog"
    need_by: date | None = None
    qualification: str | None = None


async def create_draft_rfq(
    session: AsyncSession,
    principal: Principal,
    *,
    title: str,
    lines: Sequence[NewRfqLine],
    source_text: str | None = None,
    response_due: date | None = None,
    base_currency: str,
) -> tuple[Rfq, list[RfqLine]]:
    """Store a draft RFQ with the given lines, in the caller's tenant session.

    `base_currency` comes from `bido_back.rfqs.currency.resolve_currency`."""
    rfq = Rfq(
        tenant_id=principal.tenant_id,
        ref=await _next_ref(session, principal.tenant_id),
        title=title,
        status="draft",
        base_currency=base_currency,
        response_due=response_due,
        source_text=source_text,
        created_by=principal.user_id,
    )
    session.add(rfq)
    await session.flush()
    rows: list[RfqLine] = []
    for position, line in enumerate(lines, start=1):
        row = RfqLine(
            tenant_id=principal.tenant_id,
            rfq_id=rfq.id,
            position=position,
            description=line.description,
            part_number=line.part_number,
            mpn=line.mpn,
            manufacturer=line.manufacturer,
            category=line.category,
            quantities=[quantity_json(line.quantity)],
            unit=line.unit,
            need_by=line.need_by,
            qualification=line.qualification,
        )
        session.add(row)
        rows.append(row)
    await session.flush()
    await session.refresh(rfq)
    return rfq, rows


async def add_draft_invite(
    session: AsyncSession, principal: Principal, rfq: Rfq, supplier_id: UUID, contact_id: UUID
) -> Invite:
    """A draft invite in the caller's tenant session (no checks beyond the constraints)."""
    invite = Invite(
        tenant_id=principal.tenant_id,
        rfq_id=rfq.id,
        supplier_id=supplier_id,
        contact_id=contact_id,
        reply_token=_new_reply_token(),
        status="draft",
    )
    session.add(invite)
    await session.flush()
    return invite


async def rfq_detail(session: AsyncSession, rfq: Rfq) -> RfqDetail:
    return await _detail(session, rfq)


async def get_rfq(db: Db, principal: Principal, rfq_id: UUID) -> RfqDetail:
    async with tenant_session(db, principal.tenant_id) as session:
        return await _detail(session, await _rfq(session, rfq_id))


async def update_line(
    db: Db, principal: Principal, rfq_id: UUID, line_id: UUID, patch: UpdateLineRequest
) -> RfqLineView:
    """Edit a line while the RFQ is a draft and no email has been prepared from it."""
    changes = patch.model_dump(exclude_unset=True)
    for required in ("description", "category", "quantities", "unit"):
        if required in changes and changes[required] is None:
            raise InvalidState(f"{required} cannot be cleared")

    async with tenant_session(db, principal.tenant_id) as session:
        await _editable_rfq(session, rfq_id)
        line = await session.scalar(
            select(RfqLine).where(RfqLine.id == line_id, RfqLine.rfq_id == rfq_id)
        )
        if line is None:
            raise NotFound(f"line {line_id} not found")
        for field, value in changes.items():
            if field == "quantities" and patch.quantities is not None:
                line.quantities = [quantity_json(q) for q in patch.quantities]
            else:
                setattr(line, field, value)
        await session.flush()
        return _line_view(line)


async def add_line(
    db: Db, principal: Principal, rfq_id: UUID, request: AddLineRequest
) -> RfqDetail:
    """Append a line to a draft RFQ that no email has been prepared from."""
    async with tenant_session(db, principal.tenant_id) as session:
        rfq = await _editable_rfq(session, rfq_id)
        last = await session.scalar(
            select(func.max(RfqLine.position)).where(RfqLine.rfq_id == rfq_id)
        )
        session.add(
            RfqLine(
                tenant_id=principal.tenant_id,
                rfq_id=rfq_id,
                position=(last or 0) + 1,
                description=request.description,
                part_number=request.part_number,
                mpn=request.mpn,
                manufacturer=request.manufacturer,
                category=request.category,
                quantities=[quantity_json(q) for q in request.quantities],
                unit=request.unit,
                need_by=request.need_by,
                incoterm=request.incoterm,
                delivery_location=request.delivery_location,
                qualification=request.qualification,
            )
        )
        await session.flush()
        return await _detail(session, rfq)


async def remove_line(db: Db, principal: Principal, rfq_id: UUID, line_id: UUID) -> RfqDetail:
    """Remove a line from a draft RFQ and close the gap in the positions.

    The last line cannot go: an RFQ asks for at least one thing.
    """
    async with tenant_session(db, principal.tenant_id) as session:
        rfq = await _editable_rfq(session, rfq_id)
        lines = await _lines(session, rfq_id)
        line = next((each for each in lines if each.id == line_id), None)
        if line is None:
            raise NotFound(f"line {line_id} not found")
        if len(lines) == 1:
            raise InvalidState(f"rfq {rfq_id} has only this line; an RFQ needs at least one")
        await session.delete(line)
        await session.flush()
        # One at a time, lowest first: (rfq_id, position) is unique at every step.
        for later in (each for each in lines if each.position > line.position):
            later.position -= 1
            await session.flush()
        return await _detail(session, rfq)


async def update_rfq(
    db: Db, principal: Principal, rfq_id: UUID, patch: UpdateRfqRequest
) -> RfqDetail:
    """RFQ-wide changes to a draft: its title, response date, currency, payment terms
    and quote validity, and the Incoterm, delivery location and need-by date of every
    line."""
    return (await edit_rfq(db, principal, rfq_id, patch)).detail


@dataclass(frozen=True, slots=True)
class EditedRfq:
    detail: RfqDetail
    # Set when the patch changed the currency (and converted any negotiation limits).
    currency_change: CurrencyChange | None = None


async def edit_rfq(
    db: Db,
    principal: Principal,
    rfq_id: UUID,
    patch: UpdateRfqRequest,
    *,
    today: date | None = None,
) -> EditedRfq:
    """`update_rfq`, also telling what a currency change converted."""
    changes = patch.model_dump(exclude_unset=True)
    for required in ("title", "currency"):
        if required in changes and changes[required] is None:
            raise InvalidState(f"{required} cannot be cleared")
    async with tenant_session(db, principal.tenant_id) as session:
        rfq = await _editable_rfq(session, rfq_id)
        currency_change = None
        if "currency" in changes:
            currency_change = await change_currency(
                session, rfq, changes["currency"], today or datetime.now(UTC).date()
            )
        for field in ("title", "response_due", "payment_terms", "quote_validity_days"):
            if field in changes:
                setattr(rfq, field, changes[field])
        line_fields = {
            field: changes[field]
            for field in ("incoterm", "delivery_location", "need_by")
            if field in changes
        }
        if line_fields:
            for line in await _lines(session, rfq_id):
                for field, value in line_fields.items():
                    setattr(line, field, value)
        await session.flush()
        await session.refresh(rfq)
        return EditedRfq(await _detail(session, rfq), currency_change)


async def _editable_rfq(session: AsyncSession, rfq_id: UUID) -> Rfq:
    """The RFQ, locked, if its content can still change: a draft that no email has been
    prepared from. What a supplier may already have seen never changes."""
    rfq = await _rfq(session, rfq_id, lock=True)
    if rfq.status != "draft":
        raise InvalidState(f"rfq {rfq_id} is {rfq.status}; lines are frozen")
    prepared = await session.scalar(
        select(func.count())
        .select_from(Invite)
        .where(Invite.rfq_id == rfq_id, Invite.status.not_in(_EDITABLE_INVITES))
    )
    if prepared:
        raise InvalidState(f"rfq {rfq_id} has prepared or sent invites; lines are frozen")
    return rfq


# A draft created before someone else's sign-off (engineering validating the BOM): the
# buyer can move on, and the mark in the title keeps the Copilot from preparing to send it.
PRELIMINARY_MARK: Final = "PRELIMINARY"


def preliminary_title(title: str, pending: str) -> str:
    return f"{PRELIMINARY_MARK} (pending {pending.strip()}): {title}"


def is_preliminary(title: str) -> bool:
    return title.startswith(PRELIMINARY_MARK)


def preliminary_hold(rfq: Rfq) -> str | None:
    """Why a PRELIMINARY RFQ cannot be prepared for sending yet; None when it can."""
    if not is_preliminary(rfq.title):
        return None
    return (
        f"{rfq.ref} is preliminary ({rfq.title.split(':', 1)[0]}). Ask the buyer to confirm "
        "the validation is done, then remove the mark with set_rfq_terms(title=...)"
    )


# --- Invites ------------------------------------------------------------------------


async def add_invite(
    db: Db, principal: Principal, rfq_id: UUID, supplier_id: UUID, contact_id: UUID
) -> InviteView:
    async with tenant_session(db, principal.tenant_id) as session:
        rfq = await _rfq(session, rfq_id, lock=True)
        if rfq.status not in ("draft", "open"):
            raise InvalidState(f"rfq {rfq_id} is {rfq.status}")
        supplier = await session.get(Supplier, supplier_id)
        if supplier is None:
            raise NotFound(f"supplier {supplier_id} not found")
        contact = await session.scalar(
            select(Contact)
            .join(SupplierContact, SupplierContact.contact_id == Contact.id)
            .where(SupplierContact.supplier_id == supplier_id, Contact.id == contact_id)
        )
        if contact is None:
            raise NotFound(f"contact {contact_id} is not a contact of supplier {supplier_id}")
        if not contact.email or contact.pseudonymized_at is not None:
            raise InvalidState(f"contact {contact_id} has no usable email address")
        existing = await session.scalar(
            select(Invite.id).where(Invite.rfq_id == rfq_id, Invite.supplier_id == supplier_id)
        )
        if existing is not None:
            raise Duplicate(f"supplier {supplier_id} is already invited to rfq {rfq_id}")

        invite = Invite(
            tenant_id=principal.tenant_id,
            rfq_id=rfq_id,
            supplier_id=supplier_id,
            contact_id=contact_id,
            reply_token=_new_reply_token(),
            status="draft",
        )
        session.add(invite)
        try:
            await session.flush()
        except IntegrityError:
            # A concurrent request invited the same supplier first.
            raise Duplicate(f"supplier {supplier_id} is already invited to rfq {rfq_id}") from None
        await session.refresh(invite)
        return _invite_view(invite, supplier.name)


# --- Preparing the send ---------------------------------------------------------------


async def buyer_display_name(session: AsyncSession, user: User, company: str) -> str:
    """The buyer's name from their `buyer` contact (matched by email), else the company."""
    name = await session.scalar(
        select(Contact.name)
        .where(
            Contact.kind == "buyer",
            Contact.email == user.email,
            Contact.pseudonymized_at.is_(None),
            Contact.name.is_not(None),
        )
        .order_by(Contact.created_at)
        .limit(1)
    )
    return name or f"{company} purchasing"


def with_marker(subject: str, rfq_ref: str) -> str:
    """The subject with the `[RFQ-<n>]` marker, added in front if the writer left it out."""
    marker = subject_marker(rfq_ref)
    return subject if marker in subject else f"{marker} {subject}"


@dataclass(frozen=True, slots=True)
class _SendContext:
    """What every invite's email of one RFQ shares."""

    rfq: Rfq
    lines: list[RfqLine]
    template: ResolvedTemplate
    buyer_name: str
    buyer_company: str


def _render_line(line: RfqLine) -> RenderLine:
    return RenderLine(
        position=line.position,
        description=line.description,
        part_number=line.part_number,
        mpn=line.mpn,
        manufacturer=line.manufacturer,
        quantities=quantities_from_json(line.quantities),
        unit=line.unit,
        need_by=line.need_by,
        incoterm=line.incoterm,
        delivery_location=line.delivery_location,
        qualification=line.qualification,
    )


def _email_line(line: RfqLine) -> RfqEmailLine:
    return RfqEmailLine(
        position=line.position,
        description=line.description,
        part_number=line.part_number,
        mpn=line.mpn,
        manufacturer=line.manufacturer,
        quantities=quantities_from_json(line.quantities),
        unit=line.unit,
        need_by=line.need_by,
        incoterm=line.incoterm,
        qualification=line.qualification,
    )


async def _send_context(session: AsyncSession, principal: Principal, rfq: Rfq) -> _SendContext:
    lines = await _lines(session, rfq.id)
    tenant = await session.get(Tenant, principal.tenant_id)
    user = await session.get(User, principal.user_id)
    if tenant is None or user is None:
        raise NotFound("tenant or user not found")
    return _SendContext(
        rfq=rfq,
        lines=lines,
        template=await resolve_for_rfq(session, rfq, lines),
        buyer_name=await buyer_display_name(session, user, tenant.name),
        buyer_company=tenant.name,
    )


async def _render_for(
    ctx: _SendContext,
    supplier: Supplier,
    writer: EmailWriterAgent,
    context: ToolContext,
    localized: dict[str, TemplateContent],
) -> RenderedRfq:
    """The supplier's email; refuses one that fails the deterministic checks."""
    language = supplier.language
    display = localized.get(language)
    if display is None:
        display = await localize(ctx.template.content, language, writer, context)
        localized[language] = display
    rendered = render_rfq(
        RenderInput(
            rfq_ref=ctx.rfq.ref,
            rfq_title=ctx.rfq.title,
            response_due=ctx.rfq.response_due,
            buyer_name=ctx.buyer_name,
            buyer_company=ctx.buyer_company,
            language=language,
            template=ctx.template.content,
            display=display,
            lines=[_render_line(line) for line in ctx.lines],
            payment_terms=ctx.rfq.payment_terms,
            quote_validity_days=ctx.rfq.quote_validity_days,
        )
    )
    problems = rfq_email_problems(
        RfqEmailInput(
            rfq_ref=ctx.rfq.ref,
            rfq_title=ctx.rfq.title,
            buyer_company=ctx.buyer_company,
            buyer_name=ctx.buyer_name,
            supplier_name=supplier.name,
            supplier_language=language,
            response_due=ctx.rfq.response_due,
            lines=[_email_line(line) for line in ctx.lines],
            payment_terms=ctx.rfq.payment_terms,
        ),
        RfqEmailOutput(subject=rendered.subject, body=rendered.text),
    )
    if problems:
        # The buyer's template text is the only free text: e.g. a price in it.
        raise InvalidState(f"rfq {ctx.rfq.id}: rendered email fails checks: {', '.join(problems)}")
    return rendered


async def preview_email(
    db: Db, principal: Principal, rfq_id: UUID, invite_id: UUID, writer: EmailWriterAgent
) -> RfqEmailPreview:
    """The email this invite would get, as `prepare_send` would propose it. Sends nothing."""
    async with tenant_session(db, principal.tenant_id) as session:
        rfq = await _rfq(session, rfq_id)
        row = (
            await session.execute(
                select(Invite, Supplier)
                .join(Supplier, Supplier.id == Invite.supplier_id)
                .where(Invite.id == invite_id, Invite.rfq_id == rfq_id)
            )
        ).first()
        if row is None:
            raise NotFound(f"invite {invite_id} not found")
        _, supplier = row
        ctx = await _send_context(session, principal, rfq)
    context = ToolContext(tenant_id=principal.tenant_id, user_id=principal.user_id, run_id=uuid4())
    rendered = await _render_for(ctx, supplier, writer, context, {})
    return RfqEmailPreview(
        invite_id=invite_id,
        subject=with_marker(rendered.subject, rfq.ref),
        text=rendered.text,
        html=rendered.html,
        language=rendered.language,
        template_id=ctx.template.template_id,
        template_version=ctx.template.version,
        missing_required=required_views(ctx.template.content),
    )


async def prepare_send(
    db: Db,
    principal: Principal,
    rfq_id: UUID,
    writer: EmailWriterAgent,
    mailboxes: MailboxSenderFactory,
    *,
    require_mailbox: bool = True,
    today: date | None = None,
) -> PrepareSendResponse:
    """Render one email per draft invite and record each as a pending approval.

    With an invite to prepare, the RFQ needs a reply-by date of `today` (UTC by
    default) or later; otherwise this fails with `ReplyByRequired` before
    anything is rendered or proposed. This is the one place RFQ emails are
    proposed, so the API, the copilot and negotiation mandates all get the rule.
    The emails will go out from the caller's connected mailbox; without one
    this fails with `MailboxNotConnected` before anything is rendered, unless
    `require_mailbox` is False (a negotiation mandate, whose sends wait for the
    mailbox instead).
    """
    async with tenant_session(db, principal.tenant_id) as session:
        rfq = await _rfq(session, rfq_id, lock=True)
        if rfq.status not in ("draft", "open"):
            raise InvalidState(f"rfq {rfq_id} is {rfq.status}")
        rows = await session.execute(
            select(Invite, Supplier, Contact)
            .join(Supplier, Supplier.id == Invite.supplier_id)
            .join(Contact, Contact.id == Invite.contact_id)
            .where(Invite.rfq_id == rfq_id, Invite.status == "draft")
            .order_by(Invite.created_at, Invite.id)
        )
        drafts = list(rows)
        # Without a date to answer by, suppliers have no deadline and follow-ups
        # and the negotiation nothing to anchor on. Checked first: the buyer can
        # fix it at once. Nothing to prepare (e.g. a mandate on an RFQ already
        # sent) means no email, so no date is needed.
        if drafts and (
            refusal := reply_by_refusal(rfq.ref, rfq.status, rfq.response_due, today or utc_today())
        ):
            raise ReplyByRequired(refusal, draft=rfq.status == "draft")
        # Fail early: nobody should approve an email that cannot be sent.
        if require_mailbox:
            await mailboxes(session, principal.tenant_id, principal.user_id)
        # Earlier rejected (or expired, e.g. with a rejected mandate) proposals per
        # invite: the next proposal is a new attempt.
        call_ids = [send_proposal_ids(invite.id)[1] for invite, _, _ in drafts]
        rejected_counts = await session.execute(
            select(Approval.tool_call_id, func.count())
            .where(
                Approval.tool == SEND_RFQ_EMAIL,
                Approval.status.in_(("rejected", "expired")),
                Approval.tool_call_id.in_(call_ids),
            )
            .group_by(Approval.tool_call_id)
        )
        attempts = {call_id: count for call_id, count in rejected_counts}
        ctx = await _send_context(session, principal, rfq)
        if drafts:
            # Every email of this RFQ, now and later, uses this template version.
            record_template(rfq, ctx.template)
            await session.flush()

    sink = PostgresApprovalSink(db)
    prepared: list[PreparedInvite] = []
    localized: dict[str, TemplateContent] = {}
    for invite, supplier, contact in drafts:
        if not contact.email or contact.pseudonymized_at is not None:
            raise InvalidState(f"invite {invite.id}: contact has no usable email address")
        context = ToolContext(
            tenant_id=principal.tenant_id, user_id=principal.user_id, run_id=uuid4()
        )
        rendered = await _render_for(ctx, supplier, writer, context, localized)
        args = SendRfqEmailArgs(
            invite_id=invite.id,
            sender_user_id=principal.user_id,
            to=[contact.email],
            subject=with_marker(rendered.subject, rfq.ref),
            body=rendered.text,
        )
        payload = cast(dict[str, JsonValue], args.model_dump(mode="json"))
        call_id = send_proposal_ids(invite.id)[1]
        run_id, _ = send_proposal_ids(invite.id, attempts.get(call_id, 0))
        try:
            await sink.request_approval(
                ApprovalProposal(
                    tenant_id=principal.tenant_id,
                    run_id=run_id,
                    session_id=f"rfq:{rfq_id}",
                    tool_call_id=call_id,
                    tool=SEND_RFQ_EMAIL,
                    args=payload,
                    args_hash=args_hash(payload),
                    requested_by=principal.user_id,
                )
            )
        except ApprovalConflict:
            # Already proposed (a retry or a concurrent prepare): that one stands.
            logger.info("send for invite %s was already proposed", invite.id)

        async with tenant_session(db, principal.tenant_id) as session:
            approval_id = await session.scalar(
                select(Approval.id).where(
                    Approval.run_id == str(run_id), Approval.tool_call_id == call_id
                )
            )
            if approval_id is None:
                raise NotFound(f"approval for invite {invite.id} not found")
            locked = await session.scalar(
                select(Invite).where(Invite.id == invite.id).with_for_update()
            )
            if locked is not None and locked.status == "draft":
                locked.status = "pending_approval"
                await session.flush()
        prepared.append(PreparedInvite(invite_id=invite.id, approval_id=approval_id))

    logger.info(
        "rfq send prepared tenant=%s rfq=%s approvals=%d",
        principal.tenant_id,
        rfq_id,
        len(prepared),
    )
    return PrepareSendResponse(rfq_id=rfq_id, approvals=prepared)


# --- The approval queue ---------------------------------------------------------------


async def list_approvals(
    db: Db,
    principal: Principal,
    status: ApprovalStatus,
    *,
    resolver: MxResolver | None = None,
) -> list[ApprovalQueueItem]:
    """Approvals in the caller's tenant by status, with the RFQ email draft to review.

    Pending acts that send email carry `recipient_checks` (DNS through `resolver`,
    the process's cached resolver by default)."""
    async with tenant_session(db, principal.tenant_id) as session:
        approvals = list(
            await session.scalars(
                select(Approval)
                .where(Approval.status == status)
                .order_by(Approval.created_at, Approval.id)
                .limit(_QUEUE_LIMIT)
            )
        )
        drafts: dict[UUID, tuple[SendRfqEmailArgs | SendFollowupEmailArgs, EmailKind]] = {}
        negotiations: dict[UUID, SendNegotiationEmailArgs] = {}
        for approval in approvals:
            try:
                if approval.tool == SEND_RFQ_EMAIL:
                    drafts[approval.id] = (SendRfqEmailArgs.model_validate(approval.args), "rfq")
                elif approval.tool == SEND_FOLLOWUP_EMAIL:
                    followup = SendFollowupEmailArgs.model_validate(approval.args)
                    drafts[approval.id] = (followup, followup.kind)
                elif approval.tool == SEND_NEGOTIATION_EMAIL:
                    negotiations[approval.id] = SendNegotiationEmailArgs.model_validate(
                        approval.args
                    )
            except ValueError:
                logger.error("approval %s has args that do not validate", approval.id)
        invite_ids = {args.invite_id for args, _ in drafts.values()} | {
            args.invite_id for args in negotiations.values()
        }
        context: dict[UUID, tuple[UUID, str, str]] = {}
        if invite_ids:
            rows = await session.execute(
                select(Invite.id, Rfq.id, Rfq.ref, Supplier.name)
                .join(Rfq, Rfq.id == Invite.rfq_id)
                .join(Supplier, Supplier.id == Invite.supplier_id)
                .where(Invite.id.in_(invite_ids))
            )
            context = {invite_id: (rfq_id, ref, name) for invite_id, rfq_id, ref, name in rows}
        # Imported here: the award service builds on this module.
        from bido_back.awards.service import queue_details

        details = await queue_details(session, approvals)

        mandate_views = await _mandate_views(session, approvals)

        checks: dict[UUID, list[RecipientCheck]] = {}
        if status == "pending":
            mx = resolver or default_resolver()
            for approval in approvals:
                addresses = recipients_in_args(approval.args)
                if addresses:
                    checks[approval.id] = await check_recipients(
                        session, principal.tenant_id, addresses, mx
                    )

    items: list[ApprovalQueueItem] = []
    for approval in approvals:
        email: RfqEmailDraft | None = None
        draft = drafts.get(approval.id)
        if draft is not None:
            args, kind = draft
            rfq_id, ref, supplier_name = context.get(args.invite_id, (None, None, None))
            email = RfqEmailDraft(
                kind=kind,
                invite_id=args.invite_id,
                sender_user_id=args.sender_user_id,
                rfq_id=rfq_id,
                rfq_ref=ref,
                supplier_name=supplier_name,
                to=list(args.to),
                cc=list(args.cc),
                subject=args.subject,
                body=args.body,
            )
        negotiation: NegotiationEmailDraft | None = None
        negotiation_args = negotiations.get(approval.id)
        if negotiation_args is not None:
            rfq_id, ref, supplier_name = context.get(negotiation_args.invite_id, (None, None, None))
            negotiation = NegotiationEmailDraft(
                action=negotiation_args.action,
                round=negotiation_args.round,
                invite_id=negotiation_args.invite_id,
                sender_user_id=negotiation_args.sender_user_id,
                rfq_id=rfq_id,
                rfq_ref=ref,
                supplier_name=supplier_name,
                to=list(negotiation_args.to),
                cc=list(negotiation_args.cc),
                subject=negotiation_args.subject,
                body=negotiation_args.body,
            )
        items.append(
            ApprovalQueueItem(
                id=approval.id,
                tool=approval.tool,
                status=cast(ApprovalStatus, approval.status),
                requested_by=approval.requested_by,
                requested_by_me=approval.requested_by == principal.user_id,
                created_at=approval.created_at,
                decided_at=approval.decided_at,
                rfq_email=email,
                negotiation_email=negotiation,
                award=details.awards.get(approval.id),
                thread_email=details.emails.get(approval.id),
                recipient_checks=checks.get(approval.id),
                mandate=mandate_views.get(approval.id),
            )
        )
    return items


async def _mandate_views(
    session: AsyncSession, approvals: Sequence[Approval]
) -> dict[UUID, MandateApprovalView]:
    # Imported here: the mandate service builds on this module.
    from bido_back.mandates.schemas import ApproveMandateArgs
    from bido_back.mandates.service import APPROVE_NEGOTIATION_MANDATE

    parsed: dict[UUID, ApproveMandateArgs] = {}
    for approval in approvals:
        if approval.tool != APPROVE_NEGOTIATION_MANDATE:
            continue
        try:
            parsed[approval.id] = ApproveMandateArgs.model_validate(approval.args)
        except ValueError:
            logger.error("approval %s has args that do not validate", approval.id)
    if not parsed:
        return {}
    rows = await session.execute(
        select(Rfq.id, Rfq.title).where(Rfq.id.in_({a.rfq_id for a in parsed.values()}))
    )
    titles: dict[UUID, str] = {rfq_id: title for rfq_id, title in rows}
    return {
        approval_id: MandateApprovalView(
            rfq_id=args.rfq_id,
            rfq_ref=args.rfq_ref,
            rfq_title=titles.get(args.rfq_id),
            currency=args.currency,
            deadline=args.deadline,
            max_rounds=args.max_rounds,
            max_messages_per_supplier_per_day=args.max_messages_per_supplier_per_day,
            initial_emails=len(args.initial_emails),
            suppliers=[
                MandateSupplierView(
                    invite_id=s.invite_id, supplier_name=s.supplier_name, to=list(s.to)
                )
                for s in args.suppliers
            ],
            lines=[
                MandateLineView(
                    rfq_line_id=line.rfq_line_id,
                    position=line.position,
                    description=line.description,
                    target_unit_price=line.target_unit_price,
                    max_unit_price=line.max_unit_price,
                    max_lead_time_days=line.max_lead_time_days,
                )
                for line in args.lines
            ],
        )
        for approval_id, args in parsed.items()
    }
