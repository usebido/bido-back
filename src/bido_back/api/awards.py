"""Award an RFQ and send its purchase orders. Every step waits for an approver.

- `POST /v1/rfqs/{rfq_id}/award` proposes the award (the recommendation, or
  the buyer's choice with a reason); `GET` shows where it stands and the POs.
- `POST /v1/rfqs/{rfq_id}/award/notices` proposes a "not selected" email to
  every supplier that took part and did not win.
- `/v1/purchase-orders`: list, detail, the document (.xlsx, or printable
  .html), and `POST .../send` to propose sending it in the RFQ thread.
- After the send, the buyer records the order's life (`awards.lifecycle`):
  `POST .../acknowledge`, `POST .../receipts` (+ `GET`), `POST .../close`,
  `POST .../cancel`. These change Bido's records only; nothing is sent.

Nothing here sends: approvers decide in `/v1/approvals`, the worker acts.
"""

from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from bido_back.api.deps import get_mailbox_senders, get_notice_writer
from bido_back.api.rfqs import CurrentPrincipal, Db, error_responses, rfq_http_error
from bido_back.awards import lifecycle, service
from bido_back.awards.agents import NoticeWriter
from bido_back.awards.render import HTML_MEDIA_TYPE, XLSX_MEDIA_TYPE
from bido_back.awards.schemas import (
    AcknowledgeRequest,
    ApprovalRequested,
    AwardRequest,
    AwardRequested,
    AwardView,
    CancelRequest,
    CloseRequest,
    FulfilmentState,
    NoticesRequested,
    PurchaseOrderStatus,
    PurchaseOrderSummary,
    PurchaseOrderView,
    ReceiptRequest,
    ReceiptView,
)
from bido_back.rfqs.errors import RfqError
from bido_back.rfqs.mailbox import MailboxSenderFactory

_BINARY: dict[str, Any] = {"schema": {"type": "string", "format": "binary"}}
_FILE_RESPONSE: dict[int | str, dict[str, Any]] = {
    status.HTTP_200_OK: {
        "content": {XLSX_MEDIA_TYPE: _BINARY, HTML_MEDIA_TYPE: {"schema": {"type": "string"}}},
        "description": "The purchase order document",
    }
}


def _http_error(exc: RfqError) -> HTTPException:
    if isinstance(
        exc, service.ReasonRequired | service.AwardSelectionInvalid | lifecycle.RefusedInput
    ):
        return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))
    return rfq_http_error(exc)


