"""Copilot tools for the purchase order after the send (`awards.lifecycle`).

- `list_purchase_orders` (read): the order book, newest first, with a count per
  fulfilment state over all the company's orders, so "which POs are open?" and
  "what did we receive?" have an answer.
- `acknowledge_purchase_order`, `receive_purchase_order`,
  `close_purchase_order`, `cancel_purchase_order` (draft, gated): they change
  the buyer's records only (nothing is sent), but never on the model's own
  initiative. Each works in two calls (`copilot.proposals`): the first stores
  exactly what it would record and answers a preview (for a receipt: ordered,
  received so far, this delivery, per line); the second, from a later run with
  the proposal's id, runs the STORED request, whatever the model passes then.

A PO is named by its reference (PO-3) or id, or by its RFQ when that RFQ has
one open order. Line quantities come from the buyer's message; the preview is
refused when they would exceed what was ordered.
"""

import re
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final, Literal, cast
from uuid import UUID

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bido_back.agents.catalog.contracts import ModelDecimal
from bido_back.agents.context import ToolContext
from bido_back.agents.tools import AnyTool, Tool, ToolError
from bido_back.auth import Principal
from bido_back.awards import lifecycle
from bido_back.awards import service as award_service
from bido_back.awards.orders import po_lines
from bido_back.awards.schemas import (
    OPEN_FULFILMENT_STATES,
    AcknowledgeRequest,
    CancelRequest,
    CloseRequest,
    FulfilmentState,
    PurchaseOrderSummary,
    ReceiptLineRequest,
    ReceiptRequest,
)
from bido_back.copilot import proposals, queries
from bido_back.copilot.proposals import (
    PROPOSAL_ARG,
    AwaitingConfirmation,
    ProposalPreview,
    ProposedOrderAction,
    ProposedOrderLine,
)
from bido_back.db import tenant_session
from bido_back.db.models import IssuedPurchaseOrder, Rfq, Supplier
from bido_back.rfqs.errors import RfqError

if TYPE_CHECKING:
    from bido_back.copilot.tools import CopilotToolDeps

MAX_LIST: Final = 20
_PO_REF: Final = re.compile(r"^\s*(?:PO|po|Po)?[\s#-]*(\d+)\s*$")
_ZERO: Final = Decimal("0")

STATE_HINT: Final = (
    "Fulfilment states: `not_sent` (draft, or the send waits for approval), `sent`, "
    "`acknowledged` (the supplier confirmed), `partially_received`, `received` (everything "
    "arrived), `closed`, `cancelled`. `open` orders are sent and neither closed nor cancelled: "
    "when the buyer asks for open, pending or outstanding POs, pass open=true."
)
_CONFIRM_FIRST: Final = (
    " Two calls: the first (no proposal_id) records nothing and answers a preview with a "
    "proposal_id; show it and ask. Call again with that proposal_id only after the buyer "
    "confirms in their own later message. Nothing is sent to the supplier either way."
)
_PO_ARG: Final = (
    "The purchase order: its reference (PO-3) or id. Or leave it empty and pass `rfq` "
    "when the buyer names the RFQ"
)
_RFQ_ARG: Final = (
    "The RFQ (RFQ-12 or id) whose order is meant, when `po` is empty; refused when the "
    "RFQ has several open orders (then name the PO)"
)


# --- Models --------------------------------------------------------------------------------


class ListPurchaseOrdersIn(BaseModel):
    rfq: str | None = Field(default=None, description="Only this RFQ's orders (RFQ-12 or id)")
    open: bool | None = Field(
        default=None, description="true: only open orders; false: only finished ones. " + STATE_HINT
    )
    state: FulfilmentState | None = Field(default=None, description="Only this fulfilment state")
    limit: int = Field(default=10, ge=1, le=MAX_LIST)


class PurchaseOrderItem(BaseModel):
    id: UUID
    ref: str
    rfq_ref: str
    supplier_name: str
    status: str = Field(description="The send: draft, pending_approval, sent")
    state: str = Field(description="The fulfilment state; see the tool's description")
    open: bool
    currency: str
    total: Decimal
    ordered_quantity: Decimal
    received_quantity: Decimal
    sent_at: date | None
    acknowledged_at: date | None
    supplier_order_ref: str | None
    closed_at: date | None
    closed_short: bool
    cancelled_at: date | None


