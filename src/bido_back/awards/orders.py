"""Purchase-order rows: numbering per tenant and the document built from them."""

import logging
from datetime import date
from typing import Literal
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from bido_back.awards.render import PoDocument, PoLine
from bido_back.branding.images import logo_for_document
from bido_back.branding.service import (
    company_logo_placement,
    logo_bytes,
    rfq_branding_rows,
    rfq_logo_placement,
)
from bido_back.db.models import (
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
    Tenant,
    User,
)
from bido_back.rfqs.document import DocumentBranding, default_branding
from bido_back.rfqs.errors import NotFound
from bido_back.rfqs.language import supplier_email_language
from bido_back.storage import BlobStorage

logger = logging.getLogger(__name__)


async def next_po_number(session: AsyncSession, tenant_id: UUID) -> int:
    """The next PO number of the tenant; serialized for the rest of the transaction."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"po_number:{tenant_id}"},
    )
    highest = await session.scalar(select(func.max(IssuedPurchaseOrder.number)))
    return (highest or 0) + 1


def po_ref(number: int) -> str:
    return f"PO-{number}"


async def po_lines(session: AsyncSession, po_id: UUID) -> list[IssuedPurchaseOrderLine]:
    return list(
        await session.scalars(
            select(IssuedPurchaseOrderLine)
            .where(IssuedPurchaseOrderLine.purchase_order_id == po_id)
            .order_by(IssuedPurchaseOrderLine.position)
        )
    )


async def _branding(
    session: AsyncSession, tenant_id: UUID, rfq: Rfq, tenant_name: str, storage: BlobStorage | None
) -> DocumentBranding:
    """The buyer company on the PO: the RFQ's branding (logo, legal name, tax id, address).

    A logo that cannot be read or made small enough is left out, never failing the PO.
    """
    rows = await rfq_branding_rows(session, tenant_id, rfq)
    row = rows.branding
    logo: tuple[bytes, Literal["image/png", "image/jpeg", "image/svg+xml"]] | None = None
    if rows.logo is not None:
        data = await logo_bytes(storage, rows.logo)
        logo = logo_for_document(data, rows.logo.content_type) if data is not None else None
        if logo is None:
            logger.warning("po logo left out of the document logo=%s", rows.logo.id)
    placement = rfq_logo_placement(rfq) or (company_logo_placement(row) if row else None)
    if row is None and logo is None and rows.footer is None and placement is None:
        return default_branding(tenant_name)
    details = [part for part in (row.phone, row.website) if part] if row else []
    address = row.address if row else None
    if details:
        address = "\n".join([address, *details]) if address else "\n".join(details)
    return DocumentBranding.create(
        company_name=((row.trade_name or row.legal_name) if row else None) or tenant_name,
        legal_name=row.legal_name if row else None,
        tax_id=row.tax_id if row else None,
        address=address[:600] if address else None,
        footer_text=rows.footer[:2000] if rows.footer else None,
        accent_color=row.accent_color if row else None,
        logo=logo[0] if logo else None,
        logo_media_type=logo[1] if logo else None,
        logo_scale=placement.scale if placement else 1.0,
        logo_align=placement.align if placement else "left",
        logo_offset_y=placement.offset_y if placement else 0,
    )


async def _person(session: AsyncSession, user_id: UUID | None) -> str | None:
    """A user as a document names them: their buyer contact's name, else their email."""
    user = await session.get(User, user_id) if user_id is not None else None
    if user is None:
        return None
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
    return name or user.email


def _po_line(line: IssuedPurchaseOrderLine, rfq_line: RfqLine | None) -> PoLine:
    return PoLine(
        position=line.position,
        description=line.description,
        mpn=line.mpn,
        quantity=line.quantity,
        unit=line.unit,
        unit_price=line.unit_price,
        line_total=line.line_total,
        need_by=line.need_by,
        lead_time_days=line.lead_time_days,
        part_number=rfq_line.part_number if rfq_line else None,
        delivery_location=rfq_line.delivery_location if rfq_line else None,
    )


async def po_document(
    session: AsyncSession,
    po: IssuedPurchaseOrder,
    storage: BlobStorage | None = None,
    *,
    branded: bool = True,
) -> PoDocument:
    """The PO as the supplier receives it, from the stored rows (`storage`: the logo's).

    `branded=False` leaves the buyer's branding out (no logo read): for code that only
    needs the order's facts.
    """
    rfq = await session.get(Rfq, po.rfq_id)
    supplier = await session.get(Supplier, po.supplier_id)
    tenant = await session.get(Tenant, po.tenant_id)
    if rfq is None or supplier is None or tenant is None:
        raise NotFound(f"purchase order {po.id}: RFQ, supplier or tenant not found")
    lines = await po_lines(session, po.id)
    rfq_lines = {
        row.id: row
        for row in await session.scalars(
            select(RfqLine).where(
                RfqLine.id.in_([line.rfq_line_id for line in lines if line.rfq_line_id])
            )
        )
    }
    contact = await session.scalar(
        select(Contact.email)
        .join(Invite, Invite.contact_id == Contact.id)
        .where(Invite.id == po.invite_id)
    )
    quote = (
        (
            await session.execute(
                select(Proposal.version, Message.occurred_at)
                .join(ProposalLine, ProposalLine.proposal_id == Proposal.id)
                .join(Message, Message.id == Proposal.message_id)
                .where(ProposalLine.id == lines[0].proposal_line_id)
            )
        ).one_or_none()
        if lines and lines[0].proposal_line_id
        else None
    )
    award = await session.get(Award, po.award_id)
    return PoDocument(
        ref=po.ref,
        rfq_ref=rfq.ref,
        issued_on=po.created_at.date() if po.created_at else date.today(),
        buyer_company=tenant.name,
        supplier_name=supplier.name,
        currency=po.currency,
        incoterm=po.incoterm,
        payment_terms=po.payment_terms,
        total=po.total,
        lines=tuple(
            _po_line(line, rfq_lines.get(line.rfq_line_id) if line.rfq_line_id else None)
            for line in lines
        ),
        language=supplier_email_language(supplier),
        branding=await _branding(session, po.tenant_id, rfq, tenant.name, storage)
        if branded
        else None,
        supplier_contact=contact,
        quote_version=quote[0] if quote else None,
        quote_date=quote[1].date() if quote and quote[1] else None,
        approved_by=await _person(session, award.approved_by if award else None),
    )
