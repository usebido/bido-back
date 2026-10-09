"""The purchase order after the send: acknowledged, received per line, closed, cancelled.

`status` (`draft`, `pending_approval`, `sent`) is the send; `fulfilment_state`
is the order's life after it:

    sent -> acknowledged -> partially_received -> received -> closed
      \\__________________/______________________/     (cancelled: before received)
                 receipts; close short with a reason from any open state

- `acknowledge`: the supplier confirmed the order. Recorded by the buyer; the
  mailbox pipeline can call the same function when it parses a confirmation
  (not wired yet: `source="email"` is the seam).
- `receive`: one delivery, a quantity per PO line. The total received per line
  never exceeds the ordered quantity (`OverReceipt`); the state follows the sum.
- `close`: everything arrived, or the rest is given up (`short`, with a reason;
  the shortfall per line goes into the event).
- `cancel`: with a reason, while nothing or only part of it arrived.

Every transition takes the PO's row lock, writes its audit event in the same
transaction (`purchase_order.*`, keyed by `rfq_id` for the activity feed), and
the terminal ones complete the RFQ (`rfqs.completed_at`, `rfq.completed`) once
all its POs are closed or cancelled with at least one closed.

The session-level functions work inside the caller's tenant session; the
`*_purchase_order` wrappers open one for the API and the Copilot.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Final, Literal, cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.auth import Principal
from bido_back.awards.orders import po_lines
from bido_back.awards.schemas import (
    OPEN_FULFILMENT_STATES,
    AcknowledgeRequest,
    CancelRequest,
    CloseRequest,
    FulfilmentState,
    PurchaseOrderFulfilment,
    ReceiptLineView,
    ReceiptRequest,
    ReceiptView,
)
from bido_back.db import tenant_session
from bido_back.db.models import (
    IssuedPurchaseOrder,
    IssuedPurchaseOrderLine,
    PurchaseOrderReceipt,
    PurchaseOrderReceiptLine,
    Rfq,
)
from bido_back.rfqs.audit import rfq_event
from bido_back.rfqs.errors import InvalidState, NotFound, RfqError

type Db = AsyncEngine | async_sessionmaker[AsyncSession]

PO_ACKNOWLEDGED: Final = "purchase_order.acknowledged"
PO_RECEIVED: Final = "purchase_order.received"
PO_CLOSED: Final = "purchase_order.closed"
PO_CANCELLED: Final = "purchase_order.cancelled"
RFQ_COMPLETED: Final = "rfq.completed"

AcknowledgementSource = Literal["manual", "email"]

_RECEIVING: Final = frozenset({"sent", "acknowledged", "partially_received"})
_ZERO: Final = Decimal("0")


class RefusedInput(RfqError):
    """The request cannot be applied as given (the message says what to change)."""


class OverReceipt(RefusedInput):
    """A receipt would bring a line above its ordered quantity."""


class ReasonRequired(RefusedInput):
    """Closing short needs a reason."""


class UnknownLine(RefusedInput):
    """A receipt names a line that is not on this purchase order."""


# --- Reading ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Quantities:
    """Per PO: ordered and received totals, received per line, how many receipts."""

    ordered: Decimal
    received: Decimal
    by_line: dict[UUID, Decimal]
    receipts: int


async def quantities(session: AsyncSession, po_ids: Sequence[UUID]) -> dict[UUID, Quantities]:
    """Ordered vs received for every PO in `po_ids`, in three grouped queries."""
    ids = list(dict.fromkeys(po_ids))
    if not ids:
        return {}
    ordered: dict[UUID, Decimal] = {po_id: _ZERO for po_id in ids}
    by_line: dict[UUID, dict[UUID, Decimal]] = {po_id: {} for po_id in ids}
    for po_id, line_id, quantity in await session.execute(
        select(
            IssuedPurchaseOrderLine.purchase_order_id,
            IssuedPurchaseOrderLine.id,
            IssuedPurchaseOrderLine.quantity,
        ).where(IssuedPurchaseOrderLine.purchase_order_id.in_(ids))
    ):
        ordered[po_id] += quantity
        by_line[po_id][line_id] = _ZERO
    for po_id, line_id, received in await session.execute(
        select(
            PurchaseOrderReceipt.purchase_order_id,
            PurchaseOrderReceiptLine.purchase_order_line_id,
            func.sum(PurchaseOrderReceiptLine.quantity),
        )
        .join(PurchaseOrderReceipt, PurchaseOrderReceipt.id == PurchaseOrderReceiptLine.receipt_id)
        .where(PurchaseOrderReceipt.purchase_order_id.in_(ids))
        .group_by(
            PurchaseOrderReceipt.purchase_order_id, PurchaseOrderReceiptLine.purchase_order_line_id
        )
    ):
        by_line[po_id][line_id] = Decimal(received)
    counts: dict[UUID, int] = {
        po_id: count
        for po_id, count in await session.execute(
            select(PurchaseOrderReceipt.purchase_order_id, func.count())
            .where(PurchaseOrderReceipt.purchase_order_id.in_(ids))
            .group_by(PurchaseOrderReceipt.purchase_order_id)
        )
    }
    return {
        po_id: Quantities(
            ordered=ordered[po_id],
            received=sum(by_line[po_id].values(), _ZERO),
            by_line=by_line[po_id],
            receipts=counts.get(po_id, 0),
        )
        for po_id in ids
    }


def fulfilment_view(po: IssuedPurchaseOrder, totals: Quantities) -> PurchaseOrderFulfilment:
    state = cast(FulfilmentState, po.fulfilment_state)
    return PurchaseOrderFulfilment(
        state=state,
        open=state in OPEN_FULFILMENT_STATES,
        ordered_quantity=totals.ordered,
        received_quantity=totals.received,
        receipts_count=totals.receipts,
        acknowledged_at=po.acknowledged_at,
        acknowledged_by=po.acknowledged_by,
        supplier_order_ref=po.supplier_order_ref,
        acknowledgement_note=po.acknowledgement_note,
        closed_at=po.closed_at,
        closed_by=po.closed_by,
        closed_short=po.closed_short,
        close_reason=po.close_reason,
        cancelled_at=po.cancelled_at,
        cancelled_by=po.cancelled_by,
        cancel_reason=po.cancel_reason,
    )


async def receipts_of(session: AsyncSession, po_id: UUID) -> list[ReceiptView]:
    """The PO's receipts, oldest first, each with its lines in PO order."""
    lines = {line.id: line for line in await po_lines(session, po_id)}
    receipts = list(
        await session.scalars(
            select(PurchaseOrderReceipt)
            .where(PurchaseOrderReceipt.purchase_order_id == po_id)
            .order_by(PurchaseOrderReceipt.received_on, PurchaseOrderReceipt.created_at)
        )
    )
    if not receipts:
        return []
    received: dict[UUID, list[PurchaseOrderReceiptLine]] = {r.id: [] for r in receipts}
    for row in await session.scalars(
        select(PurchaseOrderReceiptLine).where(
            PurchaseOrderReceiptLine.receipt_id.in_(list(received))
        )
    ):
        received[row.receipt_id].append(row)
    views: list[ReceiptView] = []
    for receipt in receipts:
        rows = sorted(received[receipt.id], key=lambda r: lines[r.purchase_order_line_id].position)
        views.append(
            ReceiptView(
                id=receipt.id,
                purchase_order_id=receipt.purchase_order_id,
                received_on=receipt.received_on,
                invoice_number=receipt.invoice_number,
                invoice_date=receipt.invoice_date,
                note=receipt.note,
                recorded_by=receipt.recorded_by,
                created_at=receipt.created_at,
                lines=[
                    ReceiptLineView(
                        line_id=row.purchase_order_line_id,
                        position=lines[row.purchase_order_line_id].position,
                        description=lines[row.purchase_order_line_id].description,
                        unit=lines[row.purchase_order_line_id].unit,
                        quantity=row.quantity,
                    )
                    for row in rows
                ],
            )
        )
    return views


