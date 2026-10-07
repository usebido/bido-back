"""Award an RFQ and send its purchase orders. Every step waits for an approver.

- `POST /v1/rfqs/{rfq_id}/award` proposes the award (the recommendation, or
  the buyer's choice with a reason); `GET` shows where it stands and the POs.
- `POST /v1/rfqs/{rfq_id}/award/notices` proposes a "not selected" email to
  every supplier that took part and did not win.
- `/v1/purchase-orders`: list, detail, the document (.xlsx, or printable
  .html), and `POST .../send` to propose sending it in the RFQ thread.

Nothing here executes: approvers decide in `/v1/approvals`, the worker acts.
"""

from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from bido_back.api.deps import get_mailbox_senders, get_notice_writer
from bido_back.api.rfqs import CurrentPrincipal, Db, error_responses, rfq_http_error
from bido_back.awards import service
from bido_back.awards.agents import NoticeWriter
from bido_back.awards.render import HTML_MEDIA_TYPE, XLSX_MEDIA_TYPE
from bido_back.awards.schemas import (
    ApprovalRequested,
    AwardRequest,
    AwardRequested,
    AwardView,
    NoticesRequested,
    PurchaseOrderStatus,
    PurchaseOrderSummary,
    PurchaseOrderView,
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
    if isinstance(exc, service.ReasonRequired | service.AwardSelectionInvalid):
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
    ) -> list[PurchaseOrderSummary]:
        return await service.list_purchase_orders(db, principal, status_filter)

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

    return router