class ListPurchaseOrdersOut(BaseModel):
    purchase_orders: list[PurchaseOrderItem]
    by_state: dict[str, int] = Field(
        description="How many orders the company has in each fulfilment state, over ALL its "
        "orders (ignoring the filters and `limit`)"
    )


class OrderRef(BaseModel):
    po: str | None = Field(default=None, description=_PO_ARG)
    rfq: str | None = Field(default=None, description=_RFQ_ARG)
    proposal_id: UUID | None = Field(default=None, description=PROPOSAL_ARG)


class AcknowledgeIn(OrderRef):
    acknowledged_on: date | None = Field(
        default=None, description="When the supplier confirmed; today when the buyer does not say"
    )
    supplier_order_ref: str | None = Field(
        default=None, max_length=120, description="The supplier's own order or confirmation number"
    )
    note: str | None = Field(default=None, max_length=2000)


class ReceiptLineIn(BaseModel):
    position: int = Field(ge=1, description="The PO line's position")
    quantity: ModelDecimal = Field(description="Received now, in the line's unit; above zero")


class ReceiveIn(OrderRef):
    received_on: date | None = Field(default=None, description="Delivery date; today if unsaid")
    lines: list[ReceiptLineIn] = Field(
        default_factory=list[ReceiptLineIn],
        max_length=200,
        description="What arrived, per line. Empty = everything still outstanding on every "
        "line (the buyer said it all arrived)",
    )
    invoice_number: str | None = Field(default=None, max_length=120)
    invoice_date: date | None = None
    note: str | None = Field(default=None, max_length=2000)


class CloseIn(OrderRef):
    short: bool = Field(
        default=False,
        description="Close although not everything arrived: the rest is given up (reason needed)",
    )
    reason: str | None = Field(default=None, max_length=2000)


class CancelIn(OrderRef):
    reason: str | None = Field(default=None, max_length=2000, description="Why; required")


class OrderActionOut(AwaitingConfirmation):
    ref: str | None = None
    state: str | None = Field(default=None, description="The order's fulfilment state now")
    receipt_id: UUID | None = None
    rfq_completed: bool = Field(
        default=False, description="This action completed the RFQ (all its orders are done)"
    )


Action = Literal[
    "acknowledge_purchase_order",
    "receive_purchase_order",
    "close_purchase_order",
    "cancel_purchase_order",
]


# --- Helpers -------------------------------------------------------------------------------


def _principal(ctx: ToolContext) -> Principal:
    if ctx.user_id is None:
        raise ToolError("the copilot needs a signed-in user")
    return Principal(tenant_id=ctx.tenant_id, user_id=ctx.user_id)


async def _find_po(session: AsyncSession, po: str | None, rfq: str | None) -> IssuedPurchaseOrder:
    if po:
        value = po.strip()
        row: IssuedPurchaseOrder | None = None
        try:
            row = await session.get(IssuedPurchaseOrder, UUID(value))
        except ValueError:
            match = _PO_REF.match(value)
            if match is not None:
                row = await session.scalar(
                    select(IssuedPurchaseOrder).where(
                        IssuedPurchaseOrder.ref == f"PO-{int(match.group(1))}"
                    )
                )
        if row is None:
            raise ToolError(f"no purchase order {po!r}; use list_purchase_orders to find it")
        return row
    if not rfq:
        raise ToolError("name the purchase order (po) or its RFQ (rfq)")
    found = await queries.find_rfq(session, rfq)
    if found is None:
        raise ToolError(f"no RFQ {rfq!r}; use list_rfqs to find it")
    orders = list(
        await session.scalars(
            select(IssuedPurchaseOrder)
            .where(IssuedPurchaseOrder.rfq_id == found.id)
            .order_by(IssuedPurchaseOrder.number)
        )
    )
    if not orders:
        raise ToolError(f"{found.ref} has no purchase order yet")
    open_orders = [o for o in orders if o.fulfilment_state in OPEN_FULFILMENT_STATES]
    candidates = open_orders or orders
    if len(candidates) == 1:
        return candidates[0]
    refs = ", ".join(f"{o.ref} ({o.fulfilment_state})" for o in candidates)
    raise ToolError(f"{found.ref} has several purchase orders: {refs}; say which one (po)")