# --- Transitions (inside the caller's tenant session) -------------------------------------------


async def locked_po(session: AsyncSession, po_id: UUID) -> IssuedPurchaseOrder:
    """The PO, locked for this transaction; its RFQ row is locked first (completion races)."""
    rfq_id = await session.scalar(
        select(IssuedPurchaseOrder.rfq_id).where(IssuedPurchaseOrder.id == po_id)
    )
    if rfq_id is None:
        raise NotFound(f"purchase order {po_id} not found")
    await session.execute(select(Rfq.id).where(Rfq.id == rfq_id).with_for_update())
    po = await session.scalar(
        select(IssuedPurchaseOrder).where(IssuedPurchaseOrder.id == po_id).with_for_update()
    )
    if po is None:
        raise NotFound(f"purchase order {po_id} not found")
    return po


def _at(day: date | None, now: datetime) -> datetime:
    """`now` when the day is today or not given; else the end of that day, UTC."""
    if day is None or day >= now.date():
        return now
    return datetime.combine(day, time.max, tzinfo=UTC)


async def acknowledge(
    session: AsyncSession,
    po: IssuedPurchaseOrder,
    actor_user_id: UUID | None,
    request: AcknowledgeRequest,
    *,
    source: AcknowledgementSource = "manual",
    now: datetime | None = None,
) -> None:
    """The supplier confirmed the order: `sent` -> `acknowledged`."""
    if po.fulfilment_state != "sent":
        raise InvalidState(
            f"purchase order {po.id} is {po.fulfilment_state}; only a sent order is acknowledged"
        )
    now = now or datetime.now(UTC)
    acknowledged_at = _at(request.acknowledged_on, now)
    po.fulfilment_state = "acknowledged"
    po.acknowledged_at = acknowledged_at
    po.acknowledged_by = actor_user_id
    po.supplier_order_ref = request.supplier_order_ref
    po.acknowledgement_note = request.note
    session.add(
        rfq_event(
            po.tenant_id,
            actor_user_id,
            PO_ACKNOWLEDGED,
            po.rfq_id,
            purchase_order_id=po.id,
            purchase_order_ref=po.ref,
            invite_id=po.invite_id,
            acknowledged_on=acknowledged_at.date(),
            supplier_order_ref=request.supplier_order_ref,
            source=source,
        )
    )
    await session.flush()


