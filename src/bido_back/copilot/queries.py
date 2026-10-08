"""Read queries behind the copilot's tools. Each runs inside the caller's tenant session (RLS)."""

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement

from bido_back.db.models import (
    Contact,
    Invite,
    Item,
    PoLine,
    Proposal,
    ProposalLine,
    PurchaseOrder,
    Rfq,
    RfqLine,
    RfqTemplate,
    Supplier,
    SupplierContact,
)

_REF = re.compile(r"^\s*RFQ\s*-?\s*(\d+)\s*$", re.IGNORECASE)
AWAITING_STATUSES = ("draft", "pending_approval", "sent")


def normalize_mpn(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", value).upper()


def _mpn_matches(column: InstrumentedAttribute[str | None], mpn: str) -> ColumnElement[bool]:
    return func.upper(func.regexp_replace(column, "[^A-Za-z0-9]", "", "g")) == normalize_mpn(mpn)


async def find_rfq(session: AsyncSession, reference: str) -> Rfq | None:
    """An RFQ by id or by its reference (`RFQ-12`, `rfq 12`, `12`)."""
    value = reference.strip()
    try:
        return await session.get(Rfq, UUID(value))
    except ValueError:
        pass
    match = _REF.match(value) or re.match(r"^\s*(\d+)\s*$", value)
    if match is None:
        return None
    return await session.scalar(select(Rfq).where(Rfq.ref == f"RFQ-{int(match.group(1))}"))


async def rfqs(session: AsyncSession, status: str | None, limit: int) -> list[Rfq]:
    query = select(Rfq).order_by(Rfq.created_at.desc(), Rfq.id).limit(limit)
    if status is not None:
        query = query.where(Rfq.status == status)
    return list(await session.scalars(query))


async def lines(session: AsyncSession, rfq_id: UUID) -> list[RfqLine]:
    return list(
        await session.scalars(
            select(RfqLine).where(RfqLine.rfq_id == rfq_id).order_by(RfqLine.position)
        )
    )


async def invites(session: AsyncSession, rfq_id: UUID) -> list[tuple[Invite, str]]:
    rows = await session.execute(
        select(Invite, Supplier.name)
        .join(Supplier, Supplier.id == Invite.supplier_id)
        .where(Invite.rfq_id == rfq_id)
        .order_by(Invite.created_at, Invite.id)
    )
    return [(invite, name) for invite, name in rows]


@dataclass(slots=True)
class LatestProposal:
    proposal: Proposal
    supplier_name: str
    lines: list[tuple[ProposalLine, int | None]] = field(
        default_factory=list[tuple[ProposalLine, int | None]]
    )


async def latest_proposals(session: AsyncSession, rfq_id: UUID) -> list[LatestProposal]:
    """The newest proposal version of each invite of an RFQ, with its lines."""
    rows = await session.execute(
        select(Proposal, Supplier.name)
        .join(Invite, Invite.id == Proposal.invite_id)
        .join(Supplier, Supplier.id == Invite.supplier_id)
        .where(Invite.rfq_id == rfq_id)
        .order_by(Proposal.invite_id, Proposal.version.desc())
    )
    latest: dict[UUID, LatestProposal] = {}
    for proposal, name in rows:
        latest.setdefault(proposal.invite_id, LatestProposal(proposal, name))
    if not latest:
        return []
    by_id = {item.proposal.id: item for item in latest.values()}
    line_rows = await session.execute(
        select(ProposalLine, RfqLine.position)
        .outerjoin(RfqLine, RfqLine.id == ProposalLine.rfq_line_id)
        .where(ProposalLine.proposal_id.in_(by_id))
        .order_by(RfqLine.position, ProposalLine.min_quantity)
    )
    for line, position in line_rows:
        by_id[line.proposal_id].lines.append((line, position))
    return sorted(latest.values(), key=lambda item: item.supplier_name)


def missing_fields(proposal: Proposal) -> list[str]:
    review: object = proposal.fields.get("_review")
    if not isinstance(review, dict):
        return []
    found: object = cast(dict[str, Any], review).get("missing_fields")
    return [str(f) for f in cast(list[object], found)] if isinstance(found, list) else []


@dataclass(frozen=True, slots=True)
class PricePoint:
    rfq_ref: str
    supplier_name: str
    quoted_at: datetime
    offered_mpn: str | None
    min_quantity: Decimal | None
    unit_price: Decimal
    currency: str | None
    unit: str | None
    lead_time_days: int | None


async def price_history(session: AsyncSession, mpn: str, limit: int) -> list[PricePoint]:
    """Quoted unit prices for an MPN (or the buyer's part number) across the tenant's RFQs,
    newest first."""
    rows = await session.execute(
        select(ProposalLine, Proposal, Rfq.ref, Supplier.name)
        .join(Proposal, Proposal.id == ProposalLine.proposal_id)
        .join(Invite, Invite.id == Proposal.invite_id)
        .join(Rfq, Rfq.id == Invite.rfq_id)
        .join(Supplier, Supplier.id == Invite.supplier_id)
        .outerjoin(RfqLine, RfqLine.id == ProposalLine.rfq_line_id)
        .where(
            ProposalLine.unit_price.is_not(None),
            or_(
                _mpn_matches(ProposalLine.offered_mpn, mpn),
                _mpn_matches(RfqLine.mpn, mpn),
                _mpn_matches(RfqLine.part_number, mpn),
            ),
        )
        .order_by(Proposal.created_at.desc(), ProposalLine.min_quantity)
        .limit(limit)
    )
    points: list[PricePoint] = []
    for line, proposal, ref, name in rows:
        assert line.unit_price is not None
        points.append(
            PricePoint(
                rfq_ref=ref,
                supplier_name=name,
                quoted_at=proposal.created_at,
                offered_mpn=line.offered_mpn,
                min_quantity=line.min_quantity,
                unit_price=line.unit_price,
                currency=proposal.currency,
                unit=line.unit,
                lead_time_days=line.lead_time_days,
            )
        )
    return points


@dataclass(slots=True)
class SupplierRecord:
    supplier: Supplier
    invited: int = 0
    responded: int = 0
    last_response: datetime | None = None


async def supplier_records(
    session: AsyncSession, *, mpn: str | None, category: str | None
) -> tuple[list[SupplierRecord], bool]:
    """Suppliers with their invite history; filtered to RFQs with a matching line when asked.

    Returns the records and whether the filter matched anything (False means
    the list is every supplier, as a fallback).
    """
    matched_rfqs: list[UUID] | None = None
    if mpn or category:
        conditions: list[ColumnElement[bool]] = []
        if mpn:
            conditions.append(_mpn_matches(RfqLine.mpn, mpn))
        if category:
            conditions.append(RfqLine.category == category)
        matched_rfqs = list(await session.scalars(select(RfqLine.rfq_id).where(or_(*conditions))))

    invite_query = select(Invite.supplier_id, Invite.status, Invite.responded_at)
    if matched_rfqs:
        invite_query = invite_query.where(Invite.rfq_id.in_(matched_rfqs))
    stats: dict[UUID, SupplierRecord] = {}
    suppliers = {s.id: s for s in await session.scalars(select(Supplier))}
    for supplier_id, status, responded_at in await session.execute(invite_query):
        supplier = suppliers.get(supplier_id)
        if supplier is None:
            continue
        record = stats.setdefault(supplier_id, SupplierRecord(supplier))
        record.invited += 1
        if status == "responded":
            record.responded += 1
            if responded_at is not None and (
                record.last_response is None or responded_at > record.last_response
            ):
                record.last_response = responded_at
    matched = bool(matched_rfqs) and bool(stats)
    if not matched:
        for supplier in suppliers.values():
            stats.setdefault(supplier.id, SupplierRecord(supplier))
    records = sorted(
        stats.values(), key=lambda r: (-r.responded, -r.invited, r.supplier.name.lower())
    )
    return records, matched


# --- The company's suppliers and what it paid (imported data) ---------------------------


@dataclass(slots=True)
class SupplierEntry:
    supplier: Supplier
    contacts: list[tuple[str | None, str]] = field(default_factory=list[tuple[str | None, str]])
    purchase_orders: int = 0
    last_ordered_at: date | None = None
    categories: set[str] = field(default_factory=set[str])
    # Part number -> description, for every part bought from the supplier.
    parts: dict[str, str] = field(default_factory=dict[str, str])
    # Part numbers and MPNs, normalized, for the part filter.
    codes: set[str] = field(default_factory=set[str])
    invited: int = 0
    responded: int = 0


async def supplier_directory(
    session: AsyncSession,
    *,
    name: str | None = None,
    category: str | None = None,
    part_number: str | None = None,
) -> list[SupplierEntry]:
    """Every supplier of the company with its contacts, what it was bought from it
    (imported purchase history) and its RFQ invites.

    `name` matches part of the supplier's name; `category` and `part_number`
    keep the suppliers the company bought that category or part from. Sorted by
    the most recent purchase, then by name.
    """
    entries = {s.id: SupplierEntry(s) for s in await session.scalars(select(Supplier))}
    contacts = await session.execute(
        select(SupplierContact.supplier_id, Contact.name, Contact.email)
        .join(Contact, Contact.id == SupplierContact.contact_id)
        .where(Contact.email.is_not(None), Contact.pseudonymized_at.is_(None))
        .order_by(Contact.created_at, Contact.id)
    )
    for supplier_id, contact_name, email in contacts:
        if supplier_id in entries and email:
            entries[supplier_id].contacts.append((contact_name, email))
    bought = await session.execute(
        select(
            PurchaseOrder.supplier_id,
            PurchaseOrder.id,
            PurchaseOrder.ordered_at,
            Item.category,
            Item.part_number,
            Item.mpn,
            Item.description,
        )
        .join(PoLine, PoLine.purchase_order_id == PurchaseOrder.id)
        .join(Item, Item.id == PoLine.item_id)
    )
    orders: dict[UUID, set[UUID]] = defaultdict(set)
    for supplier_id, order_id, ordered_at, item_category, item_part, item_mpn, item_text in bought:
        entry = entries.get(supplier_id)
        if entry is None:
            continue
        orders[supplier_id].add(order_id)
        if entry.last_ordered_at is None or ordered_at > entry.last_ordered_at:
            entry.last_ordered_at = ordered_at
        if item_category:
            entry.categories.add(item_category)
        entry.parts[item_part] = item_text
        entry.codes.add(normalize_mpn(item_part))
        if item_mpn:
            entry.codes.add(normalize_mpn(item_mpn))
    for supplier_id, order_ids in orders.items():
        entries[supplier_id].purchase_orders = len(order_ids)
    for supplier_id, status in await session.execute(select(Invite.supplier_id, Invite.status)):
        entry = entries.get(supplier_id)
        if entry is not None:
            entry.invited += 1
            if status == "responded":
                entry.responded += 1

    def keep(entry: SupplierEntry) -> bool:
        if name and name.strip().lower() not in entry.supplier.name.lower():
            return False
        if category and category.strip().lower() not in {c.lower() for c in entry.categories}:
            return False
        if part_number:
            return normalize_mpn(part_number) in entry.codes
        return True

    found = [e for e in entries.values() if keep(e)]
    return sorted(
        found,
        key=lambda e: (
            e.last_ordered_at is None,
            -(e.last_ordered_at or date.min).toordinal(),
            e.supplier.name.lower(),
        ),
    )


@dataclass(frozen=True, slots=True)
class PaidLine:
    supplier_name: str
    po_number: str
    ordered_at: date
    quantity: Decimal
    unit_price: Decimal
    currency: str


@dataclass(slots=True)
class PaidPart:
    item: Item
    lines: list[PaidLine] = field(default_factory=list[PaidLine])


async def purchase_history(
    session: AsyncSession, *, part_numbers: list[str], supplier: str | None, limit: int
) -> list[PaidPart]:
    """What the company PAID per part, from its imported purchase orders, newest first.

    With no part numbers, the parts bought most recently (up to `limit`). A part
    number matches the internal part number or the MPN, ignoring punctuation.
    """
    query = (
        select(PoLine, PurchaseOrder, Supplier.name, Item)
        .join(PurchaseOrder, PurchaseOrder.id == PoLine.purchase_order_id)
        .join(Supplier, Supplier.id == PurchaseOrder.supplier_id)
        .join(Item, Item.id == PoLine.item_id)
        .order_by(PurchaseOrder.ordered_at.desc(), PurchaseOrder.po_number, PoLine.line_no)
    )
    if part_numbers:
        query = query.where(
            or_(
                *(_mpn_matches(Item.part_number, p) for p in part_numbers),
                *(_mpn_matches(Item.mpn, p) for p in part_numbers),
            )
        )
    if supplier:
        query = query.where(Supplier.name.ilike(f"%{supplier.strip()}%"))
    parts: dict[UUID, PaidPart] = {}
    for line, order, supplier_name, item in await session.execute(query):
        part = parts.get(item.id)
        if part is None:
            if len(parts) >= limit:
                continue
            part = parts[item.id] = PaidPart(item)
        part.lines.append(
            PaidLine(
                supplier_name=supplier_name,
                po_number=order.po_number,
                ordered_at=order.ordered_at,
                quantity=line.quantity,
                unit_price=line.unit_price,
                currency=order.currency,
            )
        )
    return list(parts.values())


async def first_contacts(session: AsyncSession, supplier_ids: list[UUID]) -> dict[UUID, UUID]:
    """Each supplier's first contact with a usable email address."""
    rows = await session.execute(
        select(SupplierContact.supplier_id, Contact.id)
        .join(Contact, Contact.id == SupplierContact.contact_id)
        .where(
            SupplierContact.supplier_id.in_(supplier_ids),
            Contact.email.is_not(None),
            Contact.pseudonymized_at.is_(None),
        )
        .order_by(SupplierContact.supplier_id, Contact.created_at, Contact.id)
    )
    found: dict[UUID, UUID] = {}
    for supplier_id, contact_id in rows:
        found.setdefault(supplier_id, contact_id)
    return found


async def default_template(session: AsyncSession) -> RfqTemplate | None:
    return await session.scalar(
        # The general default: category variants have their own defaults (#11).
        select(RfqTemplate).where(
            RfqTemplate.is_default,
            RfqTemplate.category.is_(None),
            RfqTemplate.status == "ready",
        )
    )


def count_by(values: list[str]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for value in values:
        counts[value] += 1
    return dict(counts)