async def _names(session: AsyncSession, po: IssuedPurchaseOrder) -> tuple[str, str]:
    rfq_ref = await session.scalar(select(Rfq.ref).where(Rfq.id == po.rfq_id))
    supplier = await session.scalar(select(Supplier.name).where(Supplier.id == po.supplier_id))
    return rfq_ref or "?", supplier or "?"


def _item(po: PurchaseOrderSummary) -> PurchaseOrderItem:
    f = po.fulfilment
    return PurchaseOrderItem(
        id=po.id,
        ref=po.ref,
        rfq_ref=po.rfq_ref,
        supplier_name=po.supplier_name,
        status=po.status,
        state=f.state,
        open=f.open,
        currency=po.currency,
        total=po.total,
        ordered_quantity=f.ordered_quantity,
        received_quantity=f.received_quantity,
        sent_at=po.sent_at.date() if po.sent_at else None,
        acknowledged_at=f.acknowledged_at.date() if f.acknowledged_at else None,
        supplier_order_ref=f.supplier_order_ref,
        closed_at=f.closed_at.date() if f.closed_at else None,
        closed_short=f.closed_short,
        cancelled_at=f.cancelled_at.date() if f.cancelled_at else None,
    )


def _preview(
    action: Action,
    po: IssuedPurchaseOrder,
    rfq_ref: str,
    supplier_name: str,
    *,
    state_after: str,
    lines: list[ProposedOrderLine],
    **details: object,
) -> ProposalPreview:
    order = ProposedOrderAction.model_validate(
        {
            "ref": po.ref,
            "rfq_ref": rfq_ref,
            "supplier_name": supplier_name,
            "state": po.fulfilment_state,
            "state_after": state_after,
            "lines": lines,
            **details,
        }
    )
    return ProposalPreview(action=action, rfq=rfq_ref, purchase_order=order)


def _service_error(exc: RfqError) -> ToolError:
    return ToolError(str(exc))


# --- The tools -----------------------------------------------------------------------------