@dataclass(frozen=True, slots=True)
class Received:
    receipt_id: UUID
    state: FulfilmentState


async def receive(
    session: AsyncSession,
    po: IssuedPurchaseOrder,
    actor_user_id: UUID,
    request: ReceiptRequest,
    *,
    now: datetime | None = None,
) -> Received:
    """One delivery against the PO; the state follows the received totals."""
    if po.fulfilment_state not in _RECEIVING:
        raise InvalidState(
            f"purchase order {po.id} is {po.fulfilment_state}; it is not expecting goods"
        )
    now = now or datetime.now(UTC)
    lines = {line.id: line for line in await po_lines(session, po.id)}
    totals = (await quantities(session, [po.id]))[po.id]
    wanted: dict[UUID, Decimal] = {}
    for item in request.lines:
        if item.line_id not in lines:
            raise UnknownLine(f"line {item.line_id} is not on purchase order {po.ref}")
        wanted[item.line_id] = wanted.get(item.line_id, _ZERO) + item.quantity
    for line_id, quantity in wanted.items():
        line = lines[line_id]
        so_far = totals.by_line.get(line_id, _ZERO)
        if so_far + quantity > line.quantity:
            raise OverReceipt(
                f"line {line.position} of {po.ref}: ordered {line.quantity} {line.unit}, "
                f"received {so_far} so far; receiving {quantity} more would exceed the order "
                f"by {so_far + quantity - line.quantity}"
            )
    receipt = PurchaseOrderReceipt(
        tenant_id=po.tenant_id,
        purchase_order_id=po.id,
        received_on=request.received_on or now.date(),
        invoice_number=request.invoice_number,
        invoice_date=request.invoice_date,
        note=request.note,
        recorded_by=actor_user_id,
    )
    session.add(receipt)
    await session.flush()
    for line_id, quantity in wanted.items():
        session.add(
            PurchaseOrderReceiptLine(
                tenant_id=po.tenant_id,
                receipt_id=receipt.id,
                purchase_order_line_id=line_id,
                quantity=quantity,
            )
        )
    after = {
        line_id: totals.by_line.get(line_id, _ZERO) + wanted.get(line_id, _ZERO)
        for line_id in lines
    }
    complete = all(after[line_id] >= line.quantity for line_id, line in lines.items())
    state: FulfilmentState = "received" if complete else "partially_received"
    po.fulfilment_state = state
    session.add(
        rfq_event(
            po.tenant_id,
            actor_user_id,
            PO_RECEIVED,
            po.rfq_id,
            purchase_order_id=po.id,
            purchase_order_ref=po.ref,
            invite_id=po.invite_id,
            receipt_id=receipt.id,
            received_on=receipt.received_on,
            invoice_number=request.invoice_number,
            state=state,
            lines=[
                {
                    "position": lines[line_id].position,
                    "quantity": quantity,
                    "received": after[line_id],
                    "ordered": lines[line_id].quantity,
                }
                for line_id, quantity in sorted(
                    wanted.items(), key=lambda item: lines[item[0]].position
                )
            ],
        )
    )
    await session.flush()
    return Received(receipt_id=receipt.id, state=state)


