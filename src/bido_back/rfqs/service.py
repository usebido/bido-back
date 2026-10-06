"""RFQ services: create from text, read, edit lines, invite suppliers, prepare sends.

Every function takes the caller's `Principal` and works in one
`tenant_session` for that tenant (forced RLS). Agent runs (seconds) happen
OUTSIDE any transaction; their results are written afterwards.

Preparing a send never sends. It first checks that the caller (the buyer)
has a connected mailbox, since that is where the emails will go out from
(`MailboxNotConnected` otherwise). Then, for each invite still in `draft`, it
runs the email writer, makes sure the subject carries the `[RFQ-<n>]` marker,
records an `ApprovalProposal` for the act tool `send_rfq_email`
through `PostgresApprovalSink` (requested by the caller), and moves the invite
to `pending_approval`. The proposal's run id and tool-call id are derived from
the invite, so preparing the same invite twice (a retry, a concurrent click)
records one approval: the second proposal hits the sink's uniqueness rule and
the first one stands.
"""

import importlib
import logging
from collections.abc import Callable, Sequence
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
    RfqIntakeInput,
)
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
from bido_back.rfqs.agents import EmailWriterAgent, IntakeAgent
from bido_back.rfqs.errors import Duplicate, InvalidState, NotFound
from bido_back.rfqs.mailbox import MailboxSenderFactory
from bido_back.rfqs.schemas import (
    ApprovalQueueItem,
    ApprovalStatus,
    InviteStatus,
    InviteView,
    PreparedInvite,
    PrepareSendResponse,
    RfqCreated,
    RfqDetail,
    RfqEmailDraft,
    RfqLineView,
    RfqStatus,
    RfqSummary,
    UpdateLineRequest,
)
from bido_back.rfqs.send import (
    SEND_RFQ_EMAIL,
    SendRfqEmailArgs,
    send_proposal_ids,
    subject_marker,
)

logger = logging.getLogger(__name__)

Db = AsyncEngine | async_sessionmaker[AsyncSession]