def order_tools(deps: "CopilotToolDeps") -> tuple[AnyTool, ...]:
    db = deps.db

    async def _propose(
        ctx: ToolContext,
        action: Action,
        args: OrderRef,
        preview: ProposalPreview,
        request: BaseModel,
    ) -> UUID:
        if deps.conversation_id is None:
            raise ToolError("this action needs a conversation to ask the buyer in")
        return await proposals.propose(
            db,
            _principal(ctx),
            conversation_id=deps.conversation_id,
            run_id=ctx.run_id,
            action=action,
            args=args.model_dump(mode="json", exclude={"proposal_id"}),
            preview={
                "shown": preview.model_dump(mode="json"),
                "request": request.model_dump(mode="json"),
            },
        )

    async def _claim(ctx: ToolContext, action: Action, proposal_id: UUID) -> proposals.Claimed:
        if deps.conversation_id is None:
            raise ToolError("this action needs a conversation to ask the buyer in")
        try:
            return await proposals.claim(
                db,
                _principal(ctx),
                conversation_id=deps.conversation_id,
                run_id=ctx.run_id,
                action=action,
                proposal_id=proposal_id,
            )
        except proposals.ProposalRefused as exc:
            raise ToolError(str(exc)) from None

    async def _done(
        ctx: ToolContext, po_id: UUID, proposal_id: UUID, **extra: Any
    ) -> OrderActionOut:
        async with tenant_session(db, ctx.tenant_id) as session:
            po = await session.get(IssuedPurchaseOrder, po_id)
            if po is None:
                raise ToolError(f"purchase order {po_id} not found")
            completed = await session.scalar(
                select(Rfq.completed_at.is_not(None)).where(Rfq.id == po.rfq_id)
            )
        return OrderActionOut(
            proposal_id=proposal_id,
            ref=po.ref,
            state=po.fulfilment_state,
            rfq_completed=bool(completed),
            **extra,
        )

    async def list_purchase_orders(
        ctx: ToolContext, args: ListPurchaseOrdersIn
    ) -> ListPurchaseOrdersOut:
        principal = _principal(ctx)
        rfq_id: UUID | None = None
        if args.rfq:
            async with tenant_session(db, ctx.tenant_id) as session:
                rfq = await queries.find_rfq(session, args.rfq)
            if rfq is None:
                raise ToolError(f"no RFQ {args.rfq!r}; use list_rfqs to find it")
            rfq_id = rfq.id
        everything = await award_service.list_purchase_orders(db, principal)
        by_state = queries.count_by([po.fulfilment.state for po in everything])
        chosen = [
            po
            for po in everything
            if (rfq_id is None or po.rfq_id == rfq_id)
            and (args.open is None or po.fulfilment.open == args.open)
            and (args.state is None or po.fulfilment.state == args.state)
        ]
        chosen.reverse()  # the service lists by number: newest last
        return ListPurchaseOrdersOut(
            purchase_orders=[_item(po) for po in chosen[: args.limit]], by_state=by_state
        )

    async def acknowledge_purchase_order(ctx: ToolContext, args: AcknowledgeIn) -> OrderActionOut:
        action: Action = "acknowledge_purchase_order"
        if args.proposal_id is None:
            async with tenant_session(db, ctx.tenant_id) as session:
                po = await _find_po(session, args.po, args.rfq)
                rfq_ref, supplier = await _names(session, po)
            if po.fulfilment_state != "sent":
                raise ToolError(
                    f"{po.ref} is {po.fulfilment_state}; only a sent order is acknowledged"
                )
            try:
                request = AcknowledgeRequest(
                    acknowledged_on=args.acknowledged_on or deps.today(),
                    supplier_order_ref=args.supplier_order_ref,
                    note=args.note,
                )
            except ValidationError as exc:
                raise ToolError(str(exc)) from None
            preview = _preview(
                action,
                po,
                rfq_ref,
                supplier,
                state_after="acknowledged",
                lines=[],
                acknowledged_on=request.acknowledged_on,
                supplier_order_ref=request.supplier_order_ref,
                note=request.note,
            )
            proposal_id = await _propose(
                ctx, action, args.model_copy(update={"po": str(po.id)}), preview, request
            )
            return OrderActionOut(
                awaiting_confirmation=True, proposal_id=proposal_id, preview=preview, ref=po.ref
            )
        claimed = await _claim(ctx, action, args.proposal_id)
        po_id = UUID(str(claimed.args["po"]))
        request = AcknowledgeRequest.model_validate(claimed.preview["request"])
        try:
            await lifecycle.acknowledge_purchase_order(db, _principal(ctx), po_id, request)
        except RfqError as exc:
            await proposals.release(db, _principal(ctx), args.proposal_id)
            raise _service_error(exc) from None
        return await _done(ctx, po_id, args.proposal_id)

    async def receive_purchase_order(ctx: ToolContext, args: ReceiveIn) -> OrderActionOut:
        action: Action = "receive_purchase_order"
        if args.proposal_id is None:
            async with tenant_session(db, ctx.tenant_id) as session:
                po = await _find_po(session, args.po, args.rfq)
                rfq_ref, supplier = await _names(session, po)
                lines = await po_lines(session, po.id)
                totals = (await lifecycle.quantities(session, [po.id]))[po.id]
            if po.fulfilment_state not in ("sent", "acknowledged", "partially_received"):
                raise ToolError(f"{po.ref} is {po.fulfilment_state}; it is not expecting goods")
            by_position = {line.position: line for line in lines}
            wanted: dict[int, Decimal] = {}
            if args.lines:
                for item in args.lines:
                    if item.position not in by_position:
                        raise ToolError(f"{po.ref} has no line {item.position}")
                    if item.quantity <= 0:
                        raise ToolError(f"line {item.position}: the quantity must be above zero")
                    wanted[item.position] = wanted.get(item.position, _ZERO) + item.quantity
            else:
                for line in lines:
                    remaining = line.quantity - totals.by_line.get(line.id, _ZERO)
                    if remaining > 0:
                        wanted[line.position] = remaining
            if not wanted:
                raise ToolError(f"{po.ref}: nothing is outstanding; close it instead")
            shown: list[ProposedOrderLine] = []
            for line in lines:
                so_far = totals.by_line.get(line.id, _ZERO)
                this = wanted.get(line.position)
                if this is not None and so_far + this > line.quantity:
                    raise ToolError(
                        f"line {line.position} of {po.ref}: ordered {line.quantity} {line.unit}, "
                        f"received {so_far} so far; {this} more would exceed the order by "
                        f"{so_far + this - line.quantity}. Ask the buyer to check the quantity"
                    )
                shown.append(
                    ProposedOrderLine(
                        position=line.position,
                        description=line.description,
                        unit=line.unit,
                        ordered=line.quantity,
                        received_so_far=so_far,
                        this_receipt=this,
                        remaining_after=line.quantity - so_far - (this or _ZERO),
                    )
                )
            complete = all(item.remaining_after <= 0 for item in shown)
            try:
                request = ReceiptRequest(
                    received_on=args.received_on or deps.today(),
                    invoice_number=args.invoice_number,
                    invoice_date=args.invoice_date,
                    note=args.note,
                    lines=[
                        ReceiptLineRequest(line_id=by_position[position].id, quantity=quantity)
                        for position, quantity in sorted(wanted.items())
                    ],
                )
            except ValidationError as exc:
                raise ToolError(str(exc)) from None
            preview = _preview(
                action,
                po,
                rfq_ref,
                supplier,
                state_after="received" if complete else "partially_received",
                lines=shown,
                received_on=request.received_on,
                invoice_number=request.invoice_number,
                invoice_date=request.invoice_date,
                note=request.note,
            )
            proposal_id = await _propose(
                ctx, action, args.model_copy(update={"po": str(po.id)}), preview, request
            )
            return OrderActionOut(
                awaiting_confirmation=True, proposal_id=proposal_id, preview=preview, ref=po.ref
            )
        claimed = await _claim(ctx, action, args.proposal_id)
        po_id = UUID(str(claimed.args["po"]))
        request = ReceiptRequest.model_validate(claimed.preview["request"])
        try:
            received = await lifecycle.receive_purchase_order(db, _principal(ctx), po_id, request)
        except RfqError as exc:
            await proposals.release(db, _principal(ctx), args.proposal_id)
            raise _service_error(exc) from None
        return await _done(ctx, po_id, args.proposal_id, receipt_id=received.receipt_id)

    async def close_purchase_order(ctx: ToolContext, args: CloseIn) -> OrderActionOut:
        action: Action = "close_purchase_order"
        if args.proposal_id is None:
            async with tenant_session(db, ctx.tenant_id) as session:
                po = await _find_po(session, args.po, args.rfq)
                rfq_ref, supplier = await _names(session, po)
                lines = await po_lines(session, po.id)
                totals = (await lifecycle.quantities(session, [po.id]))[po.id]
            state = po.fulfilment_state
            if state in ("closed", "cancelled"):
                raise ToolError(f"{po.ref} is already {state}")
            if state not in OPEN_FULFILMENT_STATES:
                raise ToolError(f"{po.ref} was not sent; cancel it instead")
            short = state != "received"
            if short and not args.short:
                raise ToolError(
                    f"{po.ref} is {state}: not everything arrived. Record the receipt first "
                    "(receive_purchase_order), or ask the buyer whether to close it short and why"
                )
            if short and not (args.reason or "").strip():
                raise ToolError(f"ask the buyer why {po.ref} is closed short; a reason is required")
            try:
                request = CloseRequest(short=short, reason=args.reason if short else args.reason)
            except ValidationError as exc:
                raise ToolError(str(exc)) from None
            shown = [
                ProposedOrderLine(
                    position=line.position,
                    description=line.description,
                    unit=line.unit,
                    ordered=line.quantity,
                    received_so_far=totals.by_line.get(line.id, _ZERO),
                    remaining_after=line.quantity - totals.by_line.get(line.id, _ZERO),
                )
                for line in lines
            ]
            preview = _preview(
                action,
                po,
                rfq_ref,
                supplier,
                state_after="closed",
                lines=shown,
                short=short,
                reason=request.reason,
            )
            proposal_id = await _propose(
                ctx, action, args.model_copy(update={"po": str(po.id)}), preview, request
            )
            return OrderActionOut(
                awaiting_confirmation=True, proposal_id=proposal_id, preview=preview, ref=po.ref
            )
        claimed = await _claim(ctx, action, args.proposal_id)
        po_id = UUID(str(claimed.args["po"]))
        request = CloseRequest.model_validate(claimed.preview["request"])
        try:
            await lifecycle.close_purchase_order(db, _principal(ctx), po_id, request)
        except RfqError as exc:
            await proposals.release(db, _principal(ctx), args.proposal_id)
            raise _service_error(exc) from None
        return await _done(ctx, po_id, args.proposal_id)

    async def cancel_purchase_order(ctx: ToolContext, args: CancelIn) -> OrderActionOut:
        action: Action = "cancel_purchase_order"
        if args.proposal_id is None:
            async with tenant_session(db, ctx.tenant_id) as session:
                po = await _find_po(session, args.po, args.rfq)
                rfq_ref, supplier = await _names(session, po)
                lines = await po_lines(session, po.id)
                totals = (await lifecycle.quantities(session, [po.id]))[po.id]
            state = po.fulfilment_state
            if state in ("closed", "cancelled"):
                raise ToolError(f"{po.ref} is already {state}")
            if state == "received":
                raise ToolError(f"{po.ref} was fully received; close it instead")
            if po.status == "pending_approval":
                raise ToolError(
                    f"{po.ref} has a send waiting for approval; it must be decided first"
                )
            if not (args.reason or "").strip():
                raise ToolError(f"ask the buyer why {po.ref} is cancelled; a reason is required")
            try:
                request = CancelRequest(reason=cast(str, args.reason))
            except ValidationError as exc:
                raise ToolError(str(exc)) from None
            shown = [
                ProposedOrderLine(
                    position=line.position,
                    description=line.description,
                    unit=line.unit,
                    ordered=line.quantity,
                    received_so_far=totals.by_line.get(line.id, _ZERO),
                    remaining_after=line.quantity - totals.by_line.get(line.id, _ZERO),
                )
                for line in lines
            ]
            preview = _preview(
                action,
                po,
                rfq_ref,
                supplier,
                state_after="cancelled",
                lines=shown,
                reason=request.reason,
            )
            proposal_id = await _propose(
                ctx, action, args.model_copy(update={"po": str(po.id)}), preview, request
            )
            return OrderActionOut(
                awaiting_confirmation=True, proposal_id=proposal_id, preview=preview, ref=po.ref
            )
        claimed = await _claim(ctx, action, args.proposal_id)
        po_id = UUID(str(claimed.args["po"]))
        request = CancelRequest.model_validate(claimed.preview["request"])
        try:
            await lifecycle.cancel_purchase_order(db, _principal(ctx), po_id, request)
        except RfqError as exc:
            await proposals.release(db, _principal(ctx), args.proposal_id)
            raise _service_error(exc) from None
        return await _done(ctx, po_id, args.proposal_id)

    return (
        Tool(
            "list_purchase_orders",
            "The company's purchase orders after the award, newest first, with a count per "
            "fulfilment state over all of them: use it for 'which POs are open / pending / "
            "received?' and to find a PO by its RFQ. (list_open_pos only lists orders not sent "
            "yet.) " + STATE_HINT,
            ListPurchaseOrdersIn,
            ListPurchaseOrdersOut,
            "read",
            list_purchase_orders,
        ),
        Tool(
            "acknowledge_purchase_order",
            "Record that the supplier confirmed a sent purchase order (date, their order "
            "number, a note), as the buyer tells you." + _CONFIRM_FIRST,
            AcknowledgeIn,
            OrderActionOut,
            "draft",
            acknowledge_purchase_order,
        ),
        Tool(
            "receive_purchase_order",
            "Record a delivery against a purchase order: the quantity received per line (by "
            "position), the date and the invoice. The preview shows ordered, received so far and "
            "this delivery per line; more than ordered is refused. The order becomes "
            "partially_received or received." + _CONFIRM_FIRST,
            ReceiveIn,
            OrderActionOut,
            "draft",
            receive_purchase_order,
        ),
        Tool(
            "close_purchase_order",
            "Close a purchase order whose goods all arrived; or close it short (short=true, with "
            "the buyer's reason) giving up what did not. When every order of the RFQ is done, "
            "the RFQ is completed." + _CONFIRM_FIRST,
            CloseIn,
            OrderActionOut,
            "draft",
            close_purchase_order,
        ),
        Tool(
            "cancel_purchase_order",
            "Cancel a purchase order with the buyer's reason, before it is fully received. "
            "This records the cancellation in Bido only; telling the supplier is the buyer's "
            "own email." + _CONFIRM_FIRST,
            CancelIn,
            OrderActionOut,
            "draft",
            cancel_purchase_order,
        ),
    )