async def close(
    session: AsyncSession,
    po: IssuedPurchaseOrder,
    actor_user_id: UUID,
    request: CloseRequest,
    *,
    now: datetime | None = None,
) -> None:
    """Close a received PO; or, with `short` and a reason, give up what did not arrive."""
    state = po.fulfilment_state
    if state in ("closed", "cancelled"):
        raise InvalidState(f"purchase order {po.id} is already {state}")
    if state not in OPEN_FULFILMENT_STATES:
        raise InvalidState(f"purchase order {po.id} was not sent; cancel it instead")
    short = state != "received"
    if short and not request.short:
        raise InvalidState(
            f"purchase order {po.id} is {state}: not everything arrived. Record the "
            "receipt, or close it short with a reason"
        )
    if short and not request.reason:
        raise ReasonRequired(f"say why purchase order {po.ref} is closed short")
    now = now or datetime.now(UTC)
    totals = (await quantities(session, [po.id]))[po.id]
    shortfall = [
        {
            "position": line.position,
            "ordered": line.quantity,
            "received": totals.by_line.get(line.id, _ZERO),
        }
        for line in await po_lines(session, po.id)
        if totals.by_line.get(line.id, _ZERO) < line.quantity
    ]
    po.fulfilment_state = "closed"
    po.closed_at = now
    po.closed_by = actor_user_id
    po.closed_short = short
    po.close_reason = request.reason
    session.add(
        rfq_event(
            po.tenant_id,
            actor_user_id,
            PO_CLOSED,
            po.rfq_id,
            purchase_order_id=po.id,
            purchase_order_ref=po.ref,
            invite_id=po.invite_id,
            short=short,
            reason=request.reason,
            shortfall=shortfall,
        )
    )
    await session.flush()
    await _complete_rfq_if_done(session, po.rfq_id, actor_user_id, now)


async def cancel(
    session: AsyncSession,
    po: IssuedPurchaseOrder,
    actor_user_id: UUID,
    request: CancelRequest,
    *,
    now: datetime | None = None,
) -> None:
    """Cancel the order with a reason, at any point before it is fully received."""
    state = po.fulfilment_state
    if state in ("closed", "cancelled"):
        raise InvalidState(f"purchase order {po.id} is already {state}")
    if state == "received":
        raise InvalidState(f"purchase order {po.id} was fully received; close it instead")
    if po.status == "pending_approval":
        raise InvalidState(
            f"purchase order {po.id} has a send waiting for approval; decide it first"
        )
    now = now or datetime.now(UTC)
    po.fulfilment_state = "cancelled"
    po.cancelled_at = now
    po.cancelled_by = actor_user_id
    po.cancel_reason = request.reason
    session.add(
        rfq_event(
            po.tenant_id,
            actor_user_id,
            PO_CANCELLED,
            po.rfq_id,
            purchase_order_id=po.id,
            purchase_order_ref=po.ref,
            invite_id=po.invite_id,
            was=state,
            sent=po.sent_at is not None,
            reason=request.reason,
        )
    )
    await session.flush()
    await _complete_rfq_if_done(session, po.rfq_id, actor_user_id, now)


def rfq_is_complete(states: Iterable[str]) -> bool:
    """Every PO closed or cancelled, at least one of them closed."""
    seen = list(states)
    return bool(seen) and "closed" in seen and all(s in ("closed", "cancelled") for s in seen)


async def _complete_rfq_if_done(
    session: AsyncSession, rfq_id: UUID, actor_user_id: UUID, now: datetime
) -> None:
    rfq = await session.get(Rfq, rfq_id)
    if rfq is None or rfq.completed_at is not None:
        return
    states = list(
        await session.scalars(
            select(IssuedPurchaseOrder.fulfilment_state).where(IssuedPurchaseOrder.rfq_id == rfq_id)
        )
    )
    if not rfq_is_complete(states):
        return
    rfq.completed_at = now
    session.add(
        rfq_event(
            rfq.tenant_id,
            actor_user_id,
            RFQ_COMPLETED,
            rfq.id,
            purchase_orders=len(states),
            closed=sum(s == "closed" for s in states),
            cancelled=sum(s == "cancelled" for s in states),
        )
    )
    await session.flush()


# --- Entry points for the API and the Copilot (their own tenant session) -------------------------


async def acknowledge_purchase_order(
    db: Db, principal: Principal, po_id: UUID, request: AcknowledgeRequest
) -> None:
    async with tenant_session(db, principal.tenant_id) as session:
        await acknowledge(session, await locked_po(session, po_id), principal.user_id, request)


async def receive_purchase_order(
    db: Db, principal: Principal, po_id: UUID, request: ReceiptRequest
) -> Received:
    async with tenant_session(db, principal.tenant_id) as session:
        return await receive(session, await locked_po(session, po_id), principal.user_id, request)


async def close_purchase_order(
    db: Db, principal: Principal, po_id: UUID, request: CloseRequest
) -> None:
    async with tenant_session(db, principal.tenant_id) as session:
        await close(session, await locked_po(session, po_id), principal.user_id, request)


async def cancel_purchase_order(
    db: Db, principal: Principal, po_id: UUID, request: CancelRequest
) -> None:
    async with tenant_session(db, principal.tenant_id) as session:
        await cancel(session, await locked_po(session, po_id), principal.user_id, request)


async def list_receipts(db: Db, principal: Principal, po_id: UUID) -> list[ReceiptView]:
    async with tenant_session(db, principal.tenant_id) as session:
        if await session.get(IssuedPurchaseOrder, po_id) is None:
            raise NotFound(f"purchase order {po_id} not found")
        return await receipts_of(session, po_id)