BASE_CURRENCY: Final = "EUR"
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
    db: Db, principal: Principal, source_text: str, intake: IntakeAgent
) -> RfqCreated:
    """Structure the buyer's text with the intake agent and store it as a draft RFQ."""
    context = ToolContext(tenant_id=principal.tenant_id, user_id=principal.user_id, run_id=uuid4())
    draft = await intake(RfqIntakeInput(text=source_text, base_currency=BASE_CURRENCY), context)

    async with tenant_session(db, principal.tenant_id) as session:
        rfq = Rfq(
            tenant_id=principal.tenant_id,
            ref=await _next_ref(session, principal.tenant_id),
            title=draft.title,
            status="draft",
            base_currency=BASE_CURRENCY,
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
    logger.info("rfq created tenant=%s rfq=%s run=%s", principal.tenant_id, rfq.id, context.run_id)
    return RfqCreated.model_validate({**detail.model_dump(), "questions": draft.questions})


async def list_rfqs(db: Db, principal: Principal) -> list[RfqSummary]:
    async with tenant_session(db, principal.tenant_id) as session:
        rfqs = await session.scalars(select(Rfq).order_by(Rfq.created_at.desc(), Rfq.id).limit(500))
        return [RfqSummary.model_validate(_summary_fields(rfq)) for rfq in rfqs]


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


async def _buyer_name(session: AsyncSession, user: User, company: str) -> str:
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


async def prepare_send(
    db: Db,
    principal: Principal,
    rfq_id: UUID,
    writer: EmailWriterAgent,
    mailboxes: MailboxSenderFactory,
) -> PrepareSendResponse:
    """Draft one email per draft invite and record each as a pending approval.

    The emails will go out from the caller's connected mailbox; without one
    this fails with `MailboxNotConnected` before any agent runs.
    """
    async with tenant_session(db, principal.tenant_id) as session:
        rfq = await _rfq(session, rfq_id)
        if rfq.status not in ("draft", "open"):
            raise InvalidState(f"rfq {rfq_id} is {rfq.status}")
        # Fail early: nobody should approve an email that cannot be sent.
        await mailboxes(session, principal.tenant_id, principal.user_id)
        lines = await _lines(session, rfq_id)
        rows = await session.execute(
            select(Invite, Supplier, Contact)
            .join(Supplier, Supplier.id == Invite.supplier_id)
            .join(Contact, Contact.id == Invite.contact_id)
            .where(Invite.rfq_id == rfq_id, Invite.status == "draft")
            .order_by(Invite.created_at, Invite.id)
        )
        drafts = list(rows)
        # Earlier rejected proposals per invite: the next proposal is a new attempt.
        call_ids = [send_proposal_ids(invite.id)[1] for invite, _, _ in drafts]
        rejected_counts = await session.execute(
            select(Approval.tool_call_id, func.count())
            .where(
                Approval.tool == SEND_RFQ_EMAIL,
                Approval.status == "rejected",
                Approval.tool_call_id.in_(call_ids),
            )
            .group_by(Approval.tool_call_id)
        )
        attempts = {call_id: count for call_id, count in rejected_counts}
        tenant = await session.get(Tenant, principal.tenant_id)
        user = await session.get(User, principal.user_id)
        if tenant is None or user is None:
            raise NotFound("tenant or user not found")
        buyer_name = await _buyer_name(session, user, tenant.name)

    email_lines = [
        RfqEmailLine(
            position=line.position,
            description=line.description,
            mpn=line.mpn,
            manufacturer=line.manufacturer,
            quantities=quantities_from_json(line.quantities),
            unit=line.unit,
            need_by=line.need_by,
            incoterm=line.incoterm,
            qualification=line.qualification,
        )
        for line in lines
    ]
    sink = PostgresApprovalSink(db)
    prepared: list[PreparedInvite] = []
    for invite, supplier, contact in drafts:
        if not contact.email or contact.pseudonymized_at is not None:
            raise InvalidState(f"invite {invite.id}: contact has no usable email address")
        context = ToolContext(
            tenant_id=principal.tenant_id, user_id=principal.user_id, run_id=uuid4()
        )
        draft = await writer(
            RfqEmailInput(
                rfq_ref=rfq.ref,
                rfq_title=rfq.title,
                buyer_company=tenant.name,
                buyer_name=buyer_name,
                supplier_name=supplier.name,
                supplier_language=supplier.language,
                response_due=rfq.response_due,
                lines=email_lines,
            ),
            context,
        )
        args = SendRfqEmailArgs(
            invite_id=invite.id,
            sender_user_id=principal.user_id,
            to=[contact.email],
            subject=with_marker(draft.subject, rfq.ref),
            body=draft.body,
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
    db: Db, principal: Principal, status: ApprovalStatus
) -> list[ApprovalQueueItem]:
    """Approvals in the caller's tenant by status, with the RFQ email draft to review."""
    async with tenant_session(db, principal.tenant_id) as session:
        approvals = list(
            await session.scalars(
                select(Approval)
                .where(Approval.status == status)
                .order_by(Approval.created_at, Approval.id)
                .limit(_QUEUE_LIMIT)
            )
        )
        drafts: dict[UUID, SendRfqEmailArgs] = {}
        for approval in approvals:
            if approval.tool == SEND_RFQ_EMAIL:
                try:
                    drafts[approval.id] = SendRfqEmailArgs.model_validate(approval.args)
                except ValueError:
                    logger.error("approval %s has args that do not validate", approval.id)
        invite_ids = {args.invite_id for args in drafts.values()}
        context: dict[UUID, tuple[UUID, str, str]] = {}
        if invite_ids:
            rows = await session.execute(
                select(Invite.id, Rfq.id, Rfq.ref, Supplier.name)
                .join(Rfq, Rfq.id == Invite.rfq_id)
                .join(Supplier, Supplier.id == Invite.supplier_id)
                .where(Invite.id.in_(invite_ids))
            )
            context = {invite_id: (rfq_id, ref, name) for invite_id, rfq_id, ref, name in rows}

    items: list[ApprovalQueueItem] = []
    for approval in approvals:
        email: RfqEmailDraft | None = None
        args = drafts.get(approval.id)
        if args is not None:
            rfq_id, ref, supplier_name = context.get(args.invite_id, (None, None, None))
            email = RfqEmailDraft(
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
            )
        )
    return items
