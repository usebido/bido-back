"""Dev-only payment demo data.

The public payment flow starts from a sent purchase order. This module creates a
small, realistic set of tenant-owned rows that leaves one USD PO ready to pay,
without sending email or calling a payment provider.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from pydantic import BaseModel
from sqlalchemy import Integer, func, select, text
from sqlalchemy import cast as sql_cast
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bido_back.auth import Principal
from bido_back.awards.views import AwardLine
from bido_back.db import tenant_session
from bido_back.db.models import (
    AuditEvent,
    Award,
    Contact,
    Invite,
    IssuedPurchaseOrder,
    IssuedPurchaseOrderLine,
    Message,
    Proposal,
    ProposalLine,
    Rfq,
    RfqLine,
    Supplier,
    SupplierContact,
)
from bido_back.email.addresses import make_message_id
from bido_back.rfqs.audit import RFQ_STATUS_CHANGED, rfq_event

SUPPLIER_NAME = "Bido Debug Payments Supplier"
SUPPLIER_DOMAIN = "payments-debug.example.test"
CONTACT_NAME = "Debug Supplier"
CONTACT_EMAIL = f"billing@{SUPPLIER_DOMAIN}"
LINE_DESCRIPTION = "Debug payment demo item"
_RFQ_REF_PATTERN = r"^RFQ-([0-9]+)$"


class DebugPaymentPurchaseOrder(BaseModel):
    id: UUID
    ref: str
    rfq_id: UUID
    rfq_ref: str
    supplier_id: UUID
    supplier_name: str
    currency: str
    total: Decimal
    status: str
    fulfilment_state: str


async def create_debug_payment_purchase_order(
    db: async_sessionmaker[AsyncSession],
    principal: Principal,
    *,
    amount_usd: Decimal,
    email_domain: str,
    today: date | None = None,
) -> DebugPaymentPurchaseOrder:
    """Create a sent USD PO for the authenticated user's tenant."""
    day = today or date.today()
    total = amount_usd.quantize(Decimal("0.01"))
    now = datetime.now(UTC)
    async with tenant_session(db, principal.tenant_id) as session:
        supplier_id, contact_id = await _debug_supplier(session, principal.tenant_id)
        rfq = Rfq(
            tenant_id=principal.tenant_id,
            ref=await _next_rfq_ref(session, principal.tenant_id),
            title="Debug payment purchase order",
            status="draft",
            base_currency="USD",
            response_due=day + timedelta(days=7),
            payment_terms="Card payment through Bido escrow",
            quote_validity_days=30,
            created_by=principal.user_id,
        )
        session.add(rfq)
        await session.flush()
        line = RfqLine(
            tenant_id=principal.tenant_id,
            rfq_id=rfq.id,
            position=1,
            description=LINE_DESCRIPTION,
            part_number="DEBUG-PAY-1",
            mpn=None,
            manufacturer=None,
            category="catalog",
            quantities=[1],
            unit="unit",
            need_by=day + timedelta(days=14),
        )
        session.add(line)
        await session.flush()
        invite = Invite(
            tenant_id=principal.tenant_id,
            rfq_id=rfq.id,
            supplier_id=supplier_id,
            contact_id=contact_id,
            reply_token=uuid4().hex,
            status="draft",
        )
        session.add(invite)
        await session.flush()
        await _record_quote(
            session,
            principal,
            invite,
            rfq.ref,
            line.id,
            total,
            email_domain,
            now,
        )
        proposal, proposal_line = await _latest_proposal(session, invite.id)
        award = Award(
            tenant_id=principal.tenant_id,
            rfq_id=rfq.id,
            approval_id=None,
            requested_by=principal.user_id,
            approved_by=principal.user_id,
            differs_from_recommendation=False,
            reason=None,
            lines=[
                AwardLine(
                    rfq_line_id=line.id,
                    position=1,
                    description=LINE_DESCRIPTION,
                    mpn=None,
                    tier=0,
                    quantity="1",
                    unit="unit",
                    need_by=line.need_by,
                    invite_id=invite.id,
                    supplier_id=supplier_id,
                    supplier_name=SUPPLIER_NAME,
                    proposal_id=proposal.id,
                    proposal_version=proposal.version,
                    proposal_line_id=proposal_line.id,
                    unit_price=f"{total:f}",
                    currency="USD",
                    lead_time_days=7,
                    recommended_invite_id=invite.id,
                ).model_dump(mode="json")
            ],
        )
        session.add(award)
        await session.flush()

        number = await _next_po_number(session, principal.tenant_id)
        po = IssuedPurchaseOrder(
            tenant_id=principal.tenant_id,
            award_id=award.id,
            rfq_id=rfq.id,
            invite_id=invite.id,
            supplier_id=supplier_id,
            number=number,
            ref=_po_ref(number),
            status="sent",
            currency="USD",
            incoterm="DAP",
            payment_terms="Card payment through Bido escrow",
            total=total,
            message_id_header=make_message_id(email_domain),
            sent_at=now,
            fulfilment_state="sent",
        )
        session.add(po)
        await session.flush()
        session.add(
            IssuedPurchaseOrderLine(
                tenant_id=principal.tenant_id,
                purchase_order_id=po.id,
                position=1,
                rfq_line_id=line.id,
                proposal_line_id=proposal_line.id,
                description=LINE_DESCRIPTION,
                mpn=None,
                quantity=Decimal("1"),
                unit="unit",
                unit_price=total,
                line_total=_line_total(Decimal("1"), total),
                need_by=line.need_by,
                lead_time_days=7,
            )
        )
        rfq.status = "closed"
        session.add(
            AuditEvent(
                tenant_id=principal.tenant_id,
                actor_user_id=principal.user_id,
                action="rfq.awarded",
                payload={
                    "rfq_id": str(rfq.id),
                    "award_id": str(award.id),
                    "purchase_order_ids": [str(po.id)],
                    "debug": True,
                },
            )
        )
        session.add(
            AuditEvent(
                tenant_id=principal.tenant_id,
                actor_user_id=principal.user_id,
                action="purchase_order.sent",
                payload={
                    "purchase_order_id": str(po.id),
                    "invite_id": str(invite.id),
                    "sender_user_id": str(principal.user_id),
                    "approved_by": str(principal.user_id),
                    "debug": True,
                },
            )
        )
        await session.flush()
        await session.refresh(po)
        return DebugPaymentPurchaseOrder(
            id=po.id,
            ref=po.ref,
            rfq_id=rfq.id,
            rfq_ref=rfq.ref,
            supplier_id=supplier_id,
            supplier_name=SUPPLIER_NAME,
            currency=po.currency,
            total=po.total,
            status=po.status,
            fulfilment_state=po.fulfilment_state,
        )


