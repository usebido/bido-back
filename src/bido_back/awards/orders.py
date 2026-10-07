"""Purchase-order rows: numbering per tenant and the document built from them."""

from datetime import date
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from bido_back.awards.render import PoDocument, PoLine
from bido_back.db.models import PurchaseOrder, PurchaseOrderLine, Rfq, Supplier, Tenant
from bido_back.rfqs.errors import NotFound


async def next_po_number(session: AsyncSession, tenant_id: UUID) -> int:
    """The next PO number of the tenant; serialized for the rest of the transaction."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"po_number:{tenant_id}"},
    )
    highest = await session.scalar(select(func.max(PurchaseOrder.number)))
    return (highest or 0) + 1


def po_ref(number: int) -> str:
    return f"PO-{number}"


async def po_lines(session: AsyncSession, po_id: UUID) -> list[PurchaseOrderLine]:
    return list(
        await session.scalars(
            select(PurchaseOrderLine)
            .where(PurchaseOrderLine.purchase_order_id == po_id)
            .order_by(PurchaseOrderLine.position)
        )
    )


async def po_document(session: AsyncSession, po: PurchaseOrder) -> PoDocument:
    rfq = await session.get(Rfq, po.rfq_id)
    supplier = await session.get(Supplier, po.supplier_id)
    tenant = await session.get(Tenant, po.tenant_id)
    if rfq is None or supplier is None or tenant is None:
        raise NotFound(f"purchase order {po.id}: RFQ, supplier or tenant not found")
    lines = await po_lines(session, po.id)
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
            PoLine(
                position=line.position,
                description=line.description,
                mpn=line.mpn,
                quantity=line.quantity,
                unit=line.unit,
                unit_price=line.unit_price,
                line_total=line.line_total,
                need_by=line.need_by,
                lead_time_days=line.lead_time_days,
            )
            for line in lines
        ),
    )