def awards_router() -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["awards"])

    @router.post(
        "/rfqs/{rfq_id}/award",
        status_code=status.HTTP_202_ACCEPTED,
        responses=error_responses(status.HTTP_404_NOT_FOUND, status.HTTP_409_CONFLICT),
    )
    async def request_award(  # pyright: ignore[reportUnusedFunction]
        rfq_id: UUID, body: AwardRequest, principal: CurrentPrincipal, db: Db
    ) -> AwardRequested:
        """Propose the award: one approval, decided by a person (the buyer by default)."""
        try:
            return await service.request_award(
                db, principal, rfq_id, body, datetime.now(UTC).date()
            )
        except RfqError as exc:
            raise _http_error(exc) from None

    @router.get("/rfqs/{rfq_id}/award", responses=error_responses(status.HTTP_404_NOT_FOUND))
    async def get_award(  # pyright: ignore[reportUnusedFunction]
        rfq_id: UUID, principal: CurrentPrincipal, db: Db
    ) -> AwardView:
        try:
            return await service.get_award(db, principal, rfq_id)
        except RfqError as exc:
            raise _http_error(exc) from None

    @router.post(
        "/rfqs/{rfq_id}/award/notices",
        status_code=status.HTTP_202_ACCEPTED,
        responses=error_responses(status.HTTP_404_NOT_FOUND, status.HTTP_409_CONFLICT),
    )
    async def request_notices(  # pyright: ignore[reportUnusedFunction]
        rfq_id: UUID,
        principal: CurrentPrincipal,
        db: Db,
        writer: Annotated[NoticeWriter, Depends(get_notice_writer)],
    ) -> NoticesRequested:
        """Propose a "not selected" email to each supplier that did not win."""
        try:
            return await service.request_notices(db, principal, rfq_id, writer)
        except RfqError as exc:
            raise _http_error(exc) from None

    @router.get("/purchase-orders", responses=error_responses())
    async def list_purchase_orders(  # pyright: ignore[reportUnusedFunction]
        principal: CurrentPrincipal,
        db: Db,
        status_filter: Annotated[PurchaseOrderStatus | None, Query(alias="status")] = None,
        open: Annotated[
            bool | None,
            Query(description="true: sent and still expecting goods; false: everything else"),
        ] = None,
        state: Annotated[
            FulfilmentState | None, Query(description="Only orders in this fulfilment state")
        ] = None,
        rfq_id: Annotated[UUID | None, Query(description="Only this RFQ's orders")] = None,
    ) -> list[PurchaseOrderSummary]:
        return await service.list_purchase_orders(
            db, principal, status_filter, open=open, state=state, rfq_id=rfq_id
        )

    @router.get(
        "/purchase-orders/{po_id}.{fmt}",
        response_class=Response,
        responses={**_FILE_RESPONSE, **error_responses(status.HTTP_404_NOT_FOUND)},
    )
    async def purchase_order_document(  # pyright: ignore[reportUnusedFunction]
        po_id: UUID, fmt: Literal["xlsx", "html"], principal: CurrentPrincipal, db: Db
    ) -> Response:
        """The PO as .xlsx, or as printable HTML (print it to PDF from the browser)."""
        try:
            doc = await service.render_purchase_order(db, principal, po_id, fmt)
        except RfqError as exc:
            raise _http_error(exc) from None
        disposition = "attachment" if fmt == "xlsx" else "inline"
        return Response(
            content=doc.content,
            media_type=doc.media_type,
            headers={"Content-Disposition": f'{disposition}; filename="{doc.filename}"'},
        )

    @router.get("/purchase-orders/{po_id}", responses=error_responses(status.HTTP_404_NOT_FOUND))
    async def get_purchase_order(  # pyright: ignore[reportUnusedFunction]
        po_id: UUID, principal: CurrentPrincipal, db: Db
    ) -> PurchaseOrderView:
        try:
            return await service.get_purchase_order(db, principal, po_id)
        except RfqError as exc:
            raise _http_error(exc) from None

    @router.post(
        "/purchase-orders/{po_id}/send",
        status_code=status.HTTP_202_ACCEPTED,
        responses=error_responses(status.HTTP_404_NOT_FOUND, status.HTTP_409_CONFLICT),
    )
    async def send_purchase_order(  # pyright: ignore[reportUnusedFunction]
        po_id: UUID,
        principal: CurrentPrincipal,
        db: Db,
        mailboxes: Annotated[MailboxSenderFactory, Depends(get_mailbox_senders)],
    ) -> ApprovalRequested:
        """Propose sending the PO to its supplier; it goes out after approval."""
        try:
            return await service.request_po_send(db, principal, po_id, mailboxes)
        except RfqError as exc:
            raise _http_error(exc) from None

    @router.post(
        "/purchase-orders/{po_id}/acknowledge",
        responses=error_responses(status.HTTP_404_NOT_FOUND, status.HTTP_409_CONFLICT),
    )
    async def acknowledge_purchase_order(  # pyright: ignore[reportUnusedFunction]
        po_id: UUID, body: AcknowledgeRequest, principal: CurrentPrincipal, db: Db
    ) -> PurchaseOrderView:
        """Record that the supplier confirmed a sent order (date, their order number, a note)."""
        try:
            await lifecycle.acknowledge_purchase_order(db, principal, po_id, body)
            return await service.get_purchase_order(db, principal, po_id)
        except RfqError as exc:
            raise _http_error(exc) from None

    @router.post(
        "/purchase-orders/{po_id}/receipts",
        status_code=status.HTTP_201_CREATED,
        responses=error_responses(
            status.HTTP_404_NOT_FOUND,
            status.HTTP_409_CONFLICT,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
        ),
    )
    async def record_receipt(  # pyright: ignore[reportUnusedFunction]
        po_id: UUID, body: ReceiptRequest, principal: CurrentPrincipal, db: Db
    ) -> PurchaseOrderView:
        """Record a delivery: quantity per line, date, invoice. Over-receipt is refused (422)."""
        try:
            await lifecycle.receive_purchase_order(db, principal, po_id, body)
            return await service.get_purchase_order(db, principal, po_id)
        except RfqError as exc:
            raise _http_error(exc) from None

    @router.get(
        "/purchase-orders/{po_id}/receipts",
        responses=error_responses(status.HTTP_404_NOT_FOUND),
    )
    async def list_receipts(  # pyright: ignore[reportUnusedFunction]
        po_id: UUID, principal: CurrentPrincipal, db: Db
    ) -> list[ReceiptView]:
        try:
            return await lifecycle.list_receipts(db, principal, po_id)
        except RfqError as exc:
            raise _http_error(exc) from None

    @router.post(
        "/purchase-orders/{po_id}/close",
        responses=error_responses(
            status.HTTP_404_NOT_FOUND,
            status.HTTP_409_CONFLICT,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
        ),
    )
    async def close_purchase_order(  # pyright: ignore[reportUnusedFunction]
        po_id: UUID, body: CloseRequest, principal: CurrentPrincipal, db: Db
    ) -> PurchaseOrderView:
        """Close a fully received order; or close it short (`short`, with a reason)."""
        try:
            await lifecycle.close_purchase_order(db, principal, po_id, body)
            return await service.get_purchase_order(db, principal, po_id)
        except RfqError as exc:
            raise _http_error(exc) from None

    @router.post(
        "/purchase-orders/{po_id}/cancel",
        responses=error_responses(status.HTTP_404_NOT_FOUND, status.HTTP_409_CONFLICT),
    )
    async def cancel_purchase_order(  # pyright: ignore[reportUnusedFunction]
        po_id: UUID, body: CancelRequest, principal: CurrentPrincipal, db: Db
    ) -> PurchaseOrderView:
        """Cancel the order with a reason, at any point before it is fully received."""
        try:
            await lifecycle.cancel_purchase_order(db, principal, po_id, body)
            return await service.get_purchase_order(db, principal, po_id)
        except RfqError as exc:
            raise _http_error(exc) from None

    return router