async def _debug_supplier(session: AsyncSession, tenant_id: UUID) -> tuple[UUID, UUID]:
    found = (
        await session.execute(
            select(Supplier.id, Contact.id)
            .join(SupplierContact, SupplierContact.supplier_id == Supplier.id)
            .join(Contact, Contact.id == SupplierContact.contact_id)
            .where(Supplier.name == SUPPLIER_NAME, Contact.email == CONTACT_EMAIL)
            .limit(1)
        )
    ).one_or_none()
    if found is not None:
        return found[0], found[1]

    supplier = Supplier(
        tenant_id=tenant_id,
        name=SUPPLIER_NAME,
        type="manufacturer",
        country="US",
        language="en",
        email_domain=SUPPLIER_DOMAIN,
    )
    contact = Contact(
        tenant_id=tenant_id,
        kind="supplier",
        name=CONTACT_NAME,
        email=CONTACT_EMAIL,
        phone=None,
        pseudonymized_at=None,
    )
    session.add_all([supplier, contact])
    await session.flush()
    session.add(
        SupplierContact(tenant_id=tenant_id, supplier_id=supplier.id, contact_id=contact.id)
    )
    await session.flush()
    return supplier.id, contact.id


async def _next_po_number(session: AsyncSession, tenant_id: UUID) -> int:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"po_number:{tenant_id}"},
    )
    highest = await session.scalar(select(func.max(IssuedPurchaseOrder.number)))
    return (highest or 0) + 1


def _po_ref(number: int) -> str:
    return f"PO-{number}"


def _line_total(quantity: Decimal, unit_price: Decimal) -> Decimal:
    return (quantity * unit_price).quantize(Decimal("0.01"))


async def _next_rfq_ref(session: AsyncSession, tenant_id: UUID) -> str:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"rfq_ref:{tenant_id}"},
    )
    highest = await session.scalar(
        select(func.max(sql_cast(func.substring(Rfq.ref, _RFQ_REF_PATTERN), Integer)))
    )
    return f"RFQ-{(highest or 0) + 1}"


async def _record_quote(
    session: AsyncSession,
    principal: Principal,
    invite: Invite,
    rfq_ref: str,
    rfq_line_id: UUID,
    total: Decimal,
    email_domain: str,
    now: datetime,
) -> None:
    rfq_message_id = make_message_id(email_domain)
    session.add(
        Message(
            tenant_id=principal.tenant_id,
            invite_id=invite.id,
            direction="out",
            message_id_header=rfq_message_id,
            subject=f"{rfq_ref}: debug payment RFQ",
            occurred_at=now - timedelta(days=2),
        )
    )
    invite.thread_message_id = rfq_message_id
    invite.status = "sent"
    invite.sent_at = now - timedelta(days=2)
    rfq = await session.get(Rfq, invite.rfq_id)
    if rfq is not None:
        rfq.status = "open"
    session.add(
        rfq_event(
            principal.tenant_id,
            principal.user_id,
            RFQ_STATUS_CHANGED,
            invite.rfq_id,
            from_status="draft",
            to_status="open",
            invite_id=invite.id,
        )
    )
    reply = Message(
        tenant_id=principal.tenant_id,
        invite_id=invite.id,
        direction="in",
        message_id_header=f"<quote-{invite.id}@{SUPPLIER_DOMAIN}>",
        in_reply_to=rfq_message_id,
        subject=f"Re: {rfq_ref}: debug payment RFQ",
        occurred_at=now - timedelta(days=1),
    )
    session.add(reply)
    await session.flush()
    proposal = Proposal(
        tenant_id=principal.tenant_id,
        invite_id=invite.id,
        message_id=reply.id,
        version=1,
        currency="USD",
        valid_until=now.date() + timedelta(days=30),
        incoterm="DAP",
        payment_terms="Card payment through Bido escrow",
    )
    session.add(proposal)
    await session.flush()
    session.add(
        ProposalLine(
            tenant_id=principal.tenant_id,
            proposal_id=proposal.id,
            rfq_line_id=rfq_line_id,
            offered_mpn=None,
            min_quantity=Decimal("1"),
            unit_price=total,
            unit="unit",
            tooling_cost=None,
            moq=None,
            lead_time_days=7,
            origin_country="US",
        )
    )
    await session.flush()


async def _latest_proposal(session: AsyncSession, invite_id: UUID) -> tuple[Proposal, ProposalLine]:
    proposal = await session.scalar(
        select(Proposal).where(Proposal.invite_id == invite_id).order_by(Proposal.version.desc())
    )
    if proposal is None:
        raise RuntimeError("debug quote was not recorded")
    line = await session.scalar(select(ProposalLine).where(ProposalLine.proposal_id == proposal.id))
    if line is None:
        raise RuntimeError("debug quote line was not recorded")
    return proposal, line
