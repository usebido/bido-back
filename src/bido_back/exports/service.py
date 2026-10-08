"""The tables behind the RFQ-list and comparison exports.

- **RFQ list:** the frontend's list columns (RFQ, status, suppliers, landed
  cost, need-by, due, owner), plus title, responses and the currency. The
  landed cost is the lowest complete first-tier total among the suppliers'
  latest proposals, computed by the comparison (so it includes FX and
  amortized tooling); it is computed only for RFQs that have a proposal.
- **Comparison:** one row per supplier, requested line and quantity tier,
  with the quoted values, the base-currency amounts, the flags and the
  fields a person corrected.
"""

from datetime import date
from decimal import Decimal
from typing import Final
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.auth import Principal
from bido_back.comparison.compute import Comparison, best_first_tier_total
from bido_back.comparison.loader import load_comparison
from bido_back.db import tenant_session
from bido_back.db.models import Invite, Proposal, Rfq, RfqLine, Supplier
from bido_back.exports.render import Table, Value
from bido_back.rfqs.errors import NotFound
from bido_back.rfqs.overview import owner_names

Db = AsyncEngine | async_sessionmaker[AsyncSession]

# Same cap as the RFQ list endpoint.
MAX_RFQS: Final = 500

RFQ_LIST_HEADER: Final = (
    "RFQ",
    "Title",
    "Status",
    "Suppliers",
    "Responses",
    "Landed cost",
    "Currency",
    "Need-by",
    "Due",
    "Owner",
    "Created",
    # Appended, so files read by column position keep working.
    "Payment terms",
    "Quote validity (days)",
)

COMPARISON_HEADER: Final = (
    "RFQ",
    "Line",
    "Description",
    "Requested MPN",
    "Quantity",
    "Unit",
    "Supplier",
    "Proposal version",
    "Offered MPN",
    "Alternative",
    "Currency",
    "Unit price",
    "Tooling",
    "MOQ",
    "Lead time (days)",
    "Valid until",
    "Incoterm",
    "Base currency",
    "Unit price (base)",
    "Effective unit price (base)",
    "Line total (base)",
    "Flags",
    "Corrected fields",
)


def comparison_table(comparison: Comparison, rfq_ref: str) -> Table:
    lines = {line.id: line for line in comparison.lines}
    rows: list[list[Value]] = []
    for supplier in comparison.suppliers:
        for cell in supplier.cells:
            line = lines[cell.rfq_line_id]
            rows.append(
                [
                    rfq_ref,
                    line.position,
                    line.description,
                    line.mpn,
                    cell.quantity,
                    line.unit,
                    supplier.supplier_name,
                    supplier.proposal_version,
                    cell.offered_mpn,
                    "yes" if cell.is_alternative else "no",
                    cell.currency,
                    cell.unit_price,
                    cell.tooling_cost,
                    cell.moq,
                    cell.lead_time_days,
                    supplier.valid_until,
                    supplier.incoterm,
                    comparison.base_currency,
                    cell.unit_price_base,
                    cell.effective_unit_price_base,
                    cell.line_total_base,
                    "; ".join([*supplier.flags, *cell.flags]),
                    "; ".join(cell.corrected_fields),
                ]
            )
    return Table(header=COMPARISON_HEADER, rows=rows)


async def export_comparison(
    db: Db, principal: Principal, rfq_id: UUID, today: date
) -> tuple[str, Table]:
    async with tenant_session(db, principal.tenant_id) as session:
        ref = await session.scalar(select(Rfq.ref).where(Rfq.id == rfq_id))
    if ref is None:
        raise NotFound(f"rfq {rfq_id} not found")
    comparison = await load_comparison(db, principal, rfq_id, today)
    return ref, comparison_table(comparison, ref)


async def export_rfq_list(db: Db, principal: Principal, today: date) -> Table:
    async with tenant_session(db, principal.tenant_id) as session:
        rfqs = list(
            await session.scalars(
                select(Rfq).order_by(Rfq.created_at.desc(), Rfq.id).limit(MAX_RFQS)
            )
        )
        ids = [rfq.id for rfq in rfqs]
        suppliers: dict[UUID, list[str]] = {i: [] for i in ids}
        responded: dict[UUID, int] = dict.fromkeys(ids, 0)
        for rfq_id, name, status in await session.execute(
            select(Invite.rfq_id, Supplier.name, Invite.status)
            .join(Supplier, Supplier.id == Invite.supplier_id)
            .where(Invite.rfq_id.in_(ids))
            .order_by(Invite.created_at, Invite.id)
        ):
            suppliers[rfq_id].append(name)
            responded[rfq_id] += status == "responded"
        need_by: dict[UUID, date | None] = {
            rfq_id: earliest
            for rfq_id, earliest in await session.execute(
                select(RfqLine.rfq_id, func.min(RfqLine.need_by))
                .where(RfqLine.rfq_id.in_(ids))
                .group_by(RfqLine.rfq_id)
            )
        }
        quoted = set(
            await session.scalars(
                select(Invite.rfq_id)
                .join(Proposal, Proposal.invite_id == Invite.id)
                .where(Invite.rfq_id.in_(ids))
                .distinct()
            )
        )
        owners = await owner_names(session, {rfq.created_by for rfq in rfqs})

    landed: dict[UUID, Decimal | None] = {}
    for rfq_id in quoted:
        landed[rfq_id] = best_first_tier_total(await load_comparison(db, principal, rfq_id, today))

    rows: list[list[Value]] = [
        [
            rfq.ref,
            rfq.title,
            rfq.status,
            "; ".join(suppliers[rfq.id]),
            f"{responded[rfq.id]}/{len(suppliers[rfq.id])}",
            landed.get(rfq.id),
            rfq.base_currency,
            need_by.get(rfq.id),
            rfq.response_due,
            owners.get(rfq.created_by),
            rfq.created_at.date(),
            rfq.payment_terms,
            rfq.quote_validity_days,
        ]
        for rfq in rfqs
    ]
    return Table(header=RFQ_LIST_HEADER, rows=rows)
