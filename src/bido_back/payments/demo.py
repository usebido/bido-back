"""Dev-only payment demo data.

The public payment flow starts from a sent purchase order. This module creates a
realistic set of tenant-owned rows (RFQ, invite, quote, award, PO) that leaves one
sent PO ready to pay, without sending email or calling a payment provider.

Two orders:
- `rfq_quote` (the default): the demo RFQ's winning quote, 21 lines of battery-pack
  materials for plants 4000 and 4100 from Embalagens Papelão Jundiaí, in BRL. The
  card charge converts it to USD at the day's ECB rate, so `fx_rates` needs BRL.
- `single_line`: one USD line for `amount_usd` from a debug supplier.

Need-by dates are kept as days from today, so the demo reads the same any day.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Final, Literal
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

DemoScenario = Literal["rfq_quote", "single_line"]

SUPPLIER_NAME = "Bido Debug Payments Supplier"
SUPPLIER_DOMAIN = "payments-debug.example.test"
CONTACT_NAME = "Debug Supplier"
CONTACT_EMAIL = f"billing@{SUPPLIER_DOMAIN}"
LINE_DESCRIPTION = "Debug payment demo item"
_RFQ_REF_PATTERN = r"^RFQ-([0-9]+)$"


@dataclass(frozen=True, slots=True)
class DemoLine:
    description: str
    part_number: str
    quantity: Decimal
    unit: str
    unit_price: Decimal
    need_by_days: int
    plant: str | None


@dataclass(frozen=True, slots=True)
class DemoSupplier:
    name: str
    type: str
    country: str
    language: str
    contact_name: str
    contact_email: str


@dataclass(frozen=True, slots=True)
class DemoOrder:
    supplier: DemoSupplier
    title: str
    currency: str
    incoterm: str
    payment_terms: str
    lead_time_days: int
    valid_days: int
    response_days: int
    lines: tuple[DemoLine, ...]


# The quote, line by line: description, part number, quantity, unit, unit price (BRL),
# need-by in days from today, plant.
_QUOTE_ROWS: Final[tuple[tuple[str, str, str, str, str, int, str], ...]] = (
    ("CELULA LFP PRISMATICA 3,2V 100AH", "130000401", "5760", "UN", "225.70", 16, "4000"),
    ("CELULA LFP PRISMATICA 3,2V 150AH", "130000402", "2880", "UN", "318.00", 57, "4000"),
    ("PLACA BMS 16S 100A CAN", "130000410", "350", "UN", "467.00", 44, "4000"),
    ("PLACA BMS 24S 150A CAN", "130000411", "50", "UN", "766.80", 100, "4000"),
    ("BARRAMENTO COBRE ESTANHADO 30X3MM", "130000420", "550", "KG", "76.70", 44, "4000"),
    ("CAIXA ALUMINIO EXTRUDADO PACK 48V", "130000430", "600", "UN", "637.20", 27, "4000"),
    ("CAIXA ALUMINIO EXTRUDADO PACK 72V", "130000431", "150", "UN", "885.60", 27, "4000"),
    ("PARAFUSO ALLEN M6X16 INOX A2", "130000440", "20000", "UN", "0.35", 30, "4000"),
    ("CONECTOR HV 200A PAR MACHO/FEMEA", "130000450", "590", "UN", "147.96", 16, "4000"),
    ("CABO FLEXIVEL 35MM2 1KV LARANJA HV", "130000460", "1400", "M", "36.99", 30, "4000"),
    ("ADESIVO TERMOCONDUTOR BICOMPONENTE", "130000470", "600", "KG", "175.50", 16, "4000"),
    ("PAD TERMICO SILICONE 200X100X2MM", "130000480", "1680", "UN", "22.14", 30, "4000"),
    ("EMBALAGEM CAIXA PAPELAO + EPS PACK", "130000490", "500", "UN", "52.92", 86, "4000"),
    ("CELULA LFP PRISMATICA 3,2V 100AH", "130000401", "1760", "UN", "225.70", 86, "4100"),
    ("PLACA BMS 16S 100A CAN REV A", "130000412", "310", "UN", "467.00", 30, "4100"),
    ("PARAFUSO ALLEN M6X16 INOX A2", "130000440", "10000", "UN", "0.35", 72, "4100"),
    ("CONECTOR HV 200A PAR MACHO/FEMEA", "130000450", "290", "UN", "147.96", 30, "4100"),
    ("CABO FLEXIVEL 35MM2 1KV LARANJA HV", "130000460", "200", "M", "36.99", 86, "4100"),
    ("ADESIVO TERMOCONDUTOR BICOMPONENTE", "130000470", "140", "KG", "175.50", 58, "4100"),
    ("PAD TERMICO SILICONE 200X100X2MM", "130000480", "140", "UN", "22.14", 86, "4100"),
    ("EMBALAGEM CAIXA PAPELAO + EPS PACK", "130000490", "500", "UN", "52.92", 44, "4100"),
)

QUOTE_ORDER = DemoOrder(
    supplier=DemoSupplier(
        name="EMBALAGENS PAPELAO JUNDIAI LTDA",
        type="manufacturer",
        country="BR",
        language="pt",
        contact_name="Equipe Comercial",
        contact_email="comercial@embalagens-jundiai.example.test",
    ),
    title="Cotação de materiais — necessidades MRP centros 4000 e 4100",
    currency="BRL",
    incoterm="CIF",
    payment_terms="30 dias da data da nota fiscal (30 ddl)",
    lead_time_days=14,
    valid_days=60,
    response_days=61,
    lines=tuple(
        DemoLine(description, part, Decimal(qty), unit, Decimal(price), days, plant)
        for description, part, qty, unit, price, days, plant in _QUOTE_ROWS
    ),
)


def single_line_order(amount_usd: Decimal) -> DemoOrder:
    return DemoOrder(
        supplier=DemoSupplier(
            name=SUPPLIER_NAME,
            type="manufacturer",
            country="US",
            language="en",
            contact_name=CONTACT_NAME,
            contact_email=CONTACT_EMAIL,
        ),
        title="Debug payment purchase order",
        currency="USD",
        incoterm="DAP",
        payment_terms="Card payment through Bido escrow",
        lead_time_days=7,
        valid_days=30,
        response_days=7,
        lines=(
            DemoLine(
                description=LINE_DESCRIPTION,
                part_number="DEBUG-PAY-1",
                quantity=Decimal("1"),
                unit="unit",
                unit_price=amount_usd.quantize(Decimal("0.01")),
                need_by_days=14,
                plant=None,
            ),
        ),
    )


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
    email_domain: str,
    scenario: DemoScenario = "rfq_quote",
    amount_usd: Decimal = Decimal("1.00"),
    today: date | None = None,
) -> DebugPaymentPurchaseOrder:
    """Create a sent PO for the authenticated user's tenant (`amount_usd`: single_line only)."""
    order = QUOTE_ORDER if scenario == "rfq_quote" else single_line_order(amount_usd)
    day = today or date.today()
    now = datetime.now(UTC)
    async with tenant_session(db, principal.tenant_id) as session:
        supplier_id, contact_id = await _demo_supplier(session, principal.tenant_id, order.supplier)
        rfq = Rfq(
            tenant_id=principal.tenant_id,
            ref=await _next_rfq_ref(session, principal.tenant_id),
            title=order.title,
            status="draft",
            base_currency=order.currency,
            response_due=day + timedelta(days=order.response_days),
            payment_terms=order.payment_terms,
            quote_validity_days=order.valid_days,
            created_by=principal.user_id,
        )
        session.add(rfq)
        await session.flush()
        lines = [
            RfqLine(
                tenant_id=principal.tenant_id,
                rfq_id=rfq.id,
                position=position,
                description=demo.description,
                part_number=demo.part_number,
                mpn=None,
                manufacturer=None,
                category="catalog",
                quantities=[int(demo.quantity)],
                unit=demo.unit,
                need_by=day + timedelta(days=demo.need_by_days),
                delivery_location=demo.plant,
            )
            for position, demo in enumerate(order.lines, start=1)
        ]
        session.add_all(lines)
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
        proposal, proposal_lines = await _record_quote(
            session, principal, invite, rfq.ref, order, lines, email_domain, now
        )
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
                    position=line.position,
                    description=line.description,
                    mpn=None,
                    tier=0,
                    quantity=f"{demo.quantity:f}",
                    unit=demo.unit,
                    need_by=line.need_by,
                    invite_id=invite.id,
                    supplier_id=supplier_id,
                    supplier_name=order.supplier.name,
                    proposal_id=proposal.id,
                    proposal_version=proposal.version,
                    proposal_line_id=proposal_line.id,
                    unit_price=f"{demo.unit_price:f}",
                    currency=order.currency,
                    lead_time_days=order.lead_time_days,
                    recommended_invite_id=invite.id,
                ).model_dump(mode="json")
                for line, proposal_line, demo in zip(
                    lines, proposal_lines, order.lines, strict=True
                )
            ],
        )
        session.add(award)
        await session.flush()

        totals = [_line_total(demo.quantity, demo.unit_price) for demo in order.lines]
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
            currency=order.currency,
            incoterm=order.incoterm,
            payment_terms=order.payment_terms,
            total=sum(totals, Decimal("0")),
            message_id_header=make_message_id(email_domain),
            sent_at=now,
            fulfilment_state="sent",
        )
        session.add(po)
        await session.flush()
        session.add_all(
            IssuedPurchaseOrderLine(
                tenant_id=principal.tenant_id,
                purchase_order_id=po.id,
                position=line.position,
                rfq_line_id=line.id,
                proposal_line_id=proposal_line.id,
                description=line.description,
                mpn=None,
                quantity=demo.quantity,
                unit=demo.unit,
                unit_price=demo.unit_price,
                line_total=total,
                need_by=line.need_by,
                lead_time_days=order.lead_time_days,
            )
            for line, proposal_line, demo, total in zip(
                lines, proposal_lines, order.lines, totals, strict=True
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
            supplier_name=order.supplier.name,
            currency=po.currency,
            total=po.total,
            status=po.status,
            fulfilment_state=po.fulfilment_state,
        )


async def _demo_supplier(
    session: AsyncSession, tenant_id: UUID, demo: DemoSupplier
) -> tuple[UUID, UUID]:
    """The tenant's supplier of that name (so a payout account added to it is reused) and
    one of its contacts; each created when missing."""
    supplier_id = await session.scalar(
        select(Supplier.id).where(Supplier.name == demo.name).order_by(Supplier.created_at).limit(1)
    )
    if supplier_id is None:
        supplier = Supplier(
            tenant_id=tenant_id,
            name=demo.name,
            type=demo.type,
            country=demo.country,
            language=demo.language,
            email_domain=demo.contact_email.rsplit("@", 1)[1],
        )
        session.add(supplier)
        await session.flush()
        supplier_id = supplier.id
    contact_id = await session.scalar(
        select(SupplierContact.contact_id)
        .join(Contact, Contact.id == SupplierContact.contact_id)
        .where(SupplierContact.supplier_id == supplier_id)
        .order_by(Contact.email != demo.contact_email, Contact.created_at)
        .limit(1)
    )
    if contact_id is not None:
        return supplier_id, contact_id

    contact = Contact(
        tenant_id=tenant_id,
        kind="supplier",
        name=demo.contact_name,
        email=demo.contact_email,
        phone=None,
        pseudonymized_at=None,
    )
    session.add(contact)
    await session.flush()
    session.add(
        SupplierContact(tenant_id=tenant_id, supplier_id=supplier_id, contact_id=contact.id)
    )
    await session.flush()
    return supplier_id, contact.id


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
    order: DemoOrder,
    lines: list[RfqLine],
    email_domain: str,
    now: datetime,
) -> tuple[Proposal, list[ProposalLine]]:
    rfq_message_id = make_message_id(email_domain)
    session.add(
        Message(
            tenant_id=principal.tenant_id,
            invite_id=invite.id,
            direction="out",
            message_id_header=rfq_message_id,
            subject=f"[{rfq_ref}] {order.title}",
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
        message_id_header=f"<quote-{invite.id}@{order.supplier.contact_email.rsplit('@', 1)[1]}>",
        in_reply_to=rfq_message_id,
        subject=f"RE: [{rfq_ref}] {order.title}",
        occurred_at=now - timedelta(days=1),
    )
    session.add(reply)
    await session.flush()
    proposal = Proposal(
        tenant_id=principal.tenant_id,
        invite_id=invite.id,
        message_id=reply.id,
        version=1,
        currency=order.currency,
        valid_until=now.date() + timedelta(days=order.valid_days),
        incoterm=order.incoterm,
        payment_terms=order.payment_terms,
    )
    session.add(proposal)
    await session.flush()
    proposal_lines = [
        ProposalLine(
            tenant_id=principal.tenant_id,
            proposal_id=proposal.id,
            rfq_line_id=line.id,
            offered_mpn=None,
            min_quantity=demo.quantity,
            unit_price=demo.unit_price,
            unit=demo.unit,
            tooling_cost=None,
            moq=None,
            lead_time_days=order.lead_time_days,
            origin_country=order.supplier.country,
        )
        for line, demo in zip(lines, order.lines, strict=True)
    ]
    session.add_all(proposal_lines)
    await session.flush()
    return proposal, proposal_lines
